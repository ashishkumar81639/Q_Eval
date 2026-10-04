"""Failure rates per check / scenario / rule / area, with Wilson intervals (build.md 13)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from math import sqrt
from statistics import median

from qeval.analysis.attempts import authoritative, latest_per_rep

SEVERITY_WEIGHT = {"critical": 10, "major": 3, "minor": 1}
SEVERITY_ORDER = {"critical": 0, "major": 1, "minor": 2}
AREA_ORDER = ["safety", "reply", "who", "triage", "memory", "time", "episodes", "general", "isolation"]
COUNTED = {"pass", "fail"}


def wilson(fails: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = fails / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - m) / d), min(1.0, (c + m) / d))


def label(fails: int, n: int) -> str:
    if n == 0:
        return "not counted"
    if fails == 0:
        return "always passes"
    if fails == n:
        return "always fails"
    return "flaky"


def percentile(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


@dataclass
class CheckStat:
    scenario_id: str
    check_id: str
    rule: str
    area: str
    severity: str
    kind: str
    fails: int = 0
    n: int = 0
    failing_runs: list[str] = field(default_factory=list)
    passing_runs: list[str] = field(default_factory=list)
    uncounted: int = 0
    unresolved: int = 0  # flagged by code, never confirmed or rejected by the judge
    unresolved_runs: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.fails / self.n if self.n else 0.0

    @property
    def ci(self) -> tuple[float, float]:
        return wilson(self.fails, self.n)

    @property
    def label(self) -> str:
        return label(self.fails, self.n)


@dataclass
class Aggregate:
    checks: list[CheckStat]
    rules: list[dict]
    areas: list[dict]
    scenarios: list[dict]
    counts: dict
    problems: dict
    cost: dict
    advisory: list[CheckStat] = field(default_factory=list)  # shown, never counted


def run_verdict(verdicts: list[str]) -> str | None:
    """One verdict per (scenario run, check): any fail wins, then unresolved, then pass."""
    if "fail" in verdicts:
        return "fail"
    if "unresolved" in verdicts:
        return "unresolved"
    if "pass" in verdicts:
        return "pass"
    return None


def aggregate(
    scenario_runs: list[dict],
    rows: list[dict],
    turns_by_sr: dict[str, list[dict]],
    usage: dict,
    rules_info: dict,
) -> Aggregate:
    sr_by_id = {s["id"]: s for s in scenario_runs}
    # Exactly one scored run per (scenario, rep): the latest valid attempt. Used for every number below.
    scored_runs = authoritative(scenario_runs)
    valid_ids = {s["id"] for s in scored_runs}
    excluded_sim = {r["scenario_run_id"] for r in rows if r["kind"] == "sim" and r["verdict"] == "fail"}

    # (scenario_run, check) -> verdicts
    # Scored rows feed every number; advisory rows (scored is False) get the same statistics, kept apart.
    per: dict[tuple[str, str], list[str]] = defaultdict(list)
    per_adv: dict[tuple[str, str], list[str]] = defaultdict(list)
    meta: dict[str, dict] = {}
    for r in rows:
        if r["scenario_run_id"] not in valid_ids or r["kind"] == "harness":
            continue
        if r["scenario_run_id"] in excluded_sim:
            continue
        if r["kind"] == "sim" or r["verdict"] == "info":
            continue
        target = per_adv if r.get("scored") is False else per
        target[(r["scenario_run_id"], r["check_id"])].append(r["verdict"])
        meta.setdefault(
            r["check_id"],
            {"rule": r["rule"], "area": r["area"], "severity": r["severity"], "kind": r["kind"]},
        )

    def tally(source: dict[tuple[str, str], list[str]]) -> list[CheckStat]:
        stats: dict[tuple[str, str], CheckStat] = {}
        for (srid, cid), verdicts in source.items():
            sc_id = sr_by_id[srid]["scenario_id"]
            m = meta[cid]
            st = stats.setdefault(
                (sc_id, cid), CheckStat(sc_id, cid, m["rule"], m["area"], m["severity"], m["kind"])
            )
            v = run_verdict(verdicts)
            if v is None:
                st.uncounted += 1
                continue
            if v == "unresolved":
                st.unresolved += 1
                st.unresolved_runs.append(srid)
                continue
            st.n += 1
            if v == "fail":
                st.fails += 1
                st.failing_runs.append(srid)
            else:
                st.passing_runs.append(srid)
        return sorted(
            stats.values(),
            key=lambda s: (SEVERITY_ORDER.get(s.severity, 9), -s.rate, s.scenario_id, s.check_id),
        )

    checks = tally(per)
    advisory = tally(per_adv)

    # rules
    by_rule: dict[str, list[CheckStat]] = defaultdict(list)
    for s in checks:
        by_rule[s.rule].append(s)
    rules = []
    for rid, info in rules_info.items():
        ss = by_rule.get(rid, [])
        fails = sum(s.fails for s in ss)
        n = sum(s.n for s in ss)
        rules.append(
            {
                "rule": rid,
                "area": info.area,
                "severity": info.severity,
                "summary": info.summary,
                "fails": fails,
                "n": n,
                "unresolved": sum(s.unresolved for s in ss),
                "fail_rate": fails / n if n else None,
                "ci": wilson(fails, n) if n else None,
                "label": label(fails, n),
                "failing_checks": sorted({f"{s.scenario_id}::{s.check_id}" for s in ss if s.fails}),
                "scenarios": sorted({s.scenario_id for s in ss}),
            }
        )

    # areas: severity-weighted pass rate over (scenario run, check) pairs
    areas = []
    for area in AREA_ORDER + sorted({s.area for s in checks} - set(AREA_ORDER)):
        ss = [s for s in checks if s.area == area]
        w_total = sum(SEVERITY_WEIGHT.get(s.severity, 1) * s.n for s in ss)
        w_pass = sum(SEVERITY_WEIGHT.get(s.severity, 1) * (s.n - s.fails) for s in ss)
        area_rules = [r for r in rules if r["area"] == area]
        failing = [r["rule"] for r in area_rules if r["fails"]]
        worst = min((SEVERITY_ORDER[r["severity"]] for r in area_rules if r["fails"]), default=None)
        areas.append(
            {
                "area": area,
                "score": (w_pass / w_total) if w_total else None,
                "checked_pairs": sum(s.n for s in ss),
                "unresolved": sum(s.unresolved for s in ss),
                "failing_rules": failing,
                "worst_severity": [k for k, v in SEVERITY_ORDER.items() if v == worst][0]
                if worst is not None
                else None,
                "rules_covered": sorted({r["rule"] for r in area_rules if r["n"]}),
                "rules_total": len(area_rules),
            }
        )

    # scenarios
    scen: dict[str, dict] = {}
    for srow in scenario_runs:
        d = scen.setdefault(
            srow["scenario_id"],
            {
                "id": srow["scenario_id"],
                "path": srow.get("scenario_path"),
                "valid": 0,
                "invalid": 0,
                "other": 0,
                "checks": [],
            },
        )
        if srow["status"] == "valid":
            d["valid"] += 1
        elif srow["status"] == "invalid":
            d["invalid"] += 1
        else:
            d["other"] += 1
    for st in checks:
        if st.scenario_id in scen:
            scen[st.scenario_id]["checks"].append(
                {
                    "check": st.check_id,
                    "rule": st.rule,
                    "severity": st.severity,
                    "kind": st.kind,
                    "fails": st.fails,
                    "n": st.n,
                    "label": st.label,
                    "examples": st.failing_runs[:3],
                }
            )

    status_counts: dict[str, int] = defaultdict(int)
    for srow in scenario_runs:
        status_counts[srow["status"]] += 1
    reps = latest_per_rep(scenario_runs)
    counts = {
        "jobs": len(reps),
        "valid": len(scored_runs),
        "attempts": dict(status_counts),
        "sim_excluded": len(excluded_sim),
    }
    sev_by_rule = {rid: info.severity for rid, info in rules_info.items()}
    scenario_rules = {
        s["scenario_id"]: set((s.get("scenario_snapshot") or {}).get("scenario", {}).get("rules") or [])
        for s in scenario_runs
    }
    problems = {
        "invalid_runs": [
            {
                "id": s["id"],
                "scenario": s["scenario_id"],
                "rep": s["rep"],
                "attempt": s["attempt"],
                "status": s["status"],
                "error": (s.get("error") or "")[:500],
            }
            for s in scenario_runs
            if s["status"] != "valid"
        ],
        "check_errors": [r for r in rows if r["verdict"] == "error" and r["kind"] != "judge"][:200],
        "judge_errors": [r for r in rows if r["verdict"] == "error" and r["kind"] == "judge"][:200],
        "judge_unclear": [r for r in rows if r["verdict"] == "unclear"][:200],
        "sim_leaks": [r for r in rows if r["kind"] == "sim" and r["verdict"] == "fail"],
        # reps whose latest attempt never became valid (after retries): their scenario contributes no evidence
        "failed_reps": [
            {
                "scenario": k[0],
                "rep": k[1],
                "status": s["status"],
                "error": (s.get("error") or "")[:300],
                "critical": any(sev_by_rule.get(r) == "critical" for r in scenario_rules.get(k[0], ())),
            }
            for k, s in sorted(reps.items())
            if s["status"] != "valid"
        ],
        "harness_rows": [r for r in rows if r["kind"] == "harness"][:200],
        "unresolved": [
            {
                "scenario": st.scenario_id,
                "check": st.check_id,
                "rule": st.rule,
                "severity": st.severity,
                "runs": st.unresolved_runs,
            }
            for st in checks
            if st.unresolved
        ],
        "judge_rows": sum(1 for r in rows if r["kind"] in ("judge", "sim")),
    }

    # Latency and tokens per message: scored runs only, so they describe the same runs as the rates.
    scored_turns = {sid: ts for sid, ts in turns_by_sr.items() if sid in valid_ids}
    lat = [t.get("latency_ms") or 0 for ts in scored_turns.values() for t in ts if t.get("latency_ms")]
    agent_tok: dict[str, int] = defaultdict(int)
    for ts in scored_turns.values():
        for t in ts:
            for k, v in (t.get("usage") or {}).items():
                if isinstance(v, (int, float)):
                    agent_tok[k] += int(v)
    msgs = sum(len(ts) for ts in scored_turns.values())
    cost = {
        "agent_messages": msgs,
        "agent_messages_all_attempts": sum(len(ts) for ts in turns_by_sr.values()),
        "agent_tokens": dict(agent_tok),
        "agent_tokens_per_message": {k: round(v / msgs) for k, v in agent_tok.items()} if msgs else {},
        "judge_tokens": usage.get("judge", {}),
        "sim_user_tokens": usage.get("sim_user", {}),
        "latency_ms": {
            "p50": round(percentile(lat, 0.5)),
            "p95": round(percentile(lat, 0.95)),
            "median": round(median(lat)) if lat else 0,
            "max": max(lat) if lat else 0,
        },
    }
    return Aggregate(
        checks, rules, areas, sorted(scen.values(), key=lambda d: d["id"]), counts, problems, cost, advisory
    )
