"""@check decorator, Verdict and CheckSpec (build.md 10.1)."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

from qeval.normalise.models import NormRun, NormTurn

Status = Literal["pass", "fail", "n/a", "flag", "error", "info"]
AREA_BY_PREFIX = {
    "R": "reply", "S": "safety", "W": "who", "T": "triage", "M": "memory",
    "TM": "time", "E": "episodes", "G": "general", "I": "isolation",
}  # fmt: skip
SEVERITY_BY_AREA = {
    "safety": "critical", "who": "major", "triage": "major", "memory": "major", "time": "major",
    "episodes": "major", "isolation": "major", "general": "minor", "reply": "major",
}  # fmt: skip


@lru_cache
def _rules() -> dict:
    from qeval.scenario.loader import load_rules

    try:
        return load_rules()
    except Exception:
        return {}


def rule_area(rule: str) -> str:
    info = _rules().get(rule)
    if info is not None:
        return info.area
    prefix = "".join(c for c in rule if c.isalpha())
    return AREA_BY_PREFIX.get(prefix, "other")


def rule_severity(rule: str) -> str:
    info = _rules().get(rule)
    if info is not None:
        return info.severity
    return SEVERITY_BY_AREA.get(rule_area(rule), "major")


@dataclass
class Verdict:
    status: Status
    evidence: dict = field(default_factory=dict)
    message: str = ""
    # Optional: items that need the confirm_with rubric (e.g. flagged facts for M2).
    flagged_items: list[dict] | None = None


@dataclass
class CheckSpec:
    id: str
    rule: str
    severity: str
    applies_to: set[str] | None
    confirm_with: str | None
    fn: Callable[[NormTurn, NormRun], Verdict]
    scored: bool = True


REGISTRY: dict[str, CheckSpec] = {}


def check(
    *, id: str, rule: str, severity: str | None = None, applies_to=None, confirm_with=None, scored=True
):
    def deco(fn):
        REGISTRY[id] = CheckSpec(
            id,
            rule,
            severity or rule_severity(rule),
            set(applies_to) if applies_to else None,
            confirm_with,
            fn,
            scored,
        )
        return fn

    return deco


def run_check(spec: CheckSpec, turn: NormTurn, run: NormRun) -> Verdict:
    if spec.applies_to is not None and turn.reply.get("kind") not in spec.applies_to:
        return Verdict("n/a", message=f"applies to {sorted(spec.applies_to)}")
    try:
        return spec.fn(turn, run)
    except Exception as e:
        tb = traceback.format_exc(limit=3)
        return Verdict("error", {"traceback": tb[-1500:]}, f"{type(e).__name__}: {e}")


def load_checks() -> dict[str, CheckSpec]:
    from qeval.checks import global_checks  # noqa: F401  (registers checks)

    return REGISTRY
