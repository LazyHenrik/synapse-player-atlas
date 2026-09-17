import io
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import Mock

from synapse.auth import Auth, CALLBACK, LoginError, digest


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.config = self.path / "client.json"
        self.settings = {
            "client_id": "client",
            "client_secret": "secret",
            "redirect_uri": "http://127.0.0.1:8790" + CALLBACK,
            "scopes": ["moderation.note.view"],
            "allowed_subjects": ["owner"],
        }
        self.save_config()
        self.now = 1000
        self.provider = Mock()
        self.provider.exchange.return_value = {
            "access_token": "access",
            "refresh_token": "refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        self.provider.identity.return_value = {"sub": "owner", "name": "Owner"}
        self.provider.refresh.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "token_type": "Bearer",
            "expires_in": 3600,
        }
        self.inner = Mock(
            side_effect=lambda env, respond: (respond("200 OK", []), [b"private data"])[
                1
            ]
        )
        self.app = Auth(
            self.inner,
            self.config,
            self.path / "auth.sqlite",
            provider=self.provider,
            clock=lambda: self.now,
        )

    def save_config(self):
        self.config.write_text(json.dumps(self.settings))

    def request(self, route="/", cookie="", method="GET", **extra):
        parsed = urlsplit(route)
        env = {
            "REQUEST_METHOD": method,
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "HTTP_HOST": "127.0.0.1:8790",
            "HTTP_COOKIE": cookie,
            "wsgi.input": io.BytesIO(),
            **extra,
        }
        result = {}

        def respond(status, headers):
            result.update(status=status, headers=headers)

        result["body"] = b"".join(self.app(env, respond))
        return result

    def begin(self):
        result = self.request("/auth/login")
        query = parse_qs(urlsplit(dict(result["headers"])["Location"]).query)
        cookie = dict(result["headers"])["Set-Cookie"].split(";")[0]
        return query, cookie

    def finish(self, query, cookie, **params):
        return self.request(
            CALLBACK
            + "?"
            + urlencode({"state": query["state"][0], "code": "code", **params}),
            cookie,
        )

    def login(self):
        query, cookie = self.begin()
        result = self.finish(query, cookie)
        self.assertEqual(result["status"], "303 See Other")
        return next(
            value.split(";")[0]
            for key, value in result["headers"]
            if key == "Set-Cookie" and value.startswith("synapse_session=")
        )

    def test_every_data_route_needs_login(self):
        for route in [
            "/api/graph",
            "/api/logs",
            "/api/player",
            "/api/players",
            "/api/health",
            "/app.js",
            "/auth/session",
        ]:
            self.assertEqual(self.request(route)["status"], "401 Unauthorized")
        self.inner.assert_not_called()
        self.assertIn(b"Sign in with MonoSuite", self.request()["body"])

    def decision(self, cookie, subject, action, **extra):
        csrf = json.loads(self.request("/auth/session", cookie)["body"])["csrf"]
        body = urlencode(dict(subject=subject, action=action, csrf=csrf)).encode()
        return self.request(
            "/admin/access",
            cookie,
            "POST",
            **{
                "HTTP_ORIGIN": self.app.origin,
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": io.BytesIO(body),
                **extra,
            }
        )

    def test_owner_approval_and_immediate_revocation(self):
        self.settings["owner_subjects"] = ["owner"]
        self.save_config()
        owner = self.login()
        self.provider.identity.return_value = {
            "sub": "staff",
            "name": "<script>alert(1)</script>",
        }
        for _ in range(2):
            query, flow = self.begin()
            self.assertEqual(self.finish(query, flow)["status"], "403 Forbidden")
        portal = self.request("/admin/access", owner)
        self.assertIn(b"Pending requests (1)", portal["body"])
        self.assertNotIn(b"<script>", portal["body"])
        self.assertEqual(
            self.decision(
                owner, "staff", "approved", HTTP_ORIGIN="https://evil.example"
            )["status"],
            "403 Forbidden",
        )
        self.assertEqual(
            self.decision(owner, "staff", "approved", HTTP_ORIGIN="null")["status"],
            "403 Forbidden",
        )
        self.assertFalse(self.app.allowed("staff"))
        self.assertEqual(
            self.decision(owner, "staff", "approved")["status"], "303 See Other"
        )
        staff = self.login()
        self.assertEqual(self.request("/api/logs", staff)["status"], "200 OK")
        self.assertEqual(
            self.request("/admin/access", staff)["status"], "403 Forbidden"
        )
        self.assertEqual(
            self.decision(staff, "owner", "revoked")["status"], "403 Forbidden"
        )
        self.assertEqual(
            self.decision(owner, "owner", "revoked")["status"], "403 Forbidden"
        )
        self.assertEqual(
            self.decision(owner, "staff", "revoked")["status"], "303 See Other"
        )
        self.assertEqual(self.request("/api/logs", staff)["status"], "401 Unauthorized")
        self.app.request_access("staff", "Staff")
        self.assertFalse(self.app.allowed("staff"))
        with self.app.db() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM access_audit").fetchone()[0], 2
            )

    def test_owner_portal_uses_same_origin_referrer_policy(self):
        self.settings["owner_subjects"] = ["owner"]
        self.save_config()
        owner = self.login()
        result = self.request("/admin/access", owner)
        self.assertEqual(
            dict(result["headers"])["Referrer-Policy"], "same-origin"
        )

    def test_deny_and_legacy_revoke_survive_restart(self):
        self.settings["owner_subjects"] = ["owner"]
        self.settings["allowed_subjects"].append("legacy")
        self.save_config()
        cookie = self.login()
        self.app.request_access("new", "New")
        self.assertEqual(
            self.decision(cookie, "new", "denied")["status"], "303 See Other"
        )
        self.assertEqual(
            self.decision(cookie, "legacy", "revoked")["status"], "303 See Other"
        )
        self.app = Auth(
            self.inner,
            self.config,
            self.path / "auth.sqlite",
            provider=self.provider,
            clock=lambda: self.now,
        )
        self.assertFalse(self.app.allowed("new"))
        self.assertFalse(self.app.allowed("legacy"))
        self.assertEqual(
            self.decision(cookie, "new", "approved")["status"], "303 See Other"
        )
        self.assertTrue(self.app.allowed("new"))
        self.config.write_text('{"allowed_subjects": null}')
        self.assertEqual(
            self.request("/api/logs", cookie)["status"], "503 Service Unavailable"
        )

    def test_pkce_state_and_browser_binding(self):
        query, cookie = self.begin()
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertNotIn("secret", str(query))
        self.assertEqual(self.finish(query, "")["status"], "400 Bad Request")
        self.provider.exchange.assert_not_called()
        self.assertEqual(self.finish(query, cookie)["status"], "303 See Other")
        self.assertEqual(self.finish(query, cookie)["status"], "400 Bad Request")
        self.assertEqual(self.provider.exchange.call_count, 1)

    def test_login_retry_after_provider_dashboard_starts_fresh_flow(self):
        first, first_cookie = self.begin()
        retry, retry_cookie = self.begin()
        self.assertNotEqual(first["state"], retry["state"])
        self.assertEqual(self.finish(first, retry_cookie)["status"], "400 Bad Request")
        self.provider.exchange.assert_not_called()
        result = self.finish(retry, retry_cookie)
        self.assertEqual(result["status"], "303 See Other")
        self.assertEqual(dict(result["headers"])["Location"], "/")
        self.assertEqual(dict(result["headers"])["Referrer-Policy"], "no-referrer")

    def test_expired_and_duplicate_state_are_rejected(self):
        query, cookie = self.begin()
        self.now += 601
        self.assertEqual(self.finish(query, cookie)["status"], "400 Bad Request")
        self.assertEqual(
            self.request(CALLBACK + "?state=x&state=y&code=z", cookie)["status"],
            "400 Bad Request",
        )
        self.provider.exchange.assert_not_called()

    def test_identity_must_be_verified_and_explicitly_allowed(self):
        self.provider.identity.return_value = {"sub": "stranger", "name": "<script>"}
        query, cookie = self.begin()
        result = self.finish(query, cookie)
        self.assertEqual(result["status"], "403 Forbidden")
        self.assertIn(b"account stranger", result["body"])
        with self.app.db() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0
            )
        self.inner.assert_not_called()

    def test_allowlist_removal_takes_effect_immediately(self):
        cookie = self.login()
        self.assertEqual(self.request("/api/logs", cookie)["body"], b"private data")
        self.settings["allowed_subjects"] = []
        self.save_config()
        self.assertEqual(
            self.request("/api/logs", cookie)["status"], "401 Unauthorized"
        )

    def test_revalidation_fails_closed(self):
        cookie = self.login()
        self.now += 61
        self.provider.identity.side_effect = LoginError()
        self.assertEqual(
            self.request("/api/logs", cookie)["status"], "503 Service Unavailable"
        )
        self.inner.assert_not_called()

    def test_refresh_rotation_survives_identity_outage(self):
        cookie = self.login()
        self.now += 3590
        self.provider.identity.side_effect = LoginError()
        self.assertEqual(
            self.request("/api/logs", cookie)["status"], "503 Service Unavailable"
        )
        with self.app.db() as db:
            tokens = json.loads(db.execute("SELECT tokens FROM sessions").fetchone()[0])
        self.assertEqual(tokens["refresh_token"], "new-refresh")
        self.provider.identity.side_effect = None
        self.assertEqual(self.request("/api/logs", cookie)["status"], "200 OK")
        self.assertEqual(self.provider.refresh.call_count, 1)

    def test_identity_switch_is_rejected(self):
        cookie = self.login()
        self.now += 61
        self.provider.identity.return_value = {"sub": "different"}
        self.assertEqual(
            self.request("/api/logs", cookie)["status"], "503 Service Unavailable"
        )

    def test_logout_requires_csrf_and_origin_and_invalidates_session(self):
        cookie = self.login()
        response = self.request("/auth/session", cookie)
        session = json.loads(response["body"])
        self.assertNotIn("access", response["body"].decode())
        self.assertEqual(
            self.request("/auth/logout", cookie, "POST")["status"], "403 Forbidden"
        )
        result = self.request(
            "/auth/logout",
            cookie,
            "POST",
            HTTP_ORIGIN=self.app.origin,
            HTTP_X_CSRF_TOKEN=session["csrf"],
        )
        self.assertEqual(result["status"], "200 OK")
        self.assertEqual(
            self.request("/api/logs", cookie)["status"], "401 Unauthorized"
        )

    def test_session_survives_restart_but_has_absolute_expiry(self):
        cookie = self.login()
        self.app = Auth(
            self.inner,
            self.config,
            self.path / "auth.sqlite",
            provider=self.provider,
            clock=lambda: self.now,
        )
        self.assertEqual(self.request("/api/logs", cookie)["status"], "200 OK")
        self.now += 28801
        self.assertEqual(
            self.request("/api/logs", cookie)["status"], "401 Unauthorized"
        )

    def test_logout_works_when_provider_is_unavailable(self):
        cookie = self.login()
        session = json.loads(self.request("/auth/session", cookie)["body"])
        self.now += 61
        self.provider.identity.side_effect = LoginError()
        result = self.request(
            "/auth/logout",
            cookie,
            "POST",
            HTTP_ORIGIN=self.app.origin,
            HTTP_X_CSRF_TOKEN=session["csrf"],
        )
        self.assertEqual(result["status"], "200 OK")

    def test_bad_host_methods_and_callback_configuration(self):
        self.assertEqual(
            self.request(HTTP_HOST="evil.example")["status"], "400 Bad Request"
        )
        self.assertEqual(
            self.request("/api/logs", method="POST")["status"], "405 Method Not Allowed"
        )
        self.settings["redirect_uri"] = "http://public.example" + CALLBACK
        self.save_config()
        with self.assertRaises(ValueError):
            Auth(self.inner, self.config, self.path / "auth.sqlite")

    def test_https_cookies_are_secure_and_host_scoped(self):
        self.settings["redirect_uri"] = "https://atlas.example.com" + CALLBACK
        self.save_config()
        app = Auth(self.inner, self.config, self.path / "auth.sqlite")
        header = app.cookie(app.session_cookie, "value", 100)[1]
        for required in [
            "__Host-synapse_session",
            "Secure",
            "HttpOnly",
            "SameSite=Lax",
            "Path=/",
        ]:
            self.assertIn(required, header)
        self.assertNotIn("Domain=", header)

    def test_invalid_token_response_and_error_details_stay_private(self):
        self.provider.exchange.return_value = {"access_token": "secret"}
        query, cookie = self.begin()
        response = self.finish(query, cookie)
        self.assertEqual(response["status"], "400 Bad Request")
        self.assertNotIn(b"secret", response["body"])


if __name__ == "__main__":
    unittest.main()
