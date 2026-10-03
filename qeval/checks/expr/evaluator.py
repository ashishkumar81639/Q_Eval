"""AST + context -> value, recording each comparison as evidence (build.md 4.4)."""

from __future__ import annotations

import ast as pyast
import re
from collections.abc import Callable
from typing import Any

from lark import Token, Tree

from qeval.checks.expr.parser import parse_expectation, split_comment


class ExprRuntimeError(Exception):
    pass


LAZY_METHODS = {"any", "all", "none", "where"}


class Coll(list):
    """List with query helpers. Items are dicts (facts, episodes, members...) or other objects."""

    @property  # `.count` is a property in the expression language, not list.count(x)
    def count(self) -> int:  # type: ignore[override]
        return len(self)

    @property
    def first(self):
        return self[0] if self else None

    @property
    def last(self):
        return self[-1] if self else None

    @property
    def ids(self) -> list:
        return [_get(x, "id") for x in self]

    @property
    def values(self) -> list:
        return [_get(x, "value") for x in self]

    @property
    def names(self) -> list:
        return [_get(x, "name") for x in self]

    def __call__(self, **filters) -> Coll:
        return Coll(x for x in self if all(_match_field(x, k, v) for k, v in filters.items()))


FIELD_ALIASES = {"subject": "subject_id", "episode": "episode_id", "account": "account_id"}


def _match_field(item, key: str, want) -> bool:
    got = _get(item, FIELD_ALIASES.get(key, key))
    if isinstance(got, str) and isinstance(want, str):
        return got.casefold() == want.casefold()
    return bool(got == want)


def _get(obj, name: str):
    if obj is None:
        return None
    if isinstance(obj, dict):
        v = obj.get(name)
        return Coll(v) if type(v) is list else v
    if isinstance(obj, list) and not isinstance(obj, Coll):
        obj = Coll(obj)
    if name.startswith("_"):
        raise ExprRuntimeError(f"attribute {name!r} not allowed")
    if not hasattr(obj, name):
        raise ExprRuntimeError(f"{type(obj).__name__} has no attribute {name!r}")
    return getattr(obj, name)


def _short(v: Any, limit: int = 300) -> Any:
    if isinstance(v, str):
        return v if len(v) <= limit else v[:limit] + "…"
    if isinstance(v, (list, tuple)):
        out = [_short(x, 120) for x in list(v)[:12]]
        if len(v) > 12:
            out.append(f"… {len(v) - 12} more")
        return out
    if isinstance(v, dict):
        keep = {
            k: v[k]
            for k in (
                "id",
                "key",
                "value",
                "subject_id",
                "episode_id",
                "category",
                "status",
                "complaint",
                "name",
                "relation",
                "kind",
                "urgency",
            )
            if k in v
        }
        return {k: _short(x, 120) for k, x in (keep or v).items()}
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return _short(repr(v))


def _as_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (list, tuple)):
        return "\n".join(_as_text(x) for x in v)
    if isinstance(v, dict):
        return str(v.get("value") or v.get("message") or v)
    return str(v)


def compare(op: str, left, right) -> bool:
    op = re.sub(r"\s+", " ", op)
    if op == "==":
        return bool(left == right)
    if op == "!=":
        return bool(left != right)
    if op in ("<", ">", "<=", ">="):
        if left is None or right is None:
            raise ExprRuntimeError(f"cannot order None ({left!r} {op} {right!r})")
        return {"<": left < right, ">": left > right, "<=": left <= right, ">=": left >= right}[op]
    if op in ("in", "not in"):
        if right is None:
            res = False
        elif isinstance(right, str):
            res = str(left).casefold() in right.casefold() if left is not None else False
        else:
            res = left in right
        return res if op == "in" else not res
    if op in ("~", "!~"):
        pat = str(right)
        try:
            found = re.search(pat, _as_text(left), re.IGNORECASE) is not None
        except re.error as e:
            raise ExprRuntimeError(f"bad regex {pat!r}: {e}") from e
        return found if op == "~" else not found
    raise ExprRuntimeError(f"unknown operator {op}")


class Evaluator:
    def __init__(self, src: str, names: dict[str, Any]):
        self.src, self.rule = split_comment(src)
        self.tree = parse_expectation(src)
        self.names = names
        self.comparisons: list[dict] = []

    def run(self) -> Any:
        return self._eval(self.tree, self.names)

    def _text(self, node: Tree) -> str:
        meta = getattr(node, "meta", None)
        if meta is not None and not meta.empty:
            return self.src[meta.start_pos : meta.end_pos]
        return ""

    # ---- node dispatch ----------------------------------------------------
    def _eval(self, node, scope: dict) -> Any:
        if isinstance(node, Token):
            raise ExprRuntimeError(f"unexpected token {node!r}")
        t = node.data
        if t == "or_":
            return self._truthy(node.children[0], scope) or self._truthy(node.children[1], scope)
        if t == "and_":
            return self._truthy(node.children[0], scope) and self._truthy(node.children[1], scope)
        if t == "not_":
            return not self._truthy(node.children[0], scope)
        if t == "compare":
            left_n, op, right_n = node.children
            left = self._eval(left_n, scope)
            right = self._eval(right_n, scope)
            res = compare(str(op), left, right)
            if scope is self.names:  # record top-level comparisons only (not per-item predicates)
                self.comparisons.append(
                    {
                        "expr": self._text(node),
                        "left": _short(left),
                        "op": str(op),
                        "right": _short(right),
                        "result": res,
                    }
                )
            return res
        if t == "string":
            return pyast.literal_eval(str(node.children[0]))
        if t == "number":
            s = str(node.children[0])
            return float(s) if any(c in s for c in ".eE") else int(s)
        if t == "true":
            return True
        if t == "false":
            return False
        if t == "null":
            return None
        if t == "list":
            return [self._eval(c, scope) for c in node.children]
        if t == "path":
            return self._path(node, scope)
        if t in ("attr", "index", "call", "kwarg"):
            raise ExprRuntimeError(f"unexpected {t}")
        raise ExprRuntimeError(f"unknown node {t}")

    def _truthy(self, node, scope) -> bool:
        v = self._eval(node, scope)
        if isinstance(v, (list, Coll)):
            return len(v) > 0
        return bool(v)

    def _path(self, node: Tree, scope: dict) -> Any:
        name = str(node.children[0])
        if name in scope:
            cur = scope[name]
        elif name in self.names:
            cur = self.names[name]
        else:
            raise ExprRuntimeError(f"unknown name {name!r}")
        trailers = node.children[1:]
        i = 0
        while i < len(trailers):
            tr = trailers[i]
            nxt = trailers[i + 1] if i + 1 < len(trailers) else None
            if tr.data == "attr":
                attr = str(tr.children[0])
                if attr in LAZY_METHODS and isinstance(cur, list) and nxt is not None and nxt.data == "call":
                    cur = self._lazy(attr, cur, nxt.children, scope)
                    i += 2
                    continue
                cur = _get(cur, attr)
            elif tr.data == "index":
                idx = self._eval(tr.children[0], scope)
                try:
                    cur = cur[idx]
                except (IndexError, KeyError, TypeError) as e:
                    raise ExprRuntimeError(f"index {idx!r} out of range on {type(cur).__name__}") from e
            elif tr.data == "call":
                cur = self._call(cur, tr.children, scope)
            i += 1
        return cur

    def _call(self, fn, arg_nodes, scope) -> Any:
        if not callable(fn):
            raise ExprRuntimeError(f"{fn!r} is not callable")
        args, kwargs = [], {}
        for a in arg_nodes:
            if isinstance(a, Tree) and a.data == "kwarg":
                kwargs[str(a.children[0])] = self._eval(a.children[1], scope)
            else:
                args.append(self._eval(a, scope))
        try:
            return fn(*args, **kwargs)
        except ExprRuntimeError:
            raise
        except TypeError as e:
            raise ExprRuntimeError(f"bad call: {e}") from e

    def _lazy(self, method: str, items: list, arg_nodes, scope) -> Any:
        if len(arg_nodes) != 1:
            raise ExprRuntimeError(f".{method}() takes one predicate")
        pred = arg_nodes[0]
        results = []
        for item in items:
            inner = dict(scope)
            if isinstance(item, dict):
                inner.update(item)
            inner["it"] = item
            results.append((item, self._truthy(pred, inner)))
        if method == "any":
            out: Any = any(r for _, r in results)
        elif method == "all":
            out = all(r for _, r in results)
        elif method == "none":
            out = not any(r for _, r in results)
        else:
            out = Coll(item for item, r in results if r)
        if scope is self.names:
            self.comparisons.append(
                {
                    "expr": f".{method}({self._text(pred)})",
                    "items": _short(list(items)),
                    "matched": _short([item for item, r in results if r]),
                    "result": out if isinstance(out, bool) else len(out),
                }
            )
        return out


def evaluate(src: str, names: dict[str, Any]) -> tuple[Any, list[dict]]:
    ev = Evaluator(src, names)
    return ev.run(), ev.comparisons


FunctionTable = dict[str, Callable[..., Any]]
