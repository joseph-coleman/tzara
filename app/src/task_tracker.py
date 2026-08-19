# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Task tracking middleware and helpers for taskiq.

Replaces arq's built-in job introspection with Redis-backed tracking:
- List in-progress tasks
- List pending/queued tasks
- List completed task results

Uses Redis namespace ``taskiq:tracker:*``.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Any, List, Optional

import redis.asyncio as aioredis
from taskiq import TaskiqMessage, TaskiqMiddleware, TaskiqResult

TRACKER_PREFIX = "taskiq:tracker"
PENDING_KEY = f"{TRACKER_PREFIX}:pending"
IN_PROGRESS_KEY = f"{TRACKER_PREFIX}:in_progress"
COMPLETED_ZSET = f"{TRACKER_PREFIX}:completed"
RESULT_PREFIX = f"{TRACKER_PREFIX}:result"
PROGRESS_PREFIX = f"{TRACKER_PREFIX}:progress"
RESULT_TTL = 3600  # 1 hour
# Ceiling on how long a task may sit between the broker kick and being popped.
# With `--max-async-tasks 3 --workers 1` the worst legitimate wait is "three long
# tasks ahead of you" (an agent run, a bulk reindex) - minutes, not an hour. Set
# well clear of that: expiring a row that is still genuinely queued would make
# is_active() lie the OTHER way and let a scheduler tick enqueue a duplicate.
PENDING_TTL = 3600  # 1 hour


@dataclass
class TrackedTaskInfo:
    task_id: str
    function: str
    success: Optional[bool] = None
    enqueue_time: Optional[float] = None
    start_time: Optional[float] = None
    finish_time: Optional[float] = None
    # A dict for tasks that return structured payloads (the common case), else a
    # truncated string. Rendered into a friendly summary by the web layer.
    result: Any = None


def _serializable_result(return_value: Any) -> Any:
    """Preserve a task's return value for the tracker in a JSON-friendly form.

    Tasks overwhelmingly return small status dicts (see rag_indexer /
    task_definitions). Keeping the dict lets the web layer format a friendly
    summary from real fields instead of dumping a stringified repr. Anything
    that isn't a JSON-serializable dict falls back to a truncated string so the
    tracker never fails to record a completion.
    """
    if not return_value:
        return None
    if isinstance(return_value, dict):
        try:
            json.dumps(return_value)
            return return_value
        except (TypeError, ValueError):
            pass
    return str(return_value)[:200]


def _client(redis_url: str):
    """Connect through the shared factory so the tracker inherits its socket
    timeout and command retry.

    This matters more here than anywhere else: the middleware below runs in the
    worker's hot path and is the ONLY writer that clears IN_PROGRESS. A dropped
    hdel in post_execute leaves a row that marks an agent permanently active -
    the exact orphan class the expiry and the startup reap exist to catch.

    Imported lazily because task_broker imports TaskTrackerMiddleware from this
    module; a module-level import would cycle.
    """
    from src.task_broker import get_async_redis
    return get_async_redis(redis_url)


# ---------------------------------------------------------------------------
# Worker-side middleware
# ---------------------------------------------------------------------------

class TaskTrackerMiddleware(TaskiqMiddleware):
    """Tracks task lifecycle in Redis (pending → in_progress → completed)."""

    def __init__(self, redis_url: str):
        super().__init__()
        self.redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None

    async def _get_redis(self) -> aioredis.Redis:
        if self._redis is None:
            self._redis = _client(self.redis_url)
        return self._redis

    async def startup(self) -> None:
        self._redis = _client(self.redis_url)

    async def shutdown(self) -> None:
        if self._redis:
            await self._redis.close()

    async def pre_execute(self, message: TaskiqMessage) -> TaskiqMessage:
        r = await self._get_redis()
        task_id = message.task_id
        pending_data = await r.hget(PENDING_KEY, task_id)
        await r.hdel(PENDING_KEY, task_id)
        info = json.loads(pending_data) if pending_data else {}
        info.setdefault("enqueue_time", time.time())
        info["start_time"] = time.time()
        info["function"] = message.task_name
        await r.hset(IN_PROGRESS_KEY, task_id, json.dumps(info))
        return message

    async def post_execute(
        self, message: TaskiqMessage, result: "TaskiqResult[Any]"
    ) -> None:
        r = await self._get_redis()
        task_id = message.task_id
        ip_data = await r.hget(IN_PROGRESS_KEY, task_id)
        await r.hdel(IN_PROGRESS_KEY, task_id)
        info = json.loads(ip_data) if ip_data else {}
        finish_time = time.time()
        result_info = {
            "task_id": task_id,
            "function": message.task_name,
            "success": not result.is_err,
            "enqueue_time": info.get("enqueue_time"),
            "start_time": info.get("start_time"),
            "finish_time": finish_time,
            "result": _serializable_result(result.return_value),
        }
        result_key = f"{RESULT_PREFIX}:{task_id}"
        await r.set(result_key, json.dumps(result_info), ex=RESULT_TTL)
        await r.zadd(COMPLETED_ZSET, {task_id: finish_time})
        # Expire old entries
        cutoff = time.time() - RESULT_TTL
        await r.zremrangebyscore(COMPLETED_ZSET, "-inf", cutoff)

        # Durable failure log. LAST in this method and inside its own try/except,
        # deliberately: taskiq runs the post_execute loop OUTSIDE any try/except
        # (Receiver.callback), and by this point the IN_PROGRESS row is already
        # HDEL'd - so anything that escapes here makes the task vanish from the
        # tracker entirely and skips the result-backend save. The everything-above
        # must already be durable before we attempt this.
        #
        # Classification is pure and in-process; the only I/O is one Redis push.
        # Postgres is written later by the maintenance loop (failure_log.drain_inbox),
        # because get_pg_connection() has no connect_timeout and a HUNG pgserver
        # would otherwise block the worker's single event loop while we tried to
        # record that Postgres was broken.
        try:
            from src.failure_log import classify_result, enqueue
            records = classify_result(
                message.task_name, task_id, message.labels,
                result.is_err, result.return_value,
            )
            if records:
                await enqueue(r, records)
        except Exception:
            pass


def pending_preseed(function_name: str) -> str:
    """The EXACT JSON value a PENDING pre-seed must carry - the middleware's
    pre_execute json.loads it, and a bare string kills the receiver before the
    task runs. The SINGLE source of truth for the shape; prefer `preseed_pending`,
    which also applies the expiry. Kept public for callers that already hold the
    encoded value (tests, and anything writing the field by hand)."""
    return json.dumps({"function": function_name, "enqueue_time": time.time()})


async def preseed_pending(r, task_id: str, function_name: str) -> None:
    """Write a PENDING row WITH its expiry - the ONE way to pre-seed a task.

    Every enqueue path writes this row before the broker kick, so a crash in that
    window (or a kick failure that outlives its own cleanup) strands a row nobody
    removes. It then reads as "queued" forever on /manage/tasks, and - worse - the
    coalesce guards in agent_scheduler._fire and events treat it as an ACTIVE run,
    silently refusing to fire that agent again.

    The expiry has to be enforced by REDIS rather than swept on read, because
    those guards hit PENDING with raw `hexists` and never touch TaskTracker (they
    run in the worker; TaskTracker's connection belongs to the web process). A
    read-side sweep would tidy the display and leave the guards blocked forever.

    HEXPIRE needs Redis >= 7.4; redisserver is on redis:alpine (8.x).
    """
    await r.hset(PENDING_KEY, task_id, pending_preseed(function_name))
    await r.hexpire(PENDING_KEY, PENDING_TTL, task_id)


async def clear_stale_in_progress(r) -> int:
    """Drop every IN_PROGRESS row at worker startup. Returns how many were stale.

    Exact, not heuristic: IN_PROGRESS is only ever cleared by post_execute, which
    by definition does not run if the worker dies mid-task. The compose command
    pins `--workers 1` against a single worker container, so at startup there is
    no process that could legitimately own one of these rows - every survivor is
    an orphan from the previous life.

    This matters more than the PENDING expiry it complements: a stale row here
    marks an agent's job_id permanently active, so agent_scheduler._fire coalesces
    every future run and the event dispatcher defers every event, forever, with no
    error surfaced anywhere. Rebuilding the worker mid-agent-run is enough to do it.

    NOTE: correctness is tied to `--workers 1` in docker-compose.yml. Raising the
    worker count means a restarting sibling would wipe a live worker's rows.
    """
    stale = await r.hlen(IN_PROGRESS_KEY)
    if stale:
        await r.delete(IN_PROGRESS_KEY)
    return stale


# ---------------------------------------------------------------------------
# Client-side helper (used by the web server to query task state)
# ---------------------------------------------------------------------------

class TaskTracker:
    """Query task tracking data from Redis."""

    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._redis: Optional[aioredis.Redis] = None

    async def startup(self) -> None:
        self._redis = _client(self.redis_url)

    async def shutdown(self) -> None:
        if self._redis:
            await self._redis.close()

    async def record_enqueue(self, task_id: str, function_name: str) -> None:
        await preseed_pending(self._redis, task_id, function_name)

    async def get_in_progress(self) -> List[TrackedTaskInfo]:
        """Get all in progress tasks."""
        data = await self._redis.hgetall(IN_PROGRESS_KEY)
        results = []
        for task_id, raw in data.items():
            info = json.loads(raw)
            results.append(
                TrackedTaskInfo(
                    task_id=task_id,
                    function=info.get("function", "unknown"),
                    start_time=info.get("start_time"),
                    enqueue_time=info.get("enqueue_time"),
                )
            )
        return results

    async def get_pending(self) -> List[TrackedTaskInfo]:
        """Get all pending tasks."""
        data = await self._redis.hgetall(PENDING_KEY)
        results = []
        for task_id, raw in data.items():
            info = json.loads(raw)
            results.append(
                TrackedTaskInfo(
                    task_id=task_id,
                    function=info.get("function", "unknown"),
                    enqueue_time=info.get("enqueue_time"),
                )
            )
        return results

    async def get_completed(self) -> List[TrackedTaskInfo]:
        """Get all completed tasks."""
        task_ids = await self._redis.zrevrange(COMPLETED_ZSET, 0, -1)
        results = []
        for task_id in task_ids:
            result_key = f"{RESULT_PREFIX}:{task_id}"
            raw = await self._redis.get(result_key)
            if raw:
                info = json.loads(raw)
                results.append(
                    TrackedTaskInfo(
                        task_id=info.get("task_id", task_id),
                        function=info.get("function", "unknown"),
                        success=info.get("success"),
                        enqueue_time=info.get("enqueue_time"),
                        start_time=info.get("start_time"),
                        finish_time=info.get("finish_time"),
                        result=info.get("result"),
                    )
                )
        return results
    
    async def is_active(self, task_id: str) -> bool:
        """True if the task is pending or in-progress."""
        pending = await self._redis.hexists(PENDING_KEY, task_id)
        in_progress = await self._redis.hexists(IN_PROGRESS_KEY, task_id)
        return pending or in_progress

    async def get_progress(self, task_id: str) -> Optional[dict]:
        """Read progress data for a task."""
        raw = await self._redis.get(f"{PROGRESS_PREFIX}:{task_id}")
        if raw:
            return json.loads(raw)
        return None

    async def delete_result(self, task_id: str) -> None:
        """Delete a specific task."""
        await self._redis.delete(f"{RESULT_PREFIX}:{task_id}")
        await self._redis.delete(f"{PROGRESS_PREFIX}:{task_id}")
        await self._redis.zrem(COMPLETED_ZSET, task_id)
        await self._redis.hdel(PENDING_KEY, task_id)
        await self._redis.hdel(IN_PROGRESS_KEY, task_id)


_RESULT_NOISE_KEYS = {"status", "content_hash", "task_id", "function"}


def summarize_task_result(result) -> str:
    """Render a task's return value as a compact, wrap-friendly table cell.

    Tasks return small status dicts; the raw ``str(dict)`` repr is noisy
    (content hashes, curly-quote mangling by the markdown renderer) and mostly
    duplicates the Success column. Here we lead with the ``status`` verb and
    append only the human-salient fields (reason / error / doc name / counts),
    joined with ", " so the cell can wrap instead of forcing a wide table.
    """
    if not result:
        return "-"
    if not isinstance(result, dict):
        text = str(result)
        return (text[:77] + "...") if len(text) > 80 else text

    d = dict(result)
    status = d.pop("status", None)
    parts = []
    # error/reason are the whole story when present.
    if d.get("error"):
        parts.append(str(d.pop("error")))
    if d.get("reason"):
        parts.append(str(d.pop("reason")))
    doc_id = d.pop("doc_id", None)
    if doc_id:
        base = os.path.basename(str(doc_id))
        parts.append(base[:-3] if base.endswith(".md") else base)
    # Remaining scalars → key=value; collections → key=count. Preserves the
    # informative bits (edges, processed, ghosted, vaults…) without dumping data.
    for k, v in d.items():
        if k in _RESULT_NOISE_KEYS:
            continue
        if isinstance(v, (list, tuple, set)):
            parts.append(f"{k}={len(v)}")
        elif isinstance(v, dict):
            continue
        else:
            parts.append(f"{k}={v}")

    detail = ", ".join(parts)
    if status and detail:
        summary = f"{status}: {detail}"
    else:
        summary = status or detail or "-"
    return (summary[:97] + "...") if len(summary) > 100 else summary
