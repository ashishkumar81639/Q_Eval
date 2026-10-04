"""Anthropic judge call with structured output, retries and a verdict cache (build.md 11.3, 11.4)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

from qeval.scenario.models import Rubric, RuleInfo
from qeval.store.repo import Repo

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a strict evaluator checking one rule of a household health assistant's behaviour.
Judge only the question asked, using only the material provided. Do not judge other qualities.
If the material is not enough to decide, answer "unclear".
Quote the exact words your verdict depends on in evidence_quote (empty string if nothing to quote).
Keep reason to one or two sentences. confidence is your probability (0 to 1) that the verdict is right."""

RETRY_WAITS_S = [2, 4, 8, 16, 32]


class JudgeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")  # schema gets additionalProperties: false
    verdict: Literal["pass", "fail", "unclear"]
    evidence_quote: str
    reason: str
    confidence: float


@dataclass
class JudgeResult:
    verdict: str  # pass | fail | unclear | error
    evidence_quote: str = ""
    reason: str = ""
    confidence: float = 0.0
    cached: bool = False
    usage: dict | None = None
    error: str = ""

    def as_evidence(self) -> dict:
        return {
            "quote": self.evidence_quote,
            "reason": self.reason,
            "confidence": self.confidence,
            "cached": self.cached,
            **({"error": self.error} if self.error else {}),
        }


def rubric_prefix(rubric: Rubric, rule: RuleInfo | None) -> str:
    ex = ""
    if rubric.examples:
        parts = []
        for label, items in rubric.examples.items():
            for it in items:
                parts.append(f"<{label}>{it}</{label}>")
        ex = "\n".join(parts)
    return (
        f"<rule>{rubric.rule}: {rule.summary if rule else ''}</rule>\n"
        f"<question>{rubric.question.strip()}</question>\n"
        f"<pass_if>{rubric.pass_if.strip()}</pass_if>\n"
        f"<fail_if>{rubric.fail_if.strip()}</fail_if>\n"
        f"<do_not_penalise>{rubric.do_not_penalise.strip()}</do_not_penalise>\n"
        f"<examples>\n{ex}\n</examples>"
    )


def material_block(inputs: dict[str, str]) -> str:
    body = "\n".join(f"<{k}>\n{v}\n</{k}>" for k, v in inputs.items())
    return f"<material>\n{body}\n</material>"


def _source_hash(module_file: str) -> str:
    from pathlib import Path

    return hashlib.sha256(Path(module_file).read_bytes()).hexdigest()[:16]


def cache_key(model: str, rubric: Rubric, inputs: dict[str, str], effort: str = "") -> str:
    """Everything that can change a verdict: the full rubric text (not just its version number), the system
    prompt, the prompt layout, the input builder's code, the model and the effort level."""
    from qeval.judge import inputs as inputs_mod

    raw = json.dumps(
        {
            "model": model,
            "effort": effort,
            "rubric": rubric.model_dump(exclude={"path"}),
            "system": SYSTEM_PROMPT,
            "prefix": rubric_prefix(rubric, None),
            "input_builder": _source_hash(inputs_mod.__file__),
            "inputs": inputs,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def usage_dict(u) -> dict:
    if u is None:
        return {}
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


def thinking_for(model: str) -> dict | None:
    if (
        model.startswith("claude-haiku-4")
        or model.startswith("claude-sonnet-4-5")
        or model.startswith("claude-opus-4-5")
    ):
        return None
    return {"type": "adaptive"}


class Judge:
    def __init__(
        self,
        repo: Repo,
        *,
        api_key: str | None,
        model: str,
        effort: str,
        rules: dict[str, RuleInfo],
        run_id: str | None,
        max_tokens_budget: int,
        concurrency: int = 4,
        sdk_client=None,
    ):
        self.repo = repo
        self.model = model
        self.effort = effort
        self.rules = rules
        self.run_id = run_id
        self.max_tokens_budget = max_tokens_budget
        self.tokens_used = 0
        self.sem = asyncio.Semaphore(concurrency)
        self.calls = 0
        self.cache_hits = 0
        if sdk_client is not None:
            self.client = sdk_client
        elif api_key:
            from anthropic import AsyncAnthropic

            self.client = AsyncAnthropic(api_key=api_key, max_retries=0)
        else:
            self.client = None

    async def judge(self, rubric: Rubric, inputs: dict[str, str], *, use_cache: bool = True) -> JudgeResult:
        """`use_cache=False` forces a fresh call and doesn't store it (stability checks)."""
        key = cache_key(self.model, rubric, inputs, self.effort)
        hit = self.repo.judge_cache_get(key) if use_cache else None
        if hit:
            self.cache_hits += 1
            return JudgeResult(
                hit["verdict"],
                hit.get("evidence_quote", ""),
                hit.get("reason", ""),
                float(hit.get("confidence", 0)),
                cached=True,
            )
        if self.client is None:
            return JudgeResult("error", error="no ANTHROPIC_API_KEY")
        if self.tokens_used >= self.max_tokens_budget:
            return JudgeResult("error", error="budget")
        async with self.sem:
            res = await self._call(rubric, inputs)
        if res.verdict != "error" and use_cache:
            self.repo.judge_cache_put(
                key,
                self.model,
                rubric.id,
                rubric.version,
                {
                    "verdict": res.verdict,
                    "evidence_quote": res.evidence_quote,
                    "reason": res.reason,
                    "confidence": res.confidence,
                },
                res.usage,
            )
        return res

    async def _call(self, rubric: Rubric, inputs: dict[str, str]) -> JudgeResult:
        system = [{"type": "text", "text": SYSTEM_PROMPT}]
        user_content = [
            # Rubric text is the same for every call of this rubric: cache it as a prefix.
            {
                "type": "text",
                "text": rubric_prefix(rubric, self.rules.get(rubric.rule)),
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": material_block(inputs)},
        ]
        kwargs: dict = {
            "model": self.model,
            "max_tokens": 4000,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
            "output_format": JudgeVerdict,
        }
        thinking = thinking_for(self.model)
        if thinking:
            kwargs["thinking"] = thinking
            kwargs["output_config"] = {"effort": self.effort}
        parse_failures = 0
        attempt = 0
        while True:
            try:
                self.calls += 1
                msg = await self.client.messages.parse(**kwargs)
                u = usage_dict(getattr(msg, "usage", None))
                self.tokens_used += (
                    u.get("input_tokens", 0)
                    + u.get("output_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0)
                )
                self.repo.add_usage(self.run_id, "judge", u)
                out = getattr(msg, "parsed_output", None)
                if out is None:
                    parse_failures += 1
                    if parse_failures > 1:
                        return JudgeResult(
                            "error",
                            usage=u,
                            error=f"no parsed output (stop_reason={getattr(msg, 'stop_reason', None)})",
                        )
                    continue
                conf = min(1.0, max(0.0, float(out.confidence)))
                return JudgeResult(out.verdict, out.evidence_quote, out.reason, conf, usage=u)
            except Exception as e:
                if _retryable(e) and attempt < len(RETRY_WAITS_S):
                    await asyncio.sleep(RETRY_WAITS_S[attempt])
                    attempt += 1
                    continue
                if _parse_error(e) and parse_failures < 1:
                    parse_failures += 1
                    continue
                if _bad_request(e) and "thinking" in kwargs:
                    log.warning("judge: model rejected thinking/effort (%s); retrying without", str(e)[:200])
                    kwargs.pop("thinking", None)
                    kwargs.pop("output_config", None)
                    continue
                log.warning("judge %s failed: %s", rubric.id, e)
                return JudgeResult("error", error=f"{type(e).__name__}: {str(e)[:300]}")


def _retryable(e: Exception) -> bool:
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return False
    if isinstance(e, (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    return isinstance(e, anthropic.APIStatusError) and e.status_code in (429, 500, 502, 503, 529)


def _bad_request(e: Exception) -> bool:
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return False
    return isinstance(e, anthropic.BadRequestError)


def _parse_error(e: Exception) -> bool:
    return isinstance(e, (ValueError,)) and not isinstance(e, TypeError)
