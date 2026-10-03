"""Run -> grade -> report against a fake Q API, on SQLite, without the judge."""

import json

import httpx

from qeval import timeutil
from qeval.client import QClient
from qeval.config import ROOT, Settings
from qeval.grading.grade import grade
from qeval.report.build import build_report
from qeval.runner.context import Budget, RunContext, SendLimiter
from qeval.runner.planner import plan_jobs
from qeval.runner.pool import run_pool
from qeval.scenario.validator import build_catalog
from qeval.store.backend import make_engine
from qeval.store.export import export_run, import_run
from qeval.store.repo import Repo
from tests.fake_q import FakeQ

SCEN = [
    ROOT / "scenarios/safety/s_chest_pain_adult.yaml",
    ROOT / "scenarios/general/g_greeting.yaml",
    ROOT / "scenarios/who/w_switch_members_no_mixing.yaml",
    ROOT / "scenarios/isolation/i_other_account_ids.yaml",
]


async def _run(tmp_path, fake: FakeQ, k=2):
    settings = Settings(QEVAL_TOKEN="t", QEVAL_RUNS_DIR=tmp_path, QEVAL_MIN_SEND_INTERVAL_MS=0)
    repo = Repo(make_engine(None, tmp_path))
    cat = build_catalog(SCEN)
    assert cat.ok, cat.errors
    client = QClient("https://fake", "t", transport=httpx.MockTransport(fake.handler))
    run_id = repo.create_run(
        name="e2e",
        anchor_date=timeutil.today_ist(),
        agent_version={"model": "fake"},
        harness_sha="x",
        config={"k": k},
    )
    ctx = RunContext(
        settings=settings,
        client=client,
        repo=repo,
        run_id=run_id,
        anchor=timeutil.today_ist(),
        limiter=SendLimiter(0),
        budget=Budget(1000, 10**9),
    )
    status = await run_pool(plan_jobs(cat.scenarios, k), ctx, concurrency=3, show_progress=False)
    repo.finish_run(run_id, status)
    await client.aclose()
    return settings, repo, run_id, ctx


async def test_end_to_end(tmp_path):
    fake = FakeQ(dose_in_reply=True)
    settings, repo, run_id, ctx = await _run(tmp_path, fake)
    srs = repo.scenario_runs(run_id)
    assert len(srs) == 8 and all(s["status"] == "valid" for s in srs)
    gid = await grade(repo, run_id, settings, judge=False)
    rows = repo.check_results(gid)
    by = {(r["check_id"], r["verdict"]) for r in rows}
    assert ("S3_no_dose", "fail") in by  # planted dose in the fake's emergency reply
    assert ("R1_single_final_respond", "pass") in by
    assert ("I1_other_account_unchanged", "pass") in by
    # re-grading gives identical deterministic results
    gid2 = await grade(repo, run_id, settings, judge=False)

    def strip(rs):
        return sorted(
            (
                r["scenario_run_id"],
                -1 if r["turn_idx"] is None else r["turn_idx"],
                r["check_id"],
                r["verdict"],
            )
            for r in rs
        )

    assert strip(repo.check_results(gid2)) == strip(rows)
    out, code = build_report(repo, run_id, gid, out_dir=tmp_path / "report")
    res = json.loads((out / "results.json").read_text())
    assert code == 1  # critical S3 failure
    s3 = next(r for r in res["rules"] if r["rule"] == "S3")
    assert s3["fails"] == 2 and s3["n"] >= 2
    html = (out / "report.html").read_text()
    assert "S3_no_dose" in html and "500 mg" in html
    assert (out / "junit.xml").read_text().startswith("<?xml")
    # portable export re-imports into a fresh SQLite database and re-grades identically
    exp = export_run(repo, run_id, tmp_path / "export.jsonl.gz")
    repo2 = Repo(make_engine(None, tmp_path / "other"))
    assert import_run(repo2, exp) == run_id
    gid3 = await grade(repo2, run_id, settings, judge=False)
    assert strip(repo2.check_results(gid3)) == strip(rows)


async def test_502_retries_on_new_account(tmp_path):
    fake = FakeQ(fail_first_message=True)
    _, repo, run_id, ctx = await _run(tmp_path, fake, k=1)
    srs = repo.scenario_runs(run_id)
    statuses = sorted(s["status"] for s in srs)
    assert statuses.count("invalid") == 1 and statuses.count("valid") == 4
    inv = next(s for s in srs if s["status"] == "invalid")
    retry = next(s for s in srs if s["scenario_id"] == inv["scenario_id"] and s["attempt"] == 2)
    assert retry["status"] == "valid"
    assert retry["accounts"] != inv["accounts"]


def test_broken_scenario_rejected(tmp_path):
    bad = tmp_path / "bad_one.yaml"
    bad.write_text("""
id: bad_one
title: broken
rules: [S99]
seed:
  subjects:
    - {ref: a, relation: son}
    - {ref: a, relation: daughter}
turns:
  - at: "2026-09-28T20:00:00"
    user: "hi {{B.refs.x}}"
    expect: ["reply.kind =="]
    judge: [NoSuchRubric]
""")
    cat = build_catalog([bad])
    msgs = "\n".join(cat.errors)
    assert "unknown rule 'S99'" in msgs
    assert "duplicate refs" in msgs
    assert "bad time" in msgs
    assert "cannot parse" in msgs
    assert "unknown rubric" in msgs
    assert "unknown account" in msgs
