import random
import time
from pathlib import Path

from .storage import event, initialize, save_failure, save_poll
from .interactions import save_log


def seed_interactions(db, end):
    if not db.execute("SELECT 1 FROM settings WHERE key='demo'").fetchone():
        raise ValueError("Synthetic logs require a demo database")
    players = [
        dict(id=r["id"], profile={"username": r["name"]})
        for r in db.execute("SELECT id,name FROM players ORDER BY id")
    ]
    by_id = {p["id"]: p for p in players}
    start = end - 7 * 86400000
    with db:
        for index in range(360):
            a, b = by_id[f"demo-{index % 30}"], by_id[f"demo-{(index % 30 + 1) % 30}"]
            members = [a, b]
            mode = index % 6
            category = [
                "Damage",
                "Deaths",
                "Communication",
                "Communication",
                "Communication",
                "Roleplay",
            ][mode]
            message = [
                "Fictional damage record: two players listed during a training scene.",
                "Fictional death record: two players listed during a staged encounter.",
                'Demo (STEAM_0:0:12345) ran chat command: "/radio" Regroup at the station. [fictional]',
                'Demo (STEAM_0:0:12345) ran chat command: "/pm" Meet for the next scene. [fictional]',
                "Fictional conversation record: players listed in a shared roleplay scene.",
                "Fictional scene record: a group exchanges supplies.",
            ][mode]
            if mode == 2:
                members = [a]
            if mode == 5:
                members.append(by_id[f"demo-{(index % 30 + 2) % 30}"])
            save_log(
                db,
                dict(
                    id=f"demo-log-{index:04}",
                    serverId="demo",
                    timestamp=(start + (index + 1) * (end - start) // 361) // 1000,
                    category=category,
                    message=message,
                    participants=members,
                ),
                "demo",
                end,
            )
        db.execute(
            "INSERT OR REPLACE INTO log_windows(start_s,end_s,status,checked_at,row_count) VALUES (?,?,'done',?,360)",
            (start // 1000, end // 1000, end),
        )


def generate(path):
    if Path(path).exists():
        raise ValueError(
            "Demo requires a new database path; existing data is never overwritten"
        )
    rng = random.Random(7)
    names = [
        "Aster",
        "Moss",
        "Finch",
        "Rowan",
        "Juniper",
        "Ash",
        "Fern",
        "Wren",
        "Cedar",
        "Sage",
        "Echo",
        "Vale",
        "Nova",
        "Rook",
        "Piper",
        "Slate",
        "Iris",
        "Orbit",
        "Rune",
        "Drift",
        "Lark",
        "Reed",
        "Ember",
        "Sol",
        "Cove",
        "Lumen",
        "Birch",
        "Rain",
        "Kit",
        "Marlow",
        "Staff Alder",
        "Staff Willow",
    ]
    players = [
        dict(id=f"demo-{i}", steamId=None, profile={"username": name})
        for i, name in enumerate(names)
    ]
    db = initialize(path, "demo", 60)
    db.execute("PRAGMA synchronous=NORMAL")
    end = int(time.time()) // 60 * 60000
    start = end - 7 * 86400000
    try:
        with db:
            db.execute("INSERT INTO settings VALUES ('demo','true')")
            db.execute("INSERT INTO settings VALUES ('group','demo-group')")
        for stamp in range(start, end + 1, 60000):
            slot = stamp // 60000
            minute = (stamp - start) // 60000
            if 2800 < minute < 2840 or rng.random() < 0.008:
                save_failure(db, slot, stamp, "DemoOutage")
                continue
            dayminute = minute % 1440
            active = []
            for i, player in enumerate(players):
                group = min(i // 10, 2)
                lo = [60, 430, 790][group] + (i % 10) * 8
                hi = lo + 170 + (i % 5) * 20
                if lo <= dayminute < hi or (
                    i in (8, 18, 28, 30, 31) and 700 <= dayminute < 960
                ):
                    active.append(player)
            save_poll(db, slot, stamp, dict(isOnline=True, onlinePlayers=active))
        with db:
            for index, target in enumerate(players[:30]):
                if index % 3:
                    continue
                stamp = end - (index % 6 + 1) * 86400000 + 3600000
                kind = ["note", "ban", "warning", "kick", "blacklist"][index % 5]
                row = dict(
                    id=f"event-{index}",
                    createdAt=stamp,
                    user=target,
                    admin=players[30 + index % 2],
                    server={"id": "demo"},
                    reason="Fictional record for demonstration.",
                    content="Fictional staff note. No real player is represented.",
                    type="Neutral",
                    expire=stamp + 3600000,
                    serverGroupWide=False,
                )
                if kind not in ("ban", "blacklist"):
                    row.pop("expire")
                event(db, kind, row, "demo", end, target)
                db.execute(
                    "INSERT INTO jobs(name,last_success) VALUES (?,?)",
                    ("player:" + target["id"], end),
                )
            db.execute("UPDATE jobs SET last_success=?", (end,))
        seed_interactions(db, end)
    finally:
        db.close()
