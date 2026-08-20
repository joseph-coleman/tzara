# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Dependency health checks, shared by the /health probe and /manage/monitor.

Extracted from main.health_endpoint 2026-08-04 so the two surfaces cannot drift.
Lives in src/ rather than main.py for two reasons: main.py is ~4000 lines, and the
WORKER cannot import from it at all.

Division of labor follows the house pattern (llm_gate.read_gate_stats returns
data; the page renders it):

    collect_health()   -> the `checks` dict, nothing else
    health_endpoint    -> keeps `ready`, `hints` and the 200/503 decision, which
                          are its documented public contract
    /manage/monitor    -> renders `checks` as markdown

EVERY check is time-bounded. Only Postgres was, before this move: a *hung* (not
down - hung) ollamaserver has no timeout at all on the native path, which is
survivable for a JSON probe a caller can abandon and fatal for a page render. The
monitor page is the page you load DURING an incident, so it must not be the one
that hangs.
"""

import asyncio
import datetime
import logging

logger = logging.getLogger("health")

DEFAULT_TIMEOUT_S = 5.0


async def _bounded(coro, timeout: float, label: str):
    """Await with a deadline, turning any failure into a ('error', msg) result."""
    try:
        return True, await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        return False, f"timed out after {timeout:.0f}s"
    except Exception as e:
        logger.debug("health: %s check failed: %s", label, e)
        return False, str(e)


def _pg_ping() -> bool:
    from config import get_pg_connection
    conn = get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1;")
        cur.fetchone()
        return True
    finally:
        conn.close()


async def _check_postgres(timeout: float) -> dict:
    ok, val = await _bounded(asyncio.to_thread(_pg_ping), timeout, "postgres")
    return {"ok": True} if ok else {"ok": False, "error": val}


async def _check_redis(timeout: float) -> dict:
    from src.task_broker import get_async_redis

    async def ping():
        r = get_async_redis()
        try:
            await r.ping()
            return True
        finally:
            await r.close()

    ok, val = await _bounded(ping(), timeout, "redis")
    return {"ok": True} if ok else {"ok": False, "error": val}


async def _check_worker(timeout: float) -> dict:
    """Is the worker alive? The dispatcher writes a status key every tick, so a
    missing or stale one means the worker is down or the scheduler is disabled -
    otherwise indistinguishable from "healthy and quiet".

    This is the only worker-liveness signal in the product, and it was previously
    computed inline on /agents only.
    """
    from config import AGENT_SCHEDULER_TICK_S
    from src import events
    from src.task_broker import get_async_redis

    async def read():
        import json
        r = get_async_redis()
        try:
            return json.loads(await r.get(events.STATUS_KEY) or "null")
        finally:
            await r.close()

    ok, status = await _bounded(read(), timeout, "worker")
    if not ok:
        return {"ok": False, "error": status}
    age = None
    if status:
        try:
            age = (datetime.datetime.now()
                   - datetime.datetime.fromisoformat(status.get("ts", ""))).total_seconds()
        except (ValueError, TypeError):
            age = None
    alive = age is not None and age <= AGENT_SCHEDULER_TICK_S * 3
    out = {"ok": alive, "last_tick_age_s": round(age) if age is not None else None}
    if not alive:
        out["error"] = "no recent dispatch tick - worker down or scheduler disabled"
    return out


async def _check_llm(timeout: float, llm_mgr=None) -> dict:
    from config import LLM_PROVIDER, LLM_EMBED_MODEL, LLM_MODEL, LLM_URL

    def _present(configured: str, models: list) -> bool:
        base = (configured or "").split(":")[0]
        return any(m.get("name") == configured
                   or m.get("name", "").split(":")[0] == base for m in models)

    async def listing():
        mgr = llm_mgr
        if mgr is None:
            from src.llm_backend import create_llm_backend
            mgr = create_llm_backend(model=LLM_MODEL)
            try:
                return await mgr.list_available_models()
            finally:
                await mgr.aclose()
        return await mgr.list_available_models()

    ok, models = await _bounded(listing(), timeout, "llm")
    if not ok:
        return {"reachable": False, "provider": LLM_PROVIDER, "url": LLM_URL,
                "error": models}
    return {
        # An empty catalog is ambiguous - a just-started server has one - so
        # report unknown rather than a false negative.
        "reachable": True if models else None,
        "provider": LLM_PROVIDER,
        "url": LLM_URL,
        "chat_model": {"name": LLM_MODEL, "present": _present(LLM_MODEL, models)},
        "embed_model": {"name": LLM_EMBED_MODEL,
                        "present": _present(LLM_EMBED_MODEL, models)},
        "models_available": len(models),
    }


async def collect_health(llm_mgr=None,
                         timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Every dependency check, run concurrently and individually bounded.

    Returns the `checks` dict only - callers decide what "ready" means. Never
    raises: a check that blows up reports its own error rather than taking the
    caller down, because both callers are things you reach for when something is
    already wrong.
    """
    pg, redis, worker, llm = await asyncio.gather(
        _check_postgres(timeout),
        _check_redis(timeout),
        _check_worker(timeout),
        _check_llm(timeout, llm_mgr),
        return_exceptions=False,
    )
    return {"postgres": pg, "redis": redis, "worker": worker, "llm": llm}
