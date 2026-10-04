"""Expectation string -> AST (lark tree). Done once at validation time."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from lark import Lark, Tree
from lark.exceptions import LarkError

# Trailing comment: "# W4" or "# W4: should ask which Meera" (rule id plus an optional plain note).
RULE_COMMENT = re.compile(r"\s+#\s*([A-Z]{1,2}\d+)(?:\s*:\s*(.*?))?\s*$")
_GRAMMAR = (Path(__file__).parent / "grammar.lark").read_text()
_PARSER = Lark(_GRAMMAR, parser="lalr", propagate_positions=True, maybe_placeholders=False)


class ExprSyntaxError(ValueError):
    pass


def split_comment(src: str) -> tuple[str, str | None]:
    """`"reply.kind == 'clarify'   # W4"` -> ("reply.kind == 'clarify'", "W4")."""
    m = RULE_COMMENT.search(src)
    if m:
        return src[: m.start()].strip(), m.group(1)
    return src.strip(), None


def comment_note(src: str) -> str | None:
    """The plain-language note after the rule id, if any: "# W4: should ask which Meera" -> "should ask..."."""
    m = RULE_COMMENT.search(src)
    return (m.group(2) or None) if m else None


@lru_cache(maxsize=4096)
def parse_expectation(src: str) -> Tree:
    body, _ = split_comment(src)
    if not body:
        raise ExprSyntaxError("empty expression")
    try:
        return _PARSER.parse(body)
    except LarkError as e:
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        raise ExprSyntaxError(f"cannot parse {body!r}: {msg}") from None
