"""Normalised views over one saved scenario run (build.md 9.1). Plain data, no decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class ToolCall:
    step: int
    tool: str
    input: dict
    output: Any
    is_error: bool


@dataclass
class Trace:
    steps: list[list[ToolCall]]
    calls: list[ToolCall]
    texts: list[str]
    respond_calls: list[ToolCall]
    errors: list[ToolCall]

    @property
    def tools(self) -> list[str]:
        return [c.tool for c in self.calls]


@dataclass
class Member:
    id: str
    ref: str | None
    relation: str
    name: str | None
    sex: str | None
    dob: str | None
    dob_estimated: bool
    label: str
    seeded: bool

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "ref": self.ref,
            "relation": self.relation,
            "name": self.name,
            "sex": self.sex,
            "date_of_birth": self.dob,
            "dob_estimated": self.dob_estimated,
            "label": self.label,
            "seeded": self.seeded,
        }


@dataclass
class StateView:
    raw: dict
    account_id: str
    members: dict[str, Member]
    ref_to_id: dict[str, str]
    facts: list[dict]
    facts_by_subject: dict[str | None, list[dict]]
    episodes: dict[str, dict]
    episodes_by_subject: dict[str, list[dict]]
    assessments: list[dict]
    turns: list[dict]
    possible_duplicates: list[tuple[str, str]] = field(default_factory=list)

    def facts_from_turn(self, user_turn_id: str | None) -> list[dict]:
        if not user_turn_id:
            return []
        return [f for f in self.facts if f.get("source_turn_id") == user_turn_id]

    def turn(self, turn_id: str | None) -> dict | None:
        for t in self.turns:
            if t.get("id") == turn_id:
                return t
        return None

    def member_name(self, sid: str | None) -> str:
        if sid is None:
            return "(account)"
        m = self.members.get(sid)
        if m is None:
            return sid
        return m.name or m.ref or m.relation


@dataclass
class Diff:
    new_facts: list[dict]
    superseded_facts: list[dict]
    new_subjects: list[Member]
    changed_subjects: list[tuple[Member, Member]]
    opened_episodes: list[dict]
    closed_episodes: list[dict]
    new_assessments: list[dict]
    new_turns: list[dict]

    def is_empty(self) -> bool:
        return not any(
            [
                self.new_facts,
                self.superseded_facts,
                self.new_subjects,
                self.changed_subjects,
                self.opened_episodes,
                self.closed_episodes,
                self.new_assessments,
            ]
        )


@dataclass
class AccountInfo:
    key: str
    account_id: str
    refs: dict[str, str]
    initial: StateView
    final: StateView | None


@dataclass
class NormTurn:
    idx: int
    account_key: str
    actor: str
    user_text: str
    received_at: datetime
    user_turn_id: str | None
    reply_turn_id: str | None
    reply: dict
    trace: Trace
    state: StateView | None
    prev_state: StateView
    diff: Diff | None
    latency_ms: int
    usage: dict
    raw: dict
    sim_meta: dict | None = None
    other_states: dict | None = None  # isolation: other accounts' states before/after

    @property
    def kind(self) -> str | None:
        return self.reply.get("kind")


@dataclass
class NormRun:
    scenario: Any  # Scenario
    scenario_run_id: str
    rep: int
    status: str
    accounts: dict[str, AccountInfo]
    turns: list[NormTurn]
    anchor_date: date
    time_shift_days: int
    persona: Any = None  # Persona for persona runs

    def turns_for(self, account_key: str) -> list[NormTurn]:
        return [t for t in self.turns if t.account_key == account_key]

    def final_state(self, account_key: str) -> StateView | None:
        acc = self.accounts.get(account_key)
        return acc.final if acc else None

    @property
    def effective_anchor(self) -> date:
        from datetime import timedelta

        return self.anchor_date - timedelta(days=self.time_shift_days)
