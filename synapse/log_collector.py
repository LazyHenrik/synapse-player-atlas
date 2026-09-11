import logging

from .interactions import save_log

LOG = logging.getLogger("synapse")
PAGE_SIZE = 500


def collect_log_step(db, source, server, now, lookback_days=7):
    end = (now // 1000 - 60) // 60 * 60
    with db:
        row = db.execute(
            "SELECT value FROM settings WHERE key='logs_enqueued_s'"
        ).fetchone()
        cursor = int(row[0]) if row else end - lookback_days * 86400
        while cursor < end:
            following = min(cursor + 3600, end)
            db.execute(
                "INSERT OR IGNORE INTO log_windows(start_s,end_s) VALUES (?,?)",
                (cursor, following),
            )
            cursor = following
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES ('logs_enqueued_s',?)",
            (str(cursor),),
        )
        replay = db.execute(
            "SELECT value FROM settings WHERE key='logs_replay_ms'"
        ).fetchone()
        if not replay or int(replay[0]) <= now:
            db.execute(
                "UPDATE log_windows SET status='pending',retry_at=0 WHERE end_s>? AND status IN ('done','truncated')",
                (end - 300,),
            )
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES ('logs_replay_ms',?)",
                (str(now + 600000),),
            )
    # Persist the alternation: request duration must not let backfill starve the tail.
    turn = db.execute("SELECT value FROM settings WHERE key='logs_turn'").fetchone()
    order = "DESC" if turn and turn[0] == "recent" else "ASC"
    window = db.execute(
        f"SELECT * FROM log_windows WHERE status='pending' AND retry_at<=? ORDER BY start_s {order} LIMIT 1",
        (now,),
    ).fetchone()
    if not window:
        return False
    with db:
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES ('logs_turn',?)",
            ("old" if order == "DESC" else "recent",),
        )
    try:
        block = source.query(
            "logs",
            server=server,
            data=dict(
                fetchCount=PAGE_SIZE,
                ordering="asc",
                startTimestamp=window["start_s"],
                endTimestamp=window["end_s"] - 1,
            ),
        )["server"]["logs"]
        rows, total = block["logs"], block["total"]
        if not isinstance(rows, list) or type(total) is not int or total < len(rows):
            raise ValueError("Invalid log result")
        ids = {r["id"] for r in rows}
        if len(ids) != len(rows):
            raise ValueError("Duplicate ids within a log response")
        saturated = total > len(rows) or len(rows) >= PAGE_SIZE
        status = (
            "split"
            if saturated and window["end_s"] - window["start_s"] > 1
            else "truncated" if saturated else "done"
        )
        with db:
            for row in rows:
                if not window["start_s"] <= row["timestamp"] < window["end_s"]:
                    raise ValueError(
                        "API returned a log outside requested second bounds"
                    )
                save_log(db, row, server, now)
            db.execute(
                "UPDATE log_windows SET status=?,checked_at=?,row_count=?,error=NULL WHERE start_s=? AND end_s=?",
                (status, now, len(rows), window["start_s"], window["end_s"]),
            )
            if status == "split":
                middle = (window["start_s"] + window["end_s"]) // 2
                db.executemany(
                    "INSERT OR IGNORE INTO log_windows(start_s,end_s) VALUES (?,?)",
                    [(window["start_s"], middle), (middle, window["end_s"])],
                )
            db.execute(
                "UPDATE jobs SET last_success=?,error=NULL WHERE name='logs'", (now,)
            )
        LOG.info(
            "logs imported rows=%s window=%s..%s status=%s",
            len(rows),
            window["start_s"],
            window["end_s"],
            status,
        )
        return True
    except Exception as exc:
        with db:
            db.execute(
                "UPDATE log_windows SET retry_at=?,error=? WHERE start_s=? AND end_s=?",
                (now + 60000, type(exc).__name__, window["start_s"], window["end_s"]),
            )
            db.execute(
                "UPDATE jobs SET error=? WHERE name='logs'", (type(exc).__name__,)
            )
        LOG.warning("log window failed error=%s", type(exc).__name__)
        return False
