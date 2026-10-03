"""Decide which rubrics run on which turns, then run them (build.md 11.1)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from qeval.checks.expectations import CheckRow, eval_one
from qeval.checks.registry import REGISTRY, rule_area, rule_severity
from qeval.judge.client import Judge
from qeval.judge.inputs import build_inputs
from qeval.normalise.models import NormRun
from qeval.scenario.models import Rubric

PERSONA_RUBRICS = [
    "T4_relevant_questions",
    "T6_not_interrogating",
    "T8_urgency_fits",
    "R9_conversation_tone",
    "SIM_fidelity",
]


@dataclass
class JudgeTask:
    rubric: Rubric
    turn_idx: int | None  # None = conversation level
    trigger: str  # listed | auto | confirm | persona
    extra: dict = field(default_factory=dict)
    suffix: str = ""

    @property
    def key(self) -> tuple:
        return (self.rubric.id, self.turn_idx, self.suffix)


# Per-turn rubrics that would double-score persona conversations (they have a conversation-level twin).
PERSONA_SKIP_AUTO = {"R9_tone"}


def plan_tasks(
    run: NormRun, det_rows: list[CheckRow], rubrics: dict[str, Rubric], *, only_sim: bool = False
) -> list[JudgeTask]:
    tasks: dict[tuple, JudgeTask] = {}

    def add(t: JudgeTask) -> None:
        if only_sim and not t.rubric.id.startswith("SIM_"):
            return
        tasks.setdefault(t.key, t)

    sc = run.scenario
    persona = run.persona
    # 1. listed
    if sc.kind == "scripted":
        for i, tdef in enumerate(sc.turns):
            for rid in tdef.judge:
                if rid in rubrics and any(t.idx == i for t in run.turns):
                    add(JudgeTask(rubrics[rid], i, "listed"))
    for rid in sc.final_judge:
        if rid in rubrics:
            add(JudgeTask(rubrics[rid], None, "listed"))
    # 2. automatic (auto_when is evaluated on the user's message and the turn, see functions.names_for)
    for r in rubrics.values():
        if not r.auto_when or r.scope != "turn":
            continue
        if persona is not None and r.id in PERSONA_SKIP_AUTO:
            continue
        for t in run.turns:
            v, _, _ = eval_one(r.auto_when, run, t)
            if v == "pass":
                add(JudgeTask(r, t.idx, "auto"))
    # 3. confirmations of deterministic flags
    for row in det_rows:
        if row.verdict != "flag":
            continue
        spec = REGISTRY.get(row.check_id)
        if spec is None or not spec.confirm_with or spec.confirm_with not in rubrics:
            continue
        r = rubrics[spec.confirm_with]
        if r.scope == "fact":
            for item in (row.evidence or {}).get("facts", []):
                add(
                    JudgeTask(
                        r,
                        row.turn_idx,
                        "confirm",
                        {
                            "fact_value": f"{item.get('key')}: {item.get('value')}",
                            "flag_reason": f"words not in the user's messages: {item.get('new_words')}",
                        },
                        suffix=f"fact:{item.get('id')}",
                    )
                )
        else:
            add(JudgeTask(r, row.turn_idx, "confirm", {"flag_reason": row.message}))
    # 4. persona runs: conversation-level rubrics, plus S8 on every scripted pushback line
    if persona is not None:
        for rid in PERSONA_RUBRICS:
            if rid in rubrics:
                add(JudgeTask(rubrics[rid], None, "persona"))
        pushback = set(persona.after_assessment)
        if pushback and "S8_holds_under_pushback" in rubrics:
            for t in run.turns:
                if t.user_text in pushback:
                    add(JudgeTask(rubrics["S8_holds_under_pushback"], t.idx, "persona"))
    return list(tasks.values())


async def run_judge(
    run: NormRun,
    det_rows: list[CheckRow],
    rubrics: dict[str, Rubric],
    judge: Judge,
    *,
    only_sim: bool = False,
) -> list[CheckRow]:
    tasks = plan_tasks(run, det_rows, rubrics, only_sim=only_sim)

    async def one(task: JudgeTask) -> CheckRow:
        turn = (
            next((t for t in run.turns if t.idx == task.turn_idx), None)
            if task.turn_idx is not None
            else None
        )
        try:
            inputs = build_inputs(task.rubric.inputs, run, turn, task.extra)
        except Exception as e:
            return _row(run, task, "error", {"error": f"inputs: {e}"}, f"inputs: {e}")
        res = await judge.judge(task.rubric, inputs)
        ev = {"trigger": task.trigger, **res.as_evidence()}
        if task.extra.get("fact_value"):
            ev["fact"] = task.extra["fact_value"]
        msg = res.reason if res.verdict != "error" else res.error
        return _row(run, task, res.verdict, ev, msg)

    return list(await asyncio.gather(*(one(t) for t in tasks)))


def _row(run: NormRun, task: JudgeTask, verdict: str, ev: dict, msg: str) -> CheckRow:
    r = task.rubric
    cid = r.id + (f"#{task.suffix}" if task.suffix else "")
    return CheckRow(
        check_id=cid,
        rule=r.rule,
        area=rule_area(r.rule),
        severity=r.severity or rule_severity(r.rule),
        kind="sim" if r.id.startswith("SIM_") else "judge",
        verdict=verdict,
        turn_idx=task.turn_idx,
        evidence=ev,
        message=msg,
        scenario_run_id=run.scenario_run_id,
    )
