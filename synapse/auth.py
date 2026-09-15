import base64
import contextlib
import hashlib
import html
import json
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.cookies import SimpleCookie
from .access import Access
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

# Discovery currently advertises /api/api/oauth routes, which return 404.
OAUTH_BASE = "https://auth.monosuite.com/api/oauth/"
CALLBACK = "/auth/monosuite/callback"
READ_SCOPES = {
    "moderation.blacklist.view",
    "moderation.note.view",
    "moderation.warning.view",
}


class LoginError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Provider:
    def __init__(self, config):
        self.config = config
        self.http = urllib.request.build_opener(NoRedirect)

    def request(self, endpoint, fields=None, token=None):
        headers = {"Accept": "application/json", "User-Agent": "Synapse-Atlas/1"}
        data = None
        if fields is not None:
            data = urlencode(fields).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            credentials = self.config["client_id"] + ":" + self.config["client_secret"]
            headers["Authorization"] = (
                "Basic " + base64.b64encode(credentials.encode()).decode()
            )
        else:
            headers["Authorization"] = "Bearer " + token
        try:
            request = urllib.request.Request(
                OAUTH_BASE + endpoint, data=data, headers=headers
            )
            with self.http.open(request, timeout=15) as response:
                data = response.read(131073)
            if len(data) > 131072:
                raise LoginError()
            result = json.loads(data)
            if not isinstance(result, dict) or "error" in result:
                raise LoginError()
            return result
        except (urllib.error.URLError, ValueError, OSError) as exc:
            # Provider errors can contain authorization codes and tokens.
            raise LoginError() from None

    def exchange(self, code, verifier):
        return self.request(
            "token",
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.config["redirect_uri"],
                "code_verifier": verifier,
            },
        )

    def refresh(self, token):
        return self.request(
            "token", {"grant_type": "refresh_token", "refresh_token": token}
        )

    def identity(self, token):
        return self.request("userinfo", token=token)


class Auth(Access):
    def __init__(self, app, config_path, store_path, provider=None, clock=time.time):
        self.app = app
        self.config_path = Path(config_path)
        self.config = json.loads(self.config_path.read_text())
        self.clock = clock
        uri = urlsplit(self.config["redirect_uri"])
        self.secure = uri.scheme == "https"
        if (
            (
                not self.secure
                and not (
                    uri.scheme == "http" and uri.hostname in {"127.0.0.1", "localhost"}
                )
            )
            or uri.path != CALLBACK
            or uri.query
            or uri.fragment
            or uri.username
            or uri.password
        ):
            raise ValueError(
                "OAuth callback must use HTTPS, or HTTP on loopback, with the exact callback path"
            )
        if (
            not uri.netloc
            or not self.config.get("client_id")
            or not self.config.get("client_secret")
        ):
            raise ValueError("Missing OAuth client settings")
        if not set(self.config.get("scopes", [])) <= READ_SCOPES:
            raise ValueError("Only the registered read scopes are allowed")
        self.origin = f"{uri.scheme}://{uri.netloc}"
        self.host = uri.netloc
        self.session_cookie = (
            "__Host-synapse_session" if self.secure else "synapse_session"
        )
        self.flow_cookie = "__Host-synapse_flow" if self.secure else "synapse_flow"
        self.provider = provider or Provider(self.config)
        self.lock = threading.RLock()
        self.store_path = Path(store_path)
        self.store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS flows (
                    state TEXT PRIMARY KEY, browser TEXT NOT NULL,
                    verifier TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, subject TEXT NOT NULL, name TEXT NOT NULL,
                    tokens TEXT NOT NULL, token_expires REAL NOT NULL,
                    expires REAL NOT NULL, checked REAL NOT NULL, csrf TEXT NOT NULL
                );
            """)
        self.init_access()
        os.chmod(self.store_path, 0o600)

    @contextlib.contextmanager
    def db(self):
        connection = sqlite3.connect(self.store_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def cookie(self, name, value, age):
        return (
            "Set-Cookie",
            f"{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}"
            + ("; Secure" if self.secure else ""),
        )

    def cookies(self, environ):
        try:
            cookies = SimpleCookie(environ.get("HTTP_COOKIE", ""))
            return {key: item.value for key, item in cookies.items()}
        except Exception:
            return {}

    def respond(self, start_response, status, data, extra=(), mime="application/json"):
        body = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        start_response(
            status,
            [
                ("Content-Type", mime),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
                ("Referrer-Policy", "no-referrer"),
                ("X-Content-Type-Options", "nosniff"),
                ("X-Frame-Options", "DENY"),
                (
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
                ),
                *extra,
            ],
        )
        return [body]

    def redirect(self, start_response, location, extra=()):
        return self.respond(
            start_response, "303 See Other", "", [("Location", location), *extra]
        )

    def page(self, start_response, title="Staff access", message=None, status="200 OK"):
        message = (
            message
            or "Sign in with MonoSuite to explore Synapse’s player relationships. Access is limited to approved staff."
        )
        body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Synapse · Sign in</title><link rel="stylesheet" href="/style.css"></head><body><main class="auth-page"><img src="/synapse-wordmark.webp" alt="Project Synapse" width="220"><p class="eyebrow">PLAYER ATLAS</p><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p><a class="auth-link" href="/auth/login">Sign in with MonoSuite →</a></main></body></html>"""
        return self.respond(
            start_response, status, body, mime="text/html; charset=utf-8"
        )

    def identity(self, tokens):
        token = tokens.get("access_token")
        if (
            not isinstance(token, str)
            or not token
            or tokens.get("token_type", "").lower() != "bearer"
        ):
            raise LoginError()
        identity = self.provider.identity(token)
        subject = identity.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > 200:
            raise LoginError()
        name = identity.get("name") or subject
        if not isinstance(name, str):
            name = subject
        return subject, name[:200]

    def token_expiry(self, tokens):
        seconds = tokens.get("expires_in")
        if (
            isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not 0 < seconds <= 366 * 86400
        ):
            raise LoginError()
        return self.clock() + seconds

    def start_login(self, start_response):
        state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .decode()
            .rstrip("=")
        )
        with self.db() as db:
            db.execute("DELETE FROM flows WHERE expires <= ?", (self.clock(),))
            db.execute("DELETE FROM sessions WHERE expires <= ?", (self.clock(),))
            if db.execute("SELECT count(*) FROM flows").fetchone()[0] >= 1000:
                return self.respond(
                    start_response,
                    "429 Too Many Requests",
                    {"error": "Try again shortly"},
                )
            db.execute(
                "INSERT INTO flows VALUES (?,?,?,?)",
                (digest(state), digest(browser), verifier, self.clock() + 600),
            )
        query = urlencode(
            {
                "client_id": self.config["client_id"],
                "redirect_uri": self.config["redirect_uri"],
                "response_type": "code",
                "scope": " ".join(self.config.get("scopes", [])),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return self.redirect(
            start_response,
            OAUTH_BASE + "authorize?" + query,
            [self.cookie(self.flow_cookie, browser, 600)],
        )

    def callback(self, environ, start_response, cookies):
        params = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        state = params.get("state", [])
        if len(state) != 1 or len(state[0]) > 200 or not cookies.get(self.flow_cookie):
            raise LoginError()
        with self.lock, self.db() as db:
            flow = db.execute(
                "SELECT * FROM flows WHERE state=?", (digest(state[0]),)
            ).fetchone()
            if (
                not flow
                or flow["expires"] <= self.clock()
                or not secrets.compare_digest(
                    flow["browser"], digest(cookies[self.flow_cookie])
                )
            ):
                raise LoginError()
            db.execute("DELETE FROM flows WHERE state=?", (digest(state[0]),))
        if "error" in params:
            return self.page(
                start_response,
                "Sign-in cancelled",
                "MonoSuite did not grant access. You can try again.",
                "403 Forbidden",
            )
        code = params.get("code", [])
        if len(code) != 1 or not code[0] or len(code[0]) > 4096:
            raise LoginError()
        tokens = self.provider.exchange(code[0], flow["verifier"])
        subject, name = self.identity(tokens)
        expires = self.token_expiry(tokens)
        if not self.allowed(subject):
            message = self.request_access(subject, name)
            return self.page(
                start_response,
                "Access needs approval",
                message,
                "403 Forbidden",
            )
        session = secrets.token_urlsafe(32)
        with self.db() as db:
            db.execute(
                "DELETE FROM sessions WHERE id=?",
                (digest(cookies.get(self.session_cookie, "")),),
            )
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?)",
                (
                    digest(session),
                    subject,
                    name,
                    json.dumps(tokens),
                    expires,
                    self.clock() + 28800,
                    self.clock(),
                    secrets.token_urlsafe(32),
                ),
            )
        return self.redirect(
            start_response,
            "/",
            [
                self.cookie(self.session_cookie, session, 28800),
                self.cookie(self.flow_cookie, "", 0),
            ],
        )

    def session(self, cookie):
        if not cookie or len(cookie) > 100:
            return None
        with self.lock, self.db() as db:
            row = db.execute(
                "SELECT * FROM sessions WHERE id=?", (digest(cookie),)
            ).fetchone()
            if not row:
                return None
            row = dict(row)
            if row["expires"] <= self.clock() or not self.allowed(row["subject"]):
                db.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
                return None
            if (
                row["checked"] + 60 <= self.clock()
                or row["token_expires"] <= self.clock() + 30
            ):
                tokens = json.loads(row["tokens"])
                if row["token_expires"] <= self.clock() + 30:
                    if not tokens.get("refresh_token"):
                        db.execute("DELETE FROM sessions WHERE id=?", (row["id"],))
                        return None
                    fresh = self.provider.refresh(tokens["refresh_token"])
                    fresh.setdefault("refresh_token", tokens["refresh_token"])
                    tokens = fresh
                    row["token_expires"] = self.token_expiry(tokens)
                    # Preserve a rotated refresh token even if the identity request fails.
                    db.execute(
                        "UPDATE sessions SET tokens=?,token_expires=? WHERE id=?",
                        (json.dumps(tokens), row["token_expires"], row["id"]),
                    )
                    db.commit()
                subject, name = self.identity(tokens)
                if subject != row["subject"]:
                    raise LoginError()
                db.execute(
                    "UPDATE sessions SET tokens=?,token_expires=?,checked=?,name=? WHERE id=?",
                    (
                        json.dumps(tokens),
                        row["token_expires"],
                        self.clock(),
                        name,
                        row["id"],
                    ),
                )
                row["name"] = name
            return row

    def __call__(self, environ, start_response):
        route, method = environ.get("PATH_INFO", "/"), environ.get(
            "REQUEST_METHOD", "GET"
        )
        if route == "/healthz" and method == "GET":
            return self.respond(start_response, "200 OK", {"ok": True})
        if environ.get("HTTP_HOST") != self.host:
            return self.respond(
                start_response,
                "400 Bad Request",
                {"error": "Use the configured application address"},
            )
        cookies = self.cookies(environ)
        try:
            if method == "GET":
                if route == "/auth/login":
                    return self.start_login(start_response)
                if route == CALLBACK:
                    return self.callback(environ, start_response, cookies)
                if route in {
                    "/style.css",
                    "/synapse-logo.png",
                    "/synapse-wordmark.webp",
                }:
                    return self.app(environ, start_response)
            elif method != "POST" or route not in {"/auth/logout", "/admin/access"}:
                return self.respond(
                    start_response, "405 Method Not Allowed", {"error": "Read only"}
                )
            if method == "POST" and route == "/auth/logout":
                # Local sign-out must still work during a MonoSuite outage.
                with self.db() as db:
                    session = db.execute(
                        "SELECT * FROM sessions WHERE id=?",
                        (digest(cookies.get(self.session_cookie, "")),),
                    ).fetchone()
            else:
                session = self.session(cookies.get(self.session_cookie))
            if not session:
                if route == "/" and method == "GET":
                    return self.page(start_response)
                return self.respond(
                    start_response, "401 Unauthorized", {"error": "Sign in required"}
                )
            if route == "/auth/logout" and method == "POST":
                if environ.get(
                    "HTTP_ORIGIN"
                ) != self.origin or not secrets.compare_digest(
                    environ.get("HTTP_X_CSRF_TOKEN", ""), session["csrf"]
                ):
                    return self.respond(
                        start_response,
                        "403 Forbidden",
                        {"error": "Invalid sign-out request"},
                    )
                with self.db() as db:
                    db.execute("DELETE FROM sessions WHERE id=?", (session["id"],))
                return self.respond(
                    start_response,
                    "200 OK",
                    {"ok": True},
                    [self.cookie(self.session_cookie, "", 0)],
                )
            if route == "/admin/access":
                return self.admin(environ, start_response, session)
            if route == "/auth/session":
                return self.respond(
                    start_response,
                    "200 OK",
                    {
                        "enabled": True,
                        "name": session["name"],
                        "csrf": session["csrf"],
                        "owner": self.is_owner(session["subject"]),
                    },
                )
            return self.app(environ, start_response)
        except LoginError:
            if route == CALLBACK:
                return self.page(
                    start_response,
                    "Sign-in could not be completed",
                    "The login expired or MonoSuite could not verify it. Please try again.",
                    "400 Bad Request",
                )
            return self.respond(
                start_response,
                "503 Service Unavailable",
                {
                    "error": "Could not verify your MonoSuite session. Try signing in again."
                },
            )
        except Exception:
            return self.respond(
                start_response,
                "503 Service Unavailable",
                {"error": "Authentication is unavailable"},
            )
