import collections
import re

from .api import timestamp_ms
from .storage import person

LOG_KINDS = {"damage", "death", "radio", "pm", "communication", "other"}
COMMAND = re.compile(
    r"^(?:(?!\(STEAM_)[^\r\n])*\(STEAM_\d:\d:\d+\)\s*(?:\(\d+\)\s*)?"
    r"ran chat command:\s*[\"']?(/[a-z]+)\b",
    re.I,
)


def classify(category, message):
    category = category.strip().lower()
    if category == "damage":
        return "damage"
    if category in {"deaths", "kills"}:
        return "death"
    if category == "communication":
        match = COMMAND.match(message)
        command = match.group(1).lower() if match else None
        if command in {"/radio", "/radiowhisper", "/radioyell", "/tac"}:
            return "radio"
        if command in {"/pm", "/privatemessage"}:
            return "pm"
        return "communication"
    return "other"


def save_log(db, row, server, now):
    if not row.get("id") or row.get("serverId") != server:
        raise ValueError("Log has no id or belongs to another server")
    seconds = row["timestamp"]
    if type(seconds) is not int:
        raise ValueError("Expected an integer log timestamp in seconds")
    stamp = timestamp_ms(seconds * 1000)
    category, message = row["category"], row["message"]
    if not isinstance(category, str) or not isinstance(message, str):
        raise ValueError("Invalid log category or message")
    participants = row.get("participants")
    if participants is not None and not isinstance(participants, list):
        raise ValueError("Invalid log participants")
    db.execute(
        """INSERT INTO logs VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
        timestamp_s=excluded.timestamp_s,created_at=excluded.created_at,category=excluded.category,
        kind=excluded.kind,message=excluded.message,participant_status=excluded.participant_status,
        seen_at=excluded.seen_at""",
        (
            str(row["id"]),
            seconds,
            stamp,
            category,
            classify(category, message),
            message,
            "missing" if participants is None else "provided",
            now,
        ),
    )
    db.execute("DELETE FROM log_participants WHERE log_id=?", (str(row["id"]),))
    for participant in participants or []:
        pid = person(db, participant)
        db.execute(
            "INSERT OR IGNORE INTO log_participants VALUES (?,?)", (str(row["id"]), pid)
        )


def log_status(db, start, end):
    windows = list(
        db.execute(
            """SELECT * FROM log_windows
        WHERE start_s*1000<? AND end_s*1000>? AND status!='split' ORDER BY start_s""",
            (end, start),
        )
    )
    covered = sum(
        max(0, min(end, r["end_s"] * 1000) - max(start, r["start_s"] * 1000))
        for r in windows
        if r["status"] == "done"
    )
    return dict(
        coverage=min(1, covered / (end - start)),
        pending=sum(r["status"] == "pending" for r in windows),
        truncated=sum(r["status"] == "truncated" for r in windows),
        failures=sum(bool(r["error"]) for r in windows),
        oldest=windows[0]["start_s"] * 1000 if windows else None,
        total=db.execute(
            "SELECT count(*) FROM logs WHERE created_at>=? AND created_at<?",
            (start, end),
        ).fetchone()[0],
    )


def search_players(db, text):
    escaped = text.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return [
        dict(r)
        for r in db.execute(
            """SELECT id,name,steam_id FROM players
        WHERE name LIKE ? ESCAPE '!' OR steam_id LIKE ? ESCAPE '!' OR id=?
        ORDER BY name,id LIMIT 30""",
            ("%" + escaped + "%", "%" + escaped + "%", text),
        )
    ]


def explore_logs(
    db,
    start,
    end,
    players=(),
    match="between",
    kinds=None,
    text="",
    before=None,
    limit=50,
):
    players = sorted(set(players))
    kinds = LOG_KINDS if kinds is None else set(kinds)
    if (
        len(players) > 20
        or match not in {"between", "all", "any"}
        or not kinds <= LOG_KINDS
        or not 1 <= limit <= 100
        or len(text) > 200
    ):
        raise ValueError("Invalid log filters")
    if before is not None and (
        not isinstance(before, list)
        or len(before) != 2
        or type(before[0]) is not int
        or not isinstance(before[1], str)
    ):
        raise ValueError("Invalid log cursor")
    clauses = ["l.created_at>=?", "l.created_at<?"]
    values = [start, end]
    if not kinds:
        clauses.append("0")
    else:
        clauses.append("l.kind IN (" + ",".join("?" for _ in kinds) + ")")
        values.extend(sorted(kinds))
    if players:
        marks = ",".join("?" for _ in players)
        needed = (
            len(players)
            if match == "all"
            else min(2, len(players)) if match == "between" else 1
        )
        clauses.append(
            f"(SELECT count(*) FROM log_participants p WHERE p.log_id=l.id AND p.player_id IN ({marks}))>=?"
        )
        values.extend(players)
        values.append(needed)
    if text:
        clauses.append("instr(lower(l.message),lower(?))>0")
        values.append(text)
    where = " AND ".join(clauses)
    total = db.execute("SELECT count(*) FROM logs l WHERE " + where, values).fetchone()[
        0
    ]
    if before:
        where += " AND (l.created_at<? OR (l.created_at=? AND l.id<?))"
        values.extend([before[0], before[0], before[1]])
    rows = [
        dict(r)
        for r in db.execute(
            "SELECT l.* FROM logs l WHERE "
            + where
            + " ORDER BY l.created_at DESC,l.id DESC LIMIT ?",
            values + [limit + 1],
        )
    ]
    more = len(rows) > limit
    rows = rows[:limit]
    members = collections.defaultdict(list)
    if rows:
        ids = [r["id"] for r in rows]
        for r in db.execute(
            """SELECT lp.log_id,p.id,p.name,p.steam_id FROM log_participants lp
            JOIN players p ON p.id=lp.player_id WHERE lp.log_id IN ("""
            + ",".join("?" for _ in ids)
            + ") ORDER BY p.name",
            ids,
        ):
            members[r["log_id"]].append(dict(r))
    for row in rows:
        row["participants"] = members[row["id"]]
    return dict(
        logs=rows,
        total=total,
        next=[rows[-1]["created_at"], rows[-1]["id"]] if more else None,
        status=log_status(db, start, end),
        players=players,
        match=match,
    )


def interaction_edges(db, start, end, kinds):
    kinds = sorted(set(kinds) & LOG_KINDS)
    if not kinds:
        return []
    return [
        dict(r)
        for r in db.execute(
            """SELECT a.player_id AS source,b.player_id AS target,l.kind,
        count(*) AS count,sum(1.0/((SELECT count(*) FROM log_participants n WHERE n.log_id=l.id)-1)) AS weight
        FROM logs l JOIN log_participants a ON a.log_id=l.id
        JOIN log_participants b ON b.log_id=l.id AND a.player_id<b.player_id
        WHERE l.created_at>=? AND l.created_at<? AND l.kind IN ("""
            + ",".join("?" for _ in kinds)
            + """)
        GROUP BY a.player_id,b.player_id,l.kind""",
            [start, end] + kinds,
        )
    ]
