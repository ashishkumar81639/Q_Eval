"""Build only the inputs a rubric lists, as plain text (build.md 11.2)."""

from __future__ import annotations

import json

from qeval import timeutil
from qeval.normalise.models import NormRun, NormTurn, StateView

KNOWN_INPUTS = {
    "user_message",
    "reply_message",
    "reply_fields",
    "now_ist",
    "members",
    "member_facts",
    "family_history",
    "episodes",
    "previous_turns",
    "transcript",
    "reference",
    "fact_value",
    "user_messages_so_far",
    "persona",
    "flag_reason",
}

REPLY_FIELDS = (
    "kind",
    "subject_ids",
    "episode_id",
    "urgency",
    "escalate_to_doctor",
    "possible_causes",
    "red_flags",
    "missing_info",
    "options",
)


def members_text(sv: StateView) -> str:
    lines = []
    for m in sv.members.values():
        label = m.label or f"{m.name or '?'} ({m.relation})"
        lines.append(f"- {label} [id {m.id}]")
    return "\n".join(lines) or "(none)"


def _turn_text(t: NormTurn, sv: StateView | None) -> str:
    who = ", ".join(sv.member_name(s) for s in t.reply.get("subject_ids") or []) if sv else ""
    head = f"[{timeutil.fmt_ist(t.received_at)}]"
    reply = t.reply.get("message") or ""
    extra = f" (kind={t.reply.get('kind')}"
    if t.reply.get("urgency"):
        extra += f", urgency={t.reply.get('urgency')}"
    if who:
        extra += f", about={who}"
    extra += ")"
    return f"{head} User: {t.user_text}\nQ{extra}: {reply}"


def _facts_text(facts: list[dict], sv: StateView) -> str:
    lines = []
    for f in facts:
        if f.get("superseded"):
            continue
        lines.append(
            f"- {sv.member_name(f.get('subject_id'))} / {f.get('category')} / {f.get('key')}: {f.get('value')}"
        )
    return "\n".join(lines) or "(none)"


def build_inputs(
    names: list[str], run: NormRun, turn: NormTurn | None, extra: dict | None = None
) -> dict[str, str]:
    extra = extra or {}
    turn = turn or (run.turns[-1] if run.turns else None)
    sv = None
    if turn is not None:
        sv = turn.state or turn.prev_state
    out: dict[str, str] = {}
    same_acc = [t for t in run.turns if turn is None or t.account_key == turn.account_key]
    upto = [t for t in same_acc if turn is None or t.idx <= turn.idx]
    for n in names:
        if n == "user_message":
            out[n] = turn.user_text if turn else ""
        elif n == "reply_message":
            out[n] = (turn.reply.get("message") or "") if turn else ""
        elif n == "reply_fields":
            fields: dict = {k: turn.reply.get(k) for k in REPLY_FIELDS} if turn else {}
            if sv and fields.get("subject_ids"):
                fields["subject_names"] = [sv.member_name(s) for s in fields["subject_ids"] or []]
            out[n] = json.dumps(fields, ensure_ascii=False, indent=1)
        elif n == "now_ist":
            out[n] = timeutil.fmt_ist(turn.received_at) if turn else ""
        elif n == "members":
            out[n] = members_text(sv) if sv else "(none)"
        elif n == "member_facts":
            ids = set(turn.reply.get("subject_ids") or []) if turn else set()
            facts = [f for f in (sv.facts if sv else []) if f.get("subject_id") in ids]
            out[n] = _facts_text(facts, sv) if sv else "(none)"
        elif n == "family_history":
            facts = [f for f in (sv.facts if sv else []) if f.get("category") == "family_history"]
            out[n] = _facts_text(facts, sv) if sv else "(none)"
        elif n == "episodes":
            ids = set(turn.reply.get("subject_ids") or []) if turn else set()
            src = turn.prev_state if turn else sv
            if src is None:
                out[n] = "(none)"
                continue
            lines = []
            for e in src.episodes.values() if src else []:
                if ids and e.get("subject_id") not in ids:
                    continue
                last = e.get("last_turn_at")
                lines.append(
                    f"- {src.member_name(e.get('subject_id'))}: {e.get('complaint')} [{e.get('status')}] opened "
                    f"{timeutil.fmt_ist(timeutil.to_ist(e['opened_at'])) if e.get('opened_at') else '?'}, last activity "
                    f"{timeutil.fmt_ist(timeutil.to_ist(last)) if last else '?'}"
                )
            out[n] = "\n".join(lines) or "(none)"
        elif n == "previous_turns":
            prev = [t for t in upto if turn is None or t.idx < turn.idx][-4:]
            out[n] = (
                "\n\n".join(_turn_text(t, t.state or t.prev_state) for t in prev)
                or "(this is the first turn)"
            )
        elif n == "transcript":
            out[n] = "\n\n".join(_turn_text(t, t.state or t.prev_state) for t in same_acc)
        elif n == "user_messages_so_far":
            out[n] = "\n".join(f"- [{timeutil.fmt_ist(t.received_at)}] {t.user_text}" for t in upto)
        elif n == "reference":
            ref = dict(getattr(run.scenario, "reference", {}) or {})
            if run.persona is not None:
                ref = {**(run.persona.reference or {}), **ref}
            out[n] = json.dumps(ref, ensure_ascii=False) if ref else "(none)"
        elif n == "persona":
            p = run.persona
            if p is None:
                out[n] = "(none)"
            else:
                hidden = "\n".join(f"- {k}: {v.value}" for k, v in p.hidden_facts.items())
                lr = (
                    f"\nLate reveal (scripted, not the simulator): {p.late_reveal.text}"
                    if p.late_reveal
                    else ""
                )
                out[n] = (
                    f"Style: {p.style}\nOpening: {p.opening}\nHidden facts:\n{hidden}\nUnknown answer: {p.unknown_answer}{lr}"
                )
        elif n in ("fact_value", "flag_reason"):
            out[n] = str(extra.get(n, ""))
        else:
            raise KeyError(f"unknown rubric input {n!r}")
    return out
