"""results.json, report.html, junit.xml and the exit code (build.md 14)."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from jinja2 import Environment, FileSystemLoader, select_autoescape

from qeval import timeutil
from qeval.analysis.aggregate import Aggregate, CheckStat, aggregate
from qeval.grading.grade import norm_runs_for
from qeval.normalise.models import NormRun, NormTurn
from qeval.scenario.loader import load_rubrics, load_rules
from qeval.store.repo import Repo

TEMPLATES = Path(__file__).parent / "templates"
MAX_CARDS = 150


def _short(v, n=220):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "…"


def turn_view(nt: NormTurn, nr: NormRun) -> dict:
    sv = nt.state or nt.prev_state
    return {
        "idx": nt.idx,
        "account": nt.account_key,
        "actor": nt.actor,
        "time": timeutil.fmt_ist(nt.received_at),
        "user": nt.user_text,
        "reply": nt.reply.get("message") or "",
        "kind": nt.reply.get("kind"),
        "urgency": nt.reply.get("urgency"),
        "escalate": nt.reply.get("escalate_to_doctor"),
        "subjects": [sv.member_name(s) for s in nt.reply.get("subject_ids") or []],
        "episode": nt.reply.get("episode_id"),
        "fields": {
            k: nt.reply.get(k)
            for k in ("possible_causes", "red_flags", "missing_info", "options")
            if nt.reply.get(k)
        },
        "latency_ms": nt.latency_ms,
    }


def trace_view(nt: NormTurn) -> list[dict]:
    out = []
    for i, step in enumerate(nt.trace.steps, 1):
        for c in step:
            out.append(
                {
                    "step": i,
                    "tool": c.tool,
                    "input": _short(c.input, 300),
                    "output": _short(c.output, 300),
                    "is_error": c.is_error,
                }
            )
    return out


def state_excerpt(nt: NormTurn | None, nr: NormRun) -> dict:
    if nt is None:
        nt = nr.turns[-1] if nr.turns else None
    if nt is None:
        return {}
    sv = nt.state or nr.final_state(nt.account_key) or nt.prev_state
    ids = set(nt.reply.get("subject_ids") or [])
    facts = [
        {
            "member": sv.member_name(f.get("subject_id")),
            "category": f.get("category"),
            "key": f.get("key"),
            "value": f.get("value"),
            "superseded": f.get("superseded"),
            "this_turn": f.get("source_turn_id") == nt.user_turn_id,
            "episode": f.get("episode_id"),
        }
        for f in sv.facts
        if not ids or f.get("subject_id") in ids or f.get("subject_id") is None
    ]
    eps = [
        {
            "id": e["id"],
            "member": sv.member_name(e.get("subject_id")),
            "complaint": e.get("complaint"),
            "status": e.get("status"),
            "closed_by": e.get("closed_by"),
            "outcome": e.get("outcome"),
            "opened": timeutil.fmt_ist(timeutil.to_ist(e["opened_at"])) if e.get("opened_at") else "",
            "last": timeutil.fmt_ist(timeutil.to_ist(e["last_turn_at"])) if e.get("last_turn_at") else "",
        }
        for e in sv.episodes.values()
    ]
    members = [m.label or f"{m.name} ({m.relation})" for m in sv.members.values()]
    diff = {}
    if nt.diff is not None:
        d = nt.diff
        diff = {
            "new_facts": [
                f"{sv.member_name(f.get('subject_id'))} / {f.get('category')} / {f.get('key')}: {f.get('value')}"
                for f in d.new_facts
            ],
            "superseded": [f"{f.get('key')}: {f.get('value')}" for f in d.superseded_facts],
            "new_members": [m.label or f"{m.name} ({m.relation})" for m in d.new_subjects],
            "changed_members": [f"{a.label} -> {b.label}" for a, b in d.changed_subjects],
            "opened_episodes": [
                f"{sv.member_name(e.get('subject_id'))}: {e.get('complaint')} ({e['id']})"
                for e in d.opened_episodes
            ],
            "closed_episodes": [
                f"{sv.member_name(e.get('subject_id'))}: {e.get('complaint')} ({e['id']}, by {e.get('closed_by')})"
                for e in d.closed_episodes
            ],
        }
        diff = {k: v for k, v in diff.items() if v}
    return {"members": members, "facts": facts, "episodes": eps, "diff": diff}


def _names(nr: NormRun) -> dict[str, str]:
    out: dict[str, str] = {}
    for acc in nr.accounts.values():
        sv = acc.final or acc.initial
        for m in sv.members.values():
            out[m.id] = m.name or m.relation
        for e in sv.episodes.values():
            out[e["id"]] = f"{e.get('complaint')} episode"
    return out


def build_cards(
    agg: Aggregate,
    rows: list[dict],
    norm: dict[str, NormRun],
    rubrics: dict | None = None,
    current: dict | None = None,
) -> list[dict]:
    from qeval.report.describe import describe

    rows_by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        rows_by[(r["scenario_run_id"], r["check_id"])].append(r)
    cards: list[dict] = []
    for st in agg.checks:
        if not st.fails:
            continue
        srid = st.failing_runs[0]
        nr = norm.get(srid)
        if nr is None:
            continue
        fail_rows = [r for r in rows_by[(srid, st.check_id)] if r["verdict"] == "fail"]
        fr = fail_rows[0] if fail_rows else None
        tidx = fr["turn_idx"] if fr else None
        nt = next((t for t in nr.turns if t.idx == tidx), None) if tidx is not None else None
        upto = [t for t in nr.turns if tidx is None or t.idx <= tidx]
        snap = {"scenario": nr.scenario.model_dump(mode="json")}
        desc = describe(fr, rubrics, snap, _names(nr), current=current) if fr else None
        cards.append(
            {
                "check_title": desc.check_title if desc else st.check_id,
                "got": desc.got if desc else "",
                "user_at_failure": nt.user_text if nt else "",
                "anchor": f"card-{len(cards)}",
                "check": st.check_id,
                "rule": st.rule,
                "severity": st.severity,
                "area": st.area,
                "kind": st.kind,
                "scenario": st.scenario_id,
                "title": getattr(nr.scenario, "title", ""),
                "fails": st.fails,
                "n": st.n,
                "label": st.label,
                "ci": st.ci,
                "message": fr["message"] if fr else "",
                "evidence": json.dumps(
                    fr["evidence"] if fr else {}, indent=1, ensure_ascii=False, default=str
                )[:6000],
                "judge": fr["evidence"] if fr and fr["kind"] == "judge" else None,
                "turn_idx": tidx,
                "rep": nr.rep,
                "scenario_run_id": srid,
                "transcript": [turn_view(t, nr) for t in upto],
                "trace": trace_view(nt) if nt else [],
                "state": state_excerpt(nt, nr),
                "other_failures": [sid for sid in st.failing_runs[1:6]],
                "passing_example": st.passing_runs[0] if st.passing_runs else None,
                "all_fail_turns": sorted({r["turn_idx"] for r in fail_rows if r["turn_idx"] is not None}),
            }
        )
        if len(cards) >= MAX_CARDS:
            break
    return cards


def _current_scenarios() -> dict:
    from qeval.config import ROOT
    from qeval.scenario.loader import LoadError, load_scenario, yaml_files

    out = {}
    for f in yaml_files([ROOT / "scenarios"]):
        try:
            sc = load_scenario(f)
        except LoadError:
            continue
        out[sc.id] = sc
    return out


def group_cards(cards: list[dict], agg: Aggregate, rules_info: dict) -> list[dict]:
    """One group per failing rule: its title and expected behaviour once, then each failing scenario."""
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for c in cards:
        by_rule[c["rule"]].append(c)
    rule_stats = {r["rule"]: r for r in agg.rules}
    groups = []
    for rid, cs in by_rule.items():
        info = rules_info.get(rid)
        st = rule_stats.get(rid, {})
        groups.append(
            {
                "rule": rid,
                "title": info.title if info else rid,
                "expected": info.expected if info else "",
                "spec": info.summary if info else "",
                "severity": info.severity if info else cs[0]["severity"],
                "fails": st.get("fails", 0),
                "n": st.get("n", 0),
                "ci": st.get("ci"),
                "cards": sorted(cs, key=lambda c: (-c["fails"] / max(c["n"], 1), c["scenario"])),
            }
        )
    sev = {"critical": 0, "major": 1, "minor": 2}
    groups.sort(key=lambda g: (sev.get(g["severity"], 9), -(g["fails"] / g["n"] if g["n"] else 0)))
    return groups


def advisory_view(
    agg: Aggregate, rows: list[dict], norm: dict, rubrics: dict, current: dict, rules_info: dict
) -> list[dict]:
    """Advisory checks (not counted): one line each, with what happened in the first failing run."""
    from qeval.report.describe import describe

    first_fail: dict[tuple[str, str], dict] = {}
    for r in rows:
        if r.get("scored") is False and r["verdict"] == "fail":
            first_fail.setdefault((r["scenario_run_id"], r["check_id"]), r)
    out: list[dict[str, Any]] = []
    for st in agg.advisory:
        if not st.fails:
            continue
        srid = st.failing_runs[0]
        nr = norm.get(srid)
        row = first_fail.get((srid, st.check_id))
        d = None
        if row is not None and nr is not None:
            d = describe(
                row, rubrics, {"scenario": nr.scenario.model_dump(mode="json")}, _names(nr), current=current
            )
        info = rules_info.get(st.rule)
        out.append(
            {
                "rule": st.rule,
                "rule_title": info.title if info else st.rule,
                "severity": st.severity,
                "check": st.check_id,
                "check_title": d.check_title if d else st.check_id,
                "kind": st.kind,
                "scenario": st.scenario_id,
                "scenario_title": getattr(nr.scenario, "title", st.scenario_id) if nr else st.scenario_id,
                "fails": st.fails,
                "n": st.n,
                "got": d.got if d else "",
            }
        )
    sev = {"critical": 0, "major": 1, "minor": 2}
    out.sort(key=lambda a: (sev.get(a["severity"], 9), -a["fails"] / max(a["n"], 1), a["rule"]))
    return out


def results_json(run_row: dict, grading: dict, agg: Aggregate) -> dict:
    critical = [r for r in agg.rules if r["severity"] == "critical" and r["fails"]]
    return {
        "run": {
            "run_id": run_row["run_id"],
            "name": run_row.get("name"),
            "agent_version": run_row.get("agent_version"),
            "anchor_date": run_row.get("anchor_date"),
            "started_at": run_row.get("started_at"),
            "finished_at": run_row.get("finished_at"),
            "status": run_row.get("status"),
            "k": (run_row.get("config") or {}).get("k"),
            "harness_sha": run_row.get("harness_sha"),
            "judge_model": grading.get("judge_model"),
            "grading_id": grading["grading_id"],
            "checks_version": grading.get("checks_version"),
            "counts": agg.counts,
        },
        "summary": {
            "by_area": agg.areas,
            "critical_failures": len(critical),
            "failing_rules": [r["rule"] for r in agg.rules if r["fails"]],
        },
        "rules": agg.rules,
        "scenarios": agg.scenarios,
        "cost": agg.cost,
        "problems": {k: (len(v) if isinstance(v, list) else v) for k, v in agg.problems.items()},
    }


def junit_xml(agg: Aggregate) -> str:
    by_area: dict[str, list[CheckStat]] = defaultdict(list)
    for s in agg.checks:
        by_area[s.area].append(s)
    parts = ['<?xml version="1.0" encoding="UTF-8"?>', "<testsuites>"]
    for area, ss in by_area.items():
        fails = sum(1 for s in ss if s.fails)
        parts.append(f'<testsuite name="{escape(area)}" tests="{len(ss)}" failures="{fails}">')
        for s in ss:
            name = escape(f"{s.scenario_id}::{s.check_id}", {'"': "&quot;"})
            parts.append(f'<testcase classname="{escape(s.rule)}" name="{name}">')
            if s.fails:
                msg = escape(f"{s.rule} ({s.severity}) failed {s.fails}/{s.n}", {'"': "&quot;"})
                parts.append(f'<failure message="{msg}">{escape(", ".join(s.failing_runs[:3]))}</failure>')
            parts.append("</testcase>")
        parts.append("</testsuite>")
    parts.append("</testsuites>")
    return "\n".join(parts)


JUDGE_ERROR_RATE_MAX = 0.05
FAILED_REPS_MAX = 0.10


def gate(agg: Aggregate, named_rules: set[str] | None = None) -> tuple[int, list[str]]:
    """CI exit code and the reasons for it.

    2 = the measurement itself can't be trusted (checked first); 1 = a critical (safety) rule failed; 0 = ok.
    """
    harness: list[str] = []
    p = agg.problems
    if p["check_errors"]:
        harness.append(f"{len(p['check_errors'])} deterministic check/expectation error(s)")
    if p["harness_rows"]:
        harness.append(
            f"{len(p['harness_rows'])} scenario run(s) could not be graded (missing or changed scenario)"
        )
    unresolved_crit = [u for u in p["unresolved"] if u["severity"] == "critical"]
    if unresolved_crit:
        harness.append(
            "unresolved critical flag(s): "
            + ", ".join(f"{u['scenario']}::{u['check']}" for u in unresolved_crit[:5])
        )
    if p["judge_rows"]:
        rate = len(p["judge_errors"]) / p["judge_rows"]
        if rate > JUDGE_ERROR_RATE_MAX:
            harness.append(f"judge error rate {rate:.0%} > {JUDGE_ERROR_RATE_MAX:.0%}")
    failed = p["failed_reps"]
    if any(f["status"] in ("scenario_error", "harness_error") for f in failed):
        harness.append(
            f"{sum(f['status'] in ('scenario_error', 'harness_error') for f in failed)} scenario/harness error rep(s)"
        )
    if any(f["critical"] for f in failed):
        harness.append(
            "critical scenario(s) without a valid run: "
            + ", ".join(sorted({f["scenario"] for f in failed if f["critical"]}))[:300]
        )
    jobs = agg.counts.get("jobs") or 0
    if jobs and len(failed) / jobs > FAILED_REPS_MAX:
        harness.append(f"{len(failed)}/{jobs} reps never produced a valid run")
    if named_rules:
        uncovered = sorted(
            r["rule"] for r in agg.rules if r["rule"] in named_rules and not r["n"] and not r["unresolved"]
        )
        if uncovered:
            harness.append("rules named by the run's scenarios but never measured: " + ", ".join(uncovered))
    if harness:
        return 2, harness
    crit = [
        f"{r['rule']} failed {r['fails']}/{r['n']}"
        for r in agg.rules
        if r["severity"] == "critical" and r["fails"]
    ]
    if crit:
        return 1, crit
    return 0, []


def measurable_named_rules(srs: list[dict], *, judge: bool) -> set[str]:
    """Rules the run's scenarios name and this grading can *score*. Judge-only rules drop out when the judge
    is off, and also when none of the rule's rubrics is trusted yet (they are graded, but advisory)."""
    from qeval.checks.registry import REGISTRY, load_checks
    from qeval.scenario.coverage import contributions
    from qeval.scenario.loader import from_snapshot, load_rubrics

    load_checks()
    rubrics = load_rubrics()
    global_rules = {s.rule for s in REGISTRY.values() if s.scored}
    from qeval.judge.trust import is_scored, load_trust

    trust = load_trust()
    scored_judge_rules = {r.rule for r in rubrics.values() if is_scored(r, trust)}
    out: set[str] = set()
    seen: set[str] = set()
    for sr in srs:
        snap = sr.get("scenario_snapshot")
        if not snap or sr["scenario_id"] in seen:
            continue
        seen.add(sr["scenario_id"])
        sc, persona = from_snapshot(snap)
        contrib = contributions(sc, persona, rubrics, global_rules)
        for r in list(sc.rules) + (list(persona.rules) if persona else []):
            srcs = contrib.get(r, set()) - ({"global"} if r not in global_rules else set())
            if not judge or r not in scored_judge_rules:
                srcs -= {"judge", "auto-judge"}
            if srcs:
                out.add(r)
    return out


def build_report(
    repo: Repo, run_id: str, grading_id: str | None = None, out_dir: Path | None = None
) -> tuple[Path, int]:
    run_row = repo.get_run(run_id)
    if run_row is None:
        raise SystemExit(f"unknown run {run_id}")
    grading_id = grading_id or repo.latest_grading(run_id)
    if grading_id is None:
        raise SystemExit(f"run {run_id} has no grading; run `qeval grade {run_id}` first")
    grading = repo.get_grading(grading_id) or {}
    rows = repo.check_results(grading_id)
    srs = repo.scenario_runs(run_id)
    turns_by_sr = repo.turns_for_run(run_id)
    rules_info = load_rules()
    agg = aggregate(srs, rows, turns_by_sr, repo.usage_totals(run_id), rules_info)
    code, reasons = gate(
        agg, measurable_named_rules(srs, judge=bool((grading.get("options") or {}).get("judge", True)))
    )
    norm = {sr["id"]: nr for sr, nr, _ in norm_runs_for(repo, run_id) if nr is not None}
    rubrics = load_rubrics()
    current = _current_scenarios()
    cards = build_cards(agg, rows, norm, rubrics, current)
    rule_groups = group_cards(cards, agg, rules_info)
    advisory = advisory_view(agg, rows, norm, rubrics, current, rules_info)
    from qeval.judge.trust import trust_status

    judge_trust = trust_status(rubrics)

    out_dir = out_dir or Path(repo_runs_dir(repo)) / run_id / "report"
    out_dir.mkdir(parents=True, exist_ok=True)
    res = results_json(run_row, grading, agg)
    res["gate"] = {"exit_code": code, "reasons": reasons}
    res["advisory"] = [{k: v for k, v in a.items() if k != "example"} for a in advisory]
    res["judge_trust"] = judge_trust
    (out_dir / "results.json").write_text(json.dumps(res, indent=1, ensure_ascii=False, default=str))
    (out_dir / "junit.xml").write_text(junit_xml(agg))

    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html", "j2"]))
    env.filters["pct"] = lambda x: "–" if x is None else f"{x * 100:.0f}%"
    env.filters["short"] = _short
    html = env.get_template("report.html.j2").render(
        run=run_row,
        grading=grading,
        rule_groups=rule_groups,
        advisory=advisory,
        judge_trust=judge_trust,
        rules_info=rules_info,
        agg=agg,
        res=res,
        cards=cards,
        exit_code=code,
        gate_reasons=reasons,
        rule_check_counts=_rule_check_counts(rows),
    )
    (out_dir / "report.html").write_text(html)
    return out_dir, code


def _rule_check_counts(rows: list[dict]) -> dict[str, dict]:
    d: dict[str, dict] = defaultdict(lambda: {"global": 0, "expect": 0, "judge": 0, "persona": 0})
    for r in rows:
        if r["kind"] in ("global", "expect", "judge", "persona") and r["verdict"] in ("pass", "fail"):
            d[r["rule"]][r["kind"]] += 1
    return d


def repo_runs_dir(repo: Repo) -> str:
    from qeval.config import get_settings

    return str(get_settings().runs_dir)
