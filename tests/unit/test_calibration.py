"""Blind label packs, calibration metrics and the trust decision (plan issues 9 and 10)."""

from __future__ import annotations

import json

import yaml

from qeval.analysis.calibration import decide_trust, make_pack, score_pack
from qeval.config import ROOT
from qeval.grading.grade import grade
from qeval.judge.trust import is_scored, load_trust, rubric_hash
from qeval.scenario.loader import load_rubrics, load_scenario
from qeval.store.backend import make_engine
from qeval.store.repo import Repo
from tests.fake_q import FakeQ
from tests.unit.test_review_fixes import run_scenarios, settings_for


async def test_pack_is_blind_and_scores(tmp_path):
    repo = Repo(make_engine(None, tmp_path))
    scs = [
        load_scenario(ROOT / "scenarios/safety/s_chest_pain_adult.yaml"),
        load_scenario(ROOT / "scenarios/general/g_greeting.yaml"),
    ]
    run_id = await run_scenarios(repo, tmp_path, scs, FakeQ(dose_in_reply=True), k=3)
    await grade(repo, run_id, settings_for(tmp_path), judge=False)
    pack_path, key_path = make_pack(
        repo,
        [run_id],
        ["S3_no_dose"],
        n=6,
        seed=1,
        mode="calibration",
        name="t",
        label_dir=tmp_path / "labels",
    )
    pack = yaml.safe_load(pack_path.read_text())
    text = pack_path.read_text()
    assert "eval_verdict" not in text and "verdict" not in json.dumps(pack["items"])  # blind
    assert len(pack["items"]) == 6 and all(it["label"] is None for it in pack["items"])
    assert all(it["spec"] and it["user"] for it in pack["items"])
    key = json.loads(key_path.read_text())["keys"]
    # a reviewer agrees on everything except one eval-pass they consider a failure
    first_pass = next(i for i in pack["items"] if key[i["id"]]["eval_verdict"] == "pass")
    for it in pack["items"]:
        it["label"] = "fail" if (key[it["id"]]["eval_verdict"] == "fail" or it is first_pass) else "pass"
    pack_path.write_text(yaml.safe_dump(pack, sort_keys=False))
    m = score_pack(pack_path)["checks"]["S3_no_dose"]
    assert m["n"] == 6 and m["missed_failures"] == 1
    assert m["fail_precision"] == 1.0 and 0 < m["fail_recall"] < 1


def test_trust_requires_every_threshold(tmp_path):
    r = load_rubrics()["R8_assessment_structure"]
    h = rubric_hash(r)
    good = {
        "n": 20,
        "agreement": 0.9,
        "kappa": 0.8,
        "fail_precision": 0.9,
        "fail_recall": 0.9,
        "missed_failures": 1,
        "rubric_hashes": [h],
        "human_fail": 10,
    }
    cal = {"pack": "p", "dataset_hash": "d", "scored_at": "now", "checks": {"R8_assessment_structure": good}}
    manifest, stab = tmp_path / "trust.yaml", tmp_path / "stability.json"
    stab.write_text(
        json.dumps({"rubrics": {"R8_assessment_structure": {"rubric_hash": h, "stability": 1.0}}})
    )
    assert decide_trust(cal, manifest_path=manifest, stability_path=stab) == {
        "R8_assessment_structure": "scored"
    }
    assert is_scored(r, load_trust(manifest))
    # too few labels -> advisory; stale stability -> advisory
    cal["checks"]["R8_assessment_structure"] = {**good, "n": 10}
    assert (
        decide_trust(cal, manifest_path=manifest, stability_path=stab)["R8_assessment_structure"]
        == "advisory"
    )
    stab.write_text(
        json.dumps({"rubrics": {"R8_assessment_structure": {"rubric_hash": "old", "stability": 1.0}}})
    )
    cal["checks"]["R8_assessment_structure"] = good
    assert (
        decide_trust(cal, manifest_path=manifest, stability_path=stab)["R8_assessment_structure"]
        == "advisory"
    )
    assert not is_scored(r, load_trust(manifest))


def test_editing_a_rubric_invalidates_its_trust():
    r = load_rubrics()["R8_assessment_structure"]
    trust = {"R8_assessment_structure": {"status": "scored", "rubric_hash": rubric_hash(r)}}
    assert is_scored(r, trust)
    edited = r.model_copy(update={"fail_if": r.fail_if + " Also X."})
    assert not is_scored(edited, trust)


def test_repo_trust_manifest_starts_empty():
    assert load_trust() == {}  # until a human calibration is recorded, every judge rubric is advisory
