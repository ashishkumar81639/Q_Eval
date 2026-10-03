"""Run one scripted Job against the live API and save everything (build.md 7.3)."""

from __future__ import annotations

import logging
import traceback
from datetime import datetime, timedelta

from qeval import timeutil
from qeval.client import AgentError, AuthError, BusyError, NotFoundError, ScenarioError, TransportError
from qeval.runner.context import RunContext
from qeval.runner.planner import Job
from qeval.scenario.loader import snapshot
from qeval.scenario.seed import build_create_body
from qeval.scenario.templating import render
from qeval.store.repo import utcnow

log = logging.getLogger(__name__)

DEFAULT_GAP = timedelta(minutes=2)


def resolve_turn_times(
    turn_specs: list[str | None], anchor, now: datetime | None = None
) -> tuple[list[datetime], int]:
    """Resolve all turn times; returns (times after the future-guard shift, shift_days)."""
    now = now or timeutil.now_ist()
    raw: list[datetime] = []
    for spec in turn_specs:
        if spec is None:
            raw.append(raw[-1] + DEFAULT_GAP if raw else now - timedelta(minutes=30))
        else:
            raw.append(timeutil.resolve_time(spec, anchor))
    shift = 0
    if raw and max(raw) > now:
        shift = (max(raw) - now).days + 1
    return [t - timedelta(days=shift) for t in raw], shift


async def create_accounts(job_id: str, seeds: dict, ctx: RunContext, shift: int) -> tuple[dict, dict]:
    accounts: dict[str, dict] = {}
    initial: dict[str, dict] = {}
    for key, seed in seeds.items():
        body = build_create_body(seed, anchor=ctx.anchor, shift_days=shift)
        res = await ctx.client.create_account(body)
        accounts[key] = {"account_id": res.account_id, "refs": res.refs, "seed_body": body}
        initial[key] = res.state
        ctx.repo.update_scenario_run(job_id, accounts=accounts, initial_states=initial)
    return accounts, initial


async def run_scripted(job: Job, ctx: RunContext) -> str:
    sc = job.scenario
    times, shift = resolve_turn_times([t.at for t in sc.turns], ctx.anchor)
    sr_id = ctx.repo.upsert_scenario_run(
        {
            "run_id": ctx.run_id,
            "scenario_id": sc.id,
            "scenario_hash": sc.content_hash,
            "scenario_path": sc.path,
            "rep": job.rep,
            "attempt": job.attempt,
            "kind": "scripted",
            "status": "running",
            "time_shift_days": shift,
            "scenario_snapshot": snapshot(sc),
            "started_at": utcnow(),
        }
    )
    try:
        accounts, _ = await create_accounts(sr_id, sc.accounts, ctx, shift)
        multi = len(accounts) > 1
        for i, (turn, at) in enumerate(zip(sc.turns, times, strict=True)):
            if ctx.stopping:
                ctx.repo.set_scenario_run_status(sr_id, "interrupted", "stopped by user")
                return "interrupted"
            acc = accounts[turn.account]
            text = render(turn.user, anchor=ctx.anchor, shift_days=shift, accounts=accounts)
            received_at = timeutil.iso(at)
            others_before = {}
            if multi:
                for k, a in accounts.items():
                    if k != turn.account:
                        others_before[k] = await ctx.client.get_state(a["account_id"])
            msg = await ctx.send(acc["account_id"], text, received_at)
            state_after = (
                await ctx.client.get_state(acc["account_id"]) if ctx.settings.state_each_turn else None
            )
            aux: dict | None = None
            if multi:
                aux = {"others": {}}
                for k, before in others_before.items():
                    after = await ctx.client.get_state(accounts[k]["account_id"])
                    aux["others"][k] = {"before": before, "after": after}
            ctx.repo.add_turn(
                {
                    "scenario_run_id": sr_id,
                    "idx": i,
                    "account_key": turn.account,
                    "actor": "script",
                    "sent_text": text,
                    "received_at": received_at,
                    "response": msg.raw,
                    "state_after": state_after,
                    "latency_ms": msg.latency_ms,
                    "http_ms": msg.http_ms,
                    "usage": msg.raw.get("usage"),
                    "aux": aux,
                }
            )
            ctx.repo.add_usage(ctx.run_id, "agent", msg.raw.get("usage"))
        finals = {k: await ctx.client.get_state(a["account_id"]) for k, a in accounts.items()}
        ctx.repo.update_scenario_run(sr_id, final_states=finals)
        ctx.repo.set_scenario_run_status(sr_id, "valid")
        return "valid"
    except AuthError:
        ctx.repo.set_scenario_run_status(sr_id, "interrupted", "auth error")
        raise
    except (AgentError, BusyError, TransportError) as e:
        log.warning("%s invalid: %s", job.label(), e)
        ctx.repo.set_scenario_run_status(sr_id, "invalid", str(e))
        return "invalid"
    except ScenarioError as e:
        log.error("%s scenario error: %s", job.label(), e)
        ctx.repo.set_scenario_run_status(sr_id, "scenario_error", f"{e} detail={e.detail!r}")
        return "scenario_error"
    except NotFoundError as e:
        ctx.repo.set_scenario_run_status(sr_id, "harness_error", str(e))
        return "harness_error"
    except Exception:
        log.exception("%s harness error", job.label())
        ctx.repo.set_scenario_run_status(sr_id, "harness_error", traceback.format_exc()[-3000:])
        return "harness_error"
    except BaseException:
        ctx.repo.set_scenario_run_status(sr_id, "interrupted", "cancelled")
        raise
