"""Saved scenario_run + turns -> NormRun (build.md 9.2)."""

from __future__ import annotations

from datetime import date

from qeval import timeutil
from qeval.normalise.diff import diff_states
from qeval.normalise.models import AccountInfo, NormRun, NormTurn, StateView
from qeval.normalise.state_view import build_state_view
from qeval.normalise.trace import build_trace


def _agent_closed_ids(turn_rows: list[dict]) -> set[str]:
    ids: set[str] = set()
    for t in turn_rows:
        for e in (t.get("response") or {}).get("trace") or []:
            if e.get("tool") == "close_episode" and not e.get("is_error"):
                eid = (e.get("input") or {}).get("episode_id")
                if eid:
                    ids.add(eid)
    return ids


def build_norm_run(
    scenario,
    sr: dict,
    turn_rows: list[dict],
    anchor_date: date | str,
    persona=None,
) -> NormRun:
    if isinstance(anchor_date, str):
        anchor_date = date.fromisoformat(anchor_date)
    turn_rows = sorted(turn_rows, key=lambda t: t["idx"])
    closed = _agent_closed_ids(turn_rows)
    accounts_meta = sr.get("accounts") or {}
    initial_states = sr.get("initial_states") or {}
    final_states = sr.get("final_states") or {}

    accounts: dict[str, AccountInfo] = {}
    for key, meta in accounts_meta.items():
        refs = meta.get("refs") or {}
        init = build_state_view(initial_states.get(key) or {}, refs, closed)
        fin = final_states.get(key)
        accounts[key] = AccountInfo(
            key=key,
            account_id=meta.get("account_id", ""),
            refs=refs,
            initial=init,
            final=build_state_view(fin, refs, closed) if fin else None,
        )

    last_state: dict[str, StateView] = {k: a.initial for k, a in accounts.items()}
    last_idx_for: dict[str, int] = {}
    for t in turn_rows:
        last_idx_for[t.get("account_key") or "main"] = t["idx"]

    turns: list[NormTurn] = []
    for t in turn_rows:
        key = t.get("account_key") or "main"
        acc = accounts.get(key)
        refs = acc.refs if acc else {}
        resp = t.get("response") or {}
        state = None
        if t.get("state_after"):
            state = build_state_view(t["state_after"], refs, closed)
        elif acc and acc.final and last_idx_for.get(key) == t["idx"]:
            state = acc.final
        prev = last_state.get(key) or build_state_view({}, refs, closed)
        nt = NormTurn(
            idx=t["idx"],
            account_key=key,
            actor=t.get("actor") or "script",
            user_text=t.get("sent_text") or "",
            received_at=timeutil.to_ist(t["received_at"]),
            user_turn_id=resp.get("user_turn_id"),
            reply_turn_id=resp.get("reply_turn_id"),
            reply=resp.get("respond") or {},
            trace=build_trace(resp.get("trace")),
            state=state,
            prev_state=prev,
            diff=diff_states(prev, state) if state is not None else None,
            latency_ms=int(t.get("latency_ms") or resp.get("latency_ms") or 0),
            usage=t.get("usage") or resp.get("usage") or {},
            raw=resp,
            sim_meta=t.get("sim_meta"),
            other_states=t.get("aux"),
        )
        turns.append(nt)
        if state is not None:
            last_state[key] = state

    return NormRun(
        scenario=scenario,
        scenario_run_id=sr.get("id", ""),
        rep=int(sr.get("rep") or 1),
        status=sr.get("status") or "",
        accounts=accounts,
        turns=turns,
        anchor_date=anchor_date,
        time_shift_days=int(sr.get("time_shift_days") or 0),
        persona=persona,
    )
