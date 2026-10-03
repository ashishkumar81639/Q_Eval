"""Backend-specific database dump (build.md 6.4)."""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

from sqlalchemy.engine import Engine

from qeval.config import ROOT


def dump(engine: Engine, runs_dir: Path, name: str) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    if engine.dialect.name == "sqlite":
        src_path = engine.url.database
        out = runs_dir / f"{name}.db"
        src = sqlite3.connect(str(src_path))
        dst = sqlite3.connect(out)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        shutil.copyfile(out, runs_dir / "latest.db")
        return out
    out = runs_dir / f"{name}.dump"
    url = engine.url
    cmd_local = [
        "pg_dump",
        "-Fc",
        "-h",
        url.host or "localhost",
        "-p",
        str(url.port or 5432),
        "-U",
        url.username or "qeval",
        url.database or "qeval",
    ]
    cmd_docker = [
        "docker",
        "compose",
        "exec",
        "-T",
        "db",
        "pg_dump",
        "-U",
        url.username or "qeval",
        "-Fc",
        url.database or "qeval",
    ]
    env = {"PGPASSWORD": url.password or ""}
    for cmd in (cmd_docker, cmd_local):
        try:
            with out.open("wb") as f:
                subprocess.run(cmd, stdout=f, check=True, cwd=ROOT, env={**_env(), **env}, timeout=600)
            shutil.copyfile(out, runs_dir / "latest.dump")
            return out
        except subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired:
            continue
    raise RuntimeError("pg_dump failed (tried docker compose and a local pg_dump)")


def _env() -> dict:
    import os

    return dict(os.environ)
