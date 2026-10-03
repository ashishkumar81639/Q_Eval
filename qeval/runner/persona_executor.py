"""Run one simulated-user Job (build.md 7.4)."""

from __future__ import annotations

import logging
import traceback
from datetime import timedelta

from qeval import timeutil
from qeval.client import AgentError, AuthError, BusyError, NotFoundError, ScenarioError, TransportError
from qeval.runner.context import RunContext
from qeval.runner.executor import create_accounts
from qeval.runner.planner import Job
from qeval.scenario.loader import snapshot
from qeval.simuser.simuser import SimError
from qeval.store.repo import utcnow

log = logging.getLogger(__name__)


async def run_persona(job: Job, ctx: RunContext) -> str:
    sc = job.scenario
    p = ctx.personas[sc.persona]
    start = timeutil.resolve_time(p.opening_at, ctx.anchor)
    last = start + timedelta(minutes=p.turn_gap_minutes * (p.max_turns + len(p.after_assessment)))
    now = timeutil.now_ist()
    shift = (last - now).days + 1 if last > now else 0
    t = start - timedelta(days=shift)
    sr_id = ctx.repo.upsert_scenario_run(
        {
            "run_id": ctx.run_id,
            "scenario_id": sc.id,
            "scenario_hash": sc.content_hash,
            "scenario_path": sc.path,
            "rep": job.rep,
            "attempt": job.attempt,
            "kind": "persona",
            "status": "running",
            "time_shift_days": shift,
            "scenario_snapshot": snapshot(sc, p),
            "started_at": utcnow(),
        }
    )
    flags: dict = {}
    try:
        accounts, _ = await create_accounts(sr_id, {"main": p.account}, ctx, shift)
        acc_id = accounts["main"]["account_id"]
        user_text, actor, sim_meta = p.opening, "script", None
        transcript: list[tuple[str, str]] = []
        after = list(p.after_assessment)
        agent_turns = 0
        assessed = False
        for idx in range(p.max_turns + len(p.after_assessment)):
            if ctx.stopping:
                ctx.repo.set_scenario_run_status(sr_id, "interrupted", "stopped by user")
                return "interrupted"
            received_at = timeutil.iso(t)
            msg = await ctx.send(acc_id, user_text, received_at)
            state_after = await ctx.client.get_state(acc_id) if ctx.settings.state_each_turn else None
            ctx.repo.add_turn(
                {
                    "scenario_run_id": sr_id,
                    "idx": idx,
                    "account_key": "main",
                    "actor": actor,
                    "sent_text": user_text,
                    "received_at": received_at,
                    "response": msg.raw,
                    "state_after": state_after,
                    "latency_ms": msg.latency_ms,
                    "http_ms": msg.http_ms,
                    "usage": msg.raw.get("usage"),
                    "sim_meta": sim_meta,
                }
            )
            ctx.repo.add_usage(ctx.run_id, "agent", msg.raw.get("usage"))
            reply = msg.raw.get("respond") or {}
            agent_turns += 1
            transcript.append((user_text, reply.get("message") or ""))
            t = t + timedelta(minutes=p.turn_gap_minutes)
            if reply.get("kind") == "assessment":
                assessed = True
                if after:
                    user_text, actor, sim_meta = after.pop(0), "script", None
                    continue
                break
            if agent_turns >= p.max_turns:
                break
            if p.late_reveal and agent_turns == p.late_reveal.after_agent_turn:
                user_text, actor, sim_meta = p.late_reveal.text, "script", None
            else:
                if ctx.sim is None:
                    raise SimError("simulated user not configured")
                user_text, sim_meta = await ctx.sim.next_message(p, transcript)
                actor = "sim_user"
        if not assessed:
            flags["no_assessment_within_max_turns"] = True
        finals = {"main": await ctx.client.get_state(acc_id)}
        ctx.repo.update_scenario_run(sr_id, final_states=finals, flags=flags)
        ctx.repo.set_scenario_run_status(sr_id, "valid")
        return "valid"
    except AuthError:
        ctx.repo.set_scenario_run_status(sr_id, "interrupted", "auth error")
        raise
    except (AgentError, BusyError, TransportError) as e:
        ctx.repo.set_scenario_run_status(sr_id, "invalid", str(e))
        return "invalid"
    except SimError as e:
        ctx.repo.set_scenario_run_status(sr_id, "harness_error", f"simulated user: {e}")
        return "harness_error"
    except ScenarioError as e:
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
