import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from synapse.graph import graph
from synapse.interactions import (
    classify,
    explore_logs,
    interaction_edges,
    save_log,
    search_players,
)
from synapse.log_collector import collect_log_step
from synapse.storage import initialize, person
from synapse.web import create_app

T = 1788000000


def record(
    key="one",
    members=("a", "b"),
    stamp=T,
    category="Damage",
    message="Fictional record",
):
    return dict(
        id=key,
        serverId="s",
        timestamp=stamp,
        category=category,
        message=message,
        participants=[dict(id=p, profile={"username": p}) for p in members],
    )


class InteractionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite"
        self.db = initialize(self.path, "s", 60)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def save(self, row):
        with self.db:
            save_log(self.db, row, "s", (T + 60) * 1000)

    def search(self, **kwargs):
        return explore_logs(self.db, T * 1000, (T + 10) * 1000, **kwargs)

    def window(self, start=T, end=T + 4):
        with self.db:
            self.db.execute(
                "INSERT INTO log_windows(start_s,end_s) VALUES (?,?)", (start, end)
            )
            self.db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('logs_enqueued_s',?)",
                (str(T + 600),),
            )
            self.db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('logs_replay_ms',?)",
                (str((T + 600) * 1000),),
            )

    def test_command_classification_uses_envelope_not_keywords(self):
        prefix = "Example (STEAM_0:1:12345) (12) ran chat command: "
        self.assertEqual(classify("Communication", prefix + '"/radio" hello'), "radio")
        self.assertEqual(classify("Communication", prefix + '"/pm" hello'), "pm")
        self.assertEqual(
            classify("Communication", prefix + '"/ic" someone mentioned /pm'),
            "communication",
        )
        self.assertEqual(
            classify("Communication", "someone wrote /radio hello"), "communication"
        )
        self.assertEqual(classify("Damage", "/radio"), "damage")
        self.assertEqual(
            classify(
                "Communication",
                'A (STEAM_0:1:1) said: B (STEAM_0:1:2) ran chat command: "/pm"',
            ),
            "communication",
        )

    def test_seconds_only_scope_and_transactional_memberships(self):
        self.save(record())
        self.assertEqual(self.search()["logs"][0]["created_at"], T * 1000)
        for bad in [
            record(stamp=T * 1000),
            record() | {"serverId": "wrong"},
            record() | {"participants": [{}]},
        ]:
            with self.assertRaises(ValueError):
                self.save(bad)
        self.assertEqual(
            [p["id"] for p in self.search()["logs"][0]["participants"]], ["a", "b"]
        )

    def test_id_upsert_replaces_members_without_duplicate_edges(self):
        self.save(record())
        self.save(record(members=("a", "c", "c")))
        self.assertEqual(self.search()["total"], 1)
        self.assertEqual(
            [p["id"] for p in self.search()["logs"][0]["participants"]], ["a", "c"]
        )
        self.assertEqual(
            interaction_edges(self.db, T * 1000, (T + 10) * 1000, {"damage"})[0][
                "count"
            ],
            1,
        )

    def test_pair_group_any_filters_and_broadcasts(self):
        for key, members in [
            ("ab", ("a", "b")),
            ("ac", ("a", "c")),
            ("abc", ("a", "b", "c")),
            ("a", ("a",)),
        ]:
            self.save(record(key, members))
        self.assertEqual(self.search(players=["a", "b"])["total"], 2)
        self.assertEqual(self.search(players=["a", "b", "c"])["total"], 3)
        self.assertEqual(self.search(players=["a", "b", "c"], match="all")["total"], 1)
        self.assertEqual(self.search(players=["a", "b", "c"], match="any")["total"], 4)
        self.assertEqual(self.search(players=["a"])["total"], 4)
        self.assertEqual(self.search(kinds=set())["total"], 0)
        edges = interaction_edges(self.db, T * 1000, (T + 10) * 1000, {"damage"})
        ab = next(e for e in edges if e["source"] == "a" and e["target"] == "b")
        self.assertEqual(ab["count"], 2)
        self.assertEqual(ab["weight"], 1.5)

    def test_single_sender_visible_but_has_no_edge_or_community(self):
        self.save(record(members=("a",), category="Communication"))
        result = graph(self.db, T * 1000, (T + 10) * 1000, kinds={"communication"})
        self.assertEqual(len(result["nodes"]), 1)
        self.assertEqual(result["edges"], [])
        self.assertIsNone(result["nodes"][0]["community"])

    def test_missing_participants_are_preserved(self):
        self.save(record() | {"participants": None})
        row = self.search()["logs"][0]
        self.assertEqual(row["participant_status"], "missing")
        self.assertEqual(row["participants"], [])

    def test_same_second_local_pagination_and_half_open_bounds(self):
        for i in range(7):
            self.save(record(str(i)))
        self.save(record("outside", stamp=T + 10))
        ids = []
        before = None
        while True:
            page = self.search(limit=2, before=before)
            ids.extend(r["id"] for r in page["logs"])
            before = page["next"]
            if not before:
                break
        self.assertEqual(ids, ["6", "5", "4", "3", "2", "1", "0"])

    def test_search_is_literal_and_does_not_interpret_markup(self):
        self.save(record(message="<script>alert(1)</script> 100%_ test"))
        self.assertEqual(self.search(text="%_")["total"], 1)
        self.assertEqual(self.search(text="' OR 1=1 --")["total"], 0)
        with self.db:
            person(self.db, dict(id="special", profile={"username": "100%_!"}))
        self.assertEqual([p["id"] for p in search_players(self.db, "%_!")], ["special"])

    def test_capped_windows_split_and_resume_without_api_cursor(self):
        self.window()
        rows = [record(str(i), stamp=T + i) for i in range(4)]
        source = Mock()

        def fetch(name, server, data):
            self.assertNotIn("paginationId", data)
            found = [
                r
                for r in rows
                if data["startTimestamp"] <= r["timestamp"] <= data["endTimestamp"]
            ]
            return {"server": {"logs": {"logs": found[:2], "total": len(found)}}}

        source.query.side_effect = fetch
        for _ in range(3):
            self.assertTrue(collect_log_step(self.db, source, "s", (T + 120) * 1000))
        self.assertEqual(self.search()["total"], 4)
        states = [
            r[0]
            for r in self.db.execute(
                "SELECT status FROM log_windows ORDER BY start_s,end_s"
            )
        ]
        self.assertEqual(sorted(states), ["done", "done", "split"])
        self.assertEqual(self.search()["status"]["pending"], 0)

    def test_one_second_saturation_is_disclosed(self):
        self.window(end=T + 1)
        source = Mock()
        source.query.return_value = {
            "server": {"logs": {"logs": [record()], "total": 10000}}
        }
        self.assertTrue(collect_log_step(self.db, source, "s", (T + 120) * 1000))
        self.assertEqual(self.search()["status"]["truncated"], 1)
        self.assertEqual(self.search()["status"]["coverage"], 0)

    def test_failed_window_retries_without_partial_write(self):
        self.window()
        source = Mock()
        source.query.return_value = {
            "server": {
                "logs": {"logs": [record(), record("bad", stamp=T + 5)], "total": 2}
            }
        }
        self.assertFalse(collect_log_step(self.db, source, "s", (T + 120) * 1000))
        self.assertEqual(self.search()["total"], 0)
        source.query.return_value = {
            "server": {"logs": {"logs": [record()], "total": 1}}
        }
        self.assertFalse(collect_log_step(self.db, source, "s", (T + 121) * 1000))
        self.assertTrue(collect_log_step(self.db, source, "s", (T + 181) * 1000))
        self.assertEqual(self.search()["total"], 1)

    def test_recent_replay_upserts_late_arrivals(self):
        self.window(end=T + 1)
        source = Mock()
        source.query.return_value = {
            "server": {"logs": {"logs": [record()], "total": 1}}
        }
        collect_log_step(self.db, source, "s", (T + 120) * 1000)
        with self.db:
            self.db.execute("UPDATE settings SET value='0' WHERE key='logs_replay_ms'")
        source.query.return_value = {
            "server": {"logs": {"logs": [record(), record("late")], "total": 2}}
        }
        collect_log_step(self.db, source, "s", (T + 121) * 1000)
        self.assertEqual(self.search()["total"], 2)

    def test_additive_v1_migration_preserves_data(self):
        with self.db:
            person(self.db, dict(id="old"))
            self.db.execute("DROP TABLE log_participants")
            self.db.execute("DROP TABLE logs")
            self.db.execute("DROP TABLE log_windows")
            self.db.execute("PRAGMA user_version=1")
        upgraded = initialize(self.path, "s", 60)
        self.assertEqual(upgraded.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertEqual(
            upgraded.execute("SELECT id FROM players").fetchone()[0], "old"
        )
        upgraded.close()

    def test_log_routes_validate_and_remain_read_only(self):
        self.save(record())
        app = create_app(self.path)

        def request(path, query="", method="GET"):
            status = []
            body = b"".join(
                app(
                    dict(REQUEST_METHOD=method, PATH_INFO=path, QUERY_STRING=query),
                    lambda s, h: status.append(s),
                )
            )
            return status[0], json.loads(body)

        query = f"start={T*1000}&end={(T+10)*1000}"
        self.assertEqual(request("/api/logs", query + "&players=a,b")[1]["total"], 1)
        self.assertEqual(
            request("/api/logs", query + "&match=wrong")[0], "400 Bad Request"
        )
        self.assertEqual(
            request("/api/logs", query + "&before={}")[0], "400 Bad Request"
        )
        self.assertEqual(request("/api/logs", query + "&kinds=")[1]["total"], 0)
        self.assertEqual(
            request("/api/logs", query, method="POST")[0], "405 Method Not Allowed"
        )
        self.assertEqual(len(request("/api/players", "q=a")[1]["players"]), 1)


if __name__ == "__main__":
    unittest.main()
