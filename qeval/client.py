"""QClient: the Q API wrapper, its error types and retry policy (build.md section 5)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

BUSY_WAITS_S = [2, 4, 8] + [10] * 7  # 10 retries on 409/429
TRANSPORT_WAITS_S = [1, 2, 4]  # 3 retries on connection errors for idempotent calls


class QError(Exception):
    def __init__(self, message: str, *, status: int | None = None, body: str = "", request: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body
        self.request = request

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.request:
            parts.append(f"request={self.request}")
        if self.body:
            parts.append(f"body={self.body[:500]}")
        return " ".join(parts)


class AuthError(QError):
    """401/403: abort the whole run."""


class NotFoundError(QError):
    """404: harness bug, fail the job."""


class ScenarioError(QError):
    """422: scenario bug; never counted against the agent."""

    @property
    def detail(self) -> Any:
        try:
            import json

            return json.loads(self.body).get("detail")
        except Exception:
            return self.body


class BusyError(QError):
    """409/429 after all retries."""


class AgentError(QError):
    """502/5xx/timeout on send_message: the attempt is invalid."""


class TransportError(QError):
    """Connection errors after retries on idempotent calls."""


@dataclass
class CreateResult:
    account_id: str
    refs: dict[str, str]
    state: dict
    raw: dict


@dataclass
class MessageResult:
    raw: dict
    latency_ms: int
    http_ms: int


class QClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_s: float = 120,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep=asyncio.sleep,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout_s, connect=15),
            transport=transport,
        )
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> QClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    # ---- public API -------------------------------------------------------

    async def version(self) -> dict:
        r = await self._idempotent("GET", "/version")
        return r.json()

    async def create_account(self, body: dict) -> CreateResult:
        r = await self._idempotent("POST", "/accounts", json=body)
        data = r.json()
        return CreateResult(data["account_id"], data.get("refs") or {}, data.get("state") or {}, data)

    async def send_message(self, account_id: str, text: str, received_at: str) -> MessageResult:
        path = f"/accounts/{account_id}/messages"
        body = {"text": text, "received_at": received_at}
        summary = f"POST {path} received_at={received_at}"
        for attempt, wait in enumerate([0, *BUSY_WAITS_S]):
            if wait:
                await self._sleep(wait)
            t0 = time.monotonic()
            try:
                r = await self._http.post(path, json=body)
            except httpx.TimeoutException as e:
                raise AgentError(f"timeout on send_message: {e!r}", request=summary) from e
            except httpx.TransportError as e:
                # The server may or may not have accepted the message; state is unknown.
                raise AgentError(f"transport error on send_message: {e!r}", request=summary) from e
            http_ms = int((time.monotonic() - t0) * 1000)
            if r.status_code in (409, 429):
                log.info("busy (%s) on %s, attempt %d", r.status_code, summary, attempt + 1)
                continue
            self._raise_for_status(r, summary, agent_call=True)
            data = r.json()
            return MessageResult(raw=data, latency_ms=int(data.get("latency_ms") or 0), http_ms=http_ms)
        raise BusyError("account still busy after retries", status=409, request=summary)

    async def get_state(self, account_id: str) -> dict:
        r = await self._idempotent("GET", f"/accounts/{account_id}/state")
        return r.json()

    async def delete_account(self, account_id: str) -> None:
        await self._idempotent("DELETE", f"/accounts/{account_id}")

    # ---- internals --------------------------------------------------------

    async def _idempotent(self, method: str, path: str, **kw) -> httpx.Response:
        summary = f"{method} {path}"
        busy_tries = 0
        transport_tries = 0
        while True:
            try:
                r = await self._http.request(method, path, **kw)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                if transport_tries >= len(TRANSPORT_WAITS_S):
                    raise TransportError(f"connection failed: {e!r}", request=summary) from e
                await self._sleep(TRANSPORT_WAITS_S[transport_tries])
                transport_tries += 1
                continue
            if r.status_code in (409, 429):
                if busy_tries >= len(BUSY_WAITS_S):
                    raise BusyError("busy after retries", status=r.status_code, request=summary)
                await self._sleep(BUSY_WAITS_S[busy_tries])
                busy_tries += 1
                continue
            self._raise_for_status(r, summary, agent_call=False)
            return r

    @staticmethod
    def _raise_for_status(r: httpx.Response, summary: str, *, agent_call: bool) -> None:
        s = r.status_code
        if s < 400:
            return
        body = r.text
        if s in (401, 403):
            raise AuthError("authentication failed", status=s, body=body, request=summary)
        if s == 404:
            raise NotFoundError("not found", status=s, body=body, request=summary)
        if s == 422:
            raise ScenarioError("request rejected (422)", status=s, body=body, request=summary)
        if s >= 500 and agent_call:
            raise AgentError("agent error", status=s, body=body, request=summary)
        if s >= 500:
            raise TransportError("server error", status=s, body=body, request=summary)
        raise QError("unexpected status", status=s, body=body, request=summary)
