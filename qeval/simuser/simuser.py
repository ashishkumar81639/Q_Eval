"""Anthropic call that writes the next user message from a hidden persona (build.md 8.1, 8.2)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

from qeval.judge.client import RETRY_WAITS_S, _bad_request, _retryable, thinking_for, usage_dict
from qeval.scenario.models import Persona

log = logging.getLogger(__name__)

SYSTEM_TEMPLATE = """You are role-playing a person using a family health chat app. Stay in character.

Who you are: {style}. You are writing about: {member}.

What you know (only reveal an item when the app asks about it):
{facts}

Rules:
- Answer only what the app's latest message asks. Do not volunteer other items.
- If asked something not in your list, say "{unknown}" in your own words.
- Write 1-2 short sentences, like a real person typing on a phone. No medical terms.
- Never guess a diagnosis, never ask for a diagnosis, never mention you are role-playing.
- If the app asks who you are talking about, answer naturally as this person would.
- List in revealed_facts the keys of any items you used in this message (use the exact keys from the list)."""


class SimTurn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str
    revealed_facts: list[str]


class SimError(Exception):
    pass


def member_description(p: Persona) -> str:
    ref = p.member_ref
    for s in p.account.subjects:
        if ref is None or s.ref == ref:
            bits = [s.relation]
            if s.name:
                bits.append(f"named {s.name}")
            if s.sex:
                bits.append(s.sex)
            if s.date_of_birth:
                bits.append(f"born {s.date_of_birth}")
            if s.age_years is not None:
                bits.append(f"{s.age_years:g} years old")
            if s.age_months is not None:
                bits.append(f"{s.age_months:g} months old")
            return "your " + ", ".join(bits)
    if p.account.holder is not None:
        return "yourself (you are the account holder)"
    return "yourself"


def system_prompt(p: Persona) -> str:
    facts = "\n".join(f"- {k}: {v.value}" for k, v in p.hidden_facts.items())
    return SYSTEM_TEMPLATE.format(
        style=p.style, member=member_description(p), facts=facts, unknown=p.unknown_answer
    )


class SimUser:
    budget: Any = None  # runner.context.Budget, shared with the pool so persona jobs stop at the token cap

    def __init__(
        self,
        *,
        api_key: str | None,
        model: str,
        effort: str,
        repo=None,
        run_id: str | None = None,
        sdk_client=None,
    ):
        self.model = model
        self.effort = effort
        self.repo = repo
        self.run_id = run_id
        if sdk_client is not None:
            self.client = sdk_client
        elif api_key:
            from anthropic import AsyncAnthropic

            self.client = AsyncAnthropic(api_key=api_key, max_retries=0)
        else:
            self.client = None

    async def next_message(self, p: Persona, transcript: list[tuple[str, str]]) -> tuple[str, dict]:
        """`transcript` = [(persona_text, agent_text), ...]; the last agent_text is the latest app message."""
        if self.client is None:
            raise SimError("ANTHROPIC_API_KEY not set (needed for the simulated user)")
        if self.budget is not None and self.budget.llm_exhausted():
            raise SimError("LLM token budget (QEVAL_MAX_JUDGE_TOKENS) exhausted")
        messages: list[dict] = []
        # Roles swapped: the app's messages are 'user', the persona's are 'assistant'.
        for i, (mine, agent) in enumerate(transcript):
            if i == 0:
                messages.append(
                    {
                        "role": "user",
                        "content": "(The conversation starts. You already sent your first message.)",
                    }
                )
            messages.append({"role": "assistant", "content": mine})
            messages.append({"role": "user", "content": agent or "(no reply)"})
        kwargs: dict = {
            "model": self.model,
            "max_tokens": 2000,
            "system": system_prompt(p),
            "messages": messages,
            "output_format": SimTurn,
        }
        if thinking_for(self.model):
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"] = {"effort": self.effort}
        attempt = 0
        while True:
            try:
                msg = await self.client.messages.parse(**kwargs)
                u = usage_dict(getattr(msg, "usage", None))
                if self.repo is not None:
                    self.repo.add_usage(self.run_id, "sim_user", u)
                if self.budget is not None:
                    self.budget.add_llm_usage(u)
                out = msg.parsed_output
                if out is None or not out.message.strip():
                    raise SimError(f"no message (stop_reason={getattr(msg, 'stop_reason', None)})")
                return out.message.strip(), {
                    "revealed_facts": out.revealed_facts,
                    "usage": u,
                    "model": self.model,
                }
            except SimError:
                raise
            except Exception as e:
                if _retryable(e) and attempt < len(RETRY_WAITS_S):
                    await asyncio.sleep(RETRY_WAITS_S[attempt])
                    attempt += 1
                    continue
                if _bad_request(e) and "thinking" in kwargs:
                    kwargs.pop("thinking", None)
                    kwargs.pop("output_config", None)
                    continue
                raise SimError(f"{type(e).__name__}: {e}") from e
