"""Portable JSONL export/import of one run, so it can be re-graded on any backend (build.md 6.4)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from qeval.store.repo import Repo
from qeval.store.schema import SCHEMA_VERSION

EXPORT_FORMAT_VERSION = 1


class ExportFormatError(SystemExit):
    pass


TURN_KEYS = (
    "idx",
    "account_key",
    "actor",
    "sent_text",
    "received_at",
    "response",
    "state_after",
    "latency_ms",
    "http_ms",
    "usage",
    "sim_meta",
    "aux",
)


def export_run(repo: Repo, run_id: str, out: Path) -> Path:
    run = repo.get_run(run_id)
    if run is None:
        raise SystemExit(f"unknown run {run_id}")
    out.parent.mkdir(parents=True, exist_ok=True)
    turns = repo.turns_for_run(run_id)
    with gzip.open(out, "wt", encoding="utf-8") as f:
        from qeval.grading.grade import checks_version

        header = {
            "type": "run",
            "export_format_version": EXPORT_FORMAT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "harness_sha": run.get("harness_sha"),
            "checks_version": checks_version(),
            "run": run,
            "usage": repo.usage_totals(run_id),
        }
        f.write(json.dumps(header, default=str) + "\n")
        for sr in repo.scenario_runs(run_id):
            sr = dict(sr)
            sr["turns"] = [{k: t.get(k) for k in TURN_KEYS} for t in turns.get(sr["id"], [])]
            f.write(json.dumps({"type": "scenario_run", **sr}, default=str) + "\n")
    return out


def import_run(repo: Repo, path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        header = json.loads(f.readline())
        validate_header(header)
        run = header["run"]
        if repo.get_run(run["run_id"]):
            raise SystemExit(f"run {run['run_id']} already exists in this database")
        repo.create_run(
            run_id=run["run_id"],
            name=run.get("name"),
            anchor_date=run.get("anchor_date"),
            agent_version=run.get("agent_version"),
            harness_sha=run.get("harness_sha"),
            config=run.get("config"),
        )
        repo.finish_run(run["run_id"], run.get("status") or "done")
        for src, u in (header.get("usage") or {}).items():
            repo.add_usage(
                run["run_id"],
                src,
                {
                    "input_tokens": u.get("input_tokens"),
                    "output_tokens": u.get("output_tokens"),
                    "cache_read_input_tokens": u.get("cache_read"),
                    "cache_creation_input_tokens": u.get("cache_write"),
                },
            )
        for line in f:
            rec = json.loads(line)
            turns = rec.pop("turns", [])
            rec.pop("type", None)
            repo.upsert_scenario_run(rec)
            for t in turns:
                repo.add_turn({"scenario_run_id": rec["id"], **t})
    return run["run_id"]


def validate_header(header: dict) -> None:
    """Refuse an export this harness can't read, before inserting a single row."""
    if header.get("type") != "run" or "run" not in header:
        raise ExportFormatError("not a qeval export (missing run header)")
    fmt = header.get("export_format_version")
    if fmt != EXPORT_FORMAT_VERSION:
        raise ExportFormatError(
            f"export format {fmt!r} is not supported (this harness reads {EXPORT_FORMAT_VERSION})"
        )
    if str(header.get("schema_version")) != SCHEMA_VERSION:
        raise ExportFormatError(
            f"export was written with database schema {header.get('schema_version')!r}; this harness uses "
            f"{SCHEMA_VERSION}. Import it with the matching harness version ({header.get('harness_sha')})."
        )
