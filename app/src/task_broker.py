# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Taskiq broker configuration.

Centralizes the broker definition so both the web server (for enqueueing)
and the worker CLI (for executing tasks) can import it.

Worker is started via:
    taskiq worker src.task_definitions:broker
"""

from taskiq import SimpleRetryMiddleware
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from config import REDIS_HOST, REDIS_PORT, TASKIQ_RETRY

REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}"


def get_async_redis(url: str | None = None):
    """The single async redis client factory (decode_responses=True). Connection
    params live here with REDIS_URL - add socket_timeout/retry/max_connections once,
    not in ~16 copies. Caller awaits .close(). `url` overrides REDIS_URL for the
    few callers that carry their own (TaskTracker/TaskTrackerMiddleware are
    constructed with one) so they can still inherit the connection policy.

    The timeouts and retry are load-bearing, not decoration. Every enqueue path
    writes its PENDING bookkeeping row BEFORE handing the message to the broker,
    so a single dropped command in that window strands a task that reads as
    "queued" forever (2026-07-29: an index_document_task lost during a 16-file
    seed-refresh burst). Retrying the command is the only fix that keeps the task
    RUNNING rather than merely tidying up after it went missing.

    Safe to apply globally because nothing routed through this factory issues a
    BLOCKING read - the broker's own BRPOP uses taskiq_redis's separate pool, so
    socket_timeout can never cut a long poll short."""
    import redis.asyncio as _aioredis
    from redis.backoff import ExponentialBackoff
    from redis.retry import Retry
    return _aioredis.from_url(
        url or REDIS_URL,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
        # Retry defaults to (ConnectionError, TimeoutError); ~50ms doubling to 1s.
        retry=Retry(ExponentialBackoff(cap=1.0, base=0.05), retries=3),
    )


def get_sync_redis():
    """The single sync redis client factory (decode_responses=True). Caller
    calls .close(). Used by sync code paths (e.g. WikiDoc.set_debounce)."""
    import redis as _redis
    return _redis.from_url(REDIS_URL, decode_responses=True)

result_backend = RedisAsyncResultBackend(
    redis_url=REDIS_URL,
    result_ex_time=3600,
)

from src.task_tracker import TaskTrackerMiddleware

broker = ListQueueBroker(
    url=REDIS_URL,
    queue_name="taskiq:queue",
).with_result_backend(
    result_backend,
).with_middlewares(
    TaskTrackerMiddleware(REDIS_URL),
    SimpleRetryMiddleware(default_retry_count=TASKIQ_RETRY),
)

