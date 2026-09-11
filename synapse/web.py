import json
import math
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

from .graph import KINDS, graph, player_detail
from .storage import connect
from .interactions import explore_logs, search_players, LOG_KINDS

STATIC = Path(__file__).parent / "static"


def create_app(path):
    cache = {}
    lock = threading.Lock()

    def app(environ, start_response):
        headers = [
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            ("X-Frame-Options", "DENY"),
            (
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
            ),
        ]

        def respond(status, data, mime="application/json"):
            body = (
                data
                if isinstance(data, bytes)
                else json.dumps(data, allow_nan=False).encode()
            )
            start_response(
                status,
                headers + [("Content-Type", mime), ("Content-Length", str(len(body)))],
            )
            return [body]

        if environ["REQUEST_METHOD"] != "GET":
            return respond("405 Method Not Allowed", {"error": "Read only"})
        route = environ.get("PATH_INFO", "/")
        if route == "/auth/session":
            return respond("200 OK", {"enabled": False})
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
            "/synapse-logo.png": ("synapse-logo.png", "image/png"),
            "/synapse-wordmark.webp": ("synapse-wordmark.webp", "image/webp"),
        }
        if route in assets:
            filename, mime = assets[route]
            return respond("200 OK", (STATIC / filename).read_bytes(), mime)
        if route not in (
            "/api/graph",
            "/api/player",
            "/api/health",
            "/api/logs",
            "/api/players",
        ):
            return respond("404 Not Found", {"error": "Not found"})
        db = None
        try:
            params = {
                k: v[-1]
                for k, v in parse_qs(
                    environ.get("QUERY_STRING", ""), keep_blank_values=True
                ).items()
            }
            now = int(time.time() * 1000)
            start, end = int(params.get("start", now - 7 * 86400000)), int(
                params.get("end", now)
            )
            if (
                not 0 < end - start <= 90 * 86400000
                or start < 0
                or end > now + 86400000
            ):
                raise ValueError(
                    "Choose a time range between 1 millisecond and 90 days"
                )
            db = connect(path, readonly=True)
            db.execute("BEGIN")
            if route == "/api/players":
                query = params.get("q", "")
                if len(query) > 100:
                    raise ValueError("Player search is too long")
                return respond("200 OK", {"players": search_players(db, query)})
            if route == "/api/logs":
                players = [p for p in params.get("players", "").split(",") if p]
                kinds = set(
                    params.get("kinds", ",".join(sorted(LOG_KINDS))).split(",")
                ) - {""}
                before = json.loads(params["before"]) if params.get("before") else None
                result = explore_logs(
                    db,
                    start,
                    end,
                    players,
                    params.get("match", "between"),
                    kinds,
                    params.get("text", ""),
                    before,
                    int(params.get("limit", 50)),
                )
                return respond("200 OK", result)
            if route == "/api/health":
                last = dict(
                    db.execute(
                        "SELECT * FROM polls ORDER BY slot DESC LIMIT 1"
                    ).fetchone()
                    or {}
                )
                interval = int(
                    db.execute(
                        "SELECT value FROM settings WHERE key='interval'"
                    ).fetchone()[0]
                )
                healthy = bool(
                    last.get("ok") and now - last["observed_at"] < interval * 3000
                )
                return respond(
                    "200 OK" if healthy else "503 Service Unavailable",
                    {"healthy": healthy, "latest_poll": last},
                )
            if route == "/api/player":
                result = player_detail(db, params.get("id", ""), start, end)
                return respond(
                    "200 OK" if result else "404 Not Found",
                    result or {"error": "Player not found"},
                )
            kinds = set(params.get("kinds", ",".join(sorted(KINDS))).split(",")) - {""}
            minimum = float(params.get("minimum", 10))
            max_nodes = int(params.get("limit", 300))
            if (
                not kinds <= KINDS
                or not math.isfinite(minimum)
                or not 0 <= minimum <= 129600
                or not 1 <= max_nodes <= 500
            ):
                raise ValueError("Invalid graph filters")
            key = (
                start,
                end,
                tuple(sorted(kinds)),
                minimum,
                max_nodes,
                params.get("focus"),
            )
            with lock:
                cached = cache.get(key)
                if cached and time.monotonic() - cached[0] < 30:
                    return respond("200 OK", cached[1])
                result = graph(
                    db, start, end, kinds, minimum, max_nodes, params.get("focus")
                )
                if len(cache) >= 16:
                    cache.clear()
                cache[key] = (time.monotonic(), result)
            return respond("200 OK", result)
        except (ValueError, OverflowError) as exc:
            return respond("400 Bad Request", {"error": str(exc)})
        except Exception:
            return respond(
                "503 Service Unavailable",
                {"error": "Data unavailable. Check the database and collector logs."},
            )
        finally:
            if db is not None:
                db.close()

    return app
