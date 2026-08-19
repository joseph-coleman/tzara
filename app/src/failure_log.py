# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Durable failure log - the record behind /manage/monitor and its nav badge.

WHY THIS EXISTS
---------------
Task results live in Redis with a 1h TTL (``task_tracker.RESULT_TTL``), so a
failure older than an hour was unrecoverable except from container logs. That is
how an orphaned ``index_document_task`` went unnoticed for 52 hours in July 2026,
and how every other fault found that month was found: by a human noticing an
anomaly in passing. The app surfaces work WAITING for you (the staged-proposal
badge) and nothing that BROKE.

SHAPE: classify in the hot path, persist off it
-----------------------------------------------
``classify_result`` is PURE - no I/O, no clock, no Redis. It is called from
``TaskTrackerMiddleware.post_execute``, which then does at most one Redis LPUSH.
The Postgres write happens later, in the maintenance loop, via ``drain_inbox``.

That split is not fastidiousness. Writing Postgres from post_execute would mean:

  * ``config.get_pg_connection()`` sets no ``connect_timeout``, so a HUNG (not
    down - hung) pgserver blocks the worker's single event loop, which also hosts
    the scheduler tick, the agent API and every other in-flight task. We would be
    recording "Postgres is broken" through Postgres, unbounded.
  * taskiq runs the post_execute loop OUTSIDE any try/except, and post_execute has
    already HDEL'd the IN_PROGRESS row by then - so a raise there makes the task
    vanish from the tracker entirely.

Routing through Redis does not remove the coupling, it relocates it to a service
whose death already means the queue is not running: nothing is lost that was not
already lost.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("failure_log")

INBOX_KEY = "taskiq:failures:inbox"
INBOX_CAP = 500          # LTRIM bound; the drain runs every tick, so this is slack
DETAIL_CHARS = 200       # house discipline (task_tracker._serializable_result, events)

# Statuses that are NOT failures. An explicit vocabulary, not duck-typing: two of
# these are load-bearing.
#   deferred  - run-lock contention. The DESIGNED happy path since agents defer
#               instead of blocking a worker slot; three agents share an 04:00
#               schedule, so badging this would cry wolf every single morning.
#   cancelled - human-initiated. Not a fault.
NON_FAILURE_STATUSES = frozenset({"ok", "skipped", "deferred", "cancelled",
                                  "unchanged", "links_only"})

# warm_model_task reports {"status": "failed"} for conditions its own docstring
# calls routine (a cold 120B load outrunning the client timeout while the server
# finishes it anyway). Recording those would train the badge to be ignored.
NEVER_RECORD_TASKS = frozenset({"warm_model_task"})


@dataclass
class FailureRecord:
    """One distinct broken thing. (kind, subject, vault_id) is the coalescing key."""
    kind: str                       # task | agent_run | memory_turn
    subject: str = ""               # task id / agent slug
    vault_id: str = ""
    detail: str = ""
    badge: bool = True              # False = recorded but never lights the badge

    def as_dict(self) -> dict:
        return {"kind": self.kind, "subject": self.subject,
                "vault_id": self.vault_id, "detail": self.detail[:DETAIL_CHARS],
                "badge": self.badge}


# ---------------------------------------------------------------------------
# The classifier: pure, and the entire trustworthiness of the badge
# ---------------------------------------------------------------------------

def is_final_attempt(labels: dict | None) -> bool:
    """Is this the LAST time taskiq will run this message?

    SimpleRetryMiddleware re-kicks with the same task_id BEFORE post_execute
    returns, so a task declaring ``retry_on_error=True`` (most of the file
    pipeline) produces one post_execute per attempt - all with ``is_err=True``.
    Without this gate, one broken file becomes TASKIQ_RETRY badge counts.

    Attempt k carries ``_retries == k-1``; the final attempt carries
    ``_retries == max_retries - 1``. A task with no retry label is always final.
    """
    labels = labels or {}
    retry_on_error = labels.get("retry_on_error")
    if isinstance(retry_on_error, str):
        retry_on_error = retry_on_error.lower() == "true"
    if not retry_on_error:
        return True
    from config import TASKIQ_RETRY
    try:
        attempt = int(labels.get("_retries", 0)) + 1
        max_retries = int(labels.get("max_retries", TASKIQ_RETRY))
    except (TypeError, ValueError):
        return True
    return attempt >= max_retries


def classify_result(task_name: str, task_id: str, labels: dict | None,
                    is_err: bool, result: Any) -> list[FailureRecord]:
    """Decide what (if anything) about this task outcome is worth recording.

    PURE - no I/O, no clock. Unit-tested in .test/test_failure_log.py, because a
    badge is only useful if it is right; everything else in this module is
    plumbing.

    THE TRAP this function exists for: ``post_execute`` records
    ``success = not result.is_err``, but ``run_agent_task`` catches per-vault
    exceptions itself and RETURNS a dict - so a run where three vaults failed
    records ``success=True`` with ``result["failed"] == 3``. ``ingest_document``
    likewise returns ``{"status": "failed"}`` without raising. Counting
    ``success=False`` alone would miss most real failures.
    """
    if task_name in NEVER_RECORD_TASKS:
        return []
    if not is_final_attempt(labels):
        return []

    # The task raised: taskiq itself judged it failed.
    if is_err:
        return [FailureRecord(kind="task", subject=task_id,
                              detail=_detail(result) or "task raised")]

    if not isinstance(result, dict):
        return []

    # Per-vault fan-out FIRST. One row per failed vault, so the record names the
    # vault and carries the error text - a row reading "failed=3" with neither
    # sends the reader back to grepping logs, which is what this replaces.
    entries = result.get("results")
    if isinstance(entries, list):
        out = [
            FailureRecord(kind="agent_run",
                          subject=str(result.get("agent") or task_id),
                          vault_id=str(e.get("vault_id") or ""),
                          detail=str(e.get("error") or "")[:DETAIL_CHARS])
            for e in entries
            if isinstance(e, dict) and e.get("error")
        ]
        if out:
            return out

    status = str(result.get("status") or "").lower()

    # A bulk task reports its own tally; "ok" with failures is still failure.
    try:
        failed_n = int(result.get("failed") or 0)
    except (TypeError, ValueError):
        failed_n = 0
    if failed_n > 0:
        return [FailureRecord(kind="task", subject=task_id,
                              detail=_detail(result))]

    if status and status not in NON_FAILURE_STATUSES:
        # "failed" and anything unrecognised. Unknown statuses are recorded
        # rather than silently dropped - a new status verb should be noticed.
        return [FailureRecord(kind="task", subject=task_id,
                              detail=_detail(result))]

    return []


def _detail(result: Any) -> str:
    """Compact one-line summary, reusing the tracker's renderer so the monitor
    page and /manage/tasks describe a result the same way."""
    try:
        from src.task_tracker import summarize_task_result
        return summarize_task_result(result)[:DETAIL_CHARS]
    except Exception:
        return str(result)[:DETAIL_CHARS]


# ---------------------------------------------------------------------------
# Hot path: enqueue only (one Redis command)
# ---------------------------------------------------------------------------

async def enqueue(r, records: list[FailureRecord]) -> None:
    """Push classified records onto the inbox. Best-effort; never raises.

    Takes the caller's redis client so post_execute reuses the connection it
    already holds - no new pool, no extra round trip beyond the push itself.
    """
    if not records:
        return
    try:
        p = r.pipeline()
        for rec in records:
            p.lpush(INBOX_KEY, json.dumps(rec.as_dict()))
        p.ltrim(INBOX_KEY, 0, INBOX_CAP - 1)
        await p.execute()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Off the hot path: Redis inbox -> Postgres, and the readers
# ---------------------------------------------------------------------------

def _conn():
    from config import get_pg_connection
    return get_pg_connection()


def upsert(records: list[dict]) -> int:
    """Coalescing insert. Returns rows written. SYNC - call via asyncio.to_thread.

    ON CONFLICT against the partial unique index bumps occurrences and
    last_seen_at instead of adding a row, so a file failing every five minutes is
    one loud row with a count rather than hundreds. A failure arriving AFTER an
    ack finds no open row and opens a fresh one, correctly re-lighting the badge.
    """
    if not records:
        return 0
    conn = _conn()
    try:
        cur = conn.cursor()
        for rec in records:
            cur.execute(
                """
                INSERT INTO system_failures (kind, subject, vault_id, detail, badge)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (kind, subject, vault_id) WHERE status = 'open'
                DO UPDATE SET occurrences  = system_failures.occurrences + 1,
                              last_seen_at = NOW(),
                              detail       = EXCLUDED.detail
                """,
                (rec.get("kind", "task"), rec.get("subject", ""),
                 rec.get("vault_id", ""), (rec.get("detail") or "")[:DETAIL_CHARS],
                 bool(rec.get("badge", True))),
            )
        conn.commit()
        return len(records)
    finally:
        conn.close()


async def drain_inbox(r, limit: int = INBOX_CAP) -> int:
    """Move everything queued in Redis into Postgres. Returns rows written."""
    import asyncio

    raw = []
    try:
        p = r.pipeline()
        p.lrange(INBOX_KEY, 0, limit - 1)
        p.ltrim(INBOX_KEY, limit, -1)
        raw, _ = await p.execute()
    except Exception:
        logger.exception("failure_log: could not read the inbox")
        return 0
    records = []
    for item in raw or []:
        try:
            records.append(json.loads(item))
        except (TypeError, ValueError):
            continue
    if not records:
        return 0
    try:
        return await asyncio.to_thread(upsert, records)
    except Exception:
        logger.exception("failure_log: could not write %d record(s)", len(records))
        return 0


def open_badge_count() -> int:
    """How many distinct broken things are currently unacknowledged AND badge-worthy."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM system_failures "
                    "WHERE status = 'open' AND badge")
        return int(cur.fetchone()[0])
    finally:
        conn.close()


def recent_failures(days: int = 30, limit: int = 200) -> list[dict]:
    """Open rows first (newest last-seen), then recently acked/resolved ones.

    Ages are computed in SQL: TIMESTAMPTZ returns tz-aware datetimes while the
    rest of this codebase is naive-local, and subtracting the two raises.
    """
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, kind, subject, vault_id, detail, badge, status, occurrences,
                   EXTRACT(EPOCH FROM (NOW() - first_seen_at))::bigint,
                   EXTRACT(EPOCH FROM (NOW() - last_seen_at))::bigint
              FROM system_failures
             WHERE last_seen_at > NOW() - make_interval(days => %s)
             ORDER BY (status = 'open') DESC, last_seen_at DESC
             LIMIT %s
            """,
            (days, limit),
        )
        cols = ("id", "kind", "subject", "vault_id", "detail", "badge", "status",
                "occurrences", "first_seen_age_s", "last_seen_age_s")
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


def ack(failure_id: int) -> bool:
    """Acknowledge ONE row. Deliberately not an ack-all: a single button that
    clears the badge trains dismissal without reading, which is precisely how a
    badge stops being trusted."""
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE system_failures SET status = 'acked', acked_at = NOW() "
                    "WHERE id = %s AND status = 'open'", (failure_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def prune(days: int = 30) -> int:
    """Drop rows untouched for `days`. Returns rows removed.

    Keyed on last_seen_at, NOT first_seen_at: with coalescing, a thing that has
    been failing continuously since day 0 would otherwise be deleted out from
    under an open badge on day 31.
    """
    conn = _conn()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM system_failures "
                    "WHERE last_seen_at < NOW() - make_interval(days => %s)", (days,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
