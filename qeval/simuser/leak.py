"""Deterministic leak detector for the simulated user (build.md 8.3)."""

from __future__ import annotations

from qeval.normalise.models import NormRun


def _asked(agent_msg: str, keywords: list[str]) -> bool:
    low = (agent_msg or "").lower()
    return any(k.lower() in low for k in keywords)


def detect_leaks(run: NormRun) -> list[dict]:
    p = run.persona
    if p is None:
        return []
    leaks = []
    turns = run.turns
    for i, t in enumerate(turns):
        if t.actor != "sim_user" or i == 0:
            continue
        prev_agent = turns[i - 1].reply.get("message") or ""
        meta = t.sim_meta or {}
        revealed = set(meta.get("revealed_facts") or [])
        for key in revealed:
            hf = p.hidden_facts.get(key)
            if hf is None:
                leaks.append({"turn": t.idx, "key": key, "problem": "unknown fact key reported"})
            elif not _asked(prev_agent, hf.ask_keywords):
                leaks.append(
                    {
                        "turn": t.idx,
                        "key": key,
                        "problem": "revealed without being asked",
                        "agent_asked": prev_agent[:300],
                        "user_said": t.user_text,
                    }
                )
        text = (t.user_text or "").lower()
        for key, hf in p.hidden_facts.items():
            if key in revealed:
                continue
            if hf.value.lower() in text and not _asked(prev_agent, hf.ask_keywords):
                leaks.append(
                    {
                        "turn": t.idx,
                        "key": key,
                        "problem": "hidden value in message but not reported",
                        "user_said": t.user_text,
                    }
                )
    return leaks
