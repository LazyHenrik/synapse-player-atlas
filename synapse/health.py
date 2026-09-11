import contextlib
import sys
import time

from .storage import connect


def healthy(path):
    with contextlib.closing(connect(path, readonly=True)) as db:
        row = db.execute(
            "SELECT observed_at,ok FROM polls ORDER BY slot DESC LIMIT 1"
        ).fetchone()
        interval = int(
            db.execute("SELECT value FROM settings WHERE key='interval'").fetchone()[0]
        )
        return bool(
            row
            and row["ok"]
            and time.time() * 1000 - row["observed_at"] < interval * 3000
        )


if __name__ == "__main__":
    try:
        sys.exit(0 if healthy(sys.argv[1]) else 1)
    except Exception:
        sys.exit(1)
