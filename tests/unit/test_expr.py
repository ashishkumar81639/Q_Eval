import pytest

from qeval.checks.expectations import eval_one
from qeval.checks.expr.parser import ExprSyntaxError, parse_expectation, split_comment

T0_PASS = [
    "reply.kind == 'followup'",
    "reply.kind in ['followup', 'assessment']",
    "reply.subject_ids[0] == ref('riya')",
    "reply.subject_ids == [ref('riya')]",
    "state.facts(subject=ref('riya'), category='episode').any(value ~ date_re(-1))",
    "state.facts(subject=ref('riya')).none(value ~ '\\\\byesterday\\\\b')",
    "trace.called('get_subject') and trace.not_called('close_episode')",
    "trace.before('get_subject', 'respond')",
    "not trace.called('create_subject')",
    "diff.opened_episodes.count == 1",
    "diff.closed_episodes.first.closed_by == 'platform'",
    "state.episodes(subject=ref('riya'), status='open').count == 1",
    "state.subjects.count == 2",
    "member(name='riya') == ref('riya')",
    "len(reply.missing_info) >= 1",
    "reply.urgency == null",
    "reply.message ~ 'temperature'",
    "lower(reply.message) ~ 'riya'",
    "questions(reply.message) == 5",
    "state.facts(this_turn=true).count == 2",
    "state.facts(category='family_history').first.subject_id == null",
    "prev_state.episodes(status='open').count == 1",
    "reply.kind == 'clarify' or reply.kind == 'followup'",
    "state.facts(subject=ref('riya')).where(category == 'profile').values == ['peanuts']",
    "reply.urgency not in ['self_care', 'see_doctor_soon']",
    "user ~ 'since yesterday'",
]

T1_PASS = [
    "reply.urgency == 'urgent' and reply.escalate_to_doctor == true",
    "turn[1].reply.episode_id == turn[0].reply.episode_id",
    "reply.episode_id == turn[0].reply.episode_id",
    "state.assessments(subject=ref('riya')).count == 1",
    "first_sentences(reply.message, 2) ~ 'urgent'",
    "state.facts(subject=ref('riya'), category='episode').any(value ~ '103')",
    "diff.new_facts.count == 4",
    "turns.count == 2",
    "turn[-1].reply.kind == 'assessment'",
]


@pytest.mark.parametrize("src", T0_PASS)
def test_turn0_pass(run, src):
    v, ev, msg = eval_one(src, run, run.turns[0])
    assert v == "pass", (src, ev, msg)


@pytest.mark.parametrize("src", T1_PASS)
def test_turn1_pass(run, src):
    v, ev, msg = eval_one(src, run, run.turns[1])
    assert v == "pass", (src, ev, msg)


def test_fail_with_evidence(run):
    v, ev, _ = eval_one("reply.urgency == 'see_doctor_soon'", run, run.turns[1])
    assert v == "fail"
    c = ev["comparisons"][0]
    assert (
        c["left"] == "urgent"
        and c["right"] == "see_doctor_soon"
        and c["expr"] == "reply.urgency == 'see_doctor_soon'"
    )


def test_any_evidence(run):
    v, ev, _ = eval_one("state.facts(subject=ref('riya')).any(value ~ 'rash')", run, run.turns[1])
    assert v == "fail"
    assert ev["comparisons"][0]["items"]


def test_runtime_error(run):
    v, ev, msg = eval_one("turn[5].reply.kind == 'info'", run, run.turns[0])
    assert v == "error" and "out of range" in msg
    v, _, msg = eval_one("ref('nobody') == 'x'", run, run.turns[0])
    assert v == "error" and "unknown ref" in msg


def test_final(run):
    v, ev, _ = eval_one("state.episodes(subject=ref('riya')).count == 2", run, run.turns[-1], final=True)
    assert v == "pass", ev


def test_rule_comment():
    assert split_comment("reply.kind == 'clarify'   # W4") == ("reply.kind == 'clarify'", "W4")
    parse_expectation("reply.kind == 'clarify'   # W4")


@pytest.mark.parametrize("bad", ["reply.kind ==", "reply.kind = 'x'", "foo(", "", "reply..kind"])
def test_syntax_errors(bad):
    with pytest.raises(ExprSyntaxError):
        parse_expectation(bad)


def test_no_python_eval(run):
    v, _, msg = eval_one("reply.__class__ == 'x'", run, run.turns[0])
    # dicts return None for unknown keys; objects refuse private attributes
    v2, _, msg2 = eval_one("trace._t == null", run, run.turns[0])
    assert v2 == "error" and "not allowed" in msg2
