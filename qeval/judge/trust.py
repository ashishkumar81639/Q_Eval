"""Which judge rubrics may affect scored rates, findings frequency and CI gates.

A rubric is *scored* only when `trust/judge_trust.yaml` holds a passing record for its exact current text
(same content hash). Anything else is *advisory*: graded and shown, never counted. Records are written by
`qeval trust` from blind human calibration plus a stability check; editing a rubric invalidates its record.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from qeval.config import ROOT
from qeval.scenario.models import Rubric

TRUST_FILE = ROOT / "trust" / "judge_trust.yaml"

# Thresholds from docs/eval-quality-fix-plan.md (issue 10).
MIN_LABELS = 15
MIN_LABELS_CRITICAL = 20
MIN_AGREEMENT = 0.80
MIN_FAIL_PRECISION = 0.75
MIN_FAIL_RECALL = 0.75
MIN_FAIL_RECALL_CRITICAL = 0.90
MIN_STABILITY = 0.90


def rubric_hash(r: Rubric) -> str:
    body = r.model_dump(exclude={"path"})
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def load_trust(path: Path | None = None) -> dict:
    path = path or TRUST_FILE
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("rubrics") or {}


def is_scored(r: Rubric, trust: dict | None = None) -> bool:
    rec = (load_trust() if trust is None else trust).get(r.id)
    return bool(rec and rec.get("status") == "scored" and rec.get("rubric_hash") == rubric_hash(r))


def trust_status(rubrics: dict[str, Rubric], trust: dict | None = None) -> dict[str, str]:
    trust = load_trust() if trust is None else trust
    out = {}
    for rid, r in rubrics.items():
        rec = trust.get(rid)
        if not rec:
            out[rid] = "advisory (not calibrated)"
        elif rec.get("rubric_hash") != rubric_hash(r):
            out[rid] = "advisory (rubric changed since calibration)"
        else:
            out[rid] = (
                "scored"
                if rec.get("status") == "scored"
                else f"advisory ({rec.get('reason', 'below threshold')})"
            )
    return out
