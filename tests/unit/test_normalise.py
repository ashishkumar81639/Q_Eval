from tests.conftest import make_run


def test_trace_steps(run):
    t0 = run.turns[0]
    assert [[c.tool for c in s] for s in t0.trace.steps] == [
        ["get_subject", "open_or_continue_episode"],
        ["record_facts", "respond"],
    ]
    assert len(t0.trace.respond_calls) == 1
    assert t0.trace.errors == []


def test_members_and_refs(run):
    sv = run.turns[1].state
    riya = sv.ref_to_id["riya"]
    assert sv.members[riya].name == "Riya"
    assert sv.ref_to_id["self"] == "s_4f1c6589"
    assert sv.members["s_4f1c6589"].relation == "self"


def test_facts_this_turn(run):
    t0, t1 = run.turns
    assert {f["key"] for f in t0.state.facts_from_turn(t0.user_turn_id)} == {"fever_onset", "fever_duration"}
    assert {f["key"] for f in t1.state.facts_from_turn(t1.user_turn_id)} == {
        "temperature_measured",
        "fluid_intake",
        "alertness",
        "red_flag_present",
    }


def test_closed_by_platform(run):
    ep = run.turns[1].state.episodes["e_56601bda"]
    assert ep["status"] == "closed" and ep["closed_by"] == "platform"


def test_diff_turn0_opens_episode(run):
    d = run.turns[0].diff
    assert [e["id"] for e in d.opened_episodes] == ["e_698a51fa"]
    assert [e["id"] for e in d.closed_episodes] == ["e_56601bda"]
    assert len(d.new_facts) == 2


def test_diff_turn1(run):
    d = run.turns[1].diff
    assert len(d.new_facts) == 4
    assert len(d.new_assessments) == 1
    assert d.opened_episodes == []


def test_without_intermediate_state(dry):
    r = make_run(dry, with_state1=False)
    assert r.turns[0].state is None
    assert r.turns[1].prev_state is r.accounts["main"].initial
