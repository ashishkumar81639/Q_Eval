"""Build the POST /accounts body from an AccountSeed."""

from __future__ import annotations

from datetime import date

from qeval.scenario.models import AccountSeed, SubjectSeed
from qeval.scenario.templating import render_obj

DEFAULT_HOLDER_NAME = "Alex"


def _subject(s: SubjectSeed) -> dict:
    return {k: v for k, v in s.model_dump().items() if v is not None}


def build_create_body(seed: AccountSeed, *, anchor: date, shift_days: int = 0) -> dict:
    subjects = []
    holder_name = DEFAULT_HOLDER_NAME
    if seed.holder is not None:
        h = seed.holder.model_copy(update={"ref": "self", "relation": "self"})
        holder_name = h.name or DEFAULT_HOLDER_NAME
        subjects.append(_subject(h))
    subjects += [_subject(s) for s in seed.subjects]
    body = {
        "holder_name": holder_name,
        "subjects": subjects,
        "episodes": [{k: v for k, v in e.model_dump().items() if v is not None} for e in seed.episodes],
        "facts": [
            {k: v for k, v in f.model_dump().items() if v is not None or k == "subject_ref"}
            for f in seed.facts
        ],
    }
    return render_obj(body, anchor=anchor, shift_days=shift_days)


def seed_refs(seed: AccountSeed) -> list[str]:
    refs = ["self"] if seed.holder is not None else []
    refs += [s.ref for s in seed.subjects]
    refs += [e.ref for e in seed.episodes if e.ref]
    return refs
