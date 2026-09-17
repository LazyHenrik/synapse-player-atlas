"""Owner-managed Atlas access; stored separately from collected player data."""

import html
import json
import secrets
from urllib.parse import parse_qs


class Access:
    def init_access(self):
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS access_requests (
                    subject TEXT PRIMARY KEY, name TEXT NOT NULL,
                    status TEXT NOT NULL, requested REAL NOT NULL,
                    reviewed REAL, reviewer TEXT
                );
                CREATE TABLE IF NOT EXISTS access_audit (
                    subject TEXT NOT NULL, action TEXT NOT NULL,
                    actor TEXT NOT NULL, created REAL NOT NULL
                );
            """)

    def access_config(self):
        config = json.loads(self.config_path.read_text())
        for key in ("allowed_subjects", "owner_subjects"):
            values = config.get(key, [])
            if not isinstance(values, list) or any(
                not isinstance(v, str) or not v for v in values
            ):
                raise ValueError("Invalid access configuration")
        return config

    def is_owner(self, subject):
        return subject in self.access_config().get("owner_subjects", [])

    def allowed(self, subject):
        config = self.access_config()
        if subject in config.get("owner_subjects", []):
            return True
        with self.db() as db:
            row = db.execute(
                "SELECT status FROM access_requests WHERE subject=?", (subject,)
            ).fetchone()
        if row and row["status"] in {"denied", "revoked"}:
            return False
        return subject in config.get("allowed_subjects", []) or bool(
            row and row["status"] == "approved"
        )

    def request_access(self, subject, name):
        with self.db() as db:
            db.execute(
                "INSERT INTO access_requests VALUES (?,?,'pending',?,NULL,NULL) ON CONFLICT(subject) DO UPDATE SET name=excluded.name",
                (subject, name, self.clock()),
            )
            status = db.execute(
                "SELECT status FROM access_requests WHERE subject=?", (subject,)
            ).fetchone()[0]
        if status == "pending":
            return f"Your access request for account {subject} has been sent to the owner. Sign in again after approval."
        return f"Access for account {subject} was {status}. Contact the owner to request a review."

    def admin(self, environ, start_response, session):
        if not self.is_owner(session["subject"]):
            return self.respond(
                start_response, "403 Forbidden", {"error": "Owner access required"}
            )
        if environ.get("REQUEST_METHOD") == "POST":
            try:
                size = int(environ.get("CONTENT_LENGTH", "0"))
                if not 0 < size <= 4096:
                    raise ValueError()
                fields = parse_qs(
                    environ["wsgi.input"].read(size).decode(), strict_parsing=True
                )
                if any(len(v) != 1 for v in fields.values()):
                    raise ValueError()
                subject, action, csrf = (
                    fields[k][0] for k in ("subject", "action", "csrf")
                )
                if (
                    action not in {"approved", "denied", "revoked"}
                    or not subject
                    or len(subject) > 200
                ):
                    raise ValueError()
            except (ValueError, KeyError, UnicodeError):
                return self.respond(
                    start_response,
                    "400 Bad Request",
                    {"error": "Invalid access decision"},
                )
            if environ.get("HTTP_ORIGIN") != self.origin or not secrets.compare_digest(
                csrf, session["csrf"]
            ):
                return self.respond(
                    start_response,
                    "403 Forbidden",
                    {"error": "Invalid access decision"},
                )
            if self.is_owner(subject):
                return self.respond(
                    start_response,
                    "403 Forbidden",
                    {"error": "Owners are managed in server configuration"},
                )
            with self.lock, self.db() as db:
                row = db.execute(
                    "SELECT * FROM access_requests WHERE subject=?", (subject,)
                ).fetchone()
                if not row and subject not in self.access_config().get(
                    "allowed_subjects", []
                ):
                    return self.respond(
                        start_response, "404 Not Found", {"error": "Account not found"}
                    )
                db.execute(
                    "INSERT INTO access_requests VALUES (?,?,?,?,?,?) ON CONFLICT(subject) DO UPDATE SET status=excluded.status, reviewed=excluded.reviewed, reviewer=excluded.reviewer",
                    (
                        subject,
                        subject,
                        action,
                        self.clock(),
                        self.clock(),
                        session["subject"],
                    ),
                )
                db.execute(
                    "INSERT INTO access_audit VALUES (?,?,?,?)",
                    (subject, action, session["subject"], self.clock()),
                )
                if action != "approved":
                    db.execute("DELETE FROM sessions WHERE subject=?", (subject,))
            return self.redirect(start_response, "/admin/access")
        with self.db() as db:
            accounts = {
                r["subject"]: dict(r)
                for r in db.execute(
                    "SELECT * FROM access_requests ORDER BY requested DESC"
                )
            }
        config = self.access_config()
        for subject in config.get("allowed_subjects", []):
            accounts.setdefault(
                subject, {"subject": subject, "name": subject, "status": "approved"}
            )
        sections = []
        for status, label in (
            ("pending", "Pending requests"),
            ("approved", "Approved staff"),
            ("denied", "Denied requests"),
            ("revoked", "Revoked access"),
        ):
            cards = []
            for account in accounts.values():
                if account["status"] != status or account["subject"] in config.get(
                    "owner_subjects", []
                ):
                    continue
                subject = html.escape(account["subject"], quote=True)
                buttons = (
                    '<button name="action" value="revoked">Revoke access</button>'
                    if status == "approved"
                    else '<button name="action" value="approved">Approve access</button>'
                )
                if status == "pending":
                    buttons += (
                        '<button name="action" value="denied">Deny request</button>'
                    )
                cards.append(
                    f'<article class="access-card"><h3>{html.escape(account["name"])}</h3><p class="access-id">{subject}</p><form method="post" action="/admin/access"><input type="hidden" name="subject" value="{subject}"><input type="hidden" name="csrf" value="{html.escape(session["csrf"], quote=True)}">{buttons}</form></article>'
                )
            sections.append(
                f'<section><h2>{label} ({len(cards)})</h2>{"".join(cards) or "<p>No accounts in this category.</p>"}</section>'
            )
        body = (
            '<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Synapse · Access management</title><link rel="stylesheet" href="/style.css"></head><body><main class="auth-page access-page"><img src="/synapse-wordmark.webp" alt="Project Synapse" width="220"><p class="eyebrow">OWNER PORTAL</p><h1>Access management</h1><p>Approve staff to use Player Atlas. Approved staff can read all collected records, including private messages.</p><a class="auth-link" href="/">Back to Atlas</a>'
            + "".join(sections)
            + "</main></body></html>"
        )
        return self.respond(
            start_response, "200 OK", body, mime="text/html; charset=utf-8",
            # Native form POSTs otherwise send Origin: null in browsers.
            referrer_policy="same-origin",
        )
