"""qeval command line (build.md 15)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from qeval import timeutil
from qeval.config import ROOT, Settings, get_settings
from qeval.logging_setup import setup_logging

app = typer.Typer(
    add_completion=False, no_args_is_help=True, help="Eval harness for the Q household health agent."
)
console = Console()

PathsArg = Annotated[
    list[Path] | None, typer.Argument(help="Scenario files or directories (default: scenarios/)")
]


def _repo(settings: Settings):
    from qeval.store.backend import make_engine
    from qeval.store.repo import Repo

    return Repo(make_engine(settings.database_url, settings.runs_dir))


def _harness_sha() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip()
        return sha + ("-dirty" if dirty else "")
    except Exception:
        return "nogit"


def _catalog(paths: list[Path] | None, only: list[str] | None = None, tags: list[str] | None = None):
    from qeval.scenario.validator import build_catalog

    cat = build_catalog(paths or [ROOT / "scenarios"])
    for w in cat.warnings:
        console.print(f"[yellow]warning[/] {w}")
    if cat.errors:
        for e in cat.errors:
            console.print(f"[red]error[/] {e}")
        console.print(f"[red]{len(cat.errors)} validation error(s); nothing was sent to the agent.[/]")
        raise typer.Exit(2)
    scen = cat.scenarios
    if only:
        wanted = set(only)
        scen = [s for s in scen if s.id in wanted]
        missing = wanted - {s.id for s in scen}
        if missing:
            console.print(f"[red]unknown scenario id(s): {sorted(missing)}[/]")
            raise typer.Exit(2)
    if tags:
        scen = [s for s in scen if set(tags) & set(s.tags)]
    cat.scenarios = scen
    return cat


@app.command()
def validate(paths: PathsArg = None) -> None:
    """Load and validate scenarios, rubrics, personas and rules.yaml; show rules without scenarios."""
    cat = _catalog(paths)
    covered: dict[str, list[str]] = {}
    for s in cat.scenarios:
        for r in s.rules:
            covered.setdefault(r, []).append(s.id)
        if s.persona and s.persona in cat.personas:
            for r in cat.personas[s.persona].rules:
                covered.setdefault(r, []).append(s.id)
    uncovered = [r for r in cat.rules if r not in covered]
    console.print(
        f"[green]OK[/]: {len(cat.scenarios)} scenarios, {len(cat.personas)} personas, "
        f"{len(cat.rubrics)} rubrics, {len(cat.rules)} rules."
    )
    if uncovered:
        console.print(
            f"Rules not named by any scenario (may still be covered by global checks): {', '.join(uncovered)}"
        )


@app.command()
def version() -> None:
    """Print the agent's /version."""
    from qeval.client import QClient

    s = get_settings()

    async def go():
        async with QClient(s.base_url, s.require_token()) as c:
            return await c.version()

    console.print_json(json.dumps(asyncio.run(go())))


async def _run_async(
    settings: Settings,
    cat,
    *,
    k: int,
    concurrency: int,
    name: str | None,
    resume: str | None,
    cleanup: bool,
    progress: bool = True,
) -> str:
    from qeval.client import AuthError, QClient
    from qeval.runner.context import Budget, RunContext, SendLimiter
    from qeval.runner.planner import plan_jobs
    from qeval.runner.pool import run_pool

    repo = _repo(settings)
    async with QClient(settings.base_url, settings.require_token()) as client:
        agent_version = await client.version()
        if resume:
            row = repo.get_run(resume)
            if row is None:
                raise typer.BadParameter(f"unknown run {resume}")
            run_id = resume
            anchor = timeutil.date.fromisoformat(row["anchor_date"])
            done = repo.completed_jobs(run_id)
            repo.reopen_run(run_id)
            k = (row.get("config") or {}).get("k", k)
        else:
            anchor = timeutil.today_ist()
            done = set()
            run_id = repo.create_run(
                name=name,
                anchor_date=anchor,
                agent_version=agent_version,
                harness_sha=_harness_sha(),
                config={
                    "k": k,
                    "concurrency": concurrency,
                    "scenarios": [s.id for s in cat.scenarios],
                    "settings": settings.public_dict(),
                },
            )
        setup_logging(settings.runs_dir / run_id / "run.log")
        console.print(
            f"run [bold]{run_id}[/] · agent {agent_version.get('model')} prompt {agent_version.get('prompt_sha')}"
            f" · anchor {anchor} · backend {repo.engine.dialect.name}"
        )
        sim = None
        if any(s.persona for s in cat.scenarios):
            from qeval.simuser.simuser import SimUser

            key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
            if not key:
                console.print(
                    "[yellow]ANTHROPIC_API_KEY not set: persona scenarios will fail as harness errors[/]"
                )
            sim = SimUser(
                api_key=key, model=settings.sim_model, effort=settings.sim_effort, repo=repo, run_id=run_id
            )
        ctx = RunContext(
            settings=settings,
            client=client,
            repo=repo,
            run_id=run_id,
            anchor=anchor,
            limiter=SendLimiter(settings.min_send_interval_ms / 1000),
            budget=Budget(settings.max_agent_messages, settings.max_judge_tokens),
            sim=sim,
            personas=cat.personas,
        )
        if sim is not None:
            sim.budget = ctx.budget
        jobs = plan_jobs(
            cat.scenarios, k, done=done, next_attempt=lambda sid, rep: repo.max_attempt(run_id, sid, rep) + 1
        )
        console.print(f"{len(jobs)} job(s) to run ({len(done)} already complete)")
        try:
            status = await run_pool(jobs, ctx, concurrency=concurrency, show_progress=progress)
        except AuthError as e:
            repo.finish_run(run_id, "aborted")
            console.print(f"[red]authentication failed: {e}[/]")
            raise typer.Exit(2) from e
        repo.finish_run(run_id, status)
        if cleanup:
            for sr in repo.scenario_runs(run_id, status="valid"):
                for a in (sr.get("accounts") or {}).values():
                    with contextlib.suppress(Exception):
                        await client.delete_account(a["account_id"])
        console.print(
            f"run {run_id} {status}: valid {ctx.stats['valid']}, invalid attempts {ctx.stats['invalid']}, "
            f"errors {ctx.stats['errors']}, messages {ctx.stats['messages']}"
        )
    return run_id


@app.command()
def run(
    paths: PathsArg = None,
    k: Annotated[int, typer.Option(help="Repetitions per scenario")] = 5,
    concurrency: Annotated[int | None, typer.Option(help="Parallel jobs (default QEVAL_CONCURRENCY)")] = None,
    only: Annotated[list[str] | None, typer.Option(help="Only these scenario ids")] = None,
    tag: Annotated[list[str] | None, typer.Option(help="Only scenarios with this tag")] = None,
    name: Annotated[str | None, typer.Option(help="Name for the run")] = None,
    resume: Annotated[str | None, typer.Option(help="Resume an earlier run id")] = None,
    cleanup: Annotated[bool, typer.Option(help="Delete valid accounts at the end")] = False,
) -> None:
    """Run step only (calls the agent); prints the run id."""
    s = get_settings()
    cat = _catalog(paths, only, tag)
    run_id = asyncio.run(
        _run_async(
            s, cat, k=k, concurrency=concurrency or s.concurrency, name=name, resume=resume, cleanup=cleanup
        )
    )
    _auto_dump(s, run_id)
    print(run_id)


def _auto_dump(s: Settings, run_id: str) -> None:
    from qeval.store.dump import dump

    repo = _repo(s)
    try:
        out = dump(repo.engine, s.runs_dir, run_id)
        console.print(f"dump: {out}")
    except Exception as e:
        console.print(f"[yellow]dump skipped: {e}[/]")


@app.command()
def grade(
    run_id: Annotated[str, typer.Argument(help="Run id ('latest' for the newest)")],
    no_judge: Annotated[
        bool, typer.Option("--no-judge", help="Deterministic checks only (fast, free)")
    ] = False,
    only_check: Annotated[list[str] | None, typer.Option(help="Only these global check ids")] = None,
    allow_changed_scenarios: Annotated[
        bool, typer.Option(help="Old runs without a snapshot: grade with the edited scenario file anyway")
    ] = False,
    relabel: Annotated[
        bool,
        typer.Option(
            help="Use current scenario labels/expectations where the conversation inputs are unchanged"
        ),
    ] = False,
) -> None:
    """Grade step only (never calls the agent); prints the grading id."""
    from qeval.grading.grade import grade as do_grade

    s = get_settings()
    repo = _repo(s)
    run_id = _resolve_run(repo, run_id)
    setup_logging(s.runs_dir / run_id / "grade.log")
    gid = asyncio.run(
        do_grade(
            repo,
            run_id,
            s,
            judge=not no_judge,
            only_checks=set(only_check) if only_check else None,
            allow_changed=allow_changed_scenarios,
            relabel=relabel,
        )
    )
    rows = repo.check_results(gid)
    by: dict[str, int] = {}
    for r in rows:
        by[r["verdict"]] = by.get(r["verdict"], 0) + 1
    console.print(f"grading {gid}: " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))
    print(gid)


def _print_gate(out: Path) -> None:
    g = json.loads((out / "results.json").read_text()).get("gate") or {}
    for r in g.get("reasons", []):
        console.print(f"  gate: {r}", markup=False)


def _resolve_run(repo, run_id: str) -> str:
    if run_id == "latest":
        rid = repo.latest_run_id()
        if rid is None:
            raise typer.BadParameter("no runs in the database")
        return rid
    return run_id


@app.command()
def report(
    run_id: Annotated[str, typer.Argument(help="Run id ('latest' for the newest)")],
    grading: Annotated[str | None, typer.Option(help="Grading id (default: latest)")] = None,
    gate: Annotated[
        bool, typer.Option(help="Exit with the CI gate code (0 ok, 1 critical failure, 2 harness problems)")
    ] = False,
) -> None:
    """Write results.json, report.html and junit.xml."""
    from qeval.report.build import build_report

    s = get_settings()
    repo = _repo(s)
    run_id = _resolve_run(repo, run_id)
    out, code = build_report(repo, run_id, grading)
    console.print(f"report: {out / 'report.html'}")
    console.print(f"results: {out / 'results.json'} · junit: {out / 'junit.xml'} · gate exit code {code}")
    _print_gate(out)
    if gate:
        raise typer.Exit(code)


@app.command()
def findings(
    run_ids: Annotated[
        list[str],
        typer.Argument(
            help="One or more run ids ('latest' allowed), e.g. a k=5 run and its k=10 confirmation"
        ),
    ],
    out: Annotated[Path, typer.Option(help="Output directory")] = ROOT / "results",
) -> None:
    """Write FINDINGS.md, findings.html and findings.json from findings.yaml and the runs' latest gradings."""
    from qeval.report.findings import build_findings, write_findings

    s = get_settings()
    repo = _repo(s)
    ids = [_resolve_run(repo, r) for r in run_ids]
    data = build_findings(repo, ids)
    for p in write_findings(data, out):
        console.print(f"wrote {p}")


@app.command()
def compare(
    run_id: Annotated[str, typer.Argument(help="Run id")],
    baseline: Annotated[str, typer.Option(help="Baseline grading id")],
    new: Annotated[str | None, typer.Option(help="New grading id (default: latest completed)")] = None,
    out: Annotated[Path, typer.Option(help="Output directory")] = ROOT / "results" / "comparison",
) -> None:
    """List every result whose verdict or scored/advisory status changed between two gradings."""
    from qeval.analysis.compare import compare as do_compare
    from qeval.analysis.compare import write_comparison

    repo = _repo(get_settings())
    run_id = _resolve_run(repo, run_id)
    new = new or repo.latest_grading(run_id)
    if new is None:
        raise typer.BadParameter("no completed grading to compare")
    data = do_compare(repo, run_id, baseline, new)
    for p in write_comparison(data, out):
        console.print(f"wrote {p}")
    console.print(f"{data['changed']} results changed ({data['critical_changed']} critical)")


@app.command("label-pack")
def label_pack(
    run_ids: Annotated[list[str], typer.Argument(help="Runs to sample from (their latest gradings)")],
    check: Annotated[list[str], typer.Option(help="Rubric/check id, or rule:W4 for all checks of a rule")],
    mode: Annotated[
        str, typer.Option(help="calibration (balanced pass/fail) or pass_audit (passes only)")
    ] = "calibration",
    n: Annotated[int, typer.Option(help="Items per check")] = 20,
    seed: Annotated[int, typer.Option()] = 7,
    name: Annotated[str | None, typer.Option(help="Pack name (default: mode + date)")] = None,
) -> None:
    """Write a blind labelling pack to labels/human/ (the eval's verdicts go to a separate key file)."""
    from datetime import date

    from qeval.analysis.calibration import make_pack

    if mode not in ("calibration", "pass_audit"):
        raise typer.BadParameter("mode must be calibration or pass_audit")
    repo = _repo(get_settings())
    ids = [_resolve_run(repo, r) for r in run_ids]
    pack, key = make_pack(
        repo, ids, check, n=n, seed=seed, mode=mode, name=name or f"{mode}_{date.today().isoformat()}"
    )
    console.print(f"pack: {pack}\nkey (do not open while labelling): {key}")


@app.command()
def stability(
    run_ids: Annotated[list[str], typer.Argument(help="Runs to sample judge items from")],
    rubric: Annotated[list[str], typer.Option(help="Rubric ids")],
    n: Annotated[int, typer.Option()] = 10,
    repeats: Annotated[int, typer.Option()] = 3,
    seed: Annotated[int, typer.Option()] = 7,
) -> None:
    """Re-judge sampled items several times without the cache; record verdict stability (judge calls only)."""
    from qeval.analysis.calibration import stability as do_stability

    s = get_settings()
    repo = _repo(s)
    ids = [_resolve_run(repo, r) for r in run_ids]
    rec = asyncio.run(do_stability(repo, s, ids, rubric, n=n, repeats=repeats, seed=seed))
    for rid, r in rec["rubrics"].items():
        console.print(
            f"{rid}: {r['identical']}/{r['items']} items identical across {r['repeats']} runs "
            f"({(r['stability'] or 0):.0%})"
        )


@app.command()
def trust(
    pack: Annotated[Path, typer.Argument(help="A labelled pack in labels/human/")],
) -> None:
    """Score a labelled pack and update trust/judge_trust.yaml (only rubrics meeting every threshold count)."""
    from qeval.analysis.calibration import decide_trust, score_pack

    cal = score_pack(pack)
    out = ROOT / "trust" / f"calibration_{cal['pack']}.json"
    out.write_text(json.dumps(cal, indent=1))
    for c, m in cal["checks"].items():
        console.print(
            f"{c}: n={m['n']} agreement {m['agreement']:.0%} kappa {m['kappa']:.2f} "
            f"fail precision {m['fail_precision']} fail recall {m['fail_recall']} "
            f"missed failures {m['missed_failures']}",
            highlight=False,
        )
    for rid, status in decide_trust(cal).items():
        console.print(f"[bold]{rid}[/]: {status}")
    console.print(f"calibration written to {out}; re-grade (judge verdicts come from cache) to apply.")


@app.command("eval")
def eval_cmd(
    paths: PathsArg = None,
    k: Annotated[int, typer.Option(help="Repetitions per scenario")] = 5,
    concurrency: Annotated[int | None, typer.Option()] = None,
    only: Annotated[list[str] | None, typer.Option()] = None,
    tag: Annotated[list[str] | None, typer.Option()] = None,
    name: Annotated[str | None, typer.Option()] = None,
    no_judge: Annotated[bool, typer.Option("--no-judge")] = False,
) -> None:
    """Run, grade, report, findings, dump and export in one command; exits with the CI gate code."""
    from qeval.grading.grade import grade as do_grade
    from qeval.report.build import build_report
    from qeval.store.export import export_run

    s = get_settings()
    cat = _catalog(paths, only, tag)
    run_id = asyncio.run(
        _run_async(
            s, cat, k=k, concurrency=concurrency or s.concurrency, name=name, resume=None, cleanup=False
        )
    )
    repo = _repo(s)
    gid = asyncio.run(do_grade(repo, run_id, s, judge=not no_judge))
    out, code = build_report(repo, run_id, gid)
    from qeval.report.findings import build_findings, write_findings

    write_findings(build_findings(repo, [run_id]), out)
    exp = export_run(repo, run_id, s.runs_dir / run_id / "export.jsonl.gz")
    _auto_dump(s, run_id)
    console.print(
        f"report: {out / 'report.html'}\nfindings: {out / 'FINDINGS.md'}\nexport: {exp}\n"
        f"gate exit code {code}"
    )
    raise typer.Exit(code)


@app.command()
def show(
    run_id: Annotated[str, typer.Argument()],
    scenario_id: Annotated[str, typer.Argument()],
    rep: Annotated[int, typer.Option()] = 1,
    trace: Annotated[bool, typer.Option(help="Show tool calls")] = True,
    checks: Annotated[bool, typer.Option(help="Show check results of the latest grading")] = True,
) -> None:
    """Print one run's transcript, trace and check results."""
    from qeval.grading.grade import ScenarioResolveError, resolve_scenario
    from qeval.normalise.build import build_norm_run
    from qeval.scenario.loader import load_personas

    s = get_settings()
    repo = _repo(s)
    run_id = _resolve_run(repo, run_id)
    run_row = repo.get_run(run_id)
    srs = [x for x in repo.scenario_runs(run_id, scenario_id=scenario_id) if x["rep"] == rep]
    if not srs:
        raise typer.BadParameter(f"no scenario run {scenario_id} rep {rep} in {run_id}")
    sr = srs[-1]
    console.rule(f"{scenario_id} rep {rep} attempt {sr['attempt']} · {sr['status']} · {sr['id']}")
    if sr.get("error"):
        console.print(f"[red]{sr['error'][:2000]}[/]")
    try:
        sc, persona = resolve_scenario(sr, {}, load_personas(), allow_changed=True)
    except ScenarioResolveError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    nr = build_norm_run(sc, sr, repo.turns(sr["id"]), run_row["anchor_date"], persona=persona)
    for acc in nr.accounts.values():
        console.print(f"account {acc.key}: {acc.account_id} refs {acc.refs}")
    for t in nr.turns:
        sv = t.state or t.prev_state
        console.print(f"\n[bold]turn {t.idx}[/] · {timeutil.fmt_ist(t.received_at)} · {t.actor}")
        console.print(f"[cyan]User:[/] {t.user_text}")
        r = t.reply
        about = ", ".join(sv.member_name(x) for x in r.get("subject_ids") or []) or "—"
        console.print(
            f"[magenta]Q[/] kind={r.get('kind')} urgency={r.get('urgency')} escalate={r.get('escalate_to_doctor')}"
            f" about={about} episode={r.get('episode_id')} ({t.latency_ms} ms)"
        )
        console.print(r.get("message") or "", markup=False)
        for f in ("possible_causes", "red_flags", "missing_info", "options"):
            if r.get(f):
                console.print(f"  {f}: {r.get(f)}", markup=False)
        if trace:
            for i, step in enumerate(t.trace.steps, 1):
                for c in step:
                    err = " [red]ERROR[/]" if c.is_error else ""
                    console.print(
                        f"  step {i}: {c.tool}{err} {json.dumps(c.input, ensure_ascii=False)[:200]}",
                        markup=True,
                        highlight=False,
                    )
        if t.diff and not t.diff.is_empty():
            for fact in t.diff.new_facts:
                console.print(
                    f"  + fact {sv.member_name(fact.get('subject_id'))} / {fact.get('category')} / {fact.get('key')}: "
                    f"{fact.get('value')}",
                    markup=False,
                )
            for m in t.diff.new_subjects:
                console.print(f"  + member {m.label}", markup=False)
            for ep in t.diff.opened_episodes:
                console.print(f"  + episode {ep.get('complaint')} ({ep['id']})", markup=False)
            for ep in t.diff.closed_episodes:
                console.print(
                    f"  - closed {ep.get('complaint')} ({ep['id']}) by {ep.get('closed_by')}", markup=False
                )
    if checks:
        gid = repo.latest_grading(run_id)
        if gid:
            rows = [
                r for r in repo.check_results(gid, sr["id"]) if r["verdict"] not in ("pass", "n/a", "info")
            ]
            console.rule(f"non-passing checks ({gid})")
            for r in rows:
                console.print(
                    f"[{'red' if r['verdict'] == 'fail' else 'yellow'}]{r['verdict']}[/] {r['rule']} "
                    f"{r['check_id']} turn {r['turn_idx']}: {r['message'] or ''}",
                    highlight=False,
                )
                if r["verdict"] == "fail":
                    console.print(
                        json.dumps(r["evidence"], ensure_ascii=False, default=str)[:800], markup=False
                    )


@app.command()
def coverage(paths: PathsArg = None) -> None:
    """Rules -> scenarios, deterministic checks and rubrics."""
    from qeval.checks.registry import REGISTRY

    cat = _catalog(paths)
    tbl = Table("Rule", "Sev", "Scenarios", "Global checks", "Rubrics")
    for rid, info in cat.rules.items():
        scen = [s.id for s in cat.scenarios if rid in s.rules]
        scen += [
            s.id
            for s in cat.scenarios
            if s.persona and rid in cat.personas[s.persona].rules and s.id not in scen
        ]
        checks = [c for c, spec in REGISTRY.items() if spec.rule == rid and spec.scored]
        rubs = [r.id for r in cat.rubrics.values() if r.rule == rid]
        tbl.add_row(
            rid, info.severity, str(len(scen)) if scen else "[red]0[/]", ", ".join(checks), ", ".join(rubs)
        )
    console.print(tbl)


@app.command()
def calibrate(
    run_id: Annotated[str, typer.Argument(help="Run id ('latest')")] = "latest",
    labels: Annotated[Path, typer.Option()] = ROOT / "labels" / "labels.yaml",
) -> None:
    """Judge agreement with hand labels in labels/labels.yaml."""
    import yaml

    s = get_settings()
    repo = _repo(s)
    run_id = _resolve_run(repo, run_id)
    gid = repo.latest_grading(run_id)
    if not gid:
        raise typer.BadParameter("run has no grading")
    items = yaml.safe_load(labels.read_text()) or []
    rows = repo.check_results(gid)
    srs = {sr["id"]: sr for sr in repo.scenario_runs(run_id)}
    by_rubric: dict[str, list[tuple[str, str]]] = {}
    missing = 0
    for it in items:
        sr_id = it.get("scenario_run_id")
        if not sr_id and it.get("scenario_id"):
            cands = [
                x
                for x in srs.values()
                if x["scenario_id"] == it["scenario_id"]
                and x["rep"] == it.get("rep", 1)
                and x["status"] == "valid"
            ]
            sr_id = cands[-1]["id"] if cands else None
        match = [
            r
            for r in rows
            if r["scenario_run_id"] == sr_id
            and r["kind"] == "judge"
            and (
                r["check_id"] == it["check_id"]
                if it.get("check_id")
                else r["check_id"].split("#")[0] == it["rubric"]
            )
            and r["turn_idx"] == it.get("turn_idx")
        ]
        if not match:
            missing += 1
            continue
        by_rubric.setdefault(it["rubric"], []).append((it["label"], match[0]["verdict"]))
    tbl = Table("Rubric", "Items", "Agreement", "Cohen's kappa")
    for rub, pairs in sorted(by_rubric.items()):
        agree = sum(1 for a, b in pairs if a == b) / len(pairs)
        tbl.add_row(rub, str(len(pairs)), f"{agree:.0%}", f"{_kappa(pairs):.2f}")
    console.print(tbl)
    if missing:
        console.print(f"[yellow]{missing} label(s) had no matching judge verdict in {gid}[/]")


def _kappa(pairs: list[tuple[str, str]]) -> float:
    labels = sorted({x for p in pairs for x in p})
    n = len(pairs)
    po = sum(1 for a, b in pairs if a == b) / n
    pe = sum(
        (sum(1 for a, _ in pairs if a == lab) / n) * (sum(1 for _, b in pairs if b == lab) / n)
        for lab in labels
    )
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


@app.command("export")
def export_cmd(run_id: Annotated[str, typer.Argument()]) -> None:
    """Portable JSONL export of a run (re-gradable on SQLite)."""
    from qeval.store.export import export_run

    s = get_settings()
    repo = _repo(s)
    run_id = _resolve_run(repo, run_id)
    print(export_run(repo, run_id, s.runs_dir / run_id / "export.jsonl.gz"))


@app.command("import")
def import_cmd(file: Annotated[Path, typer.Argument()]) -> None:
    """Import a run exported with `qeval export`."""
    from qeval.store.export import import_run

    print(import_run(_repo(get_settings()), file))


@app.command("dump")
def dump_cmd() -> None:
    """Backend-specific dump of the whole database."""
    from qeval.store.dump import dump

    s = get_settings()
    repo = _repo(s)
    name = repo.latest_run_id() or "empty"
    print(dump(repo.engine, s.runs_dir, name))


@app.command()
def runs(limit: int = 15) -> None:
    """List recent runs."""
    s = get_settings()
    repo = _repo(s)
    tbl = Table("Run", "Name", "Started", "Status", "Scenario runs", "Gradings")
    for r in repo.list_runs(limit):
        srs = repo.scenario_runs(r["run_id"])
        valid = sum(1 for x in srs if x["status"] == "valid")
        tbl.add_row(
            r["run_id"],
            r.get("name") or "",
            r.get("started_at") or "",
            r.get("status") or "",
            f"{valid}/{len(srs)}",
            repo.latest_grading(r["run_id"]) or "",
        )
    console.print(tbl)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
