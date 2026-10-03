from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from qeval.normalise.build import build_norm_run
from qeval.scenario.models import Scenario

ROOT = Path(__file__).resolve().parent.parent
DRY = ROOT / "dryrun"


def load(name: str) -> dict:
    return json.loads((DRY / name).read_text())


@pytest.fixture
def dry() -> dict:
    return {
        "create": load("01_create_account.response.json"),
        "t1": load("02_turn1_followup.response.json"),
        "t2": load("03_turn2_assessment.response.json"),
        "state2": load("04_state_after_turn2.json"),
        "greeting": load("05_greeting_info.response.json"),
    }


def make_run(dry: dict, *, scenario: Scenario | None = None, t1=None, t2=None, state2=None, with_state1=True):
    create = dry["create"]
    t1 = t1 or dry["t1"]
    t2 = t2 or dry["t2"]
    state2 = state2 or dry["state2"]
    # State after turn 1 = state after turn 2 minus turn-2 artefacts.
    s1 = copy.deepcopy(state2)
    t2_user = t2["user_turn_id"]
    s1["facts"] = [f for f in s1["facts"] if f.get("source_turn_id") != t2_user]
    s1["assessments"] = []
    s1["turns"] = [t for t in s1["turns"] if t["seq"] <= 2]
    scenario = scenario or Scenario(
        id="dry",
        title="dry run",
        rules=["TM1"],
        turns=[
            {"at": "2026-09-28T20:00:00+05:30", "user": "my daughter has had a fever since yesterday"},
            {
                "at": "2026-09-28T20:10:00+05:30",
                "user": "she has 103F, not drinking much, very sleepy and a bit hard to wake up",
            },
        ],
    )
    sr = {
        "id": "sr1",
        "rep": 1,
        "status": "valid",
        "time_shift_days": 0,
        "accounts": {"main": {"account_id": create["account_id"], "refs": create["refs"]}},
        "initial_states": {"main": create["state"]},
        "final_states": {"main": state2},
    }
    turns = [
        {
            "idx": 0,
            "account_key": "main",
            "actor": "script",
            "sent_text": scenario.turns[0].user,
            "received_at": "2026-09-28T20:00:00+05:30",
            "response": t1,
            "state_after": s1 if with_state1 else None,
        },
        {
            "idx": 1,
            "account_key": "main",
            "actor": "script",
            "sent_text": scenario.turns[1].user,
            "received_at": "2026-09-28T20:10:00+05:30",
            "response": t2,
            "state_after": state2,
        },
    ]
    return build_norm_run(scenario, sr, turns, "2026-09-28")


@pytest.fixture
def run(dry):
    return make_run(dry)
