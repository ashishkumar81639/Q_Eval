"""Map ids to members; flag likely duplicate members."""

from __future__ import annotations

from qeval.normalise.models import Member

NICKNAMES = [
    {"mira", "meera"},
    {"ishaan", "ishan"},
    {"aarav", "arav"},
    {"riya", "ria"},
    {"kabir", "kabeer"},
    {"anaya", "ananya"},
    {"sid", "siddharth"},
    {"vicky", "vikram"},
    {"bunty", "bunny"},
]


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def similar_names(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    a, b = a.casefold().strip(), b.casefold().strip()
    if a == b:
        return True
    if any(a in pair and b in pair for pair in NICKNAMES):
        return True
    return edit_distance(a, b) <= 2 and min(len(a), len(b)) >= 3


def build_members(subjects: list[dict], refs: dict[str, str]) -> tuple[dict[str, Member], dict[str, str]]:
    id_to_ref = {v: k for k, v in refs.items() if v.startswith("s_")}
    ref_to_id = dict(refs)
    members: dict[str, Member] = {}
    for s in subjects:
        sid = s["id"]
        ref = id_to_ref.get(sid)
        if s.get("relation") == "self":
            ref_to_id.setdefault("self", sid)
            ref = ref or "self"
        members[sid] = Member(
            id=sid,
            ref=ref,
            relation=s.get("relation") or "",
            name=s.get("name"),
            sex=s.get("sex"),
            dob=s.get("date_of_birth"),
            dob_estimated=bool(s.get("dob_estimated")),
            label=s.get("label") or "",
            seeded=sid in id_to_ref or s.get("relation") == "self",
        )
    return members, ref_to_id


def possible_duplicates(members: dict[str, Member]) -> list[tuple[str, str]]:
    ms = list(members.values())
    out = []
    for i, a in enumerate(ms):
        for b in ms[i + 1 :]:
            if not similar_names(a.name, b.name):
                continue
            if a.relation == b.relation or (a.sex and a.sex == b.sex):
                out.append((a.id, b.id))
    return out
