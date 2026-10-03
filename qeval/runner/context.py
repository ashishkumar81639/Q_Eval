"""Shared state for one run: client, repo, limiter, budget, clock."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from qeval.client import QClient
from qeval.config import Settings
from qeval.store.repo import Repo


class SendLimiter:
    """Any two send_message calls, across all workers, are at least `interval_s` apart."""

    def __init__(self, interval_s: float):
        self.interval_s = interval_s
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self) -> None:
        async with self._lock:
            gap = self._last + self.interval_s - time.monotonic()
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = time.monotonic()


@dataclass
class Budget:
    max_agent_messages: int
    max_judge_tokens: int
    agent_messages: int = 0
    llm_tokens: int = 0  # judge + simulated-user tokens (input + output + cache writes) spent by this process
    reserved_messages: int = 0

    def can_start(self, turns_needed: int, *, uses_llm: bool = False) -> bool:
        if self.agent_messages + self.reserved_messages + turns_needed > self.max_agent_messages:
            return False
        return not (uses_llm and self.llm_exhausted())

    def llm_exhausted(self) -> bool:
        return self.llm_tokens >= self.max_judge_tokens

    def add_llm_usage(self, usage: dict) -> None:
        self.llm_tokens += (
            int(usage.get("input_tokens") or 0)
            + int(usage.get("output_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0)
        )


@dataclass
class RunContext:
    settings: Settings
    client: QClient
    repo: Repo
    run_id: str
    anchor: date
    limiter: SendLimiter
    budget: Budget
    sim: Any = None  # SimUser, for persona jobs
    personas: dict = field(default_factory=dict)
    stopping: bool = False
    stats: dict = field(default_factory=lambda: {"valid": 0, "invalid": 0, "errors": 0, "messages": 0})

    async def send(self, account_id: str, text: str, received_at: str):
        await self.limiter.wait()
        res = await self.client.send_message(account_id, text, received_at)
        self.budget.agent_messages += 1
        self.stats["messages"] += 1
        return res
