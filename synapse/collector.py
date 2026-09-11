import json
import logging
import random
import threading
import time

from .api import Source, make_client
from .storage import connect, event, initialize, save_failure, save_poll
from .log_collector import collect_log_step

LOG = logging.getLogger("synapse")


def now_ms():
    return time.time_ns() // 1_000_000


def failure_name(exc):
    # API error bodies can contain credentials or private notes.
    return type(exc).__name__


class Collector:
    def __init__(
        self,
        path,
        server,
        interval=60,
        history_interval=21600,
        source_factory=None,
        log_lookback_days=7,
    ):
        if interval < 10 or history_interval < 60:
            raise ValueError("Poll interval must be >=10s; history interval >=60s")
        self.path, self.server, self.interval = path, server, interval
        self.history_interval = history_interval * 1000
        if not 1 <= log_lookback_days <= 90:
            raise ValueError("Log lookback must be between 1 and 90 days")
        self.log_lookback_days = log_lookback_days
        self.source_factory = source_factory or (lambda: Source(make_client()))
        self.stop = threading.Event()
        db = initialize(path, server, interval)
        db.close()

    def poll(self, db, source, now=None):
        started = now if now is not None else now_ms()
        slot = started // (self.interval * 1000)
        prior = db.execute("SELECT ok FROM polls WHERE slot=?", (slot,)).fetchone()
        if prior and prior["ok"]:
            return True
        try:
            server = source.query("presence", server=self.server)["server"]
            if (
                not server
                or server.get("id") != self.server
                or not server.get("serverGroupId")
            ):
                raise ValueError("Wrong server or missing group")
            existing = db.execute(
                "SELECT value FROM settings WHERE key='group'"
            ).fetchone()
            if existing and existing[0] != server["serverGroupId"]:
                raise ValueError("Server changed group; start a separate database")
            with db:
                db.execute(
                    "INSERT OR IGNORE INTO settings VALUES ('group',?)",
                    (server["serverGroupId"],),
                )
                db.execute(
                    "INSERT OR REPLACE INTO settings VALUES ('server_name',?)",
                    (server["name"],),
                )
            finished = now if now is not None else now_ms()
            save_poll(db, slot, finished, server)
            LOG.info(
                "presence ok slot=%s players=%s", slot, len(server["onlinePlayers"])
            )
            return True
        except Exception as exc:
            save_failure(db, slot, started, failure_name(exc))
            LOG.warning("presence failed slot=%s error=%s", slot, failure_name(exc))
            return False

    def history_step(self, db, source, now=None):
        now = now if now is not None else now_ms()
        group = db.execute("SELECT value FROM settings WHERE key='group'").fetchone()
        if not group:
            return False
        job = db.execute("SELECT * FROM jobs WHERE name='bans'").fetchone()
        if job["due"] <= now:
            try:
                block = source.query(
                    "bans", group=group[0], limit=100, offset=job["offset"]
                )["group"]["bans"]
                rows, total = block["bans"], block["total"]
                if not isinstance(rows, list) or type(total) is not int or total < 0:
                    raise ValueError("Invalid ban page")
                if not rows and job["offset"] < total:
                    raise ValueError("Ban page ended before reported total")
                with db:
                    before = db.total_changes
                    for row in rows:
                        db.execute(
                            "INSERT OR IGNORE INTO ban_seen VALUES (?,?)",
                            (job["cycle"], row["id"]),
                        )
                    if rows and db.total_changes == before:
                        raise ValueError("Ban pagination repeated a page")
                    for row in rows:
                        event(db, "ban", row, self.server, now)
                    offset = job["offset"] + len(rows)
                    if offset >= total:
                        db.execute(
                            "UPDATE jobs SET offset=0,due=?,last_success=?,error=NULL,cycle=cycle+1 WHERE name='bans'",
                            (now + self.history_interval, now),
                        )
                        db.execute("DELETE FROM ban_seen")
                    else:
                        db.execute(
                            "UPDATE jobs SET offset=?,error=NULL WHERE name='bans'",
                            (offset,),
                        )
            except Exception as exc:
                with db:
                    db.execute(
                        "UPDATE jobs SET due=?,error=? WHERE name='bans'",
                        (now + 60000, failure_name(exc)),
                    )
                LOG.warning("ban page failed error=%s", failure_name(exc))
            # One page per step leaves room for individual histories even during backfill.
        job = db.execute("SELECT * FROM jobs WHERE name='blacklists'").fetchone()
        if job["due"] <= now:
            try:
                rows = source.query("blacklists", server=self.server)["server"][
                    "blacklists"
                ]
                if not isinstance(rows, list):
                    raise ValueError("Missing blacklists")
                with db:
                    for row in rows:
                        event(db, "blacklist", row, self.server, now)
                    db.execute(
                        "UPDATE jobs SET due=?,last_success=?,error=NULL WHERE name='blacklists'",
                        (now + self.history_interval, now),
                    )
            except Exception as exc:
                with db:
                    db.execute(
                        "UPDATE jobs SET due=?,error=? WHERE name='blacklists'",
                        (now + 60000, failure_name(exc)),
                    )
                LOG.warning("blacklists failed error=%s", failure_name(exc))
        player = db.execute(
            "SELECT * FROM players WHERE steam_id IS NOT NULL AND history_due<=? ORDER BY history_due,id LIMIT 1",
            (now,),
        ).fetchone()
        if player:
            try:
                row = source.query(
                    "player", server=self.server, value=player["steam_id"]
                )["server"]["player"]
                if not row or row["id"] != player["id"]:
                    raise ValueError("Player lookup did not match")
                with db:
                    for field, kind in [
                        ("notes", "note"),
                        ("warnings", "warning"),
                        ("kicks", "kick"),
                    ]:
                        if not isinstance(row.get(field), list):
                            raise ValueError(f"Missing {field}")
                        for item in row[field]:
                            event(db, kind, item, self.server, now, subject=row)
                    db.execute(
                        "UPDATE players SET history_due=? WHERE id=?",
                        (now + self.history_interval, player["id"]),
                    )
                    db.execute(
                        "INSERT OR REPLACE INTO jobs(name,last_success) VALUES (?,?)",
                        ("player:" + player["id"], now),
                    )
            except Exception as exc:
                with db:
                    db.execute(
                        "UPDATE players SET history_due=? WHERE id=?",
                        (now + 300000, player["id"]),
                    )
                    db.execute(
                        """INSERT INTO jobs(name,error) VALUES (?,?) ON CONFLICT(name)
                        DO UPDATE SET error=excluded.error""",
                        ("player:" + player["id"], failure_name(exc)),
                    )
                LOG.warning("player history failed error=%s", failure_name(exc))
        return True

    def worker(self, history=False, logs=False):
        db = connect(self.path)
        source = None
        schema_due = 0
        failures = 0
        try:
            while not self.stop.is_set():
                try:
                    if source is None:
                        source = self.source_factory()
                    if time.monotonic() >= schema_due:
                        schema = source.check_schema()
                        with db:
                            db.execute(
                                "INSERT OR REPLACE INTO settings VALUES ('schema',?)",
                                (json.dumps(schema),),
                            )
                            db.execute(
                                "UPDATE jobs SET last_success=?,error=NULL WHERE name='schema'",
                                (now_ms(),),
                            )
                        schema_due = time.monotonic() + 86400
                    if logs:
                        collect_log_step(
                            db, source, self.server, now_ms(), self.log_lookback_days
                        )
                        delay = 2
                    elif history:
                        self.history_step(db, source)
                        delay = 2
                    else:
                        ok = self.poll(db, source)
                        failures = 0 if ok else failures + 1
                        delay = self.interval - (time.time() % self.interval)
                        if failures:
                            delay = max(
                                delay, min(300, 2 ** min(failures, 8)) + random.random()
                            )
                    self.stop.wait(delay)
                except Exception as exc:
                    LOG.error(
                        "worker failed mode=%s error=%s",
                        "logs" if logs else "history" if history else "presence",
                        failure_name(exc),
                    )
                    try:
                        with db:
                            db.execute(
                                "UPDATE jobs SET error=? WHERE name='schema'",
                                (failure_name(exc),),
                            )
                        if not history and not logs:
                            stamp = now_ms()
                            save_failure(
                                db,
                                stamp // (self.interval * 1000),
                                stamp,
                                failure_name(exc),
                            )
                    except Exception:
                        LOG.error("could not record worker failure")
                    self.stop.wait(30)
        finally:
            db.close()

    def run(self):
        threads = [
            threading.Thread(target=self.worker, kwargs={"history": h}, name=str(h))
            for h in (False, True)
        ]
        threads.append(
            threading.Thread(target=self.worker, kwargs={"logs": True}, name="logs")
        )
        for thread in threads:
            thread.start()
        try:
            while any(thread.is_alive() for thread in threads):
                self.stop.wait(1)
                if self.stop.is_set():
                    break
        finally:
            self.stop.set()
            for thread in threads:
                thread.join()
