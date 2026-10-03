import os

import pytest

from qeval.store.backend import make_engine
from qeval.store.repo import Repo


@pytest.fixture(params=["sqlite"] + (["postgres"] if os.environ.get("DATABASE_URL") else []))
def repo(request, tmp_path):
    url = None if request.param == "sqlite" else os.environ["DATABASE_URL"]
    return Repo(make_engine(url, tmp_path))


def test_roundtrip(repo):
    rid = repo.create_run(
        name="t", anchor_date="2026-10-01", agent_version={"model": "m"}, harness_sha="abc", config={"k": 2}
    )
    sr = repo.upsert_scenario_run(
        {
            "run_id": rid,
            "scenario_id": "s1",
            "scenario_hash": "h",
            "rep": 1,
            "attempt": 1,
            "kind": "scripted",
            "status": "running",
        }
    )
    repo.update_scenario_run(
        sr, accounts={"main": {"account_id": "acc"}}, final_states={"main": {"x": [1, 2]}}
    )
    repo.add_turn(
        {
            "scenario_run_id": sr,
            "idx": 0,
            "account_key": "main",
            "sent_text": "hi",
            "received_at": "2026-10-01T10:00:00+05:30",
            "response": {"respond": {"kind": "info"}},
        }
    )
    repo.set_scenario_run_status(sr, "valid")
    assert repo.completed_jobs(rid) == {("s1", "h", 1)}
    assert repo.turns(sr)[0]["response"]["respond"]["kind"] == "info"
    assert repo.scenario_runs(rid)[0]["final_states"] == {"main": {"x": [1, 2]}}
    g = repo.create_grading(rid, "v1", {}, "m", {})
    repo.add_check_results(
        g,
        [
            {
                "scenario_run_id": sr,
                "turn_idx": 0,
                "check_id": "c",
                "rule": "R1",
                "area": "reply",
                "severity": "major",
                "kind": "global",
                "verdict": "pass",
                "evidence": {},
                "message": "",
            }
        ],
    )
    assert repo.latest_grading(rid) is None  # still running: never used as "latest"
    repo.finish_grading(g, "done")
    assert repo.latest_grading(rid) == g
    assert repo.check_results(g)[0]["verdict"] == "pass"
    repo.judge_cache_put("k", "m", "r", 1, {"verdict": "pass"}, {})
    repo.judge_cache_put("k", "m", "r", 1, {"verdict": "fail"}, {})
    assert repo.judge_cache_get("k") == {"verdict": "pass"}
    repo.add_usage(rid, "agent", {"input_tokens": 3, "output_tokens": 4, "cache_read_input_tokens": 5})
    assert repo.usage_totals(rid)["agent"]["cache_read"] == 5
