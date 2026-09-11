import json
import sqlite3
from pathlib import Path

from .api import timestamp_ms

DDL = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS players(
 id TEXT PRIMARY KEY, steam_id TEXT, name TEXT NOT NULL,
 first_seen INTEGER, last_seen INTEGER, history_due INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS polls(
 slot INTEGER PRIMARY KEY, observed_at INTEGER NOT NULL, ok INTEGER NOT NULL,
 online INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS presence(
 slot INTEGER NOT NULL REFERENCES polls(slot), player_id TEXT NOT NULL REFERENCES players(id),
 PRIMARY KEY(slot,player_id));
CREATE INDEX IF NOT EXISTS presence_player ON presence(player_id,slot);
CREATE TABLE IF NOT EXISTS events(
 kind TEXT NOT NULL, id TEXT NOT NULL, subject TEXT NOT NULL REFERENCES players(id),
 actor TEXT REFERENCES players(id), created_at INTEGER NOT NULL, scope TEXT NOT NULL,
 body TEXT NOT NULL, data TEXT NOT NULL, seen_at INTEGER NOT NULL,
 PRIMARY KEY(kind,id));
CREATE INDEX IF NOT EXISTS events_time ON events(created_at);
CREATE INDEX IF NOT EXISTS events_subject ON events(subject,created_at);
CREATE TABLE IF NOT EXISTS jobs(
 name TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0, due INTEGER NOT NULL DEFAULT 0,
 last_success INTEGER, error TEXT, cycle INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ban_seen(cycle INTEGER, id TEXT, PRIMARY KEY(cycle,id));
CREATE TABLE IF NOT EXISTS logs(
 id TEXT PRIMARY KEY, timestamp_s INTEGER NOT NULL, created_at INTEGER NOT NULL,
 category TEXT NOT NULL, kind TEXT NOT NULL, message TEXT NOT NULL,
 participant_status TEXT NOT NULL, seen_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS logs_time ON logs(created_at,id);
CREATE INDEX IF NOT EXISTS logs_kind_time ON logs(kind,created_at);
CREATE TABLE IF NOT EXISTS log_participants(
 log_id TEXT NOT NULL REFERENCES logs(id), player_id TEXT NOT NULL REFERENCES players(id),
 PRIMARY KEY(log_id,player_id));
CREATE INDEX IF NOT EXISTS log_participants_player ON log_participants(player_id,log_id);
CREATE TABLE IF NOT EXISTS log_windows(
 start_s INTEGER NOT NULL, end_s INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
 retry_at INTEGER NOT NULL DEFAULT 0, checked_at INTEGER, row_count INTEGER,
 error TEXT, PRIMARY KEY(start_s,end_s));
CREATE INDEX IF NOT EXISTS log_windows_pending ON log_windows(status,start_s,retry_at);
PRAGMA user_version=2;
"""


def connect(path, readonly=False):
    if readonly:
        db = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=10
        )
        db.execute("PRAGMA query_only=ON")
    else:
        db = sqlite3.connect(path, timeout=10)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def initialize(path, server, interval):
    db = connect(path)
    try:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, 2):
            raise ValueError(f"Unsupported database version {version}")
        db.executescript(DDL)
        with db:
            for key, value in [("server", server), ("interval", str(interval))]:
                db.execute("INSERT OR IGNORE INTO settings VALUES (?,?)", (key, value))
                if (
                    db.execute(
                        "SELECT value FROM settings WHERE key=?", (key,)
                    ).fetchone()[0]
                    != value
                ):
                    raise ValueError(f"Database {key} differs; use a separate database")
            for name in ["bans", "blacklists", "schema", "logs"]:
                db.execute("INSERT OR IGNORE INTO jobs(name) VALUES (?)", (name,))
    except Exception:
        db.close()
        raise
    return db


def person(db, row, observed=None):
    if not isinstance(row, dict) or not row.get("id"):
        raise ValueError("Missing player id")
    pid = str(row["id"])
    name = (row.get("profile") or {}).get("username") or row.get("steamId") or pid
    db.execute(
        """INSERT INTO players(id,steam_id,name,first_seen,last_seen) VALUES (?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET steam_id=COALESCE(excluded.steam_id,players.steam_id),
        name=excluded.name, first_seen=COALESCE(players.first_seen,excluded.first_seen),
        last_seen=COALESCE(excluded.last_seen,players.last_seen)""",
        (pid, row.get("steamId"), name, observed, observed),
    )
    return pid


def save_poll(db, slot, observed, server):
    if not isinstance(server, dict) or type(server.get("isOnline")) is not bool:
        raise ValueError("Missing server status")
    rows = server.get("onlinePlayers")
    if not isinstance(rows, list):
        raise ValueError("Missing onlinePlayers; null is not an empty server")
    if not server["isOnline"] and rows:
        raise ValueError("Offline server has a nonempty roster")
    with db:
        prior = db.execute("SELECT ok FROM polls WHERE slot=?", (slot,)).fetchone()
        if prior and prior["ok"]:
            return
        db.execute(
            "INSERT OR REPLACE INTO polls VALUES (?,?,1,?,NULL)",
            (slot, observed, int(server["isOnline"])),
        )
        for row in rows:
            pid = person(db, row, observed)
            db.execute("INSERT OR IGNORE INTO presence VALUES (?,?)", (slot, pid))


def save_failure(db, slot, observed, error):
    with db:
        db.execute(
            "INSERT OR IGNORE INTO polls VALUES (?,?,0,NULL,?)", (slot, observed, error)
        )


def event(db, kind, row, server, now, subject=None):
    origin = (row.get("server") or {}).get("id")
    group_wide = kind == "ban" and row.get("serverGroupWide") is True
    if kind != "note" and not group_wide and origin != server:
        if origin is None:
            raise ValueError("History row has unknown server scope")
        return
    target = subject if kind == "note" else row.get("user")
    if not target or not target.get("id"):
        raise ValueError("History row has no subject")
    pid = person(db, target)
    actor = person(db, row["admin"]) if row.get("admin") else None
    created = timestamp_ms(row["createdAt"])
    for key in ("updatedAt", "editedAt", "unbannedAt", "expire"):
        if row.get(key) not in (None, 0):
            timestamp_ms(row[key], expiry=key == "expire")
    scope = "group" if group_wide or kind == "note" else "server"
    body = row["content"] if kind == "note" else row["reason"]
    if not row.get("id"):
        raise ValueError("History row has no id")
    db.execute(
        """INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(kind,id) DO UPDATE SET subject=excluded.subject, actor=excluded.actor,
        created_at=excluded.created_at, scope=excluded.scope, body=excluded.body,
        data=excluded.data,seen_at=excluded.seen_at""",
        (
            kind,
            str(row["id"]),
            pid,
            actor,
            created,
            scope,
            body,
            json.dumps(row, ensure_ascii=False),
            now,
        ),
    )
