import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from graphql import build_client_schema, parse, validate
from monosuite_cli import ApiError, AuthError, MonoSuiteClient

from synapse.api import (
    INTROSPECTION,
    QUERIES,
    Source,
    read_only,
    timestamp_ms,
    token_provider,
)
from synapse.collector import Collector
from synapse.graph import graph, player_detail
from synapse.storage import connect, event, initialize, save_failure, save_poll
from synapse.web import create_app
from synapse.__main__ import collector_lock

T = 1788000000000


def player(pid):
    return dict(
        id=pid, steamId="7656119800000000" + pid, profile={"username": "Player " + pid}
    )


def ban(bid, server="s", group_wide=False):
    return dict(
        id=str(bid),
        user=player("1"),
        admin=player("2"),
        server={"id": server},
        createdAt=T,
        reason="Test record",
        expire=None,
        serverGroupWide=group_wide,
    )


class DatabaseTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite"
        self.db = initialize(self.path, "s", 60)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def poll(self, index, *ids, at=None):
        stamp = T + index * 60000 if at is None else at
        save_poll(
            self.db,
            T // 60000 + index,
            stamp,
            dict(isOnline=True, onlinePlayers=[player(p) for p in ids]),
        )

    def test_duplicate_poll_is_immutable(self):
        self.poll(0, "1", "1", "2")
        self.poll(0, "3")
        self.assertEqual(self.db.execute("SELECT count(*) FROM polls").fetchone()[0], 1)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM presence").fetchone()[0], 2
        )

    def test_failed_poll_can_be_retried_without_duplicate(self):
        save_failure(self.db, T // 60000, T, "TransportError")
        self.poll(0, "1")
        save_failure(self.db, T // 60000, T, "TransportError")
        self.assertEqual(self.db.execute("SELECT ok FROM polls").fetchone()[0], 1)

    def test_invalid_roster_is_atomic(self):
        with self.assertRaises(ValueError):
            save_poll(
                self.db, 1, T, dict(isOnline=True, onlinePlayers=[player("1"), {}])
            )
        self.assertEqual(self.db.execute("SELECT count(*) FROM polls").fetchone()[0], 0)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM players").fetchone()[0], 0
        )

    def test_null_roster_and_stale_offline_roster_are_rejected(self):
        for server in [
            dict(isOnline=True, onlinePlayers=None),
            dict(isOnline=False, onlinePlayers=[player("1")]),
        ]:
            with self.assertRaises(ValueError):
                save_poll(self.db, 1, T, server)

    def test_weighting_and_boundary_clipping(self):
        self.poll(0, "1", "2", "3")
        self.poll(1, "1", "2", "3")
        self.poll(2, "1", "2")
        data = graph(self.db, T + 30000, T + 90000, {"presence"}, 0)
        pair = next(
            e for e in data["edges"] if (e["source"], e["target"]) == ("1", "2")
        )
        self.assertEqual(pair["minutes"], 1)
        self.assertEqual(pair["weight"], 0.5)
        self.assertEqual(pair["jaccard"], 1)
        self.assertEqual(data["stats"]["coverage"], 1)

    def test_join_leave_does_not_credit_one_sided_presence(self):
        self.poll(0, "1")
        self.poll(1, "1", "2")
        self.poll(2, "2")
        data = graph(self.db, T, T + 120000, {"presence"}, 0)
        self.assertEqual(data["edges"], [])
        self.assertEqual([n["minutes"] for n in data["nodes"]], [1, 1])

    def test_no_bridge_across_failure_missing_slot_or_long_delay(self):
        self.poll(0, "1", "2")
        save_failure(self.db, T // 60000 + 1, T + 60000, "TimeoutError")
        self.poll(2, "1", "2")
        self.poll(4, "1", "2")
        self.poll(5, "1", "2", at=T + 400000)
        data = graph(self.db, T, T + 500000, {"presence"}, 0)
        self.assertEqual(data["edges"], [])
        self.assertEqual(data["stats"]["coverage"], 0)

    def test_server_scope_and_event_edits_are_idempotent(self):
        with self.db:
            event(self.db, "ban", ban(1), "s", T)
            changed = ban(1)
            changed["reason"] = "Edited"
            changed["unbannedAt"] = T + 1000
            event(self.db, "ban", changed, "s", T + 2000)
            event(self.db, "ban", ban(2, "other"), "s", T)
            event(self.db, "ban", ban(3, "other", True), "s", T)
        rows = list(self.db.execute("SELECT * FROM events ORDER BY id"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["body"], "Edited")
        self.assertEqual(rows[1]["scope"], "group")

    def test_administrative_direction_time_filters_and_missing_actor(self):
        with self.db:
            event(self.db, "ban", ban(1), "s", T)
            b = ban(2)
            b["admin"] = None
            event(self.db, "ban", b, "s", T)
        data = graph(self.db, T, T + 1000, {"ban"}, 0)
        self.assertEqual(data["edges"][0]["source"], "2")
        self.assertEqual(data["edges"][0]["target"], "1")
        self.assertEqual(data["stats"]["communities"], 0)
        self.assertEqual(len(player_detail(self.db, "1", T, T + 1000)["events"]), 2)
        self.assertEqual(graph(self.db, T + 1, T + 1000, {"ban"}, 0)["nodes"], [])

    def test_database_is_bound_to_one_server_and_cadence(self):
        for server, interval in [("other", 60), ("s", 30)]:
            with self.assertRaises(ValueError):
                initialize(self.path, server, interval)

    def test_readonly_database_rejects_writes(self):
        with contextlib.closing(connect(self.path, readonly=True)) as db:
            with self.assertRaises(Exception):
                db.execute("DELETE FROM players")

    def test_collector_lock_prevents_second_process(self):
        with collector_lock(self.path):
            with self.assertRaises(RuntimeError):
                with collector_lock(self.path):
                    pass

    def test_poll_recovers_and_does_not_repeat_success(self):
        collector = Collector(self.path, "s")
        source = Mock()
        source.query.side_effect = [
            TimeoutError(),
            {
                "server": dict(
                    id="s",
                    name="Server",
                    serverGroupId="g",
                    isOnline=True,
                    onlinePlayers=[player("1")],
                )
            },
        ]
        self.assertFalse(collector.poll(self.db, source, T))
        self.assertTrue(collector.poll(self.db, source, T + 60000))
        self.assertTrue(collector.poll(self.db, source, T + 60000))
        self.assertEqual(source.query.call_count, 2)

    def test_paginated_bans_resume_after_failure_and_reconcile(self):
        with self.db:
            self.db.execute("INSERT INTO settings VALUES ('group','g')")
            self.db.execute(
                "UPDATE jobs SET due=? WHERE name='blacklists'", (T + 9999999,)
            )
        collector = Collector(self.path, "s")
        seen = []
        failing = True

        def query(name, **variables):
            nonlocal failing
            if name == "blacklists":
                return {"server": {"blacklists": []}}
            if name == "player":
                row = player(variables["value"][-1])
                row.update(notes=[], warnings=[], kicks=[])
                return {"server": {"player": row}}
            offset = variables["offset"]
            seen.append(offset)
            if offset == 100 and failing:
                failing = False
                raise TimeoutError()
            return {
                "group": {
                    "bans": {
                        "total": 205,
                        "bans": [ban(i) for i in range(offset, min(offset + 100, 205))],
                    }
                }
            }

        source = Mock()
        source.query.side_effect = query
        collector.history_step(self.db, source, T)
        collector.history_step(self.db, source, T + 1)
        self.assertEqual(
            self.db.execute("SELECT offset FROM jobs WHERE name='bans'").fetchone()[0],
            100,
        )
        self.db.close()
        self.db = connect(self.path)
        collector = Collector(self.path, "s")
        collector.history_step(self.db, source, T + 60002)
        collector.history_step(self.db, source, T + 60003)
        self.assertEqual(seen, [0, 100, 100, 200])
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM events").fetchone()[0], 205
        )
        job = self.db.execute("SELECT * FROM jobs WHERE name='bans'").fetchone()
        self.assertEqual(job["offset"], 0)
        self.assertIsNotNone(job["last_success"])
        collector.history_step(self.db, source, T + 21600001)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM events").fetchone()[0], 205
        )

    def test_repeated_page_does_not_claim_completion(self):
        with self.db:
            self.db.execute("INSERT INTO settings VALUES ('group','g')")
            self.db.execute(
                "UPDATE jobs SET due=? WHERE name='blacklists'", (T + 9999999,)
            )
        source = Mock()

        def query(name, **kwargs):
            if name == "player":
                row = player(kwargs["value"][-1])
                row.update(notes=[], warnings=[], kicks=[])
                return {"server": {"player": row}}
            return {"group": {"bans": {"total": 200, "bans": [ban(1)]}}}

        source.query.side_effect = query
        collector = Collector(self.path, "s")
        collector.history_step(self.db, source, T)
        collector.history_step(self.db, source, T + 1)
        job = self.db.execute("SELECT * FROM jobs WHERE name='bans'").fetchone()
        self.assertEqual(job["offset"], 1)
        self.assertIsNone(job["last_success"])
        self.assertEqual(job["error"], "ValueError")

    def test_worker_survives_schema_outage_then_collects(self):
        source = Mock()
        source.check_schema.side_effect = [TimeoutError(), {"__schema": {}}]
        source.query.return_value = {
            "server": dict(
                id="s",
                name="Server",
                serverGroupId="g",
                isOnline=True,
                onlinePlayers=[player("1")],
            )
        }
        collector = Collector(self.path, "s", source_factory=lambda: source)
        waits = []

        def wait(delay):
            waits.append(delay)
            if len(waits) >= 2:
                collector.stop.set()

        with patch.object(collector.stop, "wait", side_effect=wait):
            collector.worker()
        self.assertEqual(source.check_schema.call_count, 2)
        self.assertEqual(self.db.execute("SELECT sum(ok) FROM polls").fetchone()[0], 1)

    def test_empty_ban_page_before_total_is_an_error(self):
        with self.db:
            self.db.execute("INSERT INTO settings VALUES ('group','g')")
            self.db.execute(
                "UPDATE jobs SET due=? WHERE name='blacklists'", (T + 9999999,)
            )
        source = Mock()
        source.query.return_value = {"group": {"bans": {"total": 150, "bans": []}}}
        Collector(self.path, "s").history_step(self.db, source, T)
        job = self.db.execute("SELECT * FROM jobs WHERE name='bans'").fetchone()
        self.assertEqual(job["offset"], 0)
        self.assertEqual(job["error"], "ValueError")
        self.assertIsNone(job["last_success"])

    def test_http_validation_and_read_only_routes(self):
        app = create_app(self.path)

        def request(path, query="", method="GET"):
            statuses = []
            result = app(
                {
                    "PATH_INFO": path,
                    "QUERY_STRING": query,
                    "REQUEST_METHOD": method,
                    "wsgi.input": io.BytesIO(),
                },
                lambda s, h: statuses.append(s),
            )
            return statuses[0], b"".join(result)

        self.assertEqual(request("/")[0], "200 OK")
        self.assertEqual(
            request("/api/graph", method="POST")[0], "405 Method Not Allowed"
        )
        self.assertEqual(request("/../monosuite_cli.py")[0], "404 Not Found")
        self.assertEqual(request("/api/graph", "minimum=nan")[0], "400 Bad Request")
        self.assertEqual(
            request("/api/graph", "start=1000&end=999")[0], "400 Bad Request"
        )
        self.assertEqual(request("/api/graph", "kinds=unknown")[0], "400 Bad Request")
        self.assertEqual(request("/api/health")[0], "503 Service Unavailable")
        self.poll(0, "1", "2")
        self.poll(1, "1", "2")
        status, body = request("/api/graph", f"start={T}&end={T+60000}&minimum=0")
        self.assertEqual(status, "200 OK")
        self.assertEqual(len(json.loads(body)["edges"]), 1)
        status, body = request("/api/graph", f"start={T}&end={T+60000}&kinds=")
        self.assertEqual(status, "200 OK")
        self.assertEqual(json.loads(body)["nodes"], [])

    def test_restricted_kicks_preserve_existing_records_and_import_notes(self):
        self.poll(0, "1")
        with self.db:
            self.db.execute("INSERT INTO settings VALUES ('group','g')")
            self.db.execute("UPDATE jobs SET due=?", (T + 9999999,))
            event(self.db, "kick", ban("old-kick"), "s", T)
        row = player("1")
        row.update(
            notes=[dict(id="n", content="A note", createdAt=T, admin=player("2"))],
            warnings=[],
        )
        source = Mock()
        source.query.return_value = {
            "server": {"player": row},
            "unavailable_history": ["kicks"],
        }
        collector = Collector(self.path, "s")
        collector.history_step(self.db, source, T)
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM events WHERE kind='note'").fetchone()[
                0
            ],
            1,
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM events WHERE kind='kick'").fetchone()[
                0
            ],
            1,
        )
        self.assertIn(
            "moderation.kick",
            self.db.execute("SELECT error FROM jobs WHERE name='kicks'").fetchone()[0],
        )
        self.assertIsNone(
            self.db.execute("SELECT error FROM jobs WHERE name='player:1'").fetchone()[
                0
            ]
        )
        row["kicks"] = []
        source.query.return_value = {"server": {"player": row}}
        with self.db:
            self.db.execute("UPDATE players SET history_due=0 WHERE id='1'")
        collector.history_step(self.db, source, T + 1000)
        self.assertIsNone(
            self.db.execute("SELECT * FROM jobs WHERE name='kicks'").fetchone()
        )
        self.assertEqual(
            self.db.execute("SELECT count(*) FROM events WHERE kind='kick'").fetchone()[
                0
            ],
            1,
        )


class ApiTest(unittest.TestCase):
    def test_kick_scope_fallback_is_explicit_and_rechecks_rotated_credential(self):
        client = Mock(token="scoped-key")
        limited = {"server": {"player": dict(notes=[], warnings=[])}}
        client.execute.side_effect = [
            ApiError("This credential is not scoped for: moderation.kick", []),
            limited,
            {"server": {"player": {}}},
            {"server": {"player": {"kicks": []}}},
        ]
        source = Source(client)
        result = source.query("player", server="s", value="1")
        self.assertEqual(result["unavailable_history"], ["kicks"])
        self.assertNotIn("kicks", result["server"]["player"])
        source.query("player", server="s", value="2")
        self.assertEqual(
            client.execute.call_args.args[0], QUERIES["player_without_kicks"]
        )
        client.token = "replacement-key"
        result = source.query("player", server="s", value="1")
        self.assertEqual(client.execute.call_args.args[0], QUERIES["player"])
        self.assertNotIn("unavailable_history", result)

    def test_other_permission_failures_are_not_hidden(self):
        client = Mock(token="scoped-key")
        client.execute.side_effect = ApiError(
            "This credential is not scoped for: moderation.note.view", []
        )
        with self.assertRaises(ApiError):
            Source(client).query("player", server="s", value="1")
        self.assertEqual(client.execute.call_count, 1)

    def test_mutations_and_side_effect_queries_blocked_before_transport(self):
        client = MonoSuiteClient(on_request=read_only)
        with patch.object(client, "_post") as post:
            for doc in [
                'mutation { deleteNote(id:"x") }',
                "query { logout }",
                'query { login(provider:"steam") }',
                "query X { __typename } mutation Y { unban }",
            ]:
                with self.assertRaises(ValueError):
                    client.execute(doc)
            post.assert_not_called()

    def test_queries_validate_against_checked_live_schema(self):
        schema = build_client_schema(
            json.loads(
                (Path(__file__).parents[1] / "synapse/schema.json").read_text(
                    encoding="utf-8"
                )
            )
        )
        for name, query in QUERIES.items():
            self.assertEqual(validate(schema, parse(query)), [], name)
            read_only(query)
        read_only(INTROSPECTION)

    def test_millisecond_conversion_is_not_guessed(self):
        self.assertEqual(timestamp_ms(T), T)
        self.assertIsNone(timestamp_ms(None, nullable=True))
        for value in [T // 1000, True, 0, -1]:
            with self.assertRaises(ValueError):
                timestamp_ms(value)
        self.assertEqual(timestamp_ms(20710239896878, expiry=True), 20710239896878)

    def test_graphql_auth_failure_refreshes_once(self):
        client = Mock()
        client.execute.side_effect = [AuthError("expired"), {"server": {}}]
        client.refresh_token.return_value = True
        Source(client).query("presence", server="s")
        client.refresh_token.assert_called_once()
        self.assertEqual(client.execute.call_count, 2)

    def test_token_file_is_reread_on_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secret"
            with patch.dict("os.environ", {"SYNAPSE_TOKEN_FILE": str(path)}):
                path.write_text("old", encoding="utf-8")
                self.assertEqual(token_provider(), "old")
                path.write_text("new", encoding="utf-8")
                self.assertEqual(token_provider(), "new")


if __name__ == "__main__":
    unittest.main()
