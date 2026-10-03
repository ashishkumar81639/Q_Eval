"""Repo: every read and write the harness does (build.md 6.3).

All calls are synchronous and made from the event-loop thread, so writes never overlap (safe on SQLite).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from qeval.store.schema import check_results, gradings, judge_cache, runs, scenario_runs, turns, usage_ledger


def utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def new_run_id() -> str:
    return f"r_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"


class Repo:
    def __init__(self, engine: Engine):
        self.engine = engine

    # ---- runs ---------------------------------------------------------------
    def create_run(self, *, name, anchor_date, agent_version, harness_sha, config, run_id=None) -> str:
        run_id = run_id or new_run_id()
        with self.engine.begin() as c:
            c.execute(
                runs.insert().values(
                    run_id=run_id,
                    name=name,
                    started_at=utcnow(),
                    status="running",
                    anchor_date=str(anchor_date),
                    agent_version=agent_version,
                    harness_sha=harness_sha,
                    config=config,
                )
            )
        return run_id

    def finish_run(self, run_id: str, status: str) -> None:
        with self.engine.begin() as c:
            c.execute(
                runs.update().where(runs.c.run_id == run_id).values(status=status, finished_at=utcnow())
            )

    def reopen_run(self, run_id: str) -> None:
        """Resume: jobs left 'running' by a crash or kill can never finish, so mark them interrupted."""
        with self.engine.begin() as c:
            c.execute(runs.update().where(runs.c.run_id == run_id).values(status="running", finished_at=None))
            c.execute(
                scenario_runs.update()
                .where(scenario_runs.c.run_id == run_id, scenario_runs.c.status.in_(["running", "pending"]))
                .values(status="interrupted", error="left running when the run stopped", finished_at=utcnow())
            )

    def get_run(self, run_id: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(sa.select(runs).where(runs.c.run_id == run_id)).mappings().first()
            return dict(r) if r else None

    def list_runs(self, limit: int = 20) -> list[dict]:
        with self.engine.connect() as c:
            rows = c.execute(sa.select(runs).order_by(runs.c.started_at.desc()).limit(limit)).mappings()
            return [dict(r) for r in rows]

    def latest_run_id(self) -> str | None:
        rs = self.list_runs(1)
        return rs[0]["run_id"] if rs else None

    # ---- scenario runs ------------------------------------------------------
    def upsert_scenario_run(self, row: dict) -> str:
        row = dict(row)
        row.setdefault("id", str(uuid.uuid4()))
        with self.engine.begin() as c:
            exists = c.execute(sa.select(scenario_runs.c.id).where(scenario_runs.c.id == row["id"])).first()
            if exists:
                c.execute(scenario_runs.update().where(scenario_runs.c.id == row["id"]).values(**row))
            else:
                c.execute(scenario_runs.insert().values(**row))
        return row["id"]

    def update_scenario_run(self, id: str, **values) -> None:
        with self.engine.begin() as c:
            c.execute(scenario_runs.update().where(scenario_runs.c.id == id).values(**values))

    def set_scenario_run_status(self, id: str, status: str, error: str | None = None) -> None:
        self.update_scenario_run(id, status=status, error=error, finished_at=utcnow())

    def completed_jobs(self, run_id: str) -> set[tuple[str, str, int]]:
        with self.engine.connect() as c:
            rows = c.execute(
                sa.select(
                    scenario_runs.c.scenario_id, scenario_runs.c.scenario_hash, scenario_runs.c.rep
                ).where(scenario_runs.c.run_id == run_id, scenario_runs.c.status == "valid")
            )
            return {(r[0], r[1], r[2]) for r in rows}

    def max_attempt(self, run_id: str, scenario_id: str, rep: int) -> int:
        with self.engine.connect() as c:
            v = c.execute(
                sa.select(sa.func.max(scenario_runs.c.attempt)).where(
                    scenario_runs.c.run_id == run_id,
                    scenario_runs.c.scenario_id == scenario_id,
                    scenario_runs.c.rep == rep,
                )
            ).scalar()
            return int(v or 0)

    def scenario_runs(
        self, run_id: str, status: str | None = None, scenario_id: str | None = None
    ) -> list[dict]:
        q = sa.select(scenario_runs).where(scenario_runs.c.run_id == run_id)
        if status:
            q = q.where(scenario_runs.c.status == status)
        if scenario_id:
            q = q.where(scenario_runs.c.scenario_id == scenario_id)
        q = q.order_by(scenario_runs.c.scenario_id, scenario_runs.c.rep, scenario_runs.c.attempt)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q).mappings()]

    def get_scenario_run(self, id: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(sa.select(scenario_runs).where(scenario_runs.c.id == id)).mappings().first()
            return dict(r) if r else None

    # ---- turns --------------------------------------------------------------
    def add_turn(self, row: dict) -> None:
        row = dict(row)
        row.setdefault("id", str(uuid.uuid4()))
        with self.engine.begin() as c:
            c.execute(turns.insert().values(**row))

    def turns(self, scenario_run_id: str) -> list[dict]:
        with self.engine.connect() as c:
            q = sa.select(turns).where(turns.c.scenario_run_id == scenario_run_id).order_by(turns.c.idx)
            return [dict(r) for r in c.execute(q).mappings()]

    def turns_for_run(self, run_id: str) -> dict[str, list[dict]]:
        q = (
            sa.select(turns)
            .join(scenario_runs, scenario_runs.c.id == turns.c.scenario_run_id)
            .where(scenario_runs.c.run_id == run_id)
            .order_by(turns.c.scenario_run_id, turns.c.idx)
        )
        out: dict[str, list[dict]] = {}
        with self.engine.connect() as c:
            for r in c.execute(q).mappings():
                out.setdefault(r["scenario_run_id"], []).append(dict(r))
        return out

    # ---- grading ------------------------------------------------------------
    def create_grading(self, run_id, checks_version, rubric_versions, judge_model, options) -> str:
        gid = f"g_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"
        with self.engine.begin() as c:
            c.execute(
                gradings.insert().values(
                    grading_id=gid,
                    run_id=run_id,
                    created_at=utcnow(),
                    checks_version=checks_version,
                    rubric_versions=rubric_versions,
                    judge_model=judge_model,
                    options=options,
                    status="running",
                )
            )
        return gid

    def finish_grading(self, grading_id: str, status: str) -> None:
        with self.engine.begin() as c:
            c.execute(
                gradings.update()
                .where(gradings.c.grading_id == grading_id)
                .values(status=status, finished_at=utcnow())
            )

    def add_check_results(self, grading_id: str, rows: list[dict]) -> None:
        if not rows:
            return
        with self.engine.begin() as c:
            c.execute(check_results.insert(), [{**r, "grading_id": grading_id} for r in rows])

    def latest_grading(self, run_id: str) -> str | None:
        with self.engine.connect() as c:
            r = c.execute(
                sa.select(gradings.c.grading_id)
                .where(gradings.c.run_id == run_id, gradings.c.status == "done")
                .order_by(gradings.c.finished_at.desc(), gradings.c.created_at.desc())
                .limit(1)
            ).first()
            return r[0] if r else None

    def get_grading(self, grading_id: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(sa.select(gradings).where(gradings.c.grading_id == grading_id)).mappings().first()
            return dict(r) if r else None

    def check_results(self, grading_id: str, scenario_run_id: str | None = None) -> list[dict]:
        q = sa.select(check_results).where(check_results.c.grading_id == grading_id)
        if scenario_run_id:
            q = q.where(check_results.c.scenario_run_id == scenario_run_id)
        with self.engine.connect() as c:
            return [dict(r) for r in c.execute(q.order_by(check_results.c.id)).mappings()]

    # ---- judge cache and usage ---------------------------------------------
    def judge_cache_get(self, key: str) -> dict | None:
        with self.engine.connect() as c:
            r = c.execute(sa.select(judge_cache.c.response).where(judge_cache.c.key == key)).first()
            return r[0] if r else None

    def judge_cache_put(self, key, model, rubric_id, version, response, usage) -> None:
        with self.engine.begin() as c:
            exists = c.execute(sa.select(judge_cache.c.key).where(judge_cache.c.key == key)).first()
            if exists:
                return
            c.execute(
                judge_cache.insert().values(
                    key=key,
                    model=model,
                    rubric_id=rubric_id,
                    rubric_version=version,
                    response=response,
                    usage=usage,
                    created_at=utcnow(),
                )
            )

    def add_usage(self, run_id: str | None, source: str, usage: dict | None) -> None:
        u = usage or {}
        with self.engine.begin() as c:
            c.execute(
                usage_ledger.insert().values(
                    run_id=run_id,
                    source=source,
                    input_tokens=int(u.get("input_tokens") or 0),
                    output_tokens=int(u.get("output_tokens") or 0),
                    cache_read=int(u.get("cache_read_input_tokens") or 0),
                    cache_write=int(u.get("cache_creation_input_tokens") or 0),
                    created_at=utcnow(),
                )
            )

    def usage_totals(self, run_id: str) -> dict[str, dict]:
        q = (
            sa.select(
                usage_ledger.c.source,
                sa.func.count(),
                sa.func.sum(usage_ledger.c.input_tokens),
                sa.func.sum(usage_ledger.c.output_tokens),
                sa.func.sum(usage_ledger.c.cache_read),
                sa.func.sum(usage_ledger.c.cache_write),
            )
            .where(usage_ledger.c.run_id == run_id)
            .group_by(usage_ledger.c.source)
        )
        out = {}
        with self.engine.connect() as c:
            for src, n, i, o, cr, cw in c.execute(q):
                out[src] = {
                    "calls": int(n or 0),
                    "input_tokens": int(i or 0),
                    "output_tokens": int(o or 0),
                    "cache_read": int(cr or 0),
                    "cache_write": int(cw or 0),
                }
        return out
