"""One authoritative attempt per (scenario, rep): used by grading, aggregation and reports alike."""

from __future__ import annotations


def authoritative(scenario_runs: list[dict]) -> list[dict]:
    """Latest *valid* attempt for each (scenario_id, rep). Earlier or invalid attempts are never scored."""
    best: dict[tuple[str, int], dict] = {}
    for s in scenario_runs:
        if s["status"] != "valid":
            continue
        key = (s["scenario_id"], s["rep"])
        if key not in best or s["attempt"] > best[key]["attempt"]:
            best[key] = s
    return sorted(best.values(), key=lambda s: (s["scenario_id"], s["rep"]))


def latest_per_rep(scenario_runs: list[dict]) -> dict[tuple[str, int], dict]:
    """Latest attempt (any status) for each (scenario_id, rep): decides whether a rep ended valid."""
    out: dict[tuple[str, int], dict] = {}
    for s in scenario_runs:
        key = (s["scenario_id"], s["rep"])
        if key not in out or s["attempt"] > out[key]["attempt"]:
            out[key] = s
    return out
