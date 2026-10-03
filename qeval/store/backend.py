"""Choose Postgres or SQLite from DATABASE_URL (build.md 6.1)."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine

from qeval.store.schema import SCHEMA_VERSION, meta, metadata


class SchemaMismatch(RuntimeError):
    pass


# Older schema version -> statements that bring it to SCHEMA_VERSION (portable SQL only).
MIGRATIONS: dict[str, list[str]] = {
    "1": [
        "ALTER TABLE scenario_runs ADD COLUMN scenario_snapshot JSON",
        "ALTER TABLE gradings ADD COLUMN status TEXT",
        "ALTER TABLE gradings ADD COLUMN finished_at TEXT",
    ],
}


def make_engine(database_url: str | None, runs_dir: Path) -> Engine:
    if database_url:
        url = database_url
    else:
        runs_dir.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{runs_dir / 'qeval.db'}"
    engine = sa.create_engine(url, future=True)
    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")  # readers and the single writer work at the same time
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.close()

    init_schema(engine)
    return engine


def init_schema(engine: Engine) -> None:
    metadata.create_all(engine)
    with engine.begin() as c:
        row = c.execute(sa.select(meta.c.value).where(meta.c.key == "schema_version")).first()
        if row is None:
            c.execute(meta.insert().values(key="schema_version", value=SCHEMA_VERSION))
        elif row[0] in MIGRATIONS:
            for stmt in MIGRATIONS[row[0]]:
                c.exec_driver_sql(stmt)
            c.execute(meta.update().where(meta.c.key == "schema_version").values(value=SCHEMA_VERSION))
        elif row[0] != SCHEMA_VERSION:
            raise SchemaMismatch(f"database schema {row[0]} != harness schema {SCHEMA_VERSION}")


def backend_name(engine: Engine) -> str:
    return engine.dialect.name
