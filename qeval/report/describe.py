"""Turn any check result into short, readable lines for reports.

Every text comes from data: rule titles and expected behaviour from `rules.yaml`, check titles and failure
templates from the `@check` registry, rubric titles from `rubrics/*.yaml`, expectation notes from scenario
comments (`# W4: should ask which Meera`). Adding a rule, check, rubric or scenario needs no change here.
"""

from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass

from qeval.checks.expr.parser import comment_note, split_comment
from qeval.checks.registry import REGISTRY, fill_template

# Persona (whole-conversation) metrics from qeval/grading/persona_metrics.py: title, failure template.
PERSONA_CHECKS: dict[str, tuple[str, str]] = {
    "T6_rounds": (
        "Assesses within the expected number of question rounds",
        "{followups} rounds of questions (limit {max_rounds}); {message}",
    ),
    "T4_must_ask": ("Asks about the decisive items", "Never asked about: {missed}"),
    "T1_no_known_asked": ("Doesn't ask for facts already on file", "Asked again about: {asked}"),
    "T7_assess_after_red_flag": (
        "Assesses as soon as a red flag appears",
        "Replied with {kind} after the red flag",
    ),
    "T8_urgency_in_reference": (
        "Urgency matches the expected level",
        "Gave {urgency}; expected one of {reference}",
    ),
    "S8_urgency_held": ("Keeps the urgency after pushback", "Urgency dropped on turn(s) {dropped_on_turns}"),
}
SIM_CHECKS = {"SIM_leak": "Simulated user didn't leak hidden facts"}


@dataclass
class Described:
    check_title: str  # what was checked, one line
    got: str  # what happened, one line
    detail_expr: str = ""  # the raw expression or check id, for the technical section


def _snippet(text: str, start: int, end: int, pad: int = 40) -> str:
    a, b = max(0, start - pad), min(len(text), end + pad)
    return ("…" if a else "") + " ".join(text[a:b].split()) + ("…" if b < len(text) else "")


def _name(v, names: dict[str, str] | None) -> str:
    """Member/episode ids -> names where known."""
    if names and isinstance(v, str) and v in names:
        return names[v]
    if names and isinstance(v, list):
        return "[" + ", ".join(names.get(x, str(x)) if isinstance(x, str) else str(x) for x in v) + "]"
    return _q(v)


def _q(v) -> str:
    if v is None:
        return "nothing"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_q(x) for x in list(v)[:4]) + ("…" if len(v) > 4 else "") + "]"
    if isinstance(v, dict):
        if v.get("content"):
            return f"“{_snippet(str(v['content']), 0, 0, 120)}”"
        if v.get("value") is not None and v.get("key"):
            return f"{v['key']} = “{v['value']}”"
        return str(v.get("value") or v.get("name") or v.get("complaint") or v.get("id") or v)
    s = str(v)
    return s if len(s) <= 140 else s[:140] + "…"


_HUMAN = [
    (r"^reply\.kind == '(\w+)'$", r"reply is a \1"),
    (r"^reply\.kind != '(\w+)'$", r"reply is not a \1"),
    (r"^reply\.kind in \[(.+)\]$", r"reply is one of \1"),
    (r"^reply\.urgency == '(\w+)'$", r"urgency is \1"),
    (r"^reply\.urgency != '(\w+)'$", r"urgency is not \1"),
    (r"^reply\.escalate_to_doctor == true$", "escalate_to_doctor is true"),
    (r"^reply\.subject_ids(?:\[0\])? == \[?ref\('(\w+)'\)\]?$", r"reply is about \1"),
    (r"^reply\.subject_ids == \[\]$", "reply tags no family member"),
    (r"^reply\.episode_id == turn\[(\d+)\]\.reply\.episode_id$", r"same illness episode as turn \1"),
    (r"^reply\.episode_id != turn\[(\d+)\]\.reply\.episode_id$", r"a different episode from turn \1"),
    (r"^reply\.episode_id != null$", "reply is linked to an illness episode"),
    (r"^diff\.new_subjects\.count == 0$", "no new family member added"),
    (r"^diff\.new_subjects\.count == 1$", "exactly one new family member added"),
    (r"^diff\.new_facts\.count == 0$", "nothing saved"),
    (r"^reply\.message !~ ", "reply doesn't contain the pattern "),
    (r"^trace\.called\('(\w+)'\)$", r"agent called \1"),
]


def humanize_expr(expr: str) -> str:
    e = expr.strip()
    for pat, rep in _HUMAN:
        if re.search(pat, e):
            return re.sub(pat, rep, e)
    return e


def _expect_source(row: dict, snapshot: dict | None) -> str | None:
    """The original expectation line (with its comment) for a scenario expect row."""
    if not snapshot:
        return None
    sc = snapshot.get("scenario") or {}
    m = re.search(r"#t(\d+)#(\d+)$", row["check_id"])
    if m:
        turns = sc.get("turns") or []
        i, n = int(m.group(1)), int(m.group(2))
        if i < len(turns) and n < len(turns[i].get("expect") or []):
            return turns[i]["expect"][n]
    m = re.search(r"#final#(\d+)$", row["check_id"])
    if m:
        fe = sc.get("final_expect") or []
        n = int(m.group(1))
        if n < len(fe):
            return fe[n]
    return None


def _expect_got(ev: dict, names: dict[str, str] | None = None) -> str:
    comps = ev.get("comparisons") or []
    if not comps:
        return f"result was {_q(ev.get('value'))}"
    c = comps[0]
    if "left" in c:
        left = c["left"]
        if c.get("op") in ("~", "!~") and isinstance(left, str):
            m = None
            with contextlib.suppress(re.error):
                m = re.search(str(c.get("right")), left, re.IGNORECASE)
            if m:
                return f"found “{_snippet(left, m.start(), m.end())}”"
            return "the pattern was not found"
        return f"got {_name(left, names)}"
    if "matched" in c:
        matched = c.get("matched") or []
        items = c.get("items") or []
        if matched:
            return f"found {_q(matched[0])}" + (f" (+{len(matched) - 1} more)" if len(matched) > 1 else "")
        return f"none of {len(items)} item(s) matched" if items else "nothing to match"
    return f"result was {_q(c.get('result'))}"


def _current_note(current: dict | None, scenario_id: str | None, expr: str) -> str | None:
    """Notes are descriptions only, so a run graded from an older snapshot may borrow the note of the
    identical expression in the current scenario file."""
    sc = (current or {}).get(scenario_id or "")
    if sc is None:
        return None
    for line in [e for t in sc.turns for e in t.expect] + list(sc.final_expect):
        if split_comment(line)[0] == expr:
            return comment_note(line)
    return None


def describe(
    row: dict,
    rubrics: dict | None = None,
    snapshot: dict | None = None,
    names: dict[str, str] | None = None,
    current: dict | None = None,
) -> Described:
    """`names` maps member/episode ids to readable names; `current` maps scenario id -> current Scenario."""
    kind, cid, ev, msg = row["kind"], row["check_id"], row.get("evidence") or {}, row.get("message") or ""
    base = cid.split("#")[0]
    if kind == "global" and base in REGISTRY:
        spec = REGISTRY[base]
        return Described(spec.title or base, spec.describe_failure(ev, msg), cid)
    if kind in ("judge", "sim") and rubrics and base in rubrics:
        r = rubrics[base]
        got = msg or ""
        quote = ev.get("quote")
        if quote:
            got = f"{got} — “{quote[:160]}”" if got else f"“{quote[:160]}”"
        if ev.get("fact"):
            got = f"fact {ev['fact']!r}: {got}"
        return Described(r.title or base, got or row["verdict"], cid)
    if kind == "persona" and base in PERSONA_CHECKS:
        title, tpl = PERSONA_CHECKS[base]
        return Described(title, fill_template(tpl, ev, msg), cid)
    if kind == "sim":
        return Described(SIM_CHECKS.get(base, base), msg or row["verdict"], cid)
    if kind == "expect":
        src = _expect_source(row, snapshot)
        expr = split_comment(src)[0] if src else ev.get("expr", "")
        note = comment_note(src) if src else None
        if not note:
            sid = ((snapshot or {}).get("scenario") or {}).get("id")
            note = _current_note(current, sid, expr)
        title = note or humanize_expr(expr)
        if row["verdict"] == "error":
            return Described(title, f"could not evaluate: {msg}", expr)
        return Described(title, _expect_got(ev, names), expr)
    return Described(base, msg or row["verdict"], cid)
