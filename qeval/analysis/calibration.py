"""Blind human calibration, judge stability and the trust decision (docs/eval-quality-fix-plan.md, issue 10).

- `make_pack`: a blind labelling pack (YAML) of saved verdicts. The reviewer sees the spec rule, the question
  being checked and the conversation; the eval's verdict, reason and confidence are kept in a separate key
  file. `mode="calibration"` balances passes and fails for judge rubrics; `mode="pass_audit"` samples passes
  only, to find failures the eval missed (issue 9).
- `score_pack`: agreement, Cohen's kappa, fail precision and fail recall per check, with hashes.
- `stability`: re-judge sampled items several times without the cache; share of items with identical verdicts.
- `decide_trust`: apply the plan's thresholds and write `trust/judge_trust.yaml`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

import yaml

from qeval.analysis.attempts import authoritative
from qeval.config import ROOT, Settings
from qeval.grading.grade import norm_runs_for
from qeval.judge import trust as T
from qeval.judge.inputs import build_inputs
from qeval.report.describe import describe
from qeval.scenario.loader import load_rubrics, load_rules
from qeval.store.repo import Repo

LABEL_DIR = ROOT / "labels" / "human"
KEY_DIR = LABEL_DIR / ".keys"  # the eval's verdicts; the reviewer does not open these
TRUST_DIR = ROOT / "trust"


def _h(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]


def _base(check_id: str) -> str:
    return check_id.split("#")[0]


def _matches(check_id: str, row: dict, wanted: str) -> bool:
    """`wanted` is a check id / rubric id, or `rule:W4` for every check scoring that rule."""
    if wanted.startswith("rule:"):
        return row["rule"] == wanted[5:]
    return _base(check_id) == wanted


def _candidates(
    repo: Repo, run_ids: list[str], wanted: str, verdicts: tuple[str, ...]
) -> list[tuple[str, dict]]:
    out = []
    for rid in run_ids:
        gid = repo.latest_grading(rid)
        if gid is None:
            continue
        valid = {s["id"] for s in authoritative(repo.scenario_runs(rid))}
        for r in repo.check_results(gid):
            if (
                r["scenario_run_id"] in valid
                and r["verdict"] in verdicts
                and _matches(r["check_id"], r, wanted)
            ):
                out.append((rid, r))
    return out


def make_pack(
    repo: Repo,
    run_ids: list[str],
    checks: list[str],
    *,
    n: int,
    seed: int,
    mode: str,
    name: str,
    label_dir: Path | None = None,
) -> tuple[Path, Path]:
    rules, rubrics = load_rules(), load_rubrics()
    rng = random.Random(seed)
    picked: list[tuple[str, dict]] = []
    for wanted in checks:
        if mode == "pass_audit":
            pool = _candidates(repo, run_ids, wanted, ("pass",))
            rng.shuffle(pool)
            picked += pool[:n]
        else:
            fails = _candidates(repo, run_ids, wanted, ("fail",))
            passes = _candidates(repo, run_ids, wanted, ("pass",))
            rng.shuffle(fails)
            rng.shuffle(passes)
            k = min(len(fails), n // 2)
            picked += fails[:k] + passes[: n - k]
    norm_cache: dict[str, dict] = {}
    items, keys = [], {}
    for rid, r in picked:
        if rid not in norm_cache:
            norm_cache[rid] = {sr["id"]: nr for sr, nr, _ in norm_runs_for(repo, rid) if nr is not None}
        nr = norm_cache[rid].get(r["scenario_run_id"])
        if nr is None:
            continue
        turn = next((t for t in nr.turns if t.idx == r["turn_idx"]), nr.turns[-1] if nr.turns else None)
        rule = rules.get(r["rule"])
        rub = rubrics.get(_base(r["check_id"]))
        d = describe(dict(r), rubrics, {"scenario": nr.scenario.model_dump(mode="json")})
        question = rub.question.strip() if rub else d.check_title
        item_id = _h([r["scenario_run_id"], r["turn_idx"], r["check_id"]])
        reply = turn.reply if turn else {}
        item = {
            "id": item_id,
            "check": r["check_id"],
            "rule": f"{r['rule']}: {rule.title if rule else ''}",
            "spec": rule.summary if rule else "",
            "question": " ".join(question.split()),
            "earlier_user_messages": [t.user_text for t in nr.turns if turn and t.idx < turn.idx][-3:],
            "user": turn.user_text if turn else "",
            "reply_kind": reply.get("kind"),
            "urgency": reply.get("urgency"),
            "reply": reply.get("message") or "",
            "label": None,  # fill in: pass | fail | unsure
            "note": "",
        }
        if _base(r["check_id"]) == "R3_causes_quality":
            item["possible_causes"] = reply.get("possible_causes")
            item["red_flags"] = reply.get("red_flags")
        fact = (r.get("evidence") or {}).get("fact")
        if fact:
            item["stored_fact"] = fact
        items.append(item)
        keys[item_id] = {
            "run_id": rid,
            "scenario_run_id": r["scenario_run_id"],
            "turn_idx": r["turn_idx"],
            "check_id": r["check_id"],
            "eval_verdict": r["verdict"],
            "kind": r["kind"],
            "rubric_hash": T.rubric_hash(rub) if rub else None,
        }
    label_dir = label_dir or LABEL_DIR
    key_dir = label_dir / ".keys"
    label_dir.mkdir(parents=True, exist_ok=True)
    key_dir.mkdir(parents=True, exist_ok=True)
    pack = {
        "pack": name,
        "mode": mode,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "seed": seed,
        "runs": run_ids,
        "checks": checks,
        "instructions": (
            "For each item decide whether the reply meets the spec rule for the question asked. "
            "Set label to pass, fail or unsure; add a note when useful. Judge only from the spec "
            "and the text shown. Do not open the .keys folder."
        ),
        "items": items,
    }
    pp = label_dir / f"{name}.yaml"
    pp.write_text(yaml.safe_dump(pack, sort_keys=False, allow_unicode=True, width=110))
    kp = key_dir / f"{name}.json"
    kp.write_text(json.dumps({"pack": name, "dataset_hash": _h(items), "keys": keys}, indent=1))
    return pp, kp


def _kappa(pairs: list[tuple[str, str]]) -> float:
    n = len(pairs)
    if not n:
        return 0.0
    po = sum(a == b for a, b in pairs) / n
    labels = {x for p in pairs for x in p}
    pe = sum((sum(a == lab for a, _ in pairs) / n) * (sum(b == lab for _, b in pairs) / n) for lab in labels)
    return 1.0 if pe == 1 else (po - pe) / (1 - pe)


def score_pack(pack_path: Path) -> dict:
    pack = yaml.safe_load(pack_path.read_text())
    key = json.loads((pack_path.parent / ".keys" / f"{pack['pack']}.json").read_text())
    by: dict[str, list[tuple[str, str]]] = defaultdict(list)
    hashes: dict[str, set] = defaultdict(set)
    unlabelled = 0
    for it in pack["items"]:
        lab = (it.get("label") or "").strip().lower()
        k = key["keys"].get(it["id"])
        if k is None:
            continue
        if lab not in ("pass", "fail"):
            unlabelled += 1
            continue
        c = _base(k["check_id"]) if k["kind"] == "judge" else k["check_id"].split("#")[0]
        by[c].append((lab, k["eval_verdict"]))  # (human, eval)
        if k.get("rubric_hash"):
            hashes[c].add(k["rubric_hash"])
    out = {}
    for c, pairs in sorted(by.items()):
        n = len(pairs)
        agree = sum(h == e for h, e in pairs) / n
        eval_fails = [h for h, e in pairs if e == "fail"]
        human_fails = [e for h, e in pairs if h == "fail"]
        out[c] = {
            "n": n,
            "human_fail": len(human_fails),
            "agreement": round(agree, 3),
            "kappa": round(_kappa(pairs), 3),
            "fail_precision": round(sum(h == "fail" for h in eval_fails) / len(eval_fails), 3)
            if eval_fails
            else None,
            "fail_recall": round(sum(e == "fail" for e in human_fails) / len(human_fails), 3)
            if human_fails
            else None,
            "missed_failures": sum(1 for h, e in pairs if h == "fail" and e == "pass"),
            "rubric_hashes": sorted(hashes[c]),
        }
    return {
        "pack": pack["pack"],
        "mode": pack["mode"],
        "dataset_hash": key["dataset_hash"],
        "unlabelled": unlabelled,
        "checks": out,
        "scored_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }


async def stability(
    repo: Repo,
    settings: Settings,
    run_ids: list[str],
    rubric_ids: list[str],
    *,
    n: int,
    repeats: int,
    seed: int,
) -> dict:
    from qeval.judge.client import Judge

    rubrics = load_rubrics()
    judge = Judge(
        repo,
        api_key=settings.require_anthropic_key(),
        model=settings.judge_model,
        effort=settings.judge_effort,
        rules=load_rules(),
        run_id=None,
        max_tokens_budget=settings.max_judge_tokens,
        concurrency=settings.judge_concurrency,
    )
    rng = random.Random(seed)
    norm_cache: dict[str, dict] = {}
    results = {}
    for rid_ in rubric_ids:
        rub = rubrics[rid_]
        pool = _candidates(repo, run_ids, rid_, ("pass", "fail"))
        rng.shuffle(pool)
        tasks = []
        for run_id, r in pool:
            if run_id not in norm_cache:
                norm_cache[run_id] = {
                    sr["id"]: nr for sr, nr, _ in norm_runs_for(repo, run_id) if nr is not None
                }
            nr = norm_cache[run_id].get(r["scenario_run_id"])
            if nr is None:
                continue
            turn = (
                next((t for t in nr.turns if t.idx == r["turn_idx"]), None)
                if r["turn_idx"] is not None
                else None
            )
            extra = {
                "fact_value": (r.get("evidence") or {}).get("fact", ""),
                "flag_reason": r.get("message") or "",
            }
            tasks.append(build_inputs(rub.inputs, nr, turn, extra))
            if len(tasks) >= n:
                break

        async def runs_for(inputs, rub=rub):
            return [(await judge.judge(rub, inputs, use_cache=False)).verdict for _ in range(repeats)]

        verdict_sets = await asyncio.gather(*(runs_for(i) for i in tasks))
        same = sum(1 for vs in verdict_sets if len(set(vs)) == 1 and "error" not in vs)
        results[rid_] = {
            "rubric_hash": T.rubric_hash(rub),
            "items": len(verdict_sets),
            "repeats": repeats,
            "identical": same,
            "stability": round(same / len(verdict_sets), 3) if verdict_sets else None,
            "verdicts": verdict_sets,
        }
    rec = {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": settings.judge_model,
        "seed": seed,
        "rubrics": results,
    }
    TRUST_DIR.mkdir(parents=True, exist_ok=True)
    path = TRUST_DIR / "stability.json"
    old = json.loads(path.read_text()) if path.exists() else {"rubrics": {}}
    old.setdefault("rubrics", {}).update(
        {k: {**v, "measured_at": rec["measured_at"], "model": rec["model"]} for k, v in results.items()}
    )
    path.write_text(json.dumps(old, indent=1))
    return rec


def decide_trust(
    calibration: dict, *, manifest_path: Path | None = None, stability_path: Path | None = None
) -> dict:
    """Apply the plan's thresholds to a scored calibration pack + stored stability; update the manifest."""
    rubrics, rules = load_rubrics(), load_rules()
    stability_path = stability_path or TRUST_DIR / "stability.json"
    stab = json.loads(stability_path.read_text()).get("rubrics", {}) if stability_path.exists() else {}
    manifest_path = manifest_path or T.TRUST_FILE
    manifest = yaml.safe_load(manifest_path.read_text()) if manifest_path.exists() else {}
    entries = manifest.get("rubrics") or {}
    decisions = {}
    for rid, m in calibration["checks"].items():
        rub = rubrics.get(rid)
        if rub is None:
            continue  # code checks are not governed by the manifest
        critical = (rub.severity or rules[rub.rule].severity) == "critical"
        current = T.rubric_hash(rub)
        st = stab.get(rid, {})
        reasons = []
        if current not in m["rubric_hashes"]:
            reasons.append("labels were made on a different rubric version")
        need = T.MIN_LABELS_CRITICAL if critical else T.MIN_LABELS
        if m["n"] < need:
            reasons.append(f"{m['n']} labels < {need}")
        if m["agreement"] < T.MIN_AGREEMENT:
            reasons.append(f"agreement {m['agreement']:.0%} < {T.MIN_AGREEMENT:.0%}")
        if m["fail_precision"] is not None and m["fail_precision"] < T.MIN_FAIL_PRECISION:
            reasons.append(f"fail precision {m['fail_precision']:.0%} < {T.MIN_FAIL_PRECISION:.0%}")
        need_recall = T.MIN_FAIL_RECALL_CRITICAL if critical else T.MIN_FAIL_RECALL
        if m["fail_recall"] is None:
            reasons.append("no human-labelled failures to measure recall")
        elif m["fail_recall"] < need_recall:
            reasons.append(f"fail recall {m['fail_recall']:.0%} < {need_recall:.0%}")
        if st.get("rubric_hash") != current or st.get("stability") is None:
            reasons.append("no stability check for this rubric version")
        elif st["stability"] < T.MIN_STABILITY:
            reasons.append(f"stability {st['stability']:.0%} < {T.MIN_STABILITY:.0%}")
        status = "advisory" if reasons else "scored"
        entries[rid] = {
            "status": status,
            "rubric_hash": current,
            "rubric_version": rub.version,
            "reason": "; ".join(reasons) if reasons else "",
            "labels": m["n"],
            "agreement": m["agreement"],
            "kappa": m["kappa"],
            "fail_precision": m["fail_precision"],
            "fail_recall": m["fail_recall"],
            "stability": st.get("stability"),
            "pack": calibration["pack"],
            "dataset_hash": calibration["dataset_hash"],
            "reviewed_at": calibration["scored_at"],
        }
        decisions[rid] = status
    header = (
        "# Which LLM-judge rubrics may affect scored rates, findings frequency and CI gates.\n"
        "# Written by `qeval trust` from blind human calibration (labels/human/*.yaml) and `qeval stability`.\n"
        "# A rubric missing here, or whose text changed since its record (rubric_hash), is advisory.\n"
    )
    manifest_path.write_text(header + yaml.safe_dump({"rubrics": entries}, sort_keys=True, width=110))
    return decisions
