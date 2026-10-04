"""Pydantic models for scenario, persona and rubric files (build.md 4.2, 4.5, 4.6)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Category = Literal["profile", "history", "family_history", "episode"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubjectSeed(_Strict):
    ref: str = "self"
    relation: str = "self"
    name: str | None = None
    sex: Literal["male", "female"] | None = None
    date_of_birth: str | None = None  # "YYYY-MM-DD" or "{{date:-7w}}"
    age_years: float | None = None
    age_months: float | None = None


class EpisodeSeed(_Strict):
    ref: str | None = None
    subject_ref: str
    complaint: str
    opened_at: str  # ISO with offset, or "{{t:-9d 10:00}}"
    last_activity_at: str | None = None
    status: Literal["open", "closed"] = "open"
    outcome: str | None = None


class FactSeed(_Strict):
    subject_ref: str | None = None  # None = account-level
    episode_ref: str | None = None
    category: Category
    key: str
    value: str
    noted_at: str | None = None


class AccountSeed(_Strict):
    holder: SubjectSeed | None = None  # becomes relation "self"
    subjects: list[SubjectSeed] = []
    episodes: list[EpisodeSeed] = []
    facts: list[FactSeed] = []


class Turn(_Strict):
    at: str | None = None  # "-3d 20:00", "0d 09:00", absolute ISO with offset; None = previous + 2 min
    account: str = "main"
    user: str
    # Ground truth: this message describes a possible emergency (S1/S2/W9). Drives the S2 check and S1 judge.
    emergency: bool = False
    expect: list[str] = []
    judge: list[str] = []


class Scenario(_Strict):
    id: str
    title: str
    rules: list[str]
    tags: list[str] = []
    twin: str | None = None
    k: int | None = None
    reference: dict = {}
    accounts: dict[str, AccountSeed] = {}
    seed: AccountSeed | None = None
    turns: list[Turn] = []
    persona: str | None = None
    final_expect: list[str] = []
    final_judge: list[str] = []
    notes: str | None = None
    # filled by the loader:
    path: str = ""
    content_hash: str = ""

    @model_validator(mode="after")
    def _seed_to_accounts(self) -> Scenario:
        if self.seed is not None:
            if self.accounts:
                raise ValueError("use either 'seed' or 'accounts', not both")
            self.accounts = {"main": self.seed}
            self.seed = None
        if not self.accounts:
            self.accounts = {"main": AccountSeed()}
        return self

    @property
    def kind(self) -> str:
        return "persona" if self.persona else "scripted"


class HiddenFact(_Strict):
    value: str
    ask_keywords: list[str]


class LateReveal(_Strict):
    after_agent_turn: int
    text: str
    emergency: bool = False  # the reveal is a red flag that must get an urgent assessment that turn


class Persona(_Strict):
    id: str
    title: str
    rules: list[str]
    account: AccountSeed
    member_ref: str | None = None  # which seeded member the persona is about (for the sim prompt)
    opening: str
    opening_at: str = "0d 13:00"
    turn_gap_minutes: int = 3
    hidden_facts: dict[str, HiddenFact]
    unknown_answer: str = "not sure"
    style: str
    late_reveal: LateReveal | None = None
    after_assessment: list[str] = []
    reference: dict = {}
    max_turns: int = 8
    path: str = ""
    content_hash: str = ""


class Rubric(_Strict):
    id: str
    rule: str
    title: str = ""  # one short line shown in reports (required by `qeval validate`)
    severity: str | None = None
    version: int = 1
    scope: Literal["turn", "conversation", "fact"] = "turn"
    auto_when: str | None = None
    inputs: list[str]
    question: str
    pass_if: str
    fail_if: str
    do_not_penalise: str = ""
    examples: dict[str, list[str]] = Field(default_factory=dict)
    path: str = ""


class RuleInfo(_Strict):
    title: str  # one short line shown in reports
    expected: str  # one short line: what the agent should do
    area: str
    severity: Literal["critical", "major", "minor"]
    summary: str
    deterministic: list[str] = []
    judge: list[str] = []
