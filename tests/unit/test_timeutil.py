import re
from datetime import date, datetime

import pytest

from qeval import timeutil as tu
from qeval.runner.executor import resolve_turn_times
from qeval.scenario.templating import TemplateError, render

A = date(2026, 10, 1)  # Thu


def test_resolve_relative():
    assert tu.iso(tu.resolve_time("-3d 20:00", A)) == "2026-09-28T20:00:00+05:30"
    assert tu.iso(tu.resolve_time("0d 09:00", A)) == "2026-10-01T09:00:00+05:30"
    assert tu.iso(tu.resolve_time("-0d 09:00", A, shift_days=1)) == "2026-09-30T09:00:00+05:30"


def test_resolve_absolute_and_utc():
    assert tu.iso(tu.resolve_time("2026-09-28T14:30:00+00:00", A)) == "2026-09-28T20:00:00+05:30"
    with pytest.raises(ValueError):
        tu.resolve_time("2026-09-28T20:00:00", A)


def test_future_guard():
    now = datetime(2026, 10, 1, 8, 0, tzinfo=tu.IST)
    times, shift = resolve_turn_times(["-3d 20:00", "0d 09:00"], A, now=now)
    assert shift == 1
    assert tu.iso(times[1]) == "2026-09-30T09:00:00+05:30"
    assert (times[1] - times[0]).total_seconds() == (3 * 24 - 11) * 3600
    _, shift = resolve_turn_times(["-3d 20:00", "0d 07:00"], A, now=now)
    assert shift == 0


def test_default_turn_times():
    now = datetime(2026, 10, 1, 12, 0, tzinfo=tu.IST)
    times, shift = resolve_turn_times([None, None], A, now=now)
    assert shift == 0 and (times[1] - times[0]).total_seconds() == 120 and times[1] < now


def test_resolve_date():
    assert tu.resolve_date("-7w", A) == date(2026, 8, 13)
    assert tu.resolve_date("-26m", A) == date(2024, 8, 1)
    assert tu.resolve_date("-40y", A) == date(1986, 10, 1)


def test_spellings_and_regex():
    d = date(2026, 9, 27)
    sp = tu.date_spellings(d)
    assert sp[0] == "Sun 27 Sep 2026" and "27/09" in sp and "September 27" in sp
    rx = re.compile(tu.date_regex(d), re.I)
    for s in sp + ["since about Sun, 27 Sep", "27th September", "Sept 27"]:
        assert rx.search(s), s
    for s in ["28 Sep 2026", "Sun 2 Sep", "127 Sep", "27 Oct"]:
        assert not rx.search(s), s


def test_fmt_ist():
    assert tu.fmt_ist(tu.to_ist("2026-09-28T14:40:00+00:00")) == "Mon 28 Sep 2026, 8:10 PM IST"


def test_templates():
    accounts = {"A": {"account_id": "acc_1", "refs": {"riya": "s_9"}}}
    assert render("{{t:-1d 10:00}}", anchor=A) == "2026-09-30T10:00:00+05:30"
    assert render("{{date:-7w}}", anchor=A) == "2026-08-13"
    assert (
        render("is {{A.refs.riya}} in {{A.account_id}}?", anchor=A, accounts=accounts) == "is s_9 in acc_1?"
    )
    with pytest.raises(TemplateError):
        render("{{A.refs.nobody}}", anchor=A, accounts=accounts)
