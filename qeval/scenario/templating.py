"""Template substitution: {{t:...}}, {{date:...}}, {{A.refs.x}}, {{A.account_id}} (build.md 4.3)."""

from __future__ import annotations

import re
from datetime import date

from qeval import timeutil

TEMPLATE_RE = re.compile(r"\{\{\s*([^}]+?)\s*\}\}")


class TemplateError(ValueError):
    pass


def render(text: str, *, anchor: date, shift_days: int = 0, accounts: dict[str, dict] | None = None) -> str:
    """Resolve every template in `text`.

    `accounts` maps account key -> {"account_id": ..., "refs": {...}} for cross-account references.
    """

    def sub(m: re.Match) -> str:
        expr = m.group(1)
        if expr.startswith("t:"):
            return timeutil.iso(timeutil.resolve_time(expr[2:].strip(), anchor, shift_days))
        if expr.startswith("date:"):
            return timeutil.resolve_date(expr[5:].strip(), anchor).isoformat()
        parts = expr.split(".")
        if accounts is not None and parts[0] in accounts:
            acc = accounts[parts[0]]
            if len(parts) == 2 and parts[1] == "account_id":
                return acc["account_id"]
            if len(parts) == 3 and parts[1] == "refs":
                try:
                    return acc["refs"][parts[2]]
                except KeyError as e:
                    raise TemplateError(f"unknown ref {parts[2]!r} in account {parts[0]!r}") from e
        if accounts is None and parts[0] and len(parts) in (2, 3):
            # Cross-account template; resolved later once accounts exist.
            return m.group(0)
        raise TemplateError(f"unknown template {{{{{expr}}}}}")

    return TEMPLATE_RE.sub(sub, text)


def template_names(text: str) -> list[str]:
    return [m.group(1) for m in TEMPLATE_RE.finditer(text)]


def render_obj(obj, **kw):
    """Render templates in every string inside a nested dict/list."""
    if isinstance(obj, str):
        return render(obj, **kw)
    if isinstance(obj, list):
        return [render_obj(x, **kw) for x in obj]
    if isinstance(obj, dict):
        return {k: render_obj(v, **kw) for k, v in obj.items()}
    return obj
