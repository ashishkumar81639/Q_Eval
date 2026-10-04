"""`qeval findings`: build FINDINGS.md, findings.html and findings.json from stored gradings.

Nothing here is written by hand or by an LLM at report time. Words come from data files (`findings.yaml`,
`rules.yaml`, check and rubric titles); every number is computed from the gradings of the given runs.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, ConfigDict

from qeval.analysis.aggregate import wilson
from qeval.analysis.attempts import authoritative
from qeval.checks.registry import load_checks
from qeval.config import ROOT
from qeval.report.describe import describe
from qeval.scenario.loader import load_rubrics, load_rules
from qeval.store.repo import Repo

TEMPLATES = Path(__file__).parent / "templates"
STATUS_LABEL = {
    "confirmed": "Confirmed (deterministic checks)",
    "judge": "Judge-graded (calibrated rubric)",
    "observation": "Observation (lower confidence)",
}


class Breakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str | None = None  # only replies of this kind
    by: str  # reply field to group by
    count: str  # reply field whose values are counted


class FindingDef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    rules: list[str]
    status: str = "confirmed"
    scenarios: list[str]
    twins: list[str] = []
    breakdown: Breakdown | None = None
    cause: str = ""  # one line: the reviewer's hypothesis for the likely cause


class FindingsFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    findings: list[FindingDef]
    notes: list[str] = []


def load_findings(path: Path | None = None) -> FindingsFile:
    path = path or ROOT / "findings.yaml"
    return FindingsFile.model_validate(yaml.safe_load(path.read_text()))


def validate_findings(ff: FindingsFile, rules: dict, scenario_ids: set[str]) -> list[str]:
    errs, seen = [], set()
    for f in ff.findings:
        if f.id in seen:
            errs.append(f"findings.yaml: duplicate id {f.id!r}")
        seen.add(f.id)
        if f.status not in STATUS_LABEL:
            errs.append(f"findings.yaml {f.id}: status must be one of {sorted(STATUS_LABEL)}")
        for r in f.rules:
            if r not in rules:
                errs.append(f"findings.yaml {f.id}: unknown rule {r!r}")
        for s in f.scenarios + f.twins:
            if s not in scenario_ids:
                errs.append(f"findings.yaml {f.id}: unknown scenario {s!r}")
    return errs


# ---- per-run data -----------------------------------------------------------------------------------------


class RunData:
    """Everything one run contributes: scored scenario runs, their check rows and turns."""

    def __init__(self, repo: Repo, run_id: str):
        self.run_id = run_id
        run = repo.get_run(run_id)
        if run is None:
            raise SystemExit(f"unknown run {run_id}")
        self.run: dict = run
        gid = repo.latest_grading(run_id)
        if gid is None:
            raise SystemExit(f"run {run_id} has no completed grading; run `qeval grade {run_id}` first")
        self.grading_id: str = gid
        self.name = self.run.get("name") or run_id
        self.k = (self.run.get("config") or {}).get("k")
        self.srs = authoritative(repo.scenario_runs(run_id))
        self.sr_by_id = {s["id"]: s for s in self.srs}
        self.by_scenario: dict[str, list[dict]] = defaultdict(list)
        for s in self.srs:
            self.by_scenario[s["scenario_id"]].append(s)
        self.rows: dict[str, list[dict]] = defaultdict(list)
        for r in repo.check_results(self.grading_id):
            if r["scenario_run_id"] in self.sr_by_id:
                self.rows[r["scenario_run_id"]].append(r)
        self.turns = repo.turns_for_run(run_id)

    def names(self, sr: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for st in (sr.get("final_states") or {}).values():
            for s in st.get("subjects") or []:
                out[s["id"]] = s.get("name") or s.get("relation") or s["id"]
            for e in st.get("episodes") or []:
                out[e["id"]] = f"{e.get('complaint')} episode"
        return out

    def failing_rows(self, sr_id: str, rules: list[str]) -> list[dict]:
        return [
            r
            for r in self.rows.get(sr_id, [])
            if r["verdict"] == "fail" and r["rule"] in rules and r["kind"] not in ("sim", "harness")
        ]

    def scenario_rate(self, scenario_id: str, rules: list[str]) -> dict | None:
        srs = self.by_scenario.get(scenario_id) or []
        if not srs:
            return None
        fails = [s for s in srs if self.failing_rows(s["id"], rules)]
        lo, hi = wilson(len(fails), len(srs))
        return {
            "fails": len(fails),
            "n": len(srs),
            "ci": [round(lo, 3), round(hi, 3)],
            "failing_runs": [s["id"] for s in fails],
        }

    def breakdown(self, b: Breakdown) -> list[dict]:
        c: Counter = Counter()
        for sr in self.srs:
            for t in self.turns.get(sr["id"], []):
                rep = (t.get("response") or {}).get("respond") or {}
                if b.kind and rep.get("kind") != b.kind:
                    continue
                c[(str(rep.get(b.by)), json.dumps(rep.get(b.count)))] += 1
        groups: dict[str, dict] = defaultdict(dict)
        for (g, v), n in c.items():
            groups[g][json.loads(v) if v != "null" else None] = n
        return [
            {
                "group": g,
                "counts": {str(k): n for k, n in sorted(v.items(), key=lambda x: str(x[0]))},
                "total": sum(v.values()),
            }
            for g, v in sorted(groups.items())
        ]


MAX_EXAMPLES = 6
MAX_TWIN_EXAMPLES = 3


def _turn_evidence(rd: RunData, sr: dict, turn: dict | None) -> dict:
    """What happened on one turn, in plain fields (no ids needed to read it)."""
    names = rd.names(sr)
    ts = rd.turns.get(sr["id"], [])
    resp = (turn or {}).get("response") or {}
    reply = resp.get("respond") or {}
    earlier = [t for t in ts if turn is not None and t["idx"] < turn["idx"]][-3:]
    tools = [e.get("tool") for e in resp.get("trace") or [] if e.get("type") == "tool_call"]
    return {
        "scenario": sr["scenario_id"],
        "rep": sr["rep"],
        "scenario_run_id": sr["id"],
        "turn_idx": (turn or {}).get("idx"),
        "earlier_user_messages": [t.get("sent_text", "") for t in earlier],
        "user": (turn or {}).get("sent_text", ""),
        "reply_kind": reply.get("kind"),
        "urgency": reply.get("urgency"),
        "escalate_to_doctor": reply.get("escalate_to_doctor"),
        "about": [names.get(x, x) for x in reply.get("subject_ids") or []],
        "reply": " ".join((reply.get("message") or "").split())[:1500],
        "tools": tools,
    }


def _failing_example(rd: RunData, sr_id: str, rules: list[str], rubrics: dict, current: dict) -> dict | None:
    rows = rd.failing_rows(sr_id, rules)
    if not rows:
        return None
    rows.sort(
        key=lambda r: (
            {"global": 0, "expect": 1, "persona": 2, "judge": 3}.get(r["kind"], 9),
            r["turn_idx"] if r["turn_idx"] is not None else 999,
        )
    )
    row = dict(rows[0])
    sr = rd.sr_by_id[sr_id]
    d = describe(row, rubrics, sr.get("scenario_snapshot"), rd.names(sr), current=current)
    ts = rd.turns.get(sr_id, [])
    turn = next((t for t in ts if t["idx"] == row["turn_idx"]), None) if row["turn_idx"] is not None else None
    turn = turn or (ts[-1] if ts else None)
    return {**_turn_evidence(rd, sr, turn), "check": d.check_title, "got": d.got, "run": rd.name}


def _passing_example(rd: RunData, sr: dict, rules: list[str]) -> dict | None:
    if rd.failing_rows(sr["id"], rules):
        return None
    ts = rd.turns.get(sr["id"], [])
    if not ts:
        return None
    return {**_turn_evidence(rd, sr, ts[0]), "run": rd.name}


def collect_examples(runs: list[RunData], f: FindingDef, rubrics: dict, current: dict) -> tuple[list, list]:
    """Failing examples (E1..), one per scenario first, latest run preferred; passing twins (T1..)."""
    examples: list[dict] = []
    for sid in f.scenarios:
        for rd in reversed(runs):
            ex = next(
                (
                    e
                    for sr in rd.by_scenario.get(sid, [])
                    if (e := _failing_example(rd, sr["id"], f.rules, rubrics, current))
                ),
                None,
            )
            if ex:
                examples.append(ex)
                break
        if len(examples) >= MAX_EXAMPLES:
            break
    twins: list[dict] = []
    for sid in f.twins:
        for rd in reversed(runs):
            ex = next(
                (e for sr in rd.by_scenario.get(sid, []) if (e := _passing_example(rd, sr, f.rules))), None
            )
            if ex:
                twins.append(ex)
                break
        if len(twins) >= MAX_TWIN_EXAMPLES:
            break
    for i, e in enumerate(examples, 1):
        e["id"] = f"E{i}"
    for i, e in enumerate(twins, 1):
        e["id"] = f"T{i}"
    return examples, twins


def build_findings(repo: Repo, run_ids: list[str], findings_path: Path | None = None) -> dict:
    load_checks()
    rules = load_rules()
    rubrics = load_rubrics()
    ff = load_findings(findings_path)
    from qeval.scenario.loader import load_scenario, yaml_files

    current = {}
    for path in yaml_files([ROOT / "scenarios"]):
        sc = load_scenario(path)
        current[sc.id] = sc
    errs = validate_findings(ff, rules, set(current))
    if errs:
        raise SystemExit("\n".join(errs))
    runs = [RunData(repo, r) for r in run_ids]
    titles = {sid: sc.title for sid, sc in current.items()}

    out_findings: list[dict[str, Any]] = []
    for f in ff.findings:
        per_scenario: list[dict[str, Any]] = []
        for sid in f.scenarios:
            cells = [rd.scenario_rate(sid, f.rules) for rd in runs]
            per_scenario.append({"id": sid, "title": titles.get(sid, sid), "runs": cells})
        twins: list[dict[str, Any]] = []
        for sid in f.twins:
            cells = [rd.scenario_rate(sid, f.rules) for rd in runs]
            twins.append({"id": sid, "title": titles.get(sid, sid), "runs": cells})
        examples, twin_examples = collect_examples(runs, f, rubrics, current)
        for e in examples + twin_examples:
            e["scenario_title"] = titles.get(e["scenario"], e["scenario"])
        breakdown = None
        if f.breakdown:
            merged: dict[str, Counter] = defaultdict(Counter)
            for rd in runs:
                for g in rd.breakdown(f.breakdown):
                    merged[g["group"]].update(g["counts"])
            breakdown = {
                "by": f.breakdown.by,
                "count": f.breakdown.count,
                "kind": f.breakdown.kind,
                "rows": [
                    {"group": g, "counts": dict(c), "total": sum(c.values())}
                    for g, c in sorted(merged.items())
                ],
            }
        # headline: the largest-n run's pooled failures over the finding's scenarios
        best: dict[str, Any] | None = None
        for i, rd in enumerate(runs):
            present: list[dict[str, Any]] = [s["runs"][i] for s in per_scenario if s["runs"][i]]
            if not present:
                continue
            fails, n = sum(c["fails"] for c in present), sum(c["n"] for c in present)
            if best is None or n > best["n"]:
                best = {"run": rd.name, "fails": fails, "n": n, "ci": [round(x, 3) for x in wilson(fails, n)]}
        out_findings.append(
            {
                "id": f.id,
                "title": f.title,
                "status": f.status,
                "status_label": STATUS_LABEL[f.status],
                "rules": [
                    {
                        "id": r,
                        "title": rules[r].title,
                        "expected": rules[r].expected,
                        "severity": rules[r].severity,
                    }
                    for r in f.rules
                ],
                "severity": min(
                    (rules[r].severity for r in f.rules),
                    key=lambda s: {"critical": 0, "major": 1, "minor": 2}[s],
                ),
                "scenarios": per_scenario,
                "twins": twins,
                "example": examples[0] if examples else None,
                "examples": examples,
                "twin_examples": twin_examples,
                "breakdown": breakdown,
                "headline": best,
                "cause": f.cause,
            }
        )
    sev_order = {"critical": 0, "major": 1, "minor": 2}
    status_order = {"confirmed": 0, "judge": 1, "observation": 2}
    out_findings.sort(key=lambda x: (status_order[x["status"]], sev_order[x["severity"]]))
    return {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "runs": [
            {
                "run_id": rd.run_id,
                "name": rd.name,
                "k": rd.k,
                "grading_id": rd.grading_id,
                "agent_version": rd.run.get("agent_version"),
                "harness_sha": rd.run.get("harness_sha"),
                "scored_runs": len(rd.srs),
            }
            for rd in runs
        ],
        "findings": out_findings,
        "notes": ff.notes,
    }


def write_findings(data: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = lambda x: f"{x * 100:.0f}%"
    paths = []
    p = out_dir / "findings.json"
    p.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str))
    paths.append(p)
    p = out_dir / "FINDINGS.md"
    p.write_text(env.get_template("findings.md.j2").render(d=data))
    paths.append(p)
    p = out_dir / "findings.html"
    p.write_text(env.get_template("findings.html.j2").render(d=data, standalone=True))
    paths.append(p)
    # Same page without the html/head/body wrapper, for hosts that add their own (e.g. a published artifact).
    p = out_dir / "findings.fragment.html"
    p.write_text(env.get_template("findings.html.j2").render(d=data, standalone=False))
    paths.append(p)
    return paths
