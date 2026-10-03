"""Indexes over one GET /state snapshot."""

from __future__ import annotations

from qeval.normalise.members import build_members, possible_duplicates
from qeval.normalise.models import StateView


def build_state_view(raw: dict, refs: dict[str, str], agent_closed_ids: set[str] | None = None) -> StateView:
    raw = raw or {}
    members, ref_to_id = build_members(raw.get("subjects") or [], refs)
    facts = list(raw.get("facts") or [])
    by_subj: dict[str | None, list[dict]] = {}
    for f in facts:
        by_subj.setdefault(f.get("subject_id"), []).append(f)
    episodes: dict[str, dict] = {}
    by_ep_subj: dict[str, list[dict]] = {}
    for e in sorted(raw.get("episodes") or [], key=lambda e: e.get("opened_at") or ""):
        e = dict(e)
        closed_by = None
        if e.get("status") == "closed":
            if (e.get("outcome") or "").lower().startswith("auto-closed"):
                closed_by = "platform"
            elif agent_closed_ids and e["id"] in agent_closed_ids:
                closed_by = "agent"
            else:
                closed_by = "unknown"
        e["closed_by"] = closed_by
        episodes[e["id"]] = e
        by_ep_subj.setdefault(str(e.get("subject_id")), []).append(e)
    return StateView(
        raw=raw,
        account_id=(raw.get("account") or {}).get("id", ""),
        members=members,
        ref_to_id=ref_to_id,
        facts=facts,
        facts_by_subject=by_subj,
        episodes=episodes,
        episodes_by_subject=by_ep_subj,
        assessments=list(raw.get("assessments") or []),
        turns=sorted(raw.get("turns") or [], key=lambda t: t.get("seq") or 0),
        possible_duplicates=possible_duplicates(members),
    )
