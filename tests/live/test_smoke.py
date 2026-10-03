"""Opt-in live smoke tests: one scripted and one simulated-user scenario at k=1 (`make smoke`)."""

from __future__ import annotations

import pytest

from qeval.cli import _run_async
from qeval.config import ROOT, get_settings
from qeval.grading.grade import grade
from qeval.scenario.validator import build_catalog
from qeval.store.backend import make_engine
from qeval.store.repo import Repo


async def _smoke(tmp_path, scenario_file: str) -> tuple[Repo, str]:
    s = get_settings().model_copy(update={"runs_dir": tmp_path, "database_url": None})
    cat = build_catalog([ROOT / scenario_file])
    assert cat.ok, cat.errors
    run_id = await _run_async(
        s, cat, k=1, concurrency=1, name="smoke", resume=None, cleanup=True, progress=False
    )
    repo = Repo(make_engine(None, tmp_path))
    return repo, run_id


@pytest.mark.asyncio
async def test_smoke_scripted(tmp_path):
    repo, run_id = await _smoke(tmp_path, "scenarios/general/g_greeting.yaml")
    srs = repo.scenario_runs(run_id)
    assert [s["status"] for s in srs] == ["valid"]
    gid = await grade(repo, run_id, get_settings(), judge=False)
    rows = repo.check_results(gid)
    assert any(r["check_id"] == "R1_single_final_respond" and r["verdict"] == "pass" for r in rows)


@pytest.mark.asyncio
async def test_smoke_persona(tmp_path):
    repo, run_id = await _smoke(tmp_path, "scenarios/persona/p_adult_cold.yaml")
    srs = repo.scenario_runs(run_id)
    assert srs and all(s["status"] == "valid" for s in srs)
    turns = repo.turns(srs[0]["id"])
    assert len(turns) >= 2 and any(t["actor"] == "sim_user" for t in turns)
