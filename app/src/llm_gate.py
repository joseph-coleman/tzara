# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared concurrency gate for background LLM requests, with instrumentation.

All heavy background LLM work (frontmatter tag/summary generation, document
embedding, model warming, agent runs) runs inside the single taskiq worker
process. Without a limit, a bulk reindex fans out into many concurrent
``client.generate`` / ``client.embed`` calls that all hit the same LLM server at
once - a self-inflicted DOS, worst on large chat models.

This module exposes one process-wide PRIORITY gate that those call sites acquire
around the actual network call. Because the worker is a single process on a single
event loop, in-process coordination is sufficient to cap total background LLM
concurrency - no cross-process/Redis coordination needed.

It was an ``asyncio.Semaphore`` until 2026-08-02. A semaphore is strictly FIFO,
which cannot express "let the 0.3s embed past the 169s consolidation" - see the
_PRIORITY table below for the measured service times that motivated the change.

Interactive chat and edit-assist run in the separate web process via
``OllamaManager`` and are NOT gated here. Note that this does NOT make them
responsive, which the previous version of this docstring claimed: skipping the
gate does not create capacity at the LLM server, which is itself serial. Measured
2026-08-02, a chat request arriving mid-consolidation waits out a 169s call. The
mechanism that actually protects interactive work is agents yielding while a human
is active (see ``human_active`` below), not being ungated.

WHY THE INSTRUMENTATION
-----------------------
Background LLM work passes through TWO queues in series: the taskiq queue, and
then this gate. The taskiq queue is the observable one, but the waiting happens
HERE - and with ``--max-async-tasks 3`` against a gate of 1, the task tracker can
report three tasks "in progress" while two sit parked on this semaphore doing
nothing at all. That hides the real bottleneck exactly when it matters.

So every acquisition records who waited, how long it waited, and how long it held
the LLM. Because the gate lives in the worker while ``/manage/tasks`` renders in
the web process, the counters are mirrored into Redis (``llm:gate:*``). Reporting
is strictly best-effort: a Redis failure must never break an LLM call.

Totals are cumulative since ``__since`` in the stats hash - which SURVIVES worker
restarts, because the counters do. Only an explicit reset opens a new window.
"""

import asyncio
import heapq
import time

from config import OLLAMA_MAX_CONCURRENCY

# Service order when the gate is contended: LOWEST number goes first.
#
# Shortest-job-first, and the spread justifies it. Measured 2026-08-02 over 2.3h
# of real traffic (average hold per call):
#     embed 0.3s | metadata 6.8-8.5s | agent turn 26.3s | agent:memory 169.2s
# A ~1000:1 service-time spread through one FIFO server produces the classic
# convoy effect - an embed was observed doing 0.3s of work after waiting 43.5s,
# and a warm waited 252s to do nothing. Letting the short job go first costs the
# long job a fraction of one turn and saves the short job minutes.
#
# Ordering only, never starvation-proofing: at capacity 1 with agents holding
# ~99% of LLM time, a low-priority job still runs the moment the agent yields
# its turn, because the agent must re-queue between turns.
_PRIORITY = {
    "warm": 10,             # short, and everything else waits on a cold model
    "embed": 20,            # sub-second
    "metadata": 30,         # single-digit seconds
    "agent": 50,            # tens of seconds per turn
    # agent:memory is the LONGEST single call (169s), so pure SJF would rank it
    # last. It does not, because SJF assumes INDEPENDENT jobs and this one is not:
    # it is the closing step of a run that holds the GLOBAL agent run lock, so
    # delaying it delays every other agent on every vault. Ranking it below normal
    # turns was a priority inversion - a low-priority job holding a resource that
    # higher-priority work needs. Same class as the turns it belongs to.
    "agent:memory": 50,
}
_DEFAULT_PRIORITY = 40


def priority_for(label: str) -> int:
    """Service class for a gate label. Most specific prefix wins, so
    "agent:memory" outranks "agent", and "agent:vault-gardener" falls back to
    "agent". Unknown labels sit between metadata and agents - a new caller
    should not silently outrank an embed or preempt an agent."""
    best = None
    for prefix, prio in _PRIORITY.items():
        if label == prefix or label.startswith(prefix + ":"):
            if best is None or len(prefix) > best[0]:
                best = (len(prefix), prio)
    return best[1] if best else _DEFAULT_PRIORITY

LIVE_KEY = "llm:gate:live"
STATS_KEY = "llm:gate:stats"
# Field separator inside the stats hash ("embed|wait_ms"). Not ":" - labels
# already contain colons ("agent:vault-gardener").
SEP = "|"
# When this counter window opened. Lives in the stats hash (HSETNX on first
# write, cleared with it on reset) rather than being derived from process state.
# Deriving it from the process was WRONG twice over: this module is imported
# lazily inside task bodies, so import time is "first gated call" and not worker
# start; and the Redis counters survive a worker restart while a process-local
# timestamp does not - which reported 78 minutes of LLM work inside a 16-minute
# window. Carries no SEP, so read_gate_stats skips it as a label for free.
SINCE_FIELD = "__since"

_depth = 0                        # waiters + current holder, this process
_max_wait_ms: dict[str, int] = {}

# Priority dispatcher, replacing asyncio.Semaphore (which is strictly FIFO and
# offers no way to let a 0.3s embed past a 169s consolidation). Same invariant:
# never more than OLLAMA_MAX_CONCURRENCY holders at once.
_free = OLLAMA_MAX_CONCURRENCY    # permits not currently held
_waiters: list = []               # heap of (priority, seq, future)
_seq = 0


async def _acquire(priority: int) -> None:
    """Take a permit, or queue by priority until one is handed over."""
    global _free, _seq
    if _free > 0 and not _waiters:
        _free -= 1
        return
    fut = asyncio.get_running_loop().create_future()
    _seq += 1
    heapq.heappush(_waiters, (priority, _seq, fut))
    try:
        await fut
    except asyncio.CancelledError:
        # Cancelled while queued. If a permit was handed to us in the same tick
        # we now OWN it, and dropping it would shrink the gate permanently.
        if fut.done() and not fut.cancelled():
            _release()
        raise


def _release() -> None:
    """Hand the permit to the highest-priority live waiter, else free it.

    Handing off directly (rather than incrementing and letting waiters race)
    is what makes the ordering hold: the permit goes to the heap's minimum,
    not to whoever the event loop happens to wake first.
    """
    global _free
    while _waiters:
        _prio, _s, fut = heapq.heappop(_waiters)
        if not fut.done():          # skip waiters cancelled while queued
            fut.set_result(True)
            return
    _free += 1


async def _report(pipe_ops) -> None:
    """Best-effort mirror to Redis. Never raises into the caller's LLM path."""
    try:
        from src.task_broker import get_async_redis
        r = get_async_redis()
        try:
            p = r.pipeline()
            pipe_ops(p)
            await p.execute()
        finally:
            await r.close()
    except Exception:
        pass


class _GateHandle:
    """One acquisition of the gate, timed. Async CM so call sites are unchanged."""

    __slots__ = ("label", "priority", "_waited", "_held_from")

    def __init__(self, label: str, priority: int | None = None):
        self.label = label
        self.priority = priority_for(label) if priority is None else priority
        self._waited = 0.0
        self._held_from = 0.0

    async def __aenter__(self):
        global _depth
        _depth += 1
        t0 = time.perf_counter()
        try:
            await _acquire(self.priority)
        except BaseException:
            # Cancelled while queued: keep the depth counter honest.
            _depth -= 1
            raise
        self._waited = time.perf_counter() - t0
        self._held_from = time.perf_counter()

        wait_ms = int(self._waited * 1000)
        if wait_ms > _max_wait_ms.get(self.label, -1):
            _max_wait_ms[self.label] = wait_ms

        label, depth_now = self.label, _depth

        def ops(p):
            p.hset(LIVE_KEY, mapping={
                "holder": label,
                "holder_since": str(time.time()),
                "depth": str(depth_now),
            })

        # The semaphore is HELD from here on, but __aexit__ only runs if we
        # return normally. So anything that escapes this window leaks the gate
        # permanently - and at OLLAMA_MAX_CONCURRENCY=1 that wedges every piece
        # of background LLM work until the worker restarts.
        #   * Exception  -> swallowed. Instrumentation must never break the
        #                   thing it measures, even if _report itself is buggy.
        #   * CancelledError (BaseException) -> still cancels, as it must, but
        #                   we hand the gate back on the way out.
        try:
            await _report(ops)
        except Exception:
            pass
        except BaseException:
            _release()
            _depth -= 1
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb):
        global _depth
        held_ms = int((time.perf_counter() - self._held_from) * 1000)
        wait_ms = int(self._waited * 1000)
        # Release BEFORE reporting: the next waiter must not queue behind a
        # Redis round trip.
        _release()
        _depth -= 1

        label, depth_now = self.label, _depth
        max_wait = _max_wait_ms.get(label, wait_ms)

        def ops(p):
            p.hsetnx(STATS_KEY, SINCE_FIELD, str(time.time()))
            p.hincrby(STATS_KEY, f"{label}{SEP}count", 1)
            p.hincrby(STATS_KEY, f"{label}{SEP}wait_ms", wait_ms)
            p.hincrby(STATS_KEY, f"{label}{SEP}hold_ms", held_ms)
            p.hset(STATS_KEY, f"{label}{SEP}max_wait_ms", str(max_wait))
            p.hset(LIVE_KEY, mapping={"depth": str(depth_now)})
            if depth_now == 0:
                p.hdel(LIVE_KEY, "holder", "holder_since")

        # Safe by construction here - the gate is already back - but a raising
        # reporter would still turn a SUCCESSFUL LLM call into an error, so the
        # same best-effort rule applies.
        try:
            await _report(ops)
        except Exception:
            pass
        return False


def get_llm_gate(label: str = "unlabeled", priority: int | None = None) -> _GateHandle:
    """Acquire the shared background-LLM gate, tagged with who is asking.

    ``label`` groups the stats AND selects the service class: "embed",
    "metadata:tags", "agent:vault-gardener". Keep the set SMALL and bounded - it
    becomes the key space of the stats hash. ``priority`` overrides the class
    derived from the label (lower runs first); prefer naming the label correctly.

    Stays callable with zero arguments because agent_runner receives this function
    itself as a per-call gate factory (``stream_cm = llm_gate()``).
    """
    return _GateHandle(label, priority)


# ---------------------------------------------------------------------------
# Reader (web process): turn the mirrored counters into something renderable
# ---------------------------------------------------------------------------

async def read_gate_stats() -> dict:
    """Aggregate view of LLM-gate contention, for /manage/tasks.

    Returns ``{"since": epoch|None, "live": {...}, "labels": [{label, count,
    wait_ms, hold_ms, max_wait_ms, avg_wait_ms, avg_hold_ms}, ...]}`` sorted by
    total HOLD time - "what is actually eating the LLM", which is the question
    worth asking of a serial resource.

    ``since`` is when the counters started accumulating, and SURVIVES worker
    restarts along with them. Use it as the denominator for any "% of wall clock"
    figure; process uptime is not that number.
    """
    from src.task_broker import get_async_redis

    r = get_async_redis()
    try:
        raw = await r.hgetall(STATS_KEY)
        live = await r.hgetall(LIVE_KEY)
    finally:
        await r.close()

    acc: dict[str, dict] = {}
    for field, val in (raw or {}).items():
        label, _, metric = field.rpartition(SEP)
        if not label:
            continue
        try:
            acc.setdefault(label, {})[metric] = int(val)
        except (TypeError, ValueError):
            continue

    labels = []
    for label, m in acc.items():
        count = m.get("count", 0) or 0
        wait = m.get("wait_ms", 0)
        hold = m.get("hold_ms", 0)
        labels.append({
            "label": label,
            "count": count,
            "wait_ms": wait,
            "hold_ms": hold,
            "max_wait_ms": m.get("max_wait_ms", 0),
            "avg_wait_ms": round(wait / count) if count else 0,
            "avg_hold_ms": round(hold / count) if count else 0,
        })
    labels.sort(key=lambda d: d["hold_ms"], reverse=True)
    try:
        since = float((raw or {}).get(SINCE_FIELD))
    except (TypeError, ValueError):
        since = None
    return {"since": since, "live": live or {}, "labels": labels}


# ---------------------------------------------------------------------------
# The OTHER shared bottleneck: the global agent run lock
# ---------------------------------------------------------------------------
#
# Agents serialize on one Redis lock across all agents and all vaults, and until
# now that contention was invisible - /manage/tasks showed a run "in progress"
# whether it was working or waiting its turn, the same blind spot the LLM gate
# had. Three agents share a 04:00 daily schedule, so this is contended in
# practice, not in theory.
#
# Kept in this module because it is the same KIND of measurement (who waits on a
# shared serial resource), and the web layer renders both tables together.

RUNLOCK_KEY = "llm:runlock:stats"


async def record_runlock(slug: str, *, wait_ms: int | None = None,
                         hold_ms: int | None = None,
                         deferred: bool = False) -> None:
    """Record one agent's encounter with the run lock. Best-effort, never raises.

    Exactly ONE of the three outcomes per call, and each argument means a
    different MOMENT in the run:

      deferred=True  - gave the worker slot back; no run happened
      wait_ms=...    - ACQUIRED the lock (this is the call that counts a run)
      hold_ms=...    - RELEASED it, after however long the run took

    The None defaults are load-bearing. With `wait_ms: int = 0`, the release call
    looked identical to an acquisition and `runs` was incremented twice per run -
    reporting 4 runs for expanse-worldbuilder's 2, and halving every average that
    divides by it. A run is counted where it STARTS, once.
    """
    def ops(p):
        p.hsetnx(RUNLOCK_KEY, SINCE_FIELD, str(time.time()))
        if deferred:
            p.hincrby(RUNLOCK_KEY, f"{slug}{SEP}deferrals", 1)
        if wait_ms is not None:
            p.hincrby(RUNLOCK_KEY, f"{slug}{SEP}runs", 1)
            p.hincrby(RUNLOCK_KEY, f"{slug}{SEP}wait_ms", int(wait_ms))
        if hold_ms is not None:
            p.hincrby(RUNLOCK_KEY, f"{slug}{SEP}hold_ms", int(hold_ms))
    await _report(ops)


async def read_runlock_stats() -> dict:
    """Per-agent run-lock contention, sorted by total hold (who owns the lock)."""
    from src.task_broker import get_async_redis

    r = get_async_redis()
    try:
        raw = await r.hgetall(RUNLOCK_KEY)
    finally:
        await r.close()

    acc: dict[str, dict] = {}
    for field, val in (raw or {}).items():
        slug, _, metric = field.rpartition(SEP)
        if not slug:
            continue
        try:
            acc.setdefault(slug, {})[metric] = int(val)
        except (TypeError, ValueError):
            continue

    rows = []
    for slug, m in acc.items():
        runs = m.get("runs", 0) or 0
        rows.append({
            "slug": slug,
            "runs": runs,
            "deferrals": m.get("deferrals", 0),
            "wait_ms": m.get("wait_ms", 0),
            "hold_ms": m.get("hold_ms", 0),
            "avg_hold_ms": round(m.get("hold_ms", 0) / runs) if runs else 0,
        })
    rows.sort(key=lambda d: d["hold_ms"], reverse=True)
    try:
        since = float((raw or {}).get(SINCE_FIELD))
    except (TypeError, ValueError):
        since = None
    return {"since": since, "agents": rows}


# ---------------------------------------------------------------------------
# Human-activity signal: the only thing that actually protects interactive work
# ---------------------------------------------------------------------------

HUMAN_KEY = "llm:human:active"


async def mark_human_active(ttl_s: int | None = None) -> None:
    """Record that a person is using the wiki right now. Best-effort, never raises.

    Written by the WEB process on page views and chat turns; read by the WORKER
    so agents can stand aside. This replaces `OllamaManager.touch()`, which set an
    in-process timestamp that nothing ever read and that the worker could not see
    anyway - the signal existed but never crossed the process boundary.

    A TTL rather than an explicit clear: "active" should decay on its own when the
    person walks away, and no shutdown path has to remember to reset it.
    """
    from config import HUMAN_ACTIVE_TTL_S
    from src.task_broker import get_async_redis

    try:
        r = get_async_redis()
        try:
            await r.set(HUMAN_KEY, "1", ex=int(ttl_s or HUMAN_ACTIVE_TTL_S))
        finally:
            await r.close()
    except Exception:
        pass


async def human_active() -> bool:
    """Is a person using the wiki right now? Best-effort; False if Redis is down,
    so a lost signal degrades to today's behavior (agents keep working) rather
    than stalling every agent."""
    from src.task_broker import get_async_redis

    try:
        r = get_async_redis()
        try:
            return bool(await r.exists(HUMAN_KEY))
        finally:
            await r.close()
    except Exception:
        return False


async def reset_gate_stats() -> None:
    """Clear the mirrored counters (the in-process max survives until restart)."""
    from src.task_broker import get_async_redis

    r = get_async_redis()
    try:
        await r.delete(STATS_KEY, LIVE_KEY)
    finally:
        await r.close()
