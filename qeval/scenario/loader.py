"""Read YAML files into models, attaching file path and content hash."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from qeval.config import ROOT
from qeval.scenario.models import Persona, Rubric, RuleInfo, Scenario


class LoadError(Exception):
    def __init__(self, path: str, message: str):
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


def _read(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise LoadError(str(path), f"YAML error: {e}") from e
    if not isinstance(data, dict):
        raise LoadError(str(path), "top level must be a mapping")
    return data, hashlib.sha256(raw).hexdigest()[:16]


def _fmt_validation(e: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors())


def yaml_files(paths: Iterable[str | Path]) -> list[Path]:
    out: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            out.extend(sorted(x for x in p.rglob("*.y*ml") if x.is_file()))
        elif p.is_file():
            out.append(p)
        else:
            raise LoadError(str(p), "no such file or directory")
    return out


def load_scenario(path: Path) -> Scenario:
    data, h = _read(path)
    try:
        sc = Scenario.model_validate(data)
    except ValidationError as e:
        raise LoadError(str(path), _fmt_validation(e)) from e
    sc.path = _rel(path)
    sc.content_hash = h
    return sc


def load_persona(path: Path) -> Persona:
    data, h = _read(path)
    try:
        p = Persona.model_validate(data)
    except ValidationError as e:
        raise LoadError(str(path), _fmt_validation(e)) from e
    p.path = _rel(path)
    p.content_hash = h
    return p


def load_rubric(path: Path) -> Rubric:
    data, _ = _read(path)
    try:
        r = Rubric.model_validate(data)
    except ValidationError as e:
        raise LoadError(str(path), _fmt_validation(e)) from e
    r.path = _rel(path)
    return r


def load_rules(path: Path | None = None) -> dict[str, RuleInfo]:
    path = path or ROOT / "rules.yaml"
    data, _ = _read(path)
    out = {}
    for rid, body in data.items():
        try:
            out[rid] = RuleInfo.model_validate(body)
        except ValidationError as e:
            raise LoadError(str(path), f"{rid}: {_fmt_validation(e)}") from e
    return out


def load_rubrics(directory: Path | None = None) -> dict[str, Rubric]:
    directory = directory or ROOT / "rubrics"
    out: dict[str, Rubric] = {}
    if directory.exists():
        for f in yaml_files([directory]):
            r = load_rubric(f)
            if r.id in out:
                raise LoadError(str(f), f"duplicate rubric id {r.id}")
            out[r.id] = r
    return out


def load_personas(directory: Path | None = None) -> dict[str, Persona]:
    directory = directory or ROOT / "personas"
    out: dict[str, Persona] = {}
    if directory.exists():
        for f in yaml_files([directory]):
            p = load_persona(f)
            if p.id in out:
                raise LoadError(str(f), f"duplicate persona id {p.id}")
            out[p.id] = p
    return out


def _rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def snapshot(scenario: Scenario, persona: Persona | None = None) -> dict:
    """Everything grading needs to re-create the scenario exactly as it was run."""
    return {
        "scenario": scenario.model_dump(mode="json"),
        "persona": persona.model_dump(mode="json") if persona is not None else None,
    }


def from_snapshot(snap: dict) -> tuple[Scenario, Persona | None]:
    sc = Scenario.model_validate(snap["scenario"])
    p = Persona.model_validate(snap["persona"]) if snap.get("persona") else None
    return sc, p
