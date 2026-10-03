"""Asyncio worker pool: concurrency, attempts, budget guard, auth abort, Ctrl-C (build.md 7.2)."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections import deque

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn

from qeval.client import AuthError
from qeval.runner.context import RunContext
from qeval.runner.planner import Job

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


async def _run_job(job: Job, ctx: RunContext) -> str:
    if job.scenario.kind == "persona":
        from qeval.runner.persona_executor import run_persona

        return await run_persona(job, ctx)
    from qeval.runner.executor import run_scripted

    return await run_scripted(job, ctx)


def _turns_needed(job: Job, ctx: RunContext) -> int:
    if job.scenario.kind == "persona":
        p = ctx.personas.get(job.scenario.persona)
        return p.max_turns if p else 8
    return len(job.scenario.turns)


async def run_pool(jobs: list[Job], ctx: RunContext, *, concurrency: int, show_progress: bool = True) -> str:
    """Run all jobs; returns the run status: done | aborted."""
    queue: deque[Job] = deque(jobs)
    status = "done"
    aborted_reason: list[str] = []
    in_flight: set[asyncio.Task] = set()
    sem = asyncio.Semaphore(concurrency)
    loop = asyncio.get_running_loop()
    presses = {"n": 0}

    def on_sigint() -> None:
        presses["n"] += 1
        ctx.stopping = True
        if presses["n"] == 1:
            log.warning("Ctrl-C: finishing current turns, no new jobs (press again to cancel now)")
        else:
            for t in in_flight:
                t.cancel()

    try:
        loop.add_signal_handler(signal.SIGINT, on_sigint)
        sig_installed = True
    except NotImplementedError, RuntimeError:
        sig_installed = False

    progress = Progress(
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("valid {task.fields[valid]} invalid {task.fields[invalid]} msgs {task.fields[msgs]}"),
        TimeElapsedColumn(),
        disable=not show_progress,
    )
    with progress:
        task_id = progress.add_task("jobs", total=len(jobs), valid=0, invalid=0, msgs=0)

        async def worker_job(job: Job) -> None:
            nonlocal status
            async with sem:
                if ctx.stopping:
                    return
                try:
                    result = await _run_job(job, ctx)
                except AuthError as e:
                    aborted_reason.append(f"auth: {e}")
                    ctx.stopping = True
                    for t in in_flight:
                        if t is not asyncio.current_task():
                            t.cancel()
                    return
                except asyncio.CancelledError:
                    return
                finally:
                    ctx.budget.reserved_messages -= _turns_needed(job, ctx)
                if result == "valid":
                    ctx.stats["valid"] += 1
                    progress.advance(task_id)
                elif result == "invalid" and job.retries < MAX_ATTEMPTS - 1 and not ctx.stopping:
                    ctx.stats["invalid"] += 1
                    queue.appendleft(Job(job.scenario, job.rep, job.attempt + 1, job.retries + 1))
                else:
                    if result == "invalid":
                        ctx.stats["invalid"] += 1
                    else:
                        ctx.stats["errors"] += 1
                    progress.advance(task_id)
                progress.update(
                    task_id,
                    valid=ctx.stats["valid"],
                    invalid=ctx.stats["invalid"],
                    msgs=ctx.stats["messages"],
                )

        while (queue or in_flight) and not (ctx.stopping and not in_flight):
            while queue and len(in_flight) < concurrency and not ctx.stopping:
                job = queue.popleft()
                need = _turns_needed(job, ctx)
                if not ctx.budget.can_start(need, uses_llm=job.scenario.kind == "persona"):
                    aborted_reason.append("budget")
                    ctx.stopping = True
                    queue.appendleft(job)
                    break
                ctx.budget.reserved_messages += need
                t = asyncio.create_task(worker_job(job), name=job.label())
                in_flight.add(t)
                t.add_done_callback(in_flight.discard)
            if in_flight:
                await asyncio.wait(set(in_flight), return_when=asyncio.FIRST_COMPLETED)
            elif ctx.stopping:
                break

    if sig_installed:
        loop.remove_signal_handler(signal.SIGINT)
    if aborted_reason or ctx.stopping or queue:
        status = "aborted"
        if aborted_reason:
            log.warning("run aborted: %s", "; ".join(aborted_reason))
        if any(r.startswith("auth") for r in aborted_reason):
            raise AuthError(aborted_reason[0])
    return status
