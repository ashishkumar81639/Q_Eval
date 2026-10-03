"""Cross-file validation before any API call (build.md 4.2, design.md section 4)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from qeval import timeutil
from qeval.scenario.coverage import unmeasured_rules
from qeval.scenario.loader import (
    LoadError,
    load_personas,
    load_rubrics,
    load_rules,
    load_scenario,
    yaml_files,
)
from qeval.scenario.models import AccountSeed, Persona, Rubric, RuleInfo, Scenario
from qeval.scenario.seed import seed_refs
from qeval.scenario.templating import TEMPLATE_RE

KNOWN_SINGLE_TEMPLATES = ("t:", "date:")


@dataclass
class Catalog:
    rules: dict[str, RuleInfo]
    rubrics: dict[str, Rubric]
    personas: dict[str, Persona]
    scenarios: list[Scenario] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _check_templates(text: str, where: str, accounts: dict[str, AccountSeed], errs: list[str]) -> None:
    for m in TEMPLATE_RE.finditer(text):
        expr = m.group(1).strip()
        if expr.startswith("t:"):
            if not timeutil.is_relative(expr[2:].strip()):
                errs.append(f"{where}: bad time template {{{{{expr}}}}}")
            continue
        if expr.startswith("date:"):
            try:
                from datetime import date

                timeutil.resolve_date(expr[5:].strip(), date(2026, 1, 1))
            except ValueError as e:
                errs.append(f"{where}: {e}")
            continue
        parts = expr.split(".")
        if parts[0] not in accounts:
            errs.append(f"{where}: template {{{{{expr}}}}} names unknown account {parts[0]!r}")
        elif len(parts) == 3 and parts[1] == "refs":
            if parts[2] not in seed_refs(accounts[parts[0]]):
                errs.append(f"{where}: template {{{{{expr}}}}} names unknown ref {parts[2]!r}")
        elif not (len(parts) == 2 and parts[1] == "account_id"):
            errs.append(f"{where}: unknown template {{{{{expr}}}}}")


def check_seed(seed: AccountSeed, where: str, errs: list[str]) -> None:
    refs = seed_refs(seed)
    dup = [r for r, n in Counter(refs).items() if n > 1]
    if dup:
        errs.append(f"{where}: duplicate refs {dup}")
    for s in seed.subjects:
        if s.relation == "self" or s.ref == "self":
            errs.append(
                f"{where}: use 'holder:' for the account holder, not a subject with relation/ref self"
            )
        if s.relation == "self" and s.ref != "self":
            pass
    subj = set(["self"] + [s.ref for s in seed.subjects])
    eps = {e.ref for e in seed.episodes if e.ref}
    for e in seed.episodes:
        if e.subject_ref not in subj:
            errs.append(f"{where}: episode {e.ref} has unknown subject_ref {e.subject_ref!r}")
        for ts_name in ("opened_at", "last_activity_at"):
            ts = getattr(e, ts_name)
            if ts and not TEMPLATE_RE.search(ts) and not timeutil.has_offset(ts):
                errs.append(f"{where}: episode {e.ref} {ts_name} {ts!r} needs a timezone offset")
    for f in seed.facts:
        if f.subject_ref is not None and f.subject_ref not in subj:
            errs.append(f"{where}: fact {f.key} has unknown subject_ref {f.subject_ref!r}")
        if f.episode_ref is not None and f.episode_ref not in eps:
            errs.append(f"{where}: fact {f.key} has unknown episode_ref {f.episode_ref!r}")
        if f.noted_at and not TEMPLATE_RE.search(f.noted_at) and not timeutil.has_offset(f.noted_at):
            errs.append(f"{where}: fact {f.key} noted_at needs a timezone offset")


def validate_scenario(sc: Scenario, cat: Catalog) -> list[str]:
    from qeval.checks.expr.parser import ExprSyntaxError, parse_expectation

    errs: list[str] = []
    where = sc.path or sc.id
    if sc.path and Path(sc.path).stem != sc.id:
        errs.append(f"{where}: id {sc.id!r} must match the file name")
    for r in sc.rules:
        if r not in cat.rules:
            errs.append(f"{where}: unknown rule {r!r}")
    if not sc.rules:
        errs.append(f"{where}: needs at least one rule")
    for key, seed in sc.accounts.items():
        check_seed(seed, f"{where} accounts.{key}", errs)

    if sc.persona:
        if sc.turns:
            errs.append(f"{where}: a persona scenario cannot also have turns")
        if sc.persona not in cat.personas:
            errs.append(f"{where}: unknown persona {sc.persona!r}")
    else:
        if not sc.turns:
            errs.append(f"{where}: needs turns (or a persona)")
        has_check = bool(sc.final_expect or sc.final_judge) or any(t.expect or t.judge for t in sc.turns)
        if not has_check:
            errs.append(f"{where}: needs at least one expectation or judge criterion")

    for i, t in enumerate(sc.turns):
        tw = f"{where} turn {i}"
        if t.account not in sc.accounts:
            errs.append(f"{tw}: unknown account {t.account!r}")
        if t.at is not None and not timeutil.valid_time_spec(t.at):
            errs.append(f"{tw}: bad time {t.at!r} (use '-3d 20:00' or ISO 8601 with offset)")
        _check_templates(t.user, tw, sc.accounts, errs)
        for j, e in enumerate(t.expect):
            try:
                parse_expectation(e)
            except ExprSyntaxError as ex:
                errs.append(f"{tw} expect[{j}]: {ex}")
            _check_rule_comment(e, cat, f"{tw} expect[{j}]", errs)
        for rid in t.judge:
            if rid not in cat.rubrics:
                errs.append(f"{tw}: unknown rubric {rid!r}")
    for j, e in enumerate(sc.final_expect):
        try:
            parse_expectation(e)
        except ExprSyntaxError as ex:
            errs.append(f"{where} final_expect[{j}]: {ex}")
        _check_rule_comment(e, cat, f"{where} final_expect[{j}]", errs)
    for rid in sc.final_judge:
        if rid not in cat.rubrics:
            errs.append(f"{where}: unknown rubric {rid!r}")
    return errs


RULE_COMMENT = re.compile(r"#\s*([A-Z]{1,2}\d+)\s*$")


def _check_rule_comment(e: str, cat: Catalog, where: str, errs: list[str]) -> None:
    m = RULE_COMMENT.search(e)
    if m and m.group(1) not in cat.rules:
        errs.append(f"{where}: unknown rule in comment {m.group(1)!r}")


def validate_persona(p: Persona, cat: Catalog) -> list[str]:
    errs: list[str] = []
    where = p.path or p.id
    for r in p.rules:
        if r not in cat.rules:
            errs.append(f"{where}: unknown rule {r!r}")
    check_seed(p.account, where, errs)
    if not timeutil.valid_time_spec(p.opening_at):
        errs.append(f"{where}: bad opening_at {p.opening_at!r}")
    for k in p.reference.get("must_ask", []):
        if k not in p.hidden_facts:
            errs.append(f"{where}: must_ask {k!r} is not a hidden fact")
    return errs


def validate_rubric(r: Rubric, cat: Catalog) -> list[str]:
    from qeval.checks.expr.parser import ExprSyntaxError, parse_expectation
    from qeval.judge.inputs import KNOWN_INPUTS

    errs = []
    where = r.path or r.id
    if r.rule not in cat.rules:
        errs.append(f"{where}: unknown rule {r.rule!r}")
    for i in r.inputs:
        if i not in KNOWN_INPUTS:
            errs.append(f"{where}: unknown input {i!r}")
    if r.auto_when:
        try:
            parse_expectation(r.auto_when)
        except ExprSyntaxError as ex:
            errs.append(f"{where} auto_when: {ex}")
    return errs


def build_catalog(paths: Sequence[str | Path] | None = None) -> Catalog:
    """Load rules, rubrics, personas and the selected scenarios, and validate all of them."""
    from qeval.checks.registry import REGISTRY, load_checks

    errors: list[str] = []
    try:
        rules = load_rules()
    except LoadError as e:
        return Catalog({}, {}, {}, errors=[str(e)])
    try:
        rubrics = load_rubrics()
    except LoadError as e:
        rubrics, errors = {}, errors + [str(e)]
    try:
        personas = load_personas()
    except LoadError as e:
        personas, errors = {}, errors + [str(e)]
    cat = Catalog(rules, rubrics, personas, errors=errors)

    load_checks()
    for cid, spec in REGISTRY.items():
        if spec.rule not in rules:
            cat.errors.append(f"check {cid}: unknown rule {spec.rule!r}")
        if spec.confirm_with and spec.confirm_with not in rubrics:
            cat.errors.append(f"check {cid}: confirm_with names unknown rubric {spec.confirm_with!r}")
    for rid, info in rules.items():
        for c in info.deterministic:
            if c not in REGISTRY:
                cat.warnings.append(f"rules.yaml {rid}: deterministic check {c!r} is not registered")
        for j in info.judge:
            if j not in rubrics:
                cat.warnings.append(f"rules.yaml {rid}: rubric {j!r} has no file yet")

    for r in rubrics.values():
        cat.errors += validate_rubric(r, cat)
    for p in personas.values():
        cat.errors += validate_persona(p, cat)

    files = yaml_files(paths) if paths else []
    seen: dict[str, str] = {}
    for f in files:
        try:
            sc = load_scenario(f)
        except LoadError as e:
            cat.errors.append(str(e))
            continue
        if sc.id in seen:
            cat.errors.append(f"{sc.path}: duplicate scenario id {sc.id!r} (also in {seen[sc.id]})")
            continue
        seen[sc.id] = sc.path
        cat.errors += validate_scenario(sc, cat)
        persona = cat.personas.get(sc.persona) if sc.persona else None
        global_rules = {spec.rule for spec in REGISTRY.values() if spec.scored}
        for rule_id in unmeasured_rules(sc, persona, cat.rubrics, global_rules):
            r_ = rule_id
            cat.errors.append(
                f"{sc.path}: names rule {r_} but nothing measures it (no expectation tagged {r_}, no {r_} rubric, "
                f"no global check); add one or drop the rule"
            )
        cat.scenarios.append(sc)
    ids = {s.id for s in cat.scenarios}
    for sc in cat.scenarios:
        if sc.twin and sc.twin not in ids:
            cat.warnings.append(f"{sc.path}: twin {sc.twin!r} not among the loaded scenarios")
    return cat
