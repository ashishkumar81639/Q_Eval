"""SQLAlchemy Core tables, portable between Postgres and SQLite (build.md 6.2)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

SCHEMA_VERSION = "3"

JSON = sa.JSON().with_variant(JSONB(), "postgresql")
metadata = sa.MetaData()

meta = sa.Table("meta", metadata, sa.Column("key", sa.Text, primary_key=True), sa.Column("value", sa.Text))

runs = sa.Table(
    "runs",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("name", sa.Text),
    sa.Column("started_at", sa.Text),
    sa.Column("finished_at", sa.Text),
    sa.Column("status", sa.Text),  # running|done|aborted
    sa.Column("anchor_date", sa.Text),
    sa.Column("agent_version", JSON),
    sa.Column("harness_sha", sa.Text),
    sa.Column("config", JSON),
)

scenario_runs = sa.Table(
    "scenario_runs",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("run_id", sa.Text, sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("scenario_id", sa.Text, nullable=False),
    sa.Column("scenario_hash", sa.Text),
    sa.Column("scenario_path", sa.Text),
    sa.Column("rep", sa.Integer, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("kind", sa.Text),  # scripted|persona
    sa.Column("status", sa.Text),  # pending|running|valid|invalid|scenario_error|harness_error|interrupted
    sa.Column("error", sa.Text),
    sa.Column("time_shift_days", sa.Integer),
    sa.Column("accounts", JSON),
    sa.Column("initial_states", JSON),
    sa.Column("final_states", JSON),
    sa.Column("flags", JSON),  # run-level flags e.g. sim_unreliable, no_assessment_within_max_turns
    # Full scenario (and persona) as it was when the job ran; grading uses this, never the file on disk.
    sa.Column("scenario_snapshot", JSON),
    sa.Column("started_at", sa.Text),
    sa.Column("finished_at", sa.Text),
    sa.UniqueConstraint("run_id", "scenario_id", "rep", "attempt"),
)
sa.Index("ix_scenario_runs_run_status", scenario_runs.c.run_id, scenario_runs.c.status)

turns = sa.Table(
    "turns",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("scenario_run_id", sa.Text, sa.ForeignKey("scenario_runs.id"), nullable=False),
    sa.Column("idx", sa.Integer, nullable=False),
    sa.Column("account_key", sa.Text),
    sa.Column("actor", sa.Text),  # script|sim_user
    sa.Column("sent_text", sa.Text),
    sa.Column("received_at", sa.Text),
    sa.Column("response", JSON),
    sa.Column("state_after", JSON),
    sa.Column("latency_ms", sa.Integer),
    sa.Column("http_ms", sa.Integer),
    sa.Column("usage", JSON),
    sa.Column("sim_meta", JSON),
    sa.Column("aux", JSON),  # isolation snapshots of other accounts
    sa.UniqueConstraint("scenario_run_id", "idx"),
)
sa.Index("ix_turns_sr", turns.c.scenario_run_id)

gradings = sa.Table(
    "gradings",
    metadata,
    sa.Column("grading_id", sa.Text, primary_key=True),
    sa.Column("run_id", sa.Text, sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("created_at", sa.Text),
    sa.Column("checks_version", sa.Text),
    sa.Column("rubric_versions", JSON),
    sa.Column("judge_model", sa.Text),
    sa.Column("options", JSON),
    sa.Column("status", sa.Text),  # running|done|failed
    sa.Column("finished_at", sa.Text),
)

check_results = sa.Table(
    "check_results",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("grading_id", sa.Text, sa.ForeignKey("gradings.grading_id"), nullable=False),
    sa.Column("scenario_run_id", sa.Text, sa.ForeignKey("scenario_runs.id"), nullable=False),
    sa.Column("turn_idx", sa.Integer, nullable=True),
    sa.Column("check_id", sa.Text),
    sa.Column("rule", sa.Text),
    sa.Column("area", sa.Text),
    sa.Column("severity", sa.Text),
    sa.Column("kind", sa.Text),
    sa.Column("verdict", sa.Text),
    sa.Column("evidence", JSON),
    sa.Column("message", sa.Text),
    # False = advisory: shown in reports, excluded from rates, findings frequency and CI gates.
    # NULL (rows written before schema 3) counts as scored.
    sa.Column("scored", sa.Boolean),
)
sa.Index("ix_check_results_grading_rule", check_results.c.grading_id, check_results.c.rule)

judge_cache = sa.Table(
    "judge_cache",
    metadata,
    sa.Column("key", sa.Text, primary_key=True),
    sa.Column("model", sa.Text),
    sa.Column("rubric_id", sa.Text),
    sa.Column("rubric_version", sa.Integer),
    sa.Column("response", JSON),
    sa.Column("usage", JSON),
    sa.Column("created_at", sa.Text),
)

usage_ledger = sa.Table(
    "usage_ledger",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("run_id", sa.Text),
    sa.Column("source", sa.Text),  # agent|judge|sim_user
    sa.Column("input_tokens", sa.Integer),
    sa.Column("output_tokens", sa.Integer),
    sa.Column("cache_read", sa.Integer),
    sa.Column("cache_write", sa.Integer),
    sa.Column("created_at", sa.Text),
)
