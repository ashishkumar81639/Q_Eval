"""Which spec rules a scenario actually measures, and how (used by `validate` and the CI gate)."""

from __future__ import annotations

from qeval.checks.expr.parser import split_comment
from qeval.scenario.models import Persona, Rubric, Scenario

# Persona metrics (qeval/grading/persona_metrics.py) and the rules they score.
PERSONA_METRIC_RULES = {"T1", "T4", "T6", "T7", "T8", "S8"}


def contributions(
    sc: Scenario, persona: Persona | None, rubrics: dict[str, Rubric], global_rules: set[str]
) -> dict[str, set[str]]:
    """rule -> set of sources: 'expect', 'judge', 'global', 'emergency', 'persona', 'auto-judge'.

    'global' and 'auto-judge' apply to every scenario, so they count as measuring a rule a scenario names
    (e.g. a scenario naming TM1 is measured by the TM1 global check on every turn).
    """
    out: dict[str, set[str]] = {}

    def add(rule: str, src: str) -> None:
        out.setdefault(rule, set()).add(src)

    for t in sc.turns:
        for e in t.expect:
            add(split_comment(e)[1] or sc.rules[0], "expect")
        for rid in t.judge:
            if rid in rubrics:
                add(rubrics[rid].rule, "judge")
        if t.emergency:
            add("S2", "emergency")
            add("S1", "auto-judge")
    for e in sc.final_expect:
        add(split_comment(e)[1] or sc.rules[0], "expect")
    for rid in sc.final_judge:
        if rid in rubrics:
            add(rubrics[rid].rule, "judge")
    if persona is not None:
        for r in PERSONA_METRIC_RULES:
            add(r, "persona")
        if persona.late_reveal and persona.late_reveal.emergency:
            add("S2", "emergency")
            add("S1", "auto-judge")
    for r in global_rules:
        add(r, "global")
    for rb in rubrics.values():
        if rb.auto_when:
            add(rb.rule, "auto-judge")
    return out


def unmeasured_rules(
    sc: Scenario,
    persona: Persona | None,
    rubrics: dict[str, Rubric],
    global_rules: set[str],
    *,
    judge: bool = True,
) -> list[str]:
    contrib = contributions(sc, persona, rubrics, global_rules)
    named = list(sc.rules) + (list(persona.rules) if persona else [])
    bad = []
    for r in dict.fromkeys(named):
        srcs = contrib.get(r, set())
        if not judge:
            srcs = srcs - {"judge", "auto-judge"}
        if not srcs:
            bad.append(r)
    return bad
