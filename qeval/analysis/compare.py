"""Baseline-vs-new comparison of two gradings of the same run (docs/eval-quality-fix-plan.md, phase 5).

Keyed by (scenario run, turn, check). Lists every result whose verdict or scored/advisory status changed,
with the reason the harness can attribute: rubric rewritten, scenario relabelled, made advisory, check
renamed, added or removed. Nothing here edits either grading.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from qeval.store.repo import Repo

# Old check id -> new check id, for checks renamed by a fix (so their verdicts still line up).
CHECK_ALIASES = {"R1_single_final_respond": "R1_valid_reply"}


def _key(r: dict) -> tuple:
    cid = CHECK_ALIASES.get(r["check_id"], r["check_id"])
    return (r["scenario_run_id"], -1 if r["turn_idx"] is None else r["turn_idx"], cid)


def _scored(r: dict) -> bool:
    return r.get("scored") is not False


def compare(repo: Repo, run_id: str, base_gid: str, new_gid: str) -> dict:
    base_g, new_g = repo.get_grading(base_gid), repo.get_grading(new_gid)
    if not base_g or not new_g:
        raise SystemExit("unknown grading id")
    if base_g["run_id"] != run_id or new_g["run_id"] != run_id:
        raise SystemExit("both gradings must belong to the given run")
    base = {_key(r): r for r in repo.check_results(base_gid)}
    new = {_key(r): r for r in repo.check_results(new_gid)}
    scen = {s["id"]: s["scenario_id"] for s in repo.scenario_runs(run_id)}
    relabelled = set(((new_g.get("options") or {}).get("relabelled_scenarios")) or [])
    bv, nv = base_g.get("rubric_versions") or {}, new_g.get("rubric_versions") or {}

    changes = []
    for k in sorted(set(base) | set(new), key=str):
        b, n = base.get(k), new.get(k)
        if b and n and b["verdict"] == n["verdict"] and _scored(b) == _scored(n):
            continue
        # Not a real change: "not applicable" vs absent, or purely informational rows (never scored).
        bverd, nverd = (b or {}).get("verdict", "n/a"), (n or {}).get("verdict", "n/a")
        if bverd == nverd == "n/a" or "info" in (bverd, nverd):
            continue
        if b is None and nverd == "pass":  # a newly added check that passes is not a changed verdict
            continue
        r: dict = n if n is not None else (b or {})
        cid = k[2]
        base_rubric = cid.split("#")[0]
        reasons = []
        if b is None:
            reasons.append("new check")
        elif n is None:
            reasons.append("check no longer runs here")
        if b and n and b["check_id"] != n["check_id"]:
            reasons.append(f"check renamed ({b['check_id']} -> {n['check_id']})")
        if r["kind"] == "judge" and bv.get(base_rubric) != nv.get(base_rubric):
            reasons.append(f"rubric rewritten (v{bv.get(base_rubric)} -> v{nv.get(base_rubric)})")
        if scen.get(k[0]) in relabelled:
            reasons.append("scenario relabelled")
        if b and n and _scored(b) != _scored(n):
            reasons.append("now advisory" if not _scored(n) else "now scored")
        if not reasons:
            reasons.append("check logic changed")
        changes.append(
            {
                "scenario": scen.get(k[0], k[0]),
                "scenario_run_id": k[0],
                "turn_idx": None if k[1] == -1 else k[1],
                "check": cid,
                "rule": r["rule"],
                "severity": r["severity"],
                "kind": r["kind"],
                "before": b["verdict"] if b else None,
                "after": n["verdict"] if n else None,
                "before_scored": _scored(b) if b else None,
                "after_scored": _scored(n) if n else None,
                "reasons": reasons,
                "after_message": (n or {}).get("message", "")[:300],
            }
        )

    for c in changes:
        c["verdict_changed"] = c["before"] != c["after"]
    summary: dict[str, Counter] = defaultdict(Counter)
    for c in changes:
        summary[c["check"].split("#")[0]][
            f"{c['before']} -> {c['after']}" + ("" if c["after_scored"] in (None, True) else " (advisory)")
        ] += 1
    return {
        "run_id": run_id,
        "baseline_grading": base_gid,
        "new_grading": new_gid,
        "baseline_rubric_versions": bv,
        "new_rubric_versions": nv,
        "relabelled_scenarios": sorted(relabelled),
        "changed": len(changes),
        "verdict_changed": sum(1 for c in changes if c["verdict_changed"]),
        "status_only_changed": sum(1 for c in changes if not c["verdict_changed"]),
        "critical_changed": sum(1 for c in changes if c["severity"] == "critical" and c["verdict_changed"]),
        "summary": {k: dict(v) for k, v in sorted(summary.items())},
        "changes": changes,
    }


def write_comparison(data: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"compare_{data['run_id']}_{data['baseline_grading']}_vs_{data['new_grading']}"
    pj = out_dir / f"{stem}.json"
    pj.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    lines = [
        f"# Grading comparison for run `{data['run_id']}`",
        "",
        f"Baseline `{data['baseline_grading']}` vs new `{data['new_grading']}`: {data['verdict_changed']} verdicts "
        f"changed ({data['critical_changed']} on critical safety checks); {data['status_only_changed']} kept their "
        f"verdict but became advisory or scored. The baseline grading is unchanged in the database.",
        "",
    ]
    if data["relabelled_scenarios"]:
        lines += [
            f"Relabelled scenarios (same conversation, current labels): {', '.join(data['relabelled_scenarios'])}",
            "",
        ]

    def table(title: str, rows: list[dict]) -> list[str]:
        cnt: dict[str, Counter] = defaultdict(Counter)
        for c in rows:
            adv = "" if c["after_scored"] in (None, True) else " (advisory)"
            cnt[c["check"].split("#")[0]][f"{c['before']} -> {c['after']}{adv}"] += 1
        out = [f"## {title}", "", "| Check | Change | Count |", "|---|---|---|"]
        for chk, counts in sorted(cnt.items()):
            for change, n in sorted(counts.items(), key=lambda x: -x[1]):
                out.append(f"| `{chk}` | {change} | {n} |")
        return out + [""]

    lines += table(
        f"Verdict changes ({data['verdict_changed']})", [c for c in data["changes"] if c["verdict_changed"]]
    )
    lines += table(
        f"Same verdict, only scored/advisory status changed ({data['status_only_changed']})",
        [c for c in data["changes"] if not c["verdict_changed"]],
    )
    crit = [c for c in data["changes"] if c["severity"] == "critical" and c["verdict_changed"]]
    lines += [
        "",
        f"## Critical (safety) changes ({len(crit)}) — review each",
        "",
        "| Scenario | Turn | Check | Before → after | Why | New verdict's reason |",
        "|---|---|---|---|---|---|",
    ]
    for c in crit:
        after = f"{c['after']}{'' if c['after_scored'] in (None, True) else ' (advisory)'}"
        msg = c["after_message"].replace("|", "/").replace("\n", " ")[:160]
        lines.append(
            f"| {c['scenario']} | {c['turn_idx']} | `{c['check']}` | {c['before']} → {after} | "
            f"{'; '.join(c['reasons'])} | {msg} |"
        )
    pm = out_dir / f"{stem}.md"
    pm.write_text("\n".join(lines) + "\n")
    return [pj, pm]
