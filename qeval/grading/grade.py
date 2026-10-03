"""Grade a saved run: normalise -> global checks -> expectations -> persona metrics -> judge (build.md 12).

Grading never calls the Q API.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from qeval.analysis.attempts import authoritative
from qeval.checks.expectations import CheckRow, run_expectations
from qeval.checks.registry import REGISTRY, load_checks, run_check
from qeval.config import ROOT, Settings
from qeval.grading.persona_metrics import persona_metrics
from qeval.normalise.build import build_norm_run
from qeval.normalise.models import NormRun
from qeval.scenario.loader import (
    LoadError,
    from_snapshot,
    load_personas,
    load_rubrics,
    load_rules,
    load_scenario,
)
from qeval.scenario.models import Scenario
from qeval.simuser.leak import detect_leaks
from qeval.store.repo import Repo

log = logging.getLogger(__name__)


def checks_version() -> str:
    h = hashlib.sha256()
    for p in sorted((ROOT / "qeval" / "checks").rglob("*")):
        if p.is_file() and p.suffix in (".py", ".lark", ".txt"):
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


def run_global_checks(run: NormRun, only: set[str] | None = None) -> list[CheckRow]:
    rows = []
    for t in run.turns:
        for cid, spec in REGISTRY.items():
            if only and cid not in only:
                continue
            v = run_check(spec, t, run)
            if v.status == "n/a" and not spec.scored:
                continue
            rows.append(
                CheckRow(
                    check_id=cid,
                    rule=spec.rule,
                    area=_area(spec.rule),
                    severity=spec.severity,
                    kind="global",
                    verdict=v.status,
                    turn_idx=t.idx,
                    evidence=v.evidence,
                    message=v.message,
                    scenario_run_id=run.scenario_run_id,
                )
            )
    return rows


def _area(rule: str) -> str:
    from qeval.checks.registry import rule_area

    return rule_area(rule)


def load_scenario_for(sr: dict, cache: dict[str, Scenario]) -> Scenario | None:
    path = sr.get("scenario_path")
    if not path:
        return None
    if path in cache:
        return cache[path]
    p = Path(path)
    if not p.is_absolute():
        p = ROOT / p
    try:
        sc = load_scenario(p)
    except (LoadError, FileNotFoundError) as e:
        log.warning("cannot load scenario %s: %s", path, e)
        return None
    cache[path] = sc
    return sc


class ScenarioResolveError(Exception):
    pass


def resolve_scenario(sr: dict, cache: dict[str, Scenario], personas: dict, *, allow_changed: bool = False):
    """The scenario (and persona) exactly as the job ran.

    Uses the snapshot saved with the run. Older runs without a snapshot fall back to the file on disk only
    if its hash still matches (or `allow_changed`), so editing a scenario can never silently change the
    grade of agent output that was produced under the old version.
    """
    snap = sr.get("scenario_snapshot")
    if snap:
        return from_snapshot(snap)
    sc = load_scenario_for(sr, cache)
    if sc is None:
        raise ScenarioResolveError("no snapshot and scenario file missing")
    if sc.content_hash != sr.get("scenario_hash") and not allow_changed:
        raise ScenarioResolveError(
            f"no snapshot and {sc.path} changed since the run (hash {sr.get('scenario_hash')} -> "
            f"{sc.content_hash}); re-run it, or grade with --allow-changed-scenarios"
        )
    return sc, (personas.get(sc.persona) if sc.persona else None)


def norm_runs_for(
    repo: Repo, run_id: str, *, allow_changed: bool = False
) -> list[tuple[dict, NormRun | None, str]]:
    """Authoritative valid attempts of a run -> [(scenario_run row, NormRun or None, problem)]."""
    run_row = repo.get_run(run_id)
    if run_row is None:
        raise SystemExit(f"unknown run {run_id}")
    personas = load_personas()
    cache: dict[str, Scenario] = {}
    turns_by_sr = repo.turns_for_run(run_id)
    out: list[tuple[dict, NormRun | None, str]] = []
    for sr in authoritative(repo.scenario_runs(run_id)):
        try:
            sc, persona = resolve_scenario(sr, cache, personas, allow_changed=allow_changed)
        except ScenarioResolveError as e:
            out.append((sr, None, str(e)))
            continue
        nr = build_norm_run(sc, sr, turns_by_sr.get(sr["id"], []), run_row["anchor_date"], persona=persona)
        out.append((sr, nr, ""))
    return out


def resolve_flags(rows: list[CheckRow]) -> list[CheckRow]:
    """A `flag` only becomes evidence once its confirming rubric gives pass or fail.

    Any flag whose confirmation is missing, errored, unclear or skipped (no key, budget, --no-judge) becomes
    `unresolved`: it is counted separately in every denominator and breaks the CI gate for critical rules,
    so a candidate safety violation can never silently disappear from the score.
    """
    judged: dict[tuple, list[str]] = {}
    for r in rows:
        if r.kind == "judge":
            base = r.check_id.split("#")[0]
            judged.setdefault((r.scenario_run_id, r.turn_idx, base), []).append(r.verdict)
    out = []
    for r in rows:
        if r.verdict == "flag":
            spec = REGISTRY.get(r.check_id)
            confirm = spec.confirm_with if spec else None
            verdicts = judged.get((r.scenario_run_id, r.turn_idx, confirm), []) if confirm else []
            if not verdicts or any(v not in ("pass", "fail") for v in verdicts):
                why = (
                    "no confirming judge verdict"
                    if not verdicts
                    else f"judge verdicts {sorted(set(verdicts))}"
                )
                r = CheckRow(
                    r.check_id,
                    r.rule,
                    r.area,
                    r.severity,
                    r.kind,
                    "unresolved",
                    r.turn_idx,
                    {**(r.evidence or {}), "unresolved_because": why},
                    r.message,
                    r.scenario_run_id,
                )
        out.append(r)
    return out


async def grade(
    repo: Repo,
    run_id: str,
    settings: Settings,
    *,
    judge: bool = True,
    only_checks: set[str] | None = None,
    allow_changed: bool = False,
) -> str:
    load_checks()
    rules = load_rules()
    rubrics = load_rubrics()
    judge_client = None
    if judge:
        from qeval.judge.client import Judge

        key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        if not key:
            log.warning("ANTHROPIC_API_KEY not set: judge rows will be 'error' unless cached")
        judge_client = Judge(
            repo,
            api_key=key,
            model=settings.judge_model,
            effort=settings.judge_effort,
            rules=rules,
            run_id=run_id,
            max_tokens_budget=settings.max_judge_tokens,
        )
    gid = repo.create_grading(
        run_id,
        checks_version(),
        {r.id: r.version for r in rubrics.values()},
        settings.judge_model if judge else None,
        {
            "judge": judge,
            "only_checks": sorted(only_checks) if only_checks else None,
            "allow_changed_scenarios": allow_changed,
        },
    )
    items = norm_runs_for(repo, run_id, allow_changed=allow_changed)
    sem = asyncio.Semaphore(4)

    async def grade_one(sr: dict, nr: NormRun | None, problem: str) -> None:
        if nr is None:
            repo.add_check_results(
                gid,
                [
                    CheckRow(
                        "HARNESS_scenario_unavailable",
                        "R1",
                        "reply",
                        "major",
                        "harness",
                        "error",
                        None,
                        {"path": sr.get("scenario_path"), "scenario": sr.get("scenario_id")},
                        problem,
                        sr["id"],
                    ).as_dict()
                ],
            )
            return
        from qeval.judge.dispatch import run_judge

        rows: list[CheckRow] = []
        if nr.persona is not None:
            leaks = detect_leaks(nr)
            if leaks or (sr.get("flags") or {}).get("sim_unreliable"):
                # Exclude the agent from scoring, but keep simulator-only diagnostics (fidelity rubric).
                rows.append(
                    CheckRow(
                        "SIM_leak",
                        "T4",
                        "triage",
                        "major",
                        "sim",
                        "fail",
                        None,
                        {"leaks": leaks},
                        "simulated user leaked hidden facts; run excluded from agent scoring",
                        sr["id"],
                    )
                )
                if judge_client is not None:
                    async with sem:
                        rows += await run_judge(nr, rows, rubrics, judge_client, only_sim=True)
                repo.add_check_results(gid, [r.as_dict() for r in rows])
                return
            rows.append(CheckRow("SIM_leak", "T4", "triage", "major", "sim", "pass", None, {}, "", sr["id"]))
        rows += run_global_checks(nr, only_checks)
        if not only_checks:
            rows += run_expectations(nr)
            rows += persona_metrics(nr)
        if judge_client is not None and not only_checks:
            async with sem:
                rows += await run_judge(nr, rows, rubrics, judge_client)
        rows = resolve_flags(rows)
        # Persist per scenario run, so an interrupted grading keeps everything finished so far.
        repo.add_check_results(gid, [r.as_dict() for r in rows])

    try:
        await asyncio.gather(*(grade_one(sr, nr, p) for sr, nr, p in items))
    except BaseException:
        repo.finish_grading(gid, "failed")
        raise
    repo.finish_grading(gid, "done")
    if judge_client is not None:
        log.info(
            "judge calls=%d cache_hits=%d tokens=%d",
            judge_client.calls,
            judge_client.cache_hits,
            judge_client.tokens_used,
        )
    return gid
