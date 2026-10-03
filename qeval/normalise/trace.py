"""Raw trace list -> steps and tool calls."""

from __future__ import annotations

from qeval.normalise.models import ToolCall, Trace


def build_trace(raw_trace: list[dict] | None) -> Trace:
    calls: list[ToolCall] = []
    texts: list[str] = []
    by_step: dict[int, list[ToolCall]] = {}
    for e in raw_trace or []:
        if e.get("type") == "text":
            if e.get("text"):
                texts.append(e["text"])
            continue
        if e.get("type") != "tool_call":
            continue
        c = ToolCall(
            step=int(e.get("step") or 0),
            tool=str(e.get("tool")),
            input=e.get("input") or {},
            output=e.get("output"),
            is_error=bool(e.get("is_error")),
        )
        calls.append(c)
        by_step.setdefault(c.step, []).append(c)
    steps = [by_step[s] for s in sorted(by_step) if by_step[s]]
    return Trace(
        steps=steps,
        calls=calls,
        texts=texts,
        respond_calls=[c for c in calls if c.tool == "respond"],
        errors=[c for c in calls if c.is_error],
    )
