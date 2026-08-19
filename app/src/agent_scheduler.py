# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The agent scheduler: the management-layer loop that runs agents unattended.

Lives in the WORKER (an asyncio task started at WORKER_STARTUP, like the
agent-API). Each tick it RESCANS the agent definitions - the rescan IS the
registry reconcile (gap #5's watcher→registry-cache chain was superseded once
on-demand scanning proved sufficient): a deleted file simply isn't scheduled
next tick; an edited file's new schedule takes effect next tick.

Firing model: `schedule:` presence is the auto-vs-manual switch. Each agent
has a redis last-run stamp (`agent:lastrun:{slug}`); an agent fires when
`next_due(schedule, last_run)` has passed. First sight of a new agent stamps
now WITHOUT firing (its first run lands at the next natural occurrence - no
thundering herd on deploy). The task-tracker `is_active` dedup plus the 
serializing run lock give overlap coalescing for free: a run that outlives
the interval just delays the next one.

Housekeeping (staged-batch GC, the durable failure drain + prune, the hourly
disk-vs-index reconcile) lives in a SEPARATE always-on `maintenance_loop` at the
bottom of this file. It used to ride this tick, which meant setting
AGENT_SCHEDULER_ENABLED=false silently disabled garbage collection too.
"""

import asyncio
import datetime
import logging

from config import (
    AGENT_SCHEDULER_ENABLED,
    AGENT_SCHEDULER_TICK_S,
    AGENT_STAGING_TTL_DAYS,
    FAILURE_LOG_TTL_DAYS,
)
from src.task_broker import get_async_redis
from src import timefmt

logger = logging.getLogger("agent_scheduler")

LASTRUN_KEY = "agent:lastrun:{slug}"
_LEGACY_STAMP_FMT = "%Y-%m-%dT%H:%M:%S"


async def get_last_run(r, slug: str) -> datetime.datetime | None:
    """The last-run stamp as a NAIVE LOCAL datetime - the clock next_due works in.

    Stamps written before the offset-carrying format was adopted have no zone at
    all; they are read as local, which is what they were.
    """
    raw = await r.get(LASTRUN_KEY.format(slug=slug))
    if not raw:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = datetime.datetime.strptime(raw, _LEGACY_STAMP_FMT)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


async def set_last_run(r, slug: str, when: datetime.datetime) -> None:
    # Offset-carrying: a zone-less stamp silently changes meaning if TZ is edited.
    await r.set(LASTRUN_KEY.format(slug=slug), timefmt.iso_local(when))


async def _fire(slug: str, target_vaults: list[str] | None = None) -> bool:
    """Worker-side twin of main._fire_agent_task (same job-id + dedup shape).

    Dedup checks BOTH job-id forms: the ``:all`` id this path enqueues AND the
    per-vault ids event-triggered runs use (src.events._fire_event_run) - a
    scheduled fire must coalesce with an active event-fired run of the same
    agent, not stack behind it on the run lock.

    Returns True when this occurrence is COVERED (enqueued, or an ``:all`` run
    is already active - a duplicate of this very fan-out), False when it is not.
    The return is now INFORMATIONAL only: last-run is stamped by the worker when
    a run actually acquires the global lock (task_definitions._stamp_agent_run),
    not by this path's caller, so an occurrence that is enqueued but later
    deferred stays due and retries instead of silently advancing the schedule."""
    from src import agent_registry
    from src.task_definitions import run_agent_task
    from src.task_tracker import IN_PROGRESS_KEY, PENDING_KEY, preseed_pending

    job_id = agent_registry.agent_job_id(slug)
    per_vault = [agent_registry.agent_job_id(slug, v)
                 for v in (target_vaults or [])]
    r = get_async_redis()
    try:
        # is_active equivalent without the web-process TaskTracker instance.
        for jid in [job_id] + per_vault:
            if (await r.hexists(PENDING_KEY, jid)
                    or await r.hexists(IN_PROGRESS_KEY, jid)):
                logger.info("scheduler: %s already active as %s; coalesced",
                            slug, jid)
                return jid == job_id
        await preseed_pending(r, job_id, "run_agent_task")
        try:
            task = await run_agent_task.kicker().with_task_id(job_id).kiq(
                agent_slug=slug, vault_id=None, trigger_source="schedule")
        except Exception:
            # The pre-seed has no TTL: left behind, it reads as "active"
            # FOREVER and coalesces every future fire of this agent.
            await r.hdel(PENDING_KEY, job_id)
            logger.exception("scheduler: enqueue failed for %s", job_id)
            return False
        logger.info("scheduler: enqueued %s (%s)", job_id, task.task_id)
        return True
    finally:
        await r.close()


async def _tick(now: datetime.datetime) -> None:
    from src import agent_registry
    from src.agent_schedule import due_state

    agents = await asyncio.to_thread(agent_registry.list_agents)
    r = get_async_redis()
    try:
        for agent in agents:
            if not agent.valid or not agent.schedule.strip():
                continue
            last = await get_last_run(r, agent.slug)
            # ONE implementation of "is this due?", shared with /agents and
            # /manage/monitor - including the jitter key and the no-stamp-yet
            # policy, which the two former inline copies disagreed about.
            st = due_state(agent.schedule, last, now, jitter_key=agent.slug)
            if st.error:
                continue  # load-time validation already surfaces this in the UI
            if st.first_sight:
                # Stamp now, fire at the next natural occurrence.
                await set_last_run(r, agent.slug, now)
                logger.info("scheduler: first sight of %s (schedule %r); next due %s",
                            agent.slug, agent.schedule, st.next_due)
                continue
            if st.is_due:
                # Fire, but do NOT stamp here. Stamping moved to the worker, at
                # the moment a run actually acquires the global run lock
                # (task_definitions._stamp_agent_run).
                #
                # Stamping at ENQUEUE meant "we asked for a run", so an
                # occurrence that never got the lock - deferred behind another
                # agent, or timed out waiting - still advanced the schedule and
                # was silently skipped. Three agents share a 04:00 schedule, so
                # two of them met that path every morning. Leaving the agent DUE
                # until it really starts makes the retry automatic: the next tick
                # tries again, and the PENDING/IN_PROGRESS coalesce guard in
                # _fire keeps that from stacking duplicate enqueues.
                await _fire(agent.slug,
                            agent_registry.resolve_target_vaults(agent))
    finally:
        await r.close()

    # Event dispatch rides the same tick, AFTER schedule fires (their PENDING
    # pre-seeds are then visible to the dispatcher's is_active coalescing) and
    # never touches the lastrun stamps - `schedule:` and `on:` compose as OR.
    from config import EVENT_TRIGGERS_ENABLED
    if EVENT_TRIGGERS_ENABLED:
        try:
            from src import events
            await events.dispatch_tick(now, agents)
        except Exception:
            logger.exception("scheduler: event dispatch failed")


async def scheduler_loop() -> None:
    if not AGENT_SCHEDULER_ENABLED:
        logger.info("agent scheduler disabled (AGENT_SCHEDULER_ENABLED=false)")
        return
    logger.info("agent scheduler: tick every %ss", AGENT_SCHEDULER_TICK_S)
    await asyncio.sleep(15)  # let worker startup settle
    while True:
        try:
            await _tick(datetime.datetime.now())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("agent scheduler tick failed")
        await asyncio.sleep(AGENT_SCHEDULER_TICK_S)


# ---------------------------------------------------------------------------
# Maintenance: housekeeping that must run whether or not agents are scheduled
# ---------------------------------------------------------------------------
#
# Split out of the scheduler tick 2026-08-04. `scheduler_loop` returns early when
# AGENT_SCHEDULER_ENABLED=false, which silently disabled staged-batch GC too - an
# install with agents turned off accumulated staged batches forever. Rather than
# extend that bug to the failure drain and its 30-day prune (a monitoring feature
# that stops recording when you disable something unrelated is worse than none),
# housekeeping now has its own always-on loop.

GAPS_KEY = "monitor:gaps"
_GAPS_GATE = "monitor:gaps:gate"
GAPS_REFRESH_S = 3600          # find_unindexed_documents costs ~0.7s over 8 vaults
GAPS_TTL_S = 7200              # > refresh interval, so a dead worker reads "unknown"


async def store_gaps(r, res: dict) -> None:
    """Publish a reconcile result as THE cached snapshot and restart the refresh clock.

    Shared with the /manage/monitor "Check now" link: a manual check pays the same
    0.7s walk the worker does, so its result must become what the plain page renders.
    Otherwise the click shows cleared gaps and the next plain load reverts to the
    hour-old snapshot.
    """
    import json
    import time

    await r.set(GAPS_KEY, json.dumps({"ts": time.time(), **res}), ex=GAPS_TTL_S)
    await r.set(_GAPS_GATE, "1", ex=GAPS_REFRESH_S)


async def _refresh_gaps(r) -> None:
    """Recompute the disk-vs-index reconcile, hourly, behind a SET NX EX gate.

    Worker computes, web renders - the pattern llm_gate stats and
    events.STATUS_KEY already use. The cached value carries its own timestamp and
    the key EXPIRES, so a stopped worker renders "unknown" instead of a stale
    number presented as current (the same reasoning as _write_status's ex=600).

    Deliberately NOT a page-load computation: 0.7s of 9p globbing per render is
    too slow for a page you leave open. Deliberately not click-only either - the
    click-gated check already existed when a lost index task went unnoticed for 52
    hours, and a check you must remember to run does not do this job.
    """
    if not await r.set(_GAPS_GATE, "1", nx=True, ex=GAPS_REFRESH_S):
        return
    from src.rag_indexer import find_unindexed_documents
    res = await find_unindexed_documents()
    await store_gaps(r, res)
    if res.get("total_missing"):
        logger.warning("maintenance: %d unindexed file(s) across %d vault(s)",
                       res["total_missing"], len(res.get("missing") or {}))
    if res.get("total_stale"):
        logger.warning("maintenance: %d stale index row(s) across %d vault(s)",
                       res["total_stale"], len(res.get("stale") or {}))


async def _maintenance_tick() -> None:
    """One pass of housekeeping. Each item is independently guarded so a failure
    in one does not stop the others."""
    from src import failure_log, write_gate
    from src.task_broker import get_async_redis

    r = get_async_redis()
    try:
        try:
            n = await failure_log.drain_inbox(r)
            if n:
                logger.info("maintenance: recorded %d failure(s)", n)
        except Exception:
            logger.exception("maintenance: failure drain failed")

        try:
            await _refresh_gaps(r)
        except Exception:
            logger.exception("maintenance: gaps refresh failed")
    finally:
        await r.close()

    try:
        removed = await asyncio.to_thread(failure_log.prune, FAILURE_LOG_TTL_DAYS)
        if removed:
            logger.info("maintenance: pruned %d old failure row(s)", removed)
    except Exception:
        logger.exception("maintenance: failure-log prune failed")

    try:
        discarded = await asyncio.to_thread(
            write_gate.gc_stale_batches, AGENT_STAGING_TTL_DAYS)
        if discarded:
            logger.info("maintenance: GC discarded stale staged batches: %s", discarded)
    except Exception:
        logger.exception("maintenance: staged-batch GC failed")


async def maintenance_loop() -> None:
    """Always on - NOT gated on AGENT_SCHEDULER_ENABLED. See the note above."""
    logger.info("maintenance: tick every %ss", AGENT_SCHEDULER_TICK_S)
    await asyncio.sleep(20)  # after the scheduler's own settle, to stagger startup
    while True:
        try:
            await _maintenance_tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("maintenance tick failed")
        await asyncio.sleep(AGENT_SCHEDULER_TICK_S)
