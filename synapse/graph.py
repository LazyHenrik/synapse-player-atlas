import collections
import itertools
import json

import networkx as nx
from .interactions import LOG_KINDS, interaction_edges, log_status

KINDS = {"presence", "ban", "note", "warning", "kick", "blacklist"} | LOG_KINDS


def graph(db, start, end, kinds=None, minimum=10, max_nodes=300, focus=None):
    kinds = set(kinds if kinds is not None else KINDS)
    interval = (
        int(db.execute("SELECT value FROM settings WHERE key='interval'").fetchone()[0])
        * 1000
    )
    polls = list(
        db.execute(
            "SELECT * FROM polls WHERE observed_at>=? AND observed_at<=? ORDER BY slot",
            (start - interval * 2, end + interval * 2),
        )
    )
    roster = collections.defaultdict(set)
    identities = {}
    if polls:
        for row in db.execute(
            "SELECT slot,player_id FROM presence WHERE slot BETWEEN ? AND ?",
            (polls[0]["slot"], polls[-1]["slot"]),
        ):
            pid = identities.setdefault(row["player_id"], row["player_id"])
            roster[row["slot"]].add(pid)
    duration = collections.Counter()
    observed = set()
    spans = collections.defaultdict(lambda: [0, set()])
    covered = 0
    for poll in polls:
        if start <= poll["observed_at"] < end and poll["ok"]:
            observed.update(roster[poll["slot"]])
    for left, right in zip(polls, polls[1:]):
        delta = right["observed_at"] - left["observed_at"]
        if (
            not left["ok"]
            or not right["ok"]
            or right["slot"] != left["slot"] + 1
            or not 0 < delta <= interval * 1.5
        ):
            continue
        lo, hi = max(start, left["observed_at"]), min(end, right["observed_at"])
        if hi <= lo:
            continue
        seconds = (hi - lo) / 1000
        covered += seconds
        common = roster[left["slot"]] & roster[right["slot"]]
        crowd = max(len(roster[left["slot"]]), len(roster[right["slot"]]), 2) - 1
        observed.update(common)
        if common:
            span = spans[frozenset(common), crowd]
            span[0] += seconds
            span[1].update(range(lo // 86400000, (hi - 1) // 86400000 + 1))
    for (common, crowd), (seconds, days) in spans.items():
        for pid in common:
            duration[pid] += seconds
    events = [
        dict(r)
        for r in db.execute(
            "SELECT * FROM events WHERE created_at>=? AND created_at<? ORDER BY created_at DESC",
            (start, end),
        )
    ]
    selected_events = [r for r in events if r["kind"] in kinds]
    candidates = set(observed) if "presence" in kinds else set()
    for row in selected_events:
        candidates.add(row["subject"])
        if row["actor"]:
            candidates.add(row["actor"])
    counts = collections.Counter(r["subject"] for r in selected_events)
    interaction_rows = interaction_edges(db, start, end, kinds)
    selected_log_kinds = sorted(kinds & LOG_KINDS)
    if selected_log_kinds:
        for row in db.execute(
            """SELECT p.player_id,count(*) AS count FROM log_participants p
            JOIN logs l ON l.id=p.log_id WHERE l.created_at>=? AND l.created_at<?
            AND l.kind IN ("""
            + ",".join("?" for _ in selected_log_kinds)
            + """) GROUP BY p.player_id""",
            [start, end] + selected_log_kinds,
        ):
            candidates.add(row["player_id"])
            counts[row["player_id"]] += row["count"]
    ranked = sorted(candidates, key=lambda p: (-duration[p], -counts[p], p))
    chosen = set(ranked[:max_nodes])
    if focus and focus in candidates and focus not in chosen:
        if len(chosen) >= max_nodes:
            chosen.remove(ranked[max_nodes - 1])
        chosen.add(focus)
    pairs = collections.defaultdict(lambda: [0, 0, set()])
    if "presence" in kinds:
        for (common, crowd), (seconds, days) in spans.items():
            for a, b in itertools.combinations(sorted(common & chosen), 2):
                pair = pairs[a, b]
                pair[0] += seconds
                pair[1] += seconds / crowd
                pair[2].update(days)
    edges = []
    social = nx.Graph()
    social.add_nodes_from(sorted(chosen))
    for (a, b), (shared, adjusted, days) in pairs.items():
        if shared < minimum * 60:
            continue
        union = duration[a] + duration[b] - shared
        item = dict(
            source=a,
            target=b,
            kind="presence",
            directed=False,
            minutes=round(shared / 60, 2),
            weight=adjusted / 60,
            jaccard=shared / union if union else 0,
            days=len(days),
        )
        edges.append(item)
        social.add_edge(a, b, weight=item["weight"])
    labels = {}
    if social.number_of_edges():
        communities = nx.community.louvain_communities(social, weight="weight", seed=7)
        communities.sort(key=lambda c: (-len(c), sorted(c)[0]))
        for index, community in enumerate(communities):
            if len(community) > 1:
                labels.update({pid: index for pid in community})
    administrative = collections.Counter()
    for row in selected_events:
        if row["actor"] in chosen and row["subject"] in chosen:
            administrative[row["actor"], row["subject"], row["kind"]] += 1
    for (actor, subject, kind), count in administrative.items():
        edges.append(
            dict(
                source=actor,
                target=subject,
                kind=kind,
                count=count,
                weight=count,
                directed=True,
            )
        )
    for row in interaction_rows:
        if row["source"] in chosen and row["target"] in chosen:
            edges.append(row | dict(directed=False, logs=True))
    edges.sort(key=lambda e: (-e["weight"], e["source"], e["target"], e["kind"]))
    total_edges = len(edges)
    edges = edges[:5000]
    nodes = []
    for row in db.execute(
        "SELECT id,steam_id,name,first_seen,last_seen FROM players ORDER BY id"
    ):
        if row["id"] in chosen:
            nodes.append(
                dict(row)
                | dict(
                    minutes=round(duration[row["id"]] / 60, 2),
                    community=labels.get(row["id"]),
                    events=counts[row["id"]],
                )
            )
    within = [p for p in polls if start <= p["observed_at"] < end]
    jobs = [
        dict(r)
        for r in db.execute(
            "SELECT name,offset,last_success,error FROM jobs ORDER BY name"
        )
    ]
    return dict(
        nodes=nodes,
        edges=edges,
        start=start,
        end=end,
        stats=dict(
            players=len(candidates),
            displayed=len(nodes),
            edges=total_edges,
            hidden_edges=total_edges - len(edges),
            communities=len(set(labels.values())),
            coverage=covered / ((end - start) / 1000),
            covered_hours=covered / 3600,
            successful_polls=sum(p["ok"] for p in within),
            failed_polls=sum(not p["ok"] for p in within),
            latest_poll=dict(
                db.execute("SELECT * FROM polls ORDER BY slot DESC LIMIT 1").fetchone()
                or {}
            ),
            interval_seconds=interval // 1000,
        ),
        jobs=jobs,
        log_status=log_status(db, start, end),
        demo=bool(db.execute("SELECT 1 FROM settings WHERE key='demo'").fetchone()),
    )


def player_detail(db, pid, start, end):
    row = db.execute(
        "SELECT id,steam_id,name,first_seen,last_seen FROM players WHERE id=?", (pid,)
    ).fetchone()
    if not row:
        return None
    events = []
    for item in db.execute(
        """SELECT e.*, p.name AS actor_name, s.name AS subject_name FROM events e
        LEFT JOIN players p ON p.id=e.actor LEFT JOIN players s ON s.id=e.subject
        WHERE (e.subject=? OR e.actor=?) AND e.created_at>=? AND e.created_at<?
        ORDER BY e.created_at DESC LIMIT 1001""",
        (pid, pid, start, end),
    ):
        item = dict(item)
        data = json.loads(item.pop("data"))
        item["details"] = {
            k: data[k]
            for k in (
                "type",
                "expire",
                "unbannedAt",
                "unbanReason",
                "editedAt",
                "updatedAt",
                "active",
                "points",
                "value",
            )
            if k in data
        }
        events.append(item)
    job = db.execute(
        "SELECT last_success,error FROM jobs WHERE name=?", ("player:" + pid,)
    ).fetchone()
    return dict(row) | dict(
        events=events[:1000],
        history_truncated=len(events) > 1000,
        history=dict(job) if job else None,
    )
