"""Names available inside expectations: reply, trace, state, prev_state, diff, turn[i], ref(), ... (build.md 4.4)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from qeval import timeutil
from qeval.checks import text as T
from qeval.checks.expr.evaluator import Coll, ExprRuntimeError
from qeval.normalise.models import Diff, NormRun, NormTurn, StateView, Trace

_UNSET = object()


class TraceNS:
    def __init__(self, tr: Trace):
        self._t = tr

    @property
    def tools(self) -> list[str]:
        return self._t.tools

    @property
    def steps(self) -> list[list[str]]:
        return [[c.tool for c in s] for s in self._t.steps]

    @property
    def errors(self) -> Coll:
        return Coll({"tool": c.tool, "input": c.input, "output": c.output} for c in self._t.errors)

    def called(self, tool: str) -> bool:
        return any(c.tool == tool and not c.is_error for c in self._t.calls)

    def not_called(self, tool: str) -> bool:
        return not self.called(tool)

    def count(self, tool: str) -> int:
        return sum(1 for c in self._t.calls if c.tool == tool and not c.is_error)

    def before(self, a: str, b: str) -> bool:
        """True if the first successful call to `a` comes before the first call to `b` (by step)."""
        sa = [c.step for c in self._t.calls if c.tool == a and not c.is_error]
        sb = [c.step for c in self._t.calls if c.tool == b and not c.is_error]
        if not sa or not sb:
            return False
        ia = next(i for i, c in enumerate(self._t.calls) if c.tool == a and not c.is_error)
        ib = next(i for i, c in enumerate(self._t.calls) if c.tool == b and not c.is_error)
        return ia < ib if sa[0] == sb[0] else sa[0] < sb[0]

    def calls(self, tool: str) -> Coll:
        return Coll(c.input for c in self._t.calls if c.tool == tool and not c.is_error)

    def __repr__(self) -> str:
        return f"trace({self.steps})"


class StateNS:
    def __init__(self, sv: StateView | None, user_turn_id: str | None = None):
        self._s = sv
        self._turn_id = user_turn_id

    def _need(self) -> StateView:
        if self._s is None:
            raise ExprRuntimeError("state was not captured for this turn")
        return self._s

    @property
    def subjects(self) -> Coll:
        return Coll(m.as_dict() for m in self._need().members.values())

    @property
    def members(self) -> Coll:
        return self.subjects

    def facts(
        self,
        *,
        subject=_UNSET,
        category=_UNSET,
        episode=_UNSET,
        superseded=False,
        this_turn=_UNSET,
        key=_UNSET,
    ) -> Coll:
        out = []
        for f in self._need().facts:
            if subject is not _UNSET and f.get("subject_id") != subject:
                continue
            if category is not _UNSET and f.get("category") != category:
                continue
            if episode is not _UNSET and f.get("episode_id") != episode:
                continue
            if superseded is not None and bool(f.get("superseded")) != bool(superseded):
                continue
            if key is not _UNSET and f.get("key") != key:
                continue
            if this_turn is not _UNSET and (f.get("source_turn_id") == self._turn_id) != bool(this_turn):
                continue
            out.append(f)
        return Coll(out)

    def episodes(self, *, subject=_UNSET, status=_UNSET) -> Coll:
        out = []
        for e in self._need().episodes.values():
            if subject is not _UNSET and e.get("subject_id") != subject:
                continue
            if status is not _UNSET and e.get("status") != status:
                continue
            out.append(e)
        return Coll(out)

    def assessments(self, *, subject=_UNSET) -> Coll:
        return Coll(
            a for a in self._need().assessments if subject is _UNSET or a.get("subject_id") == subject
        )

    def turns(self, *, role=_UNSET) -> Coll:
        return Coll(t for t in self._need().turns if role is _UNSET or t.get("role") == role)

    @property
    def possible_duplicates(self) -> Coll:
        return Coll(list(p) for p in self._need().possible_duplicates)

    @property
    def account_id(self) -> str:
        return self._need().account_id

    def __repr__(self) -> str:
        return "state(...)"


class DiffNS:
    def __init__(self, d: Diff | None):
        self._d = d

    def _need(self) -> Diff:
        if self._d is None:
            raise ExprRuntimeError("no diff: state was not captured for this turn")
        return self._d

    @property
    def new_facts(self) -> Coll:
        return Coll(self._need().new_facts)

    @property
    def superseded_facts(self) -> Coll:
        return Coll(self._need().superseded_facts)

    @property
    def new_subjects(self) -> Coll:
        return Coll(m.as_dict() for m in self._need().new_subjects)

    @property
    def changed_subjects(self) -> Coll:
        return Coll({"before": a.as_dict(), "after": b.as_dict()} for a, b in self._need().changed_subjects)

    @property
    def opened_episodes(self) -> Coll:
        return Coll(self._need().opened_episodes)

    @property
    def closed_episodes(self) -> Coll:
        return Coll(self._need().closed_episodes)

    @property
    def new_assessments(self) -> Coll:
        return Coll(self._need().new_assessments)


class TurnNS:
    def __init__(self, t: NormTurn):
        self._t = t
        self.reply = t.reply
        self.trace = TraceNS(t.trace)
        self.state = StateNS(t.state, t.user_turn_id)
        self.prev_state = StateNS(t.prev_state, t.user_turn_id)
        self.diff = DiffNS(t.diff)
        self.user = t.user_text
        self.kind = t.reply.get("kind")
        self.received_at = timeutil.iso(t.received_at)
        self.account = t.account_key

    def __repr__(self) -> str:
        return f"turn[{self._t.idx}]"


class AccountNS:
    """Explicit cross-account access for multi-household scenarios: account('B').turns, account('B').state."""

    def __init__(self, run: NormRun, key: str):
        if key not in run.accounts:
            raise ExprRuntimeError(f"unknown account {key!r}")
        self._run, self._key = run, key
        self.turns = Coll(TurnNS(t) for t in run.turns if t.account_key == key)
        self.state = StateNS(run.accounts[key].final)
        self.account_id = run.accounts[key].account_id


def is_emergency_turn(run: NormRun, turn: NormTurn) -> bool:
    """Ground truth from the scenario file: does this user message describe a possible emergency?"""
    sc = run.scenario
    if getattr(sc, "kind", "scripted") == "scripted":
        return 0 <= turn.idx < len(sc.turns) and bool(sc.turns[turn.idx].emergency)
    lr = getattr(run.persona, "late_reveal", None) if run.persona is not None else None
    return bool(lr and lr.emergency and turn.user_text == lr.text)


def names_for(run: NormRun, turn: NormTurn, *, final: bool = False) -> dict[str, Any]:
    """Build the evaluation scope for one turn (or the final state when `final`)."""
    account_key = turn.account_key
    acc = run.accounts.get(account_key)
    state_view = (acc.final if acc and acc.final else turn.state) if final else turn.state
    anchor = run.effective_anchor
    # turn[i] / turns only ever see this account's turns, in order; other households need account('B').
    same_account_turns = [TurnNS(t) for t in run.turns if t.account_key == account_key]

    def ref(name: str, account: str | None = None) -> str:
        a = run.accounts.get(account or account_key)
        if a is None:
            raise ExprRuntimeError(f"unknown account {account!r}")
        if name in a.refs:
            return a.refs[name]
        sid = a.initial.ref_to_id.get(name)
        if sid:
            return sid
        raise ExprRuntimeError(f"unknown ref {name!r}")

    def member(name: str | None = None, relation: str | None = None, sex: str | None = None):
        sv = state_view or turn.prev_state
        for m in sv.members.values():
            if name is not None and (m.name or "").casefold() != name.casefold():
                continue
            if relation is not None and m.relation.casefold() != relation.casefold():
                continue
            if sex is not None and m.sex != sex:
                continue
            return m.id
        return None

    def date_re(n: int) -> str:
        return timeutil.date_regex(anchor + timedelta(days=int(n)))

    def date_str(n: int) -> str:
        d = anchor + timedelta(days=int(n))
        return timeutil.date_spellings(d)[0]

    def turn_date_re(n: int = 0) -> str:
        return timeutil.date_regex(turn.received_at.date() + timedelta(days=int(n)))

    def last_weekday_re(name: str) -> str:
        """Most recent `name` weekday strictly before the turn's date ("since last Friday")."""
        target = T.weekday_index(name)
        d = turn.received_at.date() - timedelta(days=1)
        while d.weekday() != target:
            d -= timedelta(days=1)
        return timeutil.date_regex(d)

    def length(x) -> int:
        return len(x) if x is not None else 0

    def lower(x) -> str:
        return (x or "").lower()

    def words(x) -> int:
        return len((x or "").split())

    def questions(x) -> int:
        return (x or "").count("?")

    def first_sentences(x, n: int = 2) -> str:
        import re

        parts = re.split(r"(?<=[.!?])\s+|\n+", (x or "").strip())
        parts = [p for p in parts if p.strip()]
        return " ".join(parts[: int(n)])

    return {
        "reply": turn.reply,
        "trace": TraceNS(turn.trace),
        "state": StateNS(state_view, turn.user_turn_id),
        "prev_state": StateNS(turn.prev_state, turn.user_turn_id),
        "diff": DiffNS(turn.diff),
        "turn": same_account_turns,
        "turns": Coll(same_account_turns),
        "user": turn.user_text,
        "ref": ref,
        "member": member,
        "date_re": date_re,
        "date_str": date_str,
        "turn_date_re": turn_date_re,
        "last_weekday_re": last_weekday_re,
        "has_date": T.has_date,
        "relative_time": T.relative_time,
        "lower": lower,
        "len": length,
        "words": words,
        "questions": questions,
        "first_sentences": first_sentences,
        "reference": getattr(run.scenario, "reference", {}) or {},
        "account": lambda key: AccountNS(run, key),
        "emergency": is_emergency_turn(run, turn),
        "emergency_signs": T.emergency_signs,
    }
