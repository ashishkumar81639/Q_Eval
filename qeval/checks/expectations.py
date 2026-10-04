"""Run a scenario's `expect` and `final_expect` lines (build.md 10.3)."""

from __future__ import annotations

from dataclasses import dataclass

from qeval.checks.expr.evaluator import ExprRuntimeError, evaluate
from qeval.checks.expr.functions import names_for
from qeval.checks.expr.parser import split_comment
from qeval.checks.registry import rule_area, rule_severity
from qeval.normalise.models import NormRun, NormTurn


@dataclass
class CheckRow:
    check_id: str
    rule: str
    area: str
    severity: str
    kind: str  # global | expect | judge | persona | sim
    verdict: str  # pass | fail | n/a | flag | unclear | error | info
    turn_idx: int | None = None
    evidence: dict | None = None
    message: str = ""
    scenario_run_id: str = ""
    scored: bool = True  # False = advisory (shown, never counted)

    def as_dict(self) -> dict:
        return {
            "scenario_run_id": self.scenario_run_id,
            "turn_idx": self.turn_idx,
            "check_id": self.check_id,
            "rule": self.rule,
            "area": self.area,
            "severity": self.severity,
            "kind": self.kind,
            "verdict": self.verdict,
            "evidence": self.evidence or {},
            "message": self.message,
            "scored": self.scored,
        }


def eval_one(src: str, run: NormRun, turn: NormTurn, *, final: bool = False) -> tuple[str, dict, str]:
    """-> (verdict, evidence, message)."""
    body, _ = split_comment(src)
    try:
        names = names_for(run, turn, final=final)
        value, comps = evaluate(src, names)
    except ExprRuntimeError as e:
        return "error", {"expr": body}, f"expression error: {e}"
    except Exception as e:  # harness bug; report, never count against the agent
        return "error", {"expr": body}, f"{type(e).__name__}: {e}"
    ok = bool(len(value) > 0) if isinstance(value, list) else bool(value)
    ev: dict = {"expr": body, "comparisons": comps}
    if not comps:
        ev["value"] = value if isinstance(value, (bool, int, float, str, type(None))) else repr(value)[:300]
    return ("pass" if ok else "fail"), ev, ""


def _row(sc, run: NormRun, src: str, check_id: str, turn_idx: int | None, verdict, ev, msg) -> CheckRow:
    _, rule = split_comment(src)
    rule = rule or sc.rules[0]
    return CheckRow(
        check_id=check_id,
        rule=rule,
        area=rule_area(rule),
        severity=rule_severity(rule),
        kind="expect",
        verdict=verdict,
        turn_idx=turn_idx,
        evidence=ev,
        message=msg,
        scenario_run_id=run.scenario_run_id,
    )


def run_expectations(run: NormRun) -> list[CheckRow]:
    sc = run.scenario
    rows: list[CheckRow] = []
    if sc.kind == "persona":
        if run.turns and sc.final_expect:
            last = run.turns[-1]
            for n, src in enumerate(sc.final_expect):
                v, ev, msg = eval_one(src, run, last, final=True)
                rows.append(_row(sc, run, src, f"{sc.id}#final#{n}", None, v, ev, msg))
        return rows
    for i, turn_def in enumerate(sc.turns):
        nt = next((t for t in run.turns if t.idx == i), None)
        for n, src in enumerate(turn_def.expect):
            cid = f"{sc.id}#t{i}#{n}"
            if nt is None:
                rows.append(_row(sc, run, src, cid, i, "error", {"expr": src}, "turn missing from saved run"))
                continue
            v, ev, msg = eval_one(src, run, nt)
            rows.append(_row(sc, run, src, cid, i, v, ev, msg))
    if sc.final_expect and run.turns:
        last = run.turns[-1]
        for n, src in enumerate(sc.final_expect):
            v, ev, msg = eval_one(src, run, last, final=True)
            rows.append(_row(sc, run, src, f"{sc.id}#final#{n}", None, v, ev, msg))
    return rows
