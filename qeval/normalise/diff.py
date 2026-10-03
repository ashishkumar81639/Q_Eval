"""Diff two state snapshots by id."""

from __future__ import annotations

from qeval.normalise.models import Diff, StateView


def diff_states(before: StateView, after: StateView) -> Diff:
    bf = {f["id"]: f for f in before.facts}
    new_facts = [f for f in after.facts if f["id"] not in bf]
    superseded = [
        f for f in after.facts if f.get("superseded") and f["id"] in bf and not bf[f["id"]].get("superseded")
    ]
    new_subjects = [m for sid, m in after.members.items() if sid not in before.members]
    changed = []
    for sid, m in after.members.items():
        o = before.members.get(sid)
        if o and (o.name, o.relation, o.sex, o.dob) != (m.name, m.relation, m.sex, m.dob):
            changed.append((o, m))
    opened = [
        e for eid, e in after.episodes.items() if eid not in before.episodes and e.get("status") == "open"
    ]
    opened += [
        e
        for eid, e in after.episodes.items()
        if eid in before.episodes
        and before.episodes[eid].get("status") == "closed"
        and e.get("status") == "open"
    ]
    closed = [
        e
        for eid, e in after.episodes.items()
        if e.get("status") == "closed"
        and (eid not in before.episodes or before.episodes[eid].get("status") != "closed")
    ]
    ba = {a["id"] for a in before.assessments}
    bt = {t["id"] for t in before.turns}
    return Diff(
        new_facts=new_facts,
        superseded_facts=superseded,
        new_subjects=new_subjects,
        changed_subjects=changed,
        opened_episodes=opened,
        closed_episodes=closed,
        new_assessments=[a for a in after.assessments if a["id"] not in ba],
        new_turns=[t for t in after.turns if t["id"] not in bt],
    )
