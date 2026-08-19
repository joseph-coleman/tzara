# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Application events: envelope, Redis-stream transport, and the dispatcher.

The wiki emits small event envelopes (XADD to the ``wiki:events`` stream) at
a handful of call sites - agent lifecycle per vault (task_definitions),
staging decisions and uploads (main.py). The worker's scheduler tick calls
dispatch_tick(), which drains the stream into a durable PENDING POOL, asks
the pure planner (src.agent_events.plan_dispatch) what to fire, and enqueues
event-triggered agent runs.

Consume is deliberately separated from fire-decision (the anti-lock-in seam):
the stream cursor always advances at consume time; deferral (cooldown, budget,
agent busy - and later ``settled Xm``) is purely pool-retention policy.

Envelope fields on the stream (all strings - decode_responses=True):
    type, vault, subject, actor ("human" | "agent:<slug>" | "system"),
    cause_run_id (may be ""), depth (stringified int), ts (naive-local ISO),
    payload (JSON, optional).
Pool entries add: id (stream id), pooled_at, delivered (slugs already fired).

Known accepted window: a crash between kiq and the pool's delivered-update can
refire the same events next tick; is_active coalescing covers the common case
and volumes here are tiny.

emit() must NEVER raise into its caller - event emission is a side channel,
not a load-bearing step of any save/upload/run path.
"""

import datetime
import json
import logging
from dataclasses import asdict

from config import (
    EVENT_BUDGET_PER_HOUR,
    EVENT_COOLDOWN_S,
    EVENT_MAX_AGE_S,
    EVENT_MAX_DEPTH,
    EVENT_STREAM_MAXLEN,
    EVENT_TRIGGERS_ENABLED,
)
from src.agent_events import Fire, Trigger, plan_dispatch
from src.task_broker import get_async_redis

logger = logging.getLogger("events")

STREAM_KEY = "wiki:events"
CURSOR_KEY = "wiki:events:cursor"
POOL_KEY = "wiki:events:pool"
STATUS_KEY = "wiki:events:status"          # last tick's deferrals/drops (for /agents)
DISPATCH_LOCK_KEY = "wiki:events:dispatchlock"
LASTGOOD_KEY = "wiki:events:lastgood"      # hash: slug -> last-good {triggers, targets}


def cooldown_key(slug: str) -> str:
    return f"wiki:events:cooldown:{slug}"


def budget_key(slug: str) -> str:
    return f"wiki:events:budget:{slug}"


# ---------------------------------------------------------------------------
# Emission (fire-and-forget safe)
# ---------------------------------------------------------------------------

async def emit(event_type: str, vault: str, subject: str, actor: str, *,
               cause_run_id: str = "", depth: int = 0,
               payload: dict | None = None,
               stream_key: str = STREAM_KEY) -> str | None:
    """Append one event to the stream. Returns the stream id, or None on any
    failure (logged) - this must never break a save/upload/run path. No-op
    (None) when event triggers are disabled, so the stream can't grow while
    nothing consumes it."""
    if not EVENT_TRIGGERS_ENABLED:
        return None
    r = None
    try:
        fields = {
            "type": event_type,
            "vault": vault or "",
            "subject": subject or "",
            "actor": actor,
            "cause_run_id": cause_run_id or "",
            "depth": str(int(depth)),
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        if payload:
            fields["payload"] = json.dumps(payload, ensure_ascii=False, default=str)
        r = get_async_redis()
        eid = await r.xadd(stream_key, fields,
                           maxlen=EVENT_STREAM_MAXLEN, approximate=True)
        return eid
    except Exception:
        logger.exception("emit failed for %s %s/%s", event_type, vault, subject)
        return None
    finally:
        if r is not None:
            try:
                await r.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Stream -> pool
# ---------------------------------------------------------------------------

def _decode_event(eid: str, fields: dict) -> dict:
    """Stream fields (all str) -> typed envelope. The ONLY place depth/payload
    are coerced - guards and planners downstream assume proper types."""
    ev = dict(fields)
    ev["id"] = eid
    try:
        ev["depth"] = int(ev.get("depth", "0") or 0)
    except ValueError:
        ev["depth"] = 0
    if "payload" in ev:
        try:
            ev["payload"] = json.loads(ev["payload"])
        except (ValueError, TypeError):
            ev["payload"] = {}
    return ev


async def consume_new(r, *, stream_key: str = STREAM_KEY,
                      cursor_key: str = CURSOR_KEY,
                      pool_key: str = POOL_KEY) -> int:
    """Move events newer than the cursor into the pool; advance the cursor.

    HSETNX keeps a crash between HSET and cursor-SET harmless: the re-consume
    can't clobber an existing pool entry's ``delivered`` bookkeeping."""
    cursor = await r.get(cursor_key)
    min_id = f"({cursor}" if cursor else "-"   # exclusive form needs Redis >= 6.2
    entries = await r.xrange(stream_key, min=min_id, max="+", count=1000)
    if not entries:
        return 0
    pooled_at = datetime.datetime.now().isoformat(timespec="seconds")
    # One pipelined round-trip: a burst drain must not serialize hundreds of
    # RTTs while holding the dispatch lock. HSETNX semantics are preserved.
    pipe = r.pipeline(transaction=False)
    for eid, fields in entries:
        ev = _decode_event(eid, fields)
        ev["pooled_at"] = pooled_at
        ev["delivered"] = []
        pipe.hsetnx(pool_key, eid, json.dumps(ev, ensure_ascii=False))
    pipe.set(cursor_key, entries[-1][0])
    await pipe.execute()
    return len(entries)


def _stream_id_key(eid: str) -> tuple[int, int]:
    """Numeric sort key for a Redis stream id ('ms-seq') - lexicographic
    comparison mis-orders seq 9 vs 10 within one millisecond."""
    try:
        ms, seq = eid.split("-", 1)
        return (int(ms), int(seq))
    except (ValueError, AttributeError):
        return (0, 0)


async def read_pool(r, *, pool_key: str = POOL_KEY) -> list[dict]:
    """All pooled events, oldest first; malformed entries are dropped."""
    raw = await r.hgetall(pool_key)
    out = []
    for eid, val in raw.items():
        try:
            out.append(json.loads(val))
        except (ValueError, TypeError):
            logger.warning("events: dropping malformed pool entry %s", eid)
            await r.hdel(pool_key, eid)
    out.sort(key=lambda e: _stream_id_key(e.get("id", "")))
    return out


async def read_recent(r, count: int = 20, *,
                      stream_key: str = STREAM_KEY) -> list[dict]:
    """Newest events from the stream tail (the /agents 'Recent events' view)."""
    entries = await r.xrevrange(stream_key, max="+", min="-", count=count)
    return [_decode_event(eid, fields) for eid, fields in entries]


# ---------------------------------------------------------------------------
# Kickoff / run-log rendering
# ---------------------------------------------------------------------------

def format_trigger_note(events: list[dict]) -> str:
    """Human-readable trigger batch, appended to the agent's kickoff message
    and reused by the run log's 'Triggered by' section (gap-#7 style: lossy,
    prompt-interpreted context - the framework stays stateless)."""
    lines = [f"You were triggered by {len(events)} event(s):"]
    for ev in events:
        lines.append(
            f"- {ev.get('type', '?')}: '{ev.get('subject', '')}' "
            f"(vault {ev.get('vault', '?')}, by {ev.get('actor', '?')}, "
            f"{ev.get('ts', '?')})")
    return "\n".join(lines)


# Event type -> readable phrase. The subject that follows differs by type
# (agent.* -> the agent slug; upload -> the uploaded path), so each phrase
# is written to read naturally with a backticked subject appended.
_EVENT_PHRASE = {
    "agent.completed": "completion of agent",
    "agent.failed": "failure of agent",
    "agent.cancelled": "cancellation of agent",
    "staging.created": "staged changes from agent",
    "staging.approved": "approved staging from agent",
    "staging.rejected": "rejected staging from agent",
    "upload": "upload of",
}


def _describe_event(ev: dict) -> str:
    """One event as a short readable clause, e.g.
    ``completion of agent `daily-digest``` or ``upload of `inbox/foo.pdf```."""
    phrase = _EVENT_PHRASE.get(ev.get("type", ""), ev.get("type", "?"))
    subject = ev.get("subject", "")
    return f"{phrase} `{subject}`" if subject else phrase


def format_trigger_summary(events: list[dict] | None,
                           trigger_source: str = "manual") -> str:
    """The run log's one-line 'triggered by' cell. For event-fired runs this
    names what actually fired (not just a count); for the schedule/manual
    paths - which carry no events - it disambiguates the two using the
    ``trigger_source`` marker threaded from the enqueuing call site."""
    if events:
        descs = [_describe_event(ev) for ev in events]
        if len(descs) == 1:
            return f"event - {descs[0]}"
        return f"{len(descs)} events - " + "; ".join(descs)
    return {
        "schedule": "scheduled run (matched a `schedule:` rule)",
        "manual": "manual run (user-initiated)",
        "event": "event (no envelopes recorded)",
    }.get(trigger_source, trigger_source or "manual")


# ---------------------------------------------------------------------------
# Subscription reconcile (pure): registry scan + last-good cache -> this
# tick's matchable subscriptions
# ---------------------------------------------------------------------------

def reconcile_subscriptions(agents_meta: list[dict], cached: dict):
    """Decide this tick's subscriptions from the registry scan PLUS the
    last-good cache (LASTGOOD_KEY hash).

    The cache exists for one failure mode: an agent file that is INVALID at
    tick time (a mid-edit save). Its pooled events must DEFER, not delete -
    and when the broken part is the ``on:`` line itself the current parse has
    no triggers to match with, so matching falls back to the last seen good
    (triggers, targets). A VALID agent without ``on:`` is a deliberate
    unsubscribe and clears its cache entry; a vanished file does too.

    agents_meta: [{"slug", "valid", "triggers": [Trigger], "targets": [str]}]
      for EVERY agent file in the registry (invalid ones included).
    cached: the lastgood hash contents, {slug: json}.

    Returns (subs, unavailable, cache_puts, cache_dels):
      subs         [(slug, [Trigger], [targets])] to match with
      unavailable  slugs to DEFER (invalid definitions; distinct reason in
                   the planner so busy and broken don't look alike)
      cache_puts   {slug: json} lastgood updates (only when changed)
      cache_dels   [slug] cache entries to drop
    """
    subs, unavailable = [], set()
    cache_puts, cache_dels = {}, []
    if not agents_meta:
        # An EMPTY scan is indistinguishable from a transiently unreadable
        # agents dir (sync hiccup, remount). Wiping the cache here would
        # destroy the mid-edit safety net during exactly the failure it
        # exists to survive - change nothing; stale entries are inert while
        # no files exist and reconcile normally once the scan sees files.
        return subs, unavailable, cache_puts, cache_dels
    present = {m["slug"] for m in agents_meta}
    cache_dels += [s for s in cached if s not in present]   # file deleted

    for m in agents_meta:
        slug = m["slug"]
        if m["valid"] and m["triggers"]:
            subs.append((slug, m["triggers"], m["targets"]))
            blob = json.dumps(
                {"triggers": [asdict(t) for t in m["triggers"]],
                 "targets": list(m["targets"])}, ensure_ascii=False)
            if cached.get(slug) != blob:
                cache_puts[slug] = blob
        elif m["valid"]:
            if slug in cached:
                cache_dels.append(slug)        # `on:` removed on purpose
        elif m["triggers"]:
            # Invalid elsewhere (capability, schedule, ...); triggers parse.
            subs.append((slug, m["triggers"], m["targets"]))
            unavailable.add(slug)
        elif slug in cached:
            # Invalid AND no parseable triggers - the `on:` line itself is
            # what's broken. Match with the last good subscription.
            try:
                data = json.loads(cached[slug])
                trigs = [Trigger(**d) for d in data["triggers"]]
                subs.append((slug, trigs, list(data["targets"])))
                unavailable.add(slug)
            except (ValueError, TypeError, KeyError):
                cache_dels.append(slug)        # unusable cache entry
    return subs, unavailable, cache_puts, cache_dels


# ---------------------------------------------------------------------------
# Dispatch (called from the worker scheduler tick)
# ---------------------------------------------------------------------------

async def _fire_event_run(r, fire: Fire) -> bool:
    """Enqueue one event-triggered run. Mirrors agent_scheduler._fire - same
    job-id + dedup + PENDING pre-seed contract - but vault-scoped and with the
    trigger batch attached."""
    from src import agent_registry
    from src.task_definitions import run_agent_task    # lazy: import cycle
    from src.task_tracker import IN_PROGRESS_KEY, PENDING_KEY, preseed_pending

    job_id = agent_registry.agent_job_id(fire.slug, fire.vault_id)
    preseeded = False
    try:
        if (await r.hexists(PENDING_KEY, job_id)
                or await r.hexists(IN_PROGRESS_KEY, job_id)):
            logger.info("events: %s already active; deferred", job_id)
            return False
        await preseed_pending(r, job_id, "run_agent_task")
        preseeded = True
        # Strip pool bookkeeping from the envelopes handed to the run.
        clean = [{k: v for k, v in ev.items() if k not in ("delivered", "pooled_at")}
                 for ev in fire.events]
        task = await run_agent_task.kicker().with_task_id(job_id).kiq(
            agent_slug=fire.slug, vault_id=fire.vault_id,
            trigger_events=clean, event_depth=fire.depth,
            trigger_source="event")
        logger.info("events: fired %s on %d event(s) (%s)",
                    job_id, len(fire.events), task.task_id)
        return True
    except Exception:
        logger.exception("events: failed to fire %s", job_id)
        if preseeded:
            # The pre-seed has no TTL: orphaned, it reads as "active" FOREVER
            # and coalesces every future fire of this agent.
            try:
                await r.hdel(PENDING_KEY, job_id)
            except Exception:
                logger.exception("events: failed to clean pre-seed %s", job_id)
        return False


async def _write_status(r, now: datetime.datetime, deferred: dict,
                        pooled: int, dropped_depth: int,
                        dropped_expired: int) -> None:
    """Publish last-tick dispatch state for the /agents surface. A budget
    deferral here is the 'possible trigger storm' signal the UI must show -
    guards that only log are guards nobody sees."""
    try:
        await r.set(STATUS_KEY, json.dumps({
            "ts": now.isoformat(timespec="seconds"),
            "deferred": deferred,
            "pooled": pooled,
            "dropped_depth": dropped_depth,
            "dropped_expired": dropped_expired,
        }, ensure_ascii=False), ex=600)
    except Exception:
        logger.exception("events: failed to write status key")


async def dispatch_tick(now: datetime.datetime, agents: list | None = None) -> None:
    """One dispatch pass: consume stream -> pool, plan (pure), execute fires,
    update delivered bookkeeping, delete finished/expired pool entries.

    Guarded by a short NX lock: the scheduler loop runs per worker PROCESS, so
    a `taskiq --workers 2` deploy would otherwise run two dispatchers against
    the same pool and double-fire (HSETNX already makes double-CONSUME safe).
    The lock TTL self-heals a crashed holder within a tick."""
    import asyncio
    import os

    from src import agent_registry
    from src.task_tracker import IN_PROGRESS_KEY, PENDING_KEY

    if agents is None:
        agents = await asyncio.to_thread(agent_registry.list_agents)

    def _build_meta():
        # resolve_target_vaults hits Postgres + the filesystem - one to_thread
        # for the whole build keeps that blocking I/O off the event loop.
        # Targets are only needed for agents that can match this tick.
        return [{"slug": a.slug, "valid": a.valid, "triggers": a.triggers,
                 "targets": (agent_registry.resolve_target_vaults(a)
                             if a.triggers else [])}
                for a in agents]

    meta = await asyncio.to_thread(_build_meta)

    r = get_async_redis()
    lock_id = f"{os.getpid()}-{now.isoformat(timespec='seconds')}"
    got_lock = False
    try:
        got_lock = await r.set(DISPATCH_LOCK_KEY, lock_id, nx=True, ex=55)
        if not got_lock:
            logger.info("events: another dispatcher holds the lock; skipping tick")
            return

        # Registry scan + last-good cache -> matchable subscriptions. Invalid
        # definitions still MATCH (current or cached triggers) but land in
        # `unavailable`, deferring their events instead of deleting them.
        cached = await r.hgetall(LASTGOOD_KEY)
        subs, unavailable, cache_puts, cache_dels = \
            reconcile_subscriptions(meta, cached)
        if cache_puts:
            await r.hset(LASTGOOD_KEY, mapping=cache_puts)
        if cache_dels:
            await r.hdel(LASTGOOD_KEY, *cache_dels)

        await consume_new(r)
        pool = await read_pool(r)
        if not pool:
            await _write_status(r, now, {}, 0, 0, 0)
            return
        if not subs:
            # No subscribers at all - the pool must not grow unbounded.
            await r.hdel(POOL_KEY, *[ev["id"] for ev in pool])
            await _write_status(r, now, {}, 0, 0, 0)
            return

        # Redis state for the pure planner: busy slugs (guard 6), cooling
        # slugs (guard 3), per-hour budget counters (guard 4).
        active: set[str] = set()
        for slug, _trigs, targets in subs:
            jids = ([agent_registry.agent_job_id(slug)]
                    + [agent_registry.agent_job_id(slug, v) for v in targets])
            for jid in jids:
                if (await r.hexists(PENDING_KEY, jid)
                        or await r.hexists(IN_PROGRESS_KEY, jid)):
                    active.add(slug)
                    break
        cooling = {slug for slug, _t, _v in subs
                   if await r.exists(cooldown_key(slug))}
        budget_used = {slug: int(await r.get(budget_key(slug)) or 0)
                       for slug, _t, _v in subs}

        plan = plan_dispatch(subs, pool, now, active, cooling, budget_used,
                             unavailable=unavailable,
                             max_depth=EVENT_MAX_DEPTH,
                             budget_per_hour=EVENT_BUDGET_PER_HOUR,
                             max_age_s=EVENT_MAX_AGE_S)

        # Execute. Delivered bookkeeping applies only to fires that actually
        # enqueued - a failed fire leaves its events pooled for next tick.
        # Cooldown/budget count once per AGENT per tick: a multi-vault event
        # batch is one trigger occasion, not several budget units.
        delivered_updates: dict[str, set] = {}
        fired_slugs: set = set()
        for fire in plan.fires:
            if not await _fire_event_run(r, fire):
                continue
            if fire.slug not in fired_slugs:
                fired_slugs.add(fire.slug)
                await r.set(cooldown_key(fire.slug),
                            now.isoformat(timespec="seconds"),
                            ex=EVENT_COOLDOWN_S)
                n = await r.incr(budget_key(fire.slug))
                if n == 1:
                    await r.expire(budget_key(fire.slug), 3600)
            for ev in fire.events:
                delivered_updates.setdefault(ev["id"], set()).add(fire.slug)

        pool_by_id = {ev["id"]: ev for ev in pool}
        for eid, new_slugs in delivered_updates.items():
            ev = pool_by_id.get(eid)
            if ev is None:
                continue
            total = set(ev.get("delivered") or []) | new_slugs
            if set(plan.matching.get(eid, [])) <= total:
                plan.delete_ids.append(eid)    # every subscriber served
            else:
                ev["delivered"] = sorted(total)
                await r.hset(POOL_KEY, eid, json.dumps(ev, ensure_ascii=False))

        if plan.delete_ids:
            await r.hdel(POOL_KEY, *set(plan.delete_ids))
        if plan.dropped_expired:
            logger.info("events: dropped %d expired event(s)",
                        plan.dropped_expired)
        if plan.dropped_depth:
            logger.warning(
                "events: %d event(s) had subscribers but hit the depth cap "
                "(EVENT_MAX_DEPTH) and were dropped - a trigger chain was cut",
                plan.dropped_depth)
        await _write_status(r, now, plan.deferred,
                            await r.hlen(POOL_KEY),
                            plan.dropped_depth, plan.dropped_expired)
    finally:
        if got_lock:
            # Release only our own lock (GET-compare-DEL, run-lock idiom).
            try:
                if await r.get(DISPATCH_LOCK_KEY) == lock_id:
                    await r.delete(DISPATCH_LOCK_KEY)
            except Exception:
                pass
        await r.close()
