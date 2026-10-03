"""Scenarios x k -> interleaved Job list (build.md 7.1)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from qeval.scenario.models import Scenario


@dataclass(frozen=True)
class Job:
    scenario: Scenario
    rep: int
    attempt: int = 1
    retries: int = 0  # invalid attempts already retried in this process

    @property
    def key(self) -> tuple[str, int]:
        return (self.scenario.id, self.rep)

    def label(self) -> str:
        return f"{self.scenario.id}#{self.rep}" + (f" (attempt {self.attempt})" if self.attempt > 1 else "")


def plan_jobs(
    scenarios: list[Scenario],
    k: int,
    *,
    done: set[tuple[str, str, int]] | None = None,
    k_override: int | None = None,
    next_attempt: Callable[[str, int], int] | None = None,
) -> list[Job]:
    """Rep 1 of every scenario, then rep 2, ... so repeats are spread over the run.

    On resume, `done` holds (scenario_id, scenario_hash, rep) of valid attempts, which are skipped. Every
    other rep (never run, invalid, interrupted, scenario_error, or run with an older version of the file)
    is run again as a new attempt numbered after the highest existing one, so earlier rows are kept.
    """
    done = done or set()
    per: dict[str, int] = {s.id: (k_override or s.k or k) for s in scenarios}
    jobs: list[Job] = []
    for rep in range(1, max(per.values(), default=0) + 1):
        for s in scenarios:
            if rep > per[s.id]:
                continue
            if (s.id, s.content_hash, rep) in done:
                continue
            jobs.append(Job(s, rep, next_attempt(s.id, rep) if next_attempt else 1))
    return jobs
