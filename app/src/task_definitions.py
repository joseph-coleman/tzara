# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Taskiq task definitions.

All background tasks are defined here and decorated with @broker.task.
The worker imports this module via:
    taskiq worker src.task_definitions:broker
"""

import asyncio
import os
import re
from pathlib import Path

from taskiq import TaskiqEvents


from config import (
    AUTO_GENERATE_TAGS,
    AUTO_GENERATE_SUMMARY,
    CHARS_PER_TOKEN,
    DEFAULT_VAULT,
    INDEX_DOCUMENT_FRONTMATTER_DEFAULT,
    LLM_KEEP_ALIVE,
    LLM_MODEL,
    LLM_URL,
    USE_GIT_VERSIONING,
    vault_root,
)

from src.wikidoc import WikiDoc

import json
from src.task_broker import broker, REDIS_URL, get_async_redis
import time
from src.task_tracker import PENDING_KEY, PROGRESS_PREFIX, RESULT_TTL, preseed_pending
# The watcher owns these key namespaces; build its ids/keys through its helpers so
# this module's self-write suppression can't drift from what the watcher reads.
from src.file_watcher import (
    CANCELLED_SET,
    WRITE_DEBOUNCE_TTL,
    watcher_debounce_key,
    watcher_task_id,
)

# Serializing lock for mutating agent runs (see run_agent_task). One run at a
# time restores the single-writer invariant the codebase assumes; the TTL frees
# the lock if a worker dies mid-run.
AGENT_RUN_LOCK_KEY = "agent:runlock"
AGENT_RUN_LOCK_TTL_S = 7200      # 2h - generously above any sane run
# There is deliberately no WAIT constant: a contended run DEFERS and retries on
# the next scheduler tick rather than waiting, so it never holds a worker
# execution slot to keep its place in line. See run_agent_task.


####################################################

# example of a long running task. 

# #import asyncio
# import redis.asyncio as aioredis
# from src.task_broker import REDIS_URL

# PROGRESS_PREFIX = "taskiq:tracker:progress"


# # @broker.task(
# #     task_name="example_long_task",
# #     retry_on_error=False,
# # )
# # async def example_long_task(job_id: str, total_items: int) -> dict:
# #     """Simulate a long-running task that reports progress."""
# #     r = get_async_redis()
    
# #     try:
# #         for i in range(1, total_items + 1):
# #             # ... do real work here ...
# #             print("doing something, ", i)
# #             await asyncio.sleep(1)  # simulate work
# #             # write progress to redis
# #             await r.set(f"{PROGRESS_PREFIX}:{job_id}", f"{i}/{total_items}", ex=3600)
# #         return {"status": "ok", "processed": total_items}
# #     finally:
# #         await r.close()


#### another edit. 

# #import asyncio
# from src.task_broker import result_backend
# from taskiq.depends.progress_tracker import TaskProgress, TaskState


# @broker.task(task_name="example_long_task", retry_on_error=False)
# async def example_long_task(job_id: str, total_items: int) -> dict:
#     """Simulate a long-running task that reports progress."""
#     for i in range(1, total_items + 1):
#         # ... do real work here ...
#         print("doing something ", i)
#         await asyncio.sleep(1)  # simulate work

#     await result_backend.set_progress(
#         job_id,
#         TaskProgress(
#             state=TaskState.STARTED,
#             meta={"step": f"{i}/{total_items}", "percent": i / total_items},
#         ),
#     )


#     return {"status": "ok", "processed": total_items}


@broker.task(task_name="warm_model_task", retry_on_error=True, max_retries=3)
async def warm_model_task(
    llm_url: str,
    model_name: str,
    keep_alive: str = "30m",
    # 30s could not load a 120B model: the client timed out and the task reported
    # {"status": "failed", "error": ""} while the server finished the load anyway,
    # so warms looked broken when they were merely slow. Sized for a cold load of
    # a large chat model; a small local model still returns in well under a second.
    timeout: float = 180.0,
    num_ctx: int = 0,
) -> dict:
    """Load a model into memory without generating text.

    Accepts any URL and model, so it can warm models on different servers
    with different keep_alive durations.  The timeout parameter controls
    how long to wait for the connection (useful when the server may be asleep).

    `num_ctx` is the load ASK (LLM_NUM_CTX). When >0 the window is requested at
    load via the provider's real knob: Ollama honors `num_ctx` on the native warm,
    but Lemonade only honors it via /v1/load -- so for Lemonade we route the ask
    through the configured backend (LemonadeBackend.warm_model) rather than the raw
    ollama client, keeping the /v1/load call in its one canonical home.
    """
    import ollama as _ollama
    from config import LLM_PROVIDER
    from src.llm_backend import create_llm_backend
    from src.llm_gate import get_llm_gate

    # Warming is a native-mount MANAGEMENT op (load into VRAM via keep_alive)
    # against the passed llm_url. Whether warming makes sense for the active
    # provider is decided by the ENQUEUER (main._fire_warm_task) - a pure OpenAI
    # /v1 server has no such concept and loads on demand, so it never enqueues.

    # Pre-check OUTSIDE the gate. Warming an already-resident model does nothing,
    # and this task's real cost is the QUEUE, not the work: measured 2026-08-02,
    # four warms waited 316s in total (worst 252s) behind an agent to accomplish
    # nothing, because a page view fires one and both models sit permanently
    # loaded here. Checking inside the gate would mean paying that wait first.
    # A ctx ASK (num_ctx>0) IS real work even when loaded - Lemonade applies the
    # window via /v1/load - so only the ask-free case is safe to skip.
    # This is a management call (/api/ps, /health), not inference, so it does not
    # need the gate. If the model unloads between here and its next use, the
    # server just loads it on demand; a missed warm costs latency, not results.
    if num_ctx <= 0:
        try:
            probe = create_llm_backend(url=llm_url, model=model_name,
                                       keep_alive=keep_alive)
            try:
                if await probe.is_any_model_loaded(model_name):
                    print(f"warm_model_task: '{model_name}' already loaded on "
                          f"{llm_url}; skipping")
                    return {"status": "skipped", "reason": "already loaded",
                            "model": model_name, "url": llm_url}
            finally:
                await probe.aclose()
        except Exception as e:
            print(f"warm_model_task: load check failed for '{model_name}' "
                  f"({e}); warming anyway")

    try:
        async with get_llm_gate("warm"):
            if num_ctx > 0 and LLM_PROVIDER == "lemonade":
                # Lemonade ignores /api/generate's num_ctx; apply the ask via /v1/load.
                from src.llm_backend import create_llm_backend
                backend = create_llm_backend(
                    url=llm_url, model=model_name, keep_alive=keep_alive,
                    num_ctx_request=num_ctx,
                )
                try:
                    await backend.warm_model()
                finally:
                    await backend.aclose()
            else:
                client = _ollama.AsyncClient(host=llm_url, timeout=timeout)
                options = {"num_ctx": num_ctx} if num_ctx > 0 else None
                try:
                    await client.generate(model=model_name, prompt="",
                                          keep_alive=keep_alive, options=options)
                except _ollama.ResponseError:
                    # Embedding models don't support generate; use embed instead
                    await client.embed(model=model_name, input=["warm"], keep_alive=keep_alive)
        print(f"warm_model_task: model '{model_name}' warmed on {llm_url} (keep_alive={keep_alive}, num_ctx={num_ctx})")
        return {"status": "ok", "model": model_name, "url": llm_url}
    except Exception as e:
        print(f"warm_model_task: failed to warm '{model_name}' on {llm_url}: {e}")
        return {"status": "failed", "error": str(e), "model": model_name, "url": llm_url}



@broker.task(task_name="example_long_task", retry_on_error=False)
async def example_long_task(job_id: str, total_items: int) -> dict:
    """Simulate a long-running task that reports progress."""
    '''
        Note to self, this task has a race condition where if the task is "deleted"
        Then this continues to run, and then on completion, this sets the results of
        the task, essentially ressurecting it since it would then appear given
        a results query. 
    '''
    r = get_async_redis()
    try:
        for i in range(1, total_items + 1):
            # ... do real work here ...
            print("doing something ", i)
            await asyncio.sleep(1)  # simulate work

            await r.set(
                f"{PROGRESS_PREFIX}:{job_id}",
                json.dumps({"step": f"{i}/{total_items}", "percent": i / total_items}),
                ex=RESULT_TTL,
            )

        return {"status": "ok", "processed": total_items}
    finally:
        await r.close()

#######################################################


@broker.task(task_name="run_agent_task", retry_on_error=False)
async def run_agent_task(agent_slug: str, vault_id: str | None = None,
                         trigger_events: list[dict] | None = None,
                         event_depth: int = 0,
                         trigger_source: str = "manual") -> dict:
    """Run one agent (a markdown definition in the system vault) against its target
    vaults. vault_id=None fans out to every vault the definition targets; a slug
    runs just that vault (it must be among the definition's targets). Each vault
    run writes the agent's output page + a run log into its owned area
    ({AGENT_OUTPUT_DIR}/{agent}/).

    Event-triggered runs (src.events dispatcher) pass the triggering envelopes
    in ``trigger_events`` (interpolated into the kickoff + recorded in the run
    log) and their chain depth in ``event_depth`` - lifecycle events emitted
    below carry that depth so chained triggers stay bounded (EVENT_MAX_DEPTH)."""
    from src import agent_registry
    from src.background_agents import (
        AgentCancelled, make_worker_llm, run_background_agent)
    from src.events import emit, format_trigger_note
    from src.llm_gate import record_runlock

    trigger_note = format_trigger_note(trigger_events) if trigger_events else None

    # Checked form: includes the cross-agent trigger-cycle errors that plain
    # get_agent can't see, so a cycle-flagged agent can't run via manual fire.
    agent_def = agent_registry.get_agent_checked(agent_slug)
    if agent_def is None:
        return {"status": "failed", "error": f"unknown agent '{agent_slug}'"}
    if not agent_def.valid:
        return {"status": "failed", "agent": agent_slug,
                "error": "; ".join(agent_def.errors)}

    targets = agent_registry.resolve_target_vaults(agent_def)
    if vault_id is None:
        vaults = targets
    elif vault_id in targets:
        vaults = [vault_id]
    else:
        return {"status": "failed", "agent": agent_slug,
                "error": f"vault '{vault_id}' is not targeted by this agent"}
    if not vaults:
        return {"status": "failed", "agent": agent_slug, "error": "no target vaults"}

    agent = agent_registry.build_background_agent(agent_def)
    job_id = agent_registry.agent_job_id(agent_slug, vault_id)

    r = get_async_redis()
    llm_mgr = make_worker_llm()

    cancel_key = agent_registry.agent_cancel_key(job_id)

    async def _cancelled() -> bool:
        # Non-consuming EXISTS so the flag persists across the vault fan-out
        # (each vault's loop re-reads it). The key's own TTL self-heals a flag
        # that's never observed; the finally deletes it on the normal path.
        return bool(await r.exists(cancel_key))
    results = []
    failed = 0
    cancelled = 0
    got_lock = False
    try:
        # SERIALIZING RUN LOCK: agents ended the single-writer world; this lock
        # restores it - one mutating agent run at a time, across all agents and
        # vaults. SET-NX-EX (the debounce idiom); TTL is the crash safety net.
        #
        # DEFER, don't block. This used to poll for up to 30 minutes inside the
        # task body, which meant a waiting agent occupied one
        # of the worker's `--max-async-tasks 3` execution slots doing nothing but
        # sleeping. Three agents share a 04:00 daily schedule, so all three slots
        # filled every morning and indexing/embedding could not be picked up at
        # all - a queueing problem converted into a capacity problem, and one the
        # LLM gate's priority ordering cannot fix because a task that is never
        # dispatched never reaches the gate.
        #
        # Giving the slot back costs a Redis round trip and a retry next
        # scheduler tick (~60s). The occurrence is NOT lost: the scheduler stamps
        # last-run only once a run actually acquires the lock (see
        # agent_scheduler), so a deferred agent stays due and tries again.
        _lock_t0 = time.monotonic()
        got_lock = await r.set(AGENT_RUN_LOCK_KEY, job_id, nx=True,
                               ex=AGENT_RUN_LOCK_TTL_S)
        if not got_lock:
            holder = await r.get(AGENT_RUN_LOCK_KEY)
            await record_runlock(agent_slug, deferred=True)
            print(f"run_agent_task: deferring '{agent_slug}' (lock held by {holder})")
            return {"status": "deferred", "agent": agent_slug,
                    "reason": f"another agent run is active ({holder})"}
        await record_runlock(
            agent_slug, wait_ms=int((time.monotonic() - _lock_t0) * 1000))
        _held_from = time.monotonic()
        # Mark the occurrence as genuinely started, now that we hold the lock.
        await _stamp_agent_run(r, agent_slug)

        total = len(vaults)
        for i, vid in enumerate(vaults, start=1):
            await r.set(
                f"{PROGRESS_PREFIX}:{job_id}",
                json.dumps({"step": f"{i}/{total}", "vault": vid}),
                ex=RESULT_TTL,
            )
            try:
                res = await run_background_agent(agent, vid, llm_mgr,
                                                 cancel_check=_cancelled,
                                                 kickoff_extra=trigger_note,
                                                 trigger_events=trigger_events,
                                                 trigger_source=trigger_source)
                results.append(res)
                # Lifecycle events are PER VAULT (no aggregate whole-task
                # event). emit() never raises - see src.events.
                await emit("agent.completed", vault=vid, subject=agent_slug,
                           actor=f"agent:{agent_slug}",
                           cause_run_id=res.get("run_id", ""), depth=event_depth,
                           payload={"staged": res.get("staged_count", 0),
                                    "applied": res.get("applied_count", 0),
                                    "output": res.get("output_path")})
                if res.get("staged_count", 0) > 0:
                    await emit("staging.created", vault=vid, subject=agent_slug,
                               actor=f"agent:{agent_slug}",
                               cause_run_id=res.get("run_id", ""),
                               depth=event_depth,
                               payload={"staged": res.get("staged_count", 0)})
            except AgentCancelled:
                # A deliberate stop, not a failure - accounted separately so the
                # activity surface doesn't cry wolf.
                cancelled += 1
                print(f"run_agent_task: agent '{agent_slug}' vault '{vid}' cancelled")
                results.append({"vault_id": vid, "status": "cancelled"})
                # The cancel flag is human-set; the run_id is lost on the
                # exception path (minted inside run_background_agent).
                await emit("agent.cancelled", vault=vid, subject=agent_slug,
                           actor="human", depth=event_depth)
            except Exception as e:
                failed += 1
                print(f"run_agent_task: agent '{agent_slug}' vault '{vid}' failed: {e}")
                results.append({"vault_id": vid, "error": str(e)})
                await emit("agent.failed", vault=vid, subject=agent_slug,
                           actor=f"agent:{agent_slug}", depth=event_depth,
                           payload={"error": str(e)[:200]})
        status = "cancelled" if cancelled and not failed else "ok"
        return {"status": status, "agent": agent_slug, "vaults": total,
                "failed": failed, "cancelled": cancelled, "results": results}
    finally:
        # Delete the cancel flag (its TTL is only the crash safety net) so it
        # can't leak into the next run of this same agent.
        await r.delete(cancel_key)
        if got_lock:
            # How long this agent OWNED the shared lock - the number that says
            # who is actually making everyone else wait.
            await record_runlock(
                agent_slug, hold_ms=int((time.monotonic() - _held_from) * 1000))
            # Release only our own lock (GET-compare-DEL; the non-atomic window
            # is acceptable single-user - TTL covers the pathological case).
            if await r.get(AGENT_RUN_LOCK_KEY) == job_id:
                await r.delete(AGENT_RUN_LOCK_KEY)
        await r.close()
        # Each run builds its own worker backend; close its /v1 httpx client so it
        # isn't leaked across the worker's lifetime.
        await llm_mgr.aclose()


@broker.task(
    task_name="test_postgresql",
    retry_on_error=False,
)
async def test_postgresql():
    response = ""
    connection = None
    try:
        # Connect to PostgreSQL
        from config import get_pg_connection
        connection = get_pg_connection()
        response += "Connection successful! <br>"
        # Create a cursor object to execute SQL queries
        cursor = connection.cursor()
        # Example query: Fetch PostgreSQL version
        cursor.execute("SELECT version();")
        db_version = cursor.fetchone()
        response += f"PostgreSQL version: {db_version} <br>"
    except Exception as error:
        response += f"Error connecting to PostgreSQL: {error} <br>"
    finally:
        # Close the connection
        if connection:
            cursor.close()
            connection.close()
            response += "Connection closed."

    print("From worker")
    print(response)
    return response


@broker.task(
    task_name="generate_tags_task",
    retry_on_error=True,
    max_retries=3,
)
async def generate_tags_task(
    file_path: str,
    normalized_url_path: str,
    llm_url: str,
    llm_model: str,
    keep_alive: str,
) -> dict:
    """Auto-generate tags for a wiki document via Ollama.
    Kept for backward compatibility - delegates to generate_metadata_task."""
    return await _generate_metadata_impl(
        file_path, normalized_url_path, llm_url, llm_model, keep_alive
    )


@broker.task(
    task_name="generate_metadata_task",
    retry_on_error=True,
    max_retries=3,
)
async def generate_metadata_task(
    file_path: str,
    normalized_url_path: str,
    llm_url: str,
    llm_model: str,
    keep_alive: str,
    vault_id: str = DEFAULT_VAULT,
) -> dict:
    """Auto-generate tags and summary for a wiki document via Ollama."""
    return await _generate_metadata_impl(
        file_path, normalized_url_path, llm_url, llm_model, keep_alive, vault_id
    )


def _compute_body_char_limit(context_length: int, num_predict: int = 256) -> int:
    """Estimate context available for use less assumed prompt and response usage."""
    PROMPT_OVERHEAD_TOKENS = 150
    available = context_length - PROMPT_OVERHEAD_TOKENS - num_predict
    if available < 500:
        available = 500
    return int(available * CHARS_PER_TOKEN)


async def _generate_metadata_impl(
    file_path: str,
    normalized_url_path: str,
    llm_url: str,
    llm_model: str,
    keep_alive: str,
    vault_id: str = DEFAULT_VAULT,
) -> dict:
    """Shared implementation for tag/summary generation."""
    import ollama as _ollama
    from src.frontmatter import parse_llm_tags
    from src.llm_gate import get_llm_gate

    try:
        content = WikiDoc.read_text_at(file_path)  # canonical LF-normalized read
    except Exception as e:
        print(f"generate_metadata_task: failed to read {file_path}: {e}")
        return {"status": "failed", "error": str(e)}

    frontmatter = WikiDoc.parse_frontmatter(content)
    _index_flag = frontmatter.get("index", str(INDEX_DOCUMENT_FRONTMATTER_DEFAULT))
    if _index_flag.lower() in ("false", "no", "0"):
        return {"status": "skipped", "reason": "indexing disabled via frontmatter"}

    body = WikiDoc.strip_frontmatter(content)

    if len(body.strip()) < 50:
        return {"status": "skipped", "reason": "body too short"}

    # Tag/summary generation is INFERENCE, so it routes through the configured
    # backend (OpenAI /v1 for ollama/lemonade/openai) rather than the Ollama
    # /api/generate mount - keeping the inference-on-/v1, management-on-/api
    # separation intact. Context length is a MANAGEMENT query (native /api/show
    # where available, else fallback) via the same backend. `max_tokens` is the
    # num_predict analog; reasoning is dropped on /v1 (the <think> strip below is
    # a belt-and-suspenders backstop for models that inline it).
    from src.llm_backend import create_llm_backend

    NUM_PREDICT = 256  # assumed size of typical response
    new_tags = []
    summary = ""
    be = create_llm_backend(url=llm_url, model=llm_model, keep_alive=keep_alive)
    try:
        context_length = await be.get_context_length()
        body_char_limit = _compute_body_char_limit(context_length, NUM_PREDICT)

        # --- Generate tags ---
        if AUTO_GENERATE_TAGS:
            tags_prompt = (
                "Read the following document and generate 3 to 8 keyword tags that describe "
                "its main topics. Return ONLY a comma-separated list of lowercase keywords. "
                "Do not include explanations or numbering.\n\n"
                "Example output: python, machine-learning, tutorial\n\n"
                f"Document:\n{body[:body_char_limit]}\n\nTags:"
            )
            try:
                async with get_llm_gate("metadata:tags"):
                    raw_tags = await be.generate(tags_prompt, max_tokens=NUM_PREDICT)
                new_tags = parse_llm_tags(raw_tags)
            except Exception as e:
                print(f"generate_metadata_task: tags generation failed: {e}")

        # --- Generate summary ---
        if AUTO_GENERATE_SUMMARY:
            summary_prompt = (
                "Summarize the following document in 1-3 concise sentences. "
                "Return ONLY the summary, no preamble, no header, no lists. Only 1-3 consise sentences. "
                "The summary will immediately follow after the document section.\n\n"
                f"Document:\n{body[:body_char_limit]}\n\nSummary:"
            )
            try:
                async with get_llm_gate("metadata:summary"):
                    raw_summary = (await be.generate(summary_prompt, max_tokens=NUM_PREDICT * 2)).strip()
                summary = re.sub(
                    r"<think>.*?</think>", "", raw_summary, flags=re.DOTALL
                ).strip()
            except Exception as e:
                print(f"generate_metadata_task: summary generation failed: {e}")
    finally:
        await be.aclose()

    result = {}

    if not new_tags and not summary:
        return {"status": "skipped", "reason": "no metadata generated"}

    # Re-read file in case it was saved again during processing.
    #
    # Preserve the file's on-disk newline style. Files reach a vault from mixed
    # sources: the container (Linux) app writes LF, but Windows editors like
    # Obsidian write CRLF. A frontmatter-only update must NOT flip the whole
    # file's line endings, or every metadata run rewrites every line and churns
    # the entire file in git. So read with newline="" (no translation), work in
    # LF internally (the frontmatter helpers assume "\n"), then restore the
    # original EOL on write, also with newline="" (no translation).
    # Canonical EOL-preserving read (this path DONATED this logic; now it calls
    # the primitive). read_text returns LF content + the detected EOL, replayed
    # by write_text below so a frontmatter-only edit never flips the whole file.
    pair = WikiDoc.read_text(vault_id, normalized_url_path)
    if pair is None:
        print(f"generate_metadata_task: failed to re-read {file_path}")
        return {"status": "failed", "error": "re-read failed"}
    content, file_newline = pair

    updated = content
    if new_tags:
        updated = WikiDoc.update_tags_in_content(updated, new_tags)
        result["tags"] = new_tags
    if summary:
        updated = WikiDoc.update_summary_in_content(updated, summary)
        result["summary"] = summary

    if updated != content:
        try:
            r = get_async_redis()
            # Suppress the watcher's modify -> update_document_task enqueue for
            # this write. Built by watcher_debounce_key so the format can't drift
            # from the watcher's - omitting the vault segment (the old bug) left
            # the write unsuppressed, firing a spurious reindex after every
            # metadata write.
            await r.set(
                watcher_debounce_key("update_document_task", vault_id,
                                     normalized_url_path),
                "1", ex=WRITE_DEBOUNCE_TTL,
            )
            await r.close()
        except Exception as e:
            print(f"generate_metadata_task: debounce set failed: {e}")

        # Suppress the watcher's OWN git commit for the write below. The watcher
        # fires on the modify, so git:debounce MUST be set BEFORE write_text --
        # otherwise the watcher's git task races our save_version on the repo lock
        # (intermittent `git add` exit 128). Same key contract as the app save path.
        if USE_GIT_VERSIONING:
            try:
                await asyncio.to_thread(WikiDoc.set_debounce, vault_id, normalized_url_path)
            except Exception:
                pass

        # Canonical write restores the original EOL (write_text replays
        # file_newline), so a frontmatter-only edit never flips the whole file.
        try:
            WikiDoc.write_text(vault_id, normalized_url_path, updated, eol=file_newline)
            print(f"generate_metadata_task: updated metadata for {file_path}")
        except Exception as e:
            print(f"generate_metadata_task: failed to write {file_path}: {e}")
            return {"status": "failed", "error": str(e)}

        if USE_GIT_VERSIONING:
            try:
                vt = _vault_tracker(vault_id)
                await asyncio.to_thread(
                    vt.save_version,
                    os.path.join(vault_root(vault_id), normalized_url_path),
                    message=f"Auto-generated metadata for {Path(file_path).name}",
                )
            except Exception as e:
                print(f"generate_metadata_task: git commit failed for {file_path}: {e}")

    result["status"] = "ok"
    return result


def _make_task_id(prefix: str, rel_path: str) -> str:
    """Generate a deterministic task ID for bulk sub-tasks."""
    return f"{prefix}:{rel_path}"


async def _record_enqueue(r, task_id: str, function_name: str):
    """Record a sub-task enqueue in the tracker pending hash."""
    await preseed_pending(r, task_id, function_name)


def _is_excluded(rel_path: str) -> bool:
    """Check if a relative path falls under an excluded folder.

    Delegates to rag_indexer.is_excluded - this module carried a byte-identical
    copy until 2026-07-31. Two independent answers to "should this be indexed?"
    is how a reconcile ends up reporting gaps that aren't real. Lazy import:
    rag_indexer is imported per-task throughout this module already.
    """
    from src.rag_indexer import is_excluded
    return is_excluded(rel_path)


@broker.task(
    task_name="generate_all_metadata_task",
    retry_on_error=False,
)
async def generate_all_metadata_task(force: bool = False, vault_id: str | None = None) -> dict:
    """Generate metadata (tags/summary) for eligible documents.

    vault_id=None processes every vault; a slug processes just that vault. The
    progress/job id mirrors this (metadata:all vs metadata:vault:{slug}).
    """
    job_id = "metadata:all" if vault_id is None else f"metadata:vault:{vault_id}"
    # include_system=False: blessed system-vault files are human-authored and must
    # never receive LLM-generated frontmatter (rag_indexer enforces this too).
    from src.rag_indexer import enumerate_vault_markdown
    vault_files = enumerate_vault_markdown(vault_id, include_system=False)
    total_files = sum(len(f) for _, _, f in vault_files)

    r = get_async_redis()
    scanned = 0
    enqueued = 0
    skipped = 0
    failed = 0

    try:
        for vid, wiki_root, md_files in vault_files:
          for rel_path in md_files:
            scanned += 1
            # generate_metadata_task takes an ABSOLUTE path (it reads and rewrites
            # the file); the enumeration yields vault-relative, so rebuild it here.
            abs_file = os.path.join(wiki_root, rel_path)

            # Skip excluded folders
            if _is_excluded(rel_path):
                skipped += 1
                continue

            # Read and check eligibility
            try:
                content = WikiDoc.read_text_at(abs_file)  # canonical LF-normalized read
            except Exception as e:
                print(f"generate_all_metadata_task: failed to read {rel_path}: {e}")
                failed += 1
                continue

            frontmatter = WikiDoc.parse_frontmatter(content)

            # Check index flag
            index_flag = frontmatter.get("index", str(INDEX_DOCUMENT_FRONTMATTER_DEFAULT))
            if index_flag.lower() in ("false", "no", "0"):
                skipped += 1
                continue

            # Check body length
            body = WikiDoc.strip_frontmatter(content)
            if len(body.strip()) < 50:
                skipped += 1
                continue

            # When not forcing, skip files that already have both summary and tags
            if not force:
                has_summary = bool(frontmatter.get("summary", "").strip())
                has_tags = bool(frontmatter.get("tags"))
                if has_summary and has_tags:
                    skipped += 1
                    continue

            # Pre-set extended debounce key so the file_watcher ignores the
            # frontmatter-write modification this metadata run triggers. The
            # metadata write only changes frontmatter, so even a leaked event
            # no-ops on the body-hash skip in ingest_document - this just
            # suppresses the wasted task.
            try:
                await r.set(watcher_debounce_key("update_document_task", vid, rel_path),
                            "1", ex=WRITE_DEBOUNCE_TTL)
            except Exception as e:
                print(f"generate_all_metadata_task: debounce set failed for {rel_path}: {e}")

            # Record in tracker BEFORE .kiq() to avoid race with worker pre_execute
            task_id = _make_task_id("metadata", f"{vid}:{rel_path}")
            await _record_enqueue(r, task_id, "generate_metadata_task")

            # Enqueue individual metadata generation
            try:
                await generate_metadata_task.kicker().with_task_id(task_id).kiq(
                    file_path=abs_file,
                    normalized_url_path=rel_path,
                    llm_url=LLM_URL,
                    llm_model=LLM_MODEL,
                    keep_alive=LLM_KEEP_ALIVE,
                    vault_id=vid,
                )
                enqueued += 1
            except Exception as e:
                print(f"generate_all_metadata_task: failed to enqueue {rel_path}: {e}")
                # Clean up the pending entry we just wrote
                await r.hdel(PENDING_KEY, task_id)
                failed += 1

            # Update progress after every file
            await r.set(
                f"{PROGRESS_PREFIX}:{job_id}",
                json.dumps({
                    "step": f"{scanned}/{total_files}",
                    "enqueued": enqueued,
                    "skipped": skipped,
                    "failed": failed,
                }),
                ex=RESULT_TTL,
            )
    finally:
        await r.close()

    return {"status": "ok", "total_files": total_files, "enqueued": enqueued, "skipped": skipped, "failed": failed}


@broker.task(
    task_name="reindex_all_task",
    retry_on_error=False,
)
async def reindex_all_task(vault_id: str | None = None) -> dict:
    """Reindex documents for RAG (force re-ingestion).

    vault_id=None reindexes every registered vault; a slug reindexes just that vault.
    The progress/job id mirrors this (reindex:all vs reindex:vault:{slug}).
    """
    job_id = "reindex:all" if vault_id is None else f"reindex:vault:{vault_id}"
    # (vault_id, wiki_root, [vault-relative md files]) for the target vault(s).
    # Shared with find_unindexed_documents so the reconcile can never disagree
    # with this task about which files exist; see enumerate_vault_markdown for
    # why system vaults are included.
    from src.rag_indexer import enumerate_vault_markdown
    vault_files = enumerate_vault_markdown(vault_id)
    total_files = sum(len(f) for _, _, f in vault_files)

    # Reconcile each vault's index against disk: drop rows for files deleted outside
    # the app. Guard against an empty glob (e.g. vault volume not mounted) so a
    # misconfiguration can never wipe a vault's index.
    pruned = {"ghosted": 0, "hard_deleted": 0}
    from src.rag_indexer import prune_deleted_documents
    for vid, _wiki_root, md_files in vault_files:
        if not md_files:
            print(f"reindex_all_task: vault {vid} glob found 0 files; skipping prune")
            continue
        existing_doc_ids = set(md_files)
        try:
            p = await prune_deleted_documents(existing_doc_ids, vid)
            pruned["ghosted"] += p["ghosted"]
            pruned["hard_deleted"] += p["hard_deleted"]
        except Exception as e:
            print(f"reindex_all_task: prune failed for vault {vid}: {e}")

    r = get_async_redis()
    scanned = 0
    enqueued = 0
    skipped = 0
    failed = 0

    try:
        for vid, _wiki_root, md_files in vault_files:
          for rel_path in md_files:
            scanned += 1

            # Skip excluded folders
            if _is_excluded(rel_path):
                skipped += 1
                continue

            # Pre-set extended debounce key so the file_watcher ignores the
            # frontmatter-write modification that reindexing this file triggers.
            try:
                await r.set(watcher_debounce_key("update_document_task", vid, rel_path),
                            "1", ex=WRITE_DEBOUNCE_TTL)
            except Exception as e:
                print(f"reindex_all_task: debounce set failed for {rel_path}: {e}")

            # Record in tracker BEFORE .kiq() to avoid race with worker pre_execute
            task_id = _make_task_id("reindex", f"{vid}:{rel_path}")
            await _record_enqueue(r, task_id, "index_document_task")

            # Enqueue index task with force=True. skip_frontmatter=True: reindex
            # rebuilds the RAG DB (chunks/embeddings/edges) only and does NOT
            # regenerate LLM tags/summary.
            try:
                await index_document_task.kicker().with_task_id(task_id).kiq(
                    file_path=rel_path, force=True, skip_frontmatter=True, vault_id=vid
                )
                enqueued += 1
            except Exception as e:
                print(f"reindex_all_task: failed to enqueue {rel_path}: {e}")
                await r.hdel(PENDING_KEY, task_id)
                failed += 1

            # Update progress after every file
            await r.set(
                f"{PROGRESS_PREFIX}:{job_id}",
                json.dumps({
                    "step": f"{scanned}/{total_files}",
                    "enqueued": enqueued,
                    "skipped": skipped,
                    "failed": failed,
                    "pruned": pruned["ghosted"],
                    "deleted": pruned["hard_deleted"],
                }),
                ex=RESULT_TTL,
            )
    finally:
        await r.close()

    return {
        "status": "ok",
        "total_files": total_files,
        "enqueued": enqueued,
        "skipped": skipped,
        "failed": failed,
        "pruned": pruned["ghosted"],
        "deleted": pruned["hard_deleted"],
    }


#######################################################
# RAG indexing tasks (triggered by file watcher)
#######################################################

GIT_DEBOUNCE_TTL = 10  # seconds


async def _git_debounced(file_path: str, vault_id: str = DEFAULT_VAULT) -> bool:
    """Check if a git commit was recently made for this file (by UI endpoint).

    Returns True if debounce key exists (skip git), False if clear (proceed). Key is
    vault-scoped to match what save_document sets (git:debounce:{vault}:{path}).
    """
    from src.wikidoc import WikiDoc
    r = get_async_redis()
    try:
        return bool(await r.exists(WikiDoc.debounce_key(vault_id, file_path)))
    finally:
        await r.close()


def _vault_tracker(vault_id: str):
    """Per-vault git tracker (worker side). Ensures the separated repo exists first so
    a plain `git init` never creates a .git dir on the Dropbox-synced tree."""
    from src.docversioning import MarkdownGitVersioning
    from src import vault_registry
    vault_registry.init_vault_repo(vault_id)
    return MarkdownGitVersioning(vault_root(vault_id))


async def _git_version_save(file_path: str, vault_id: str = DEFAULT_VAULT):
    """Create a git commit for a file if git versioning is enabled and not debounced.

    Args:
        file_path: vault-relative path (e.g. "subfolder/doc.md")
        vault_id: which vault's repo to commit to
    """
    if not USE_GIT_VERSIONING:
        return
    if await _git_debounced(file_path, vault_id):
        print(f"_git_version_save: skipping {file_path} (debounced)")
        return
    try:
        vt = _vault_tracker(vault_id)
        # save_version expects a path under the vault work tree (vaults/{vault}/...).
        versioned_path = os.path.join(vault_root(vault_id), file_path)
        await asyncio.to_thread(vt.save_version, versioned_path)
    except Exception as e:
        print(f"_git_version_save: git commit failed for {file_path}: {e}")


async def _git_version_remove(file_path: str, vault_id: str = DEFAULT_VAULT):
    """Create a git delete commit for a file if git versioning is enabled and not debounced."""
    if not USE_GIT_VERSIONING:
        return
    if await _git_debounced(file_path, vault_id):
        print(f"_git_version_remove: skipping {file_path} (debounced)")
        return
    try:
        vt = _vault_tracker(vault_id)
        versioned_path = os.path.join(vault_root(vault_id), file_path)
        await asyncio.to_thread(vt.remove_file, versioned_path)
    except Exception as e:
        print(f"_git_version_remove: git commit failed for {file_path}: {e}")


async def _git_version_move(src_path: str, dest_path: str, vault_id: str = DEFAULT_VAULT):
    """Create a git move commit if git versioning is enabled and not debounced."""
    if not USE_GIT_VERSIONING:
        return
    if await _git_debounced(dest_path, vault_id):
        print(f"_git_version_move: skipping {dest_path} (debounced)")
        return
    try:
        vt = _vault_tracker(vault_id)
        versioned_src = os.path.join(vault_root(vault_id), src_path)
        versioned_dest = os.path.join(vault_root(vault_id), dest_path)
        await asyncio.to_thread(vt.move_file, versioned_src, versioned_dest)
    except Exception as e:
        print(f"_git_version_move: git commit failed for {src_path} -> {dest_path}: {e}")


def _file_exists(rel_path: str, vault_id: str = DEFAULT_VAULT) -> bool:
    """Check if a vault file exists on disk."""
    abs_path = os.path.join(vault_root(vault_id), rel_path)
    return os.path.isfile(abs_path)


async def _stamp_agent_run(r, slug: str) -> None:
    """Mark a scheduled occurrence as having actually STARTED.

    Stamping moved here from the scheduler's enqueue path (2026-08-03). At
    enqueue time it meant "we asked for a run", so an occurrence that later
    deferred - or timed out on the run lock - advanced the schedule anyway and
    was silently skipped. Stamped on lock acquisition, `last run` means what it
    says, and a deferred agent stays due and retries next tick.
    """
    import datetime

    from src.agent_scheduler import set_last_run
    try:
        await set_last_run(r, slug, datetime.datetime.now())
    except Exception as e:
        print(f"_stamp_agent_run: failed for {slug}: {e}")


async def _is_cancelled(task_name: str, file_path: str, vault_id: str = DEFAULT_VAULT) -> bool:
    """Check if this task was cancelled by a contradicting file event.

    Uses atomic SREM: returns True (and cleans up) if the task ID was in the cancelled
    set, False otherwise. Vault-scoped to match the watcher's cancellation keys.
    """
    task_id = watcher_task_id(task_name, vault_id, file_path)
    r = get_async_redis()
    try:
        was_cancelled = await r.srem(CANCELLED_SET, task_id)
        return bool(was_cancelled)
    finally:
        await r.close()


@broker.task(
    task_name="index_document_task",
    retry_on_error=True,
    max_retries=3,
)
async def index_document_task(
    file_path: str, force: bool = False, skip_frontmatter: bool = False,
    vault_id: str = DEFAULT_VAULT,
) -> dict:
    """Index a newly created document for RAG.

    skip_frontmatter: rebuild the RAG DB only, leaving LLM frontmatter
    generation to the dedicated metadata actions (used by bulk reindex).
    """
    if await _is_cancelled("index_document_task", file_path, vault_id):
        print(f"index_document_task: cancelled for {file_path}")
        return {"status": "skipped", "reason": "cancelled by contradicting event"}
    if not _file_exists(file_path, vault_id):
        print(f"index_document_task: skipping {file_path} (file gone before execution)")
        return {"status": "skipped", "reason": "file gone before execution"}
    print(f"index_document_task: {file_path}")
    from src.rag_indexer import ingest_document
    result = await ingest_document(file_path, force=force, skip_frontmatter_gen=skip_frontmatter, vault_id=vault_id)
    if result.get("status") == "ok":
        await _git_version_save(file_path, vault_id)
    return result


@broker.task(
    task_name="update_document_task",
    retry_on_error=True,
    max_retries=3,
)
async def update_document_task(file_path: str, force: bool = False,
                               vault_id: str = DEFAULT_VAULT) -> dict:
    """Re-index a modified document for RAG."""
    if await _is_cancelled("update_document_task", file_path, vault_id):
        print(f"update_document_task: cancelled for {file_path}")
        return {"status": "skipped", "reason": "cancelled by contradicting event"}
    if not _file_exists(file_path, vault_id):
        print(f"update_document_task: skipping {file_path} (file gone before execution)")
        return {"status": "skipped", "reason": "file gone before execution"}
    print(f"update_document_task: {file_path}")
    from src.rag_indexer import ingest_document
    result = await ingest_document(file_path, force=force, vault_id=vault_id)
    if result.get("status") == "ok":
        await _git_version_save(file_path, vault_id)
    return result


@broker.task(
    task_name="generate_frontmatter_task",
    retry_on_error=True,
    max_retries=3,
)
async def generate_frontmatter_task(file_path: str, vault_id: str = DEFAULT_VAULT) -> dict:
    """Generate LLM tags/summary for a document, then spawn embedding."""
    if not _file_exists(file_path, vault_id):
        print(f"generate_frontmatter_task: skipping {file_path} (file gone before execution)")
        return {"status": "skipped", "reason": "file gone before execution"}
    print(f"generate_frontmatter_task: {file_path}")
    from src.rag_indexer import generate_frontmatter
    return await generate_frontmatter(file_path, vault_id)


@broker.task(
    task_name="embed_document_task",
    retry_on_error=True,
    max_retries=3,
)
async def embed_document_task(file_path: str, vault_id: str = DEFAULT_VAULT) -> dict:
    """Chunk, embed, and write a document to the RAG database."""
    if not _file_exists(file_path, vault_id):
        print(f"embed_document_task: skipping {file_path} (file gone before execution)")
        return {"status": "skipped", "reason": "file gone before execution"}
    print(f"embed_document_task: {file_path}")
    from src.rag_indexer import embed_document
    return await embed_document(file_path, vault_id)


@broker.task(
    task_name="remove_document_task",
    retry_on_error=True,
    max_retries=3,
)
async def remove_document_task(file_path: str, vault_id: str = DEFAULT_VAULT) -> dict:
    """Remove a deleted document from the RAG index."""
    if await _is_cancelled("remove_document_task", file_path, vault_id):
        print(f"remove_document_task: cancelled for {file_path}")
        return {"status": "skipped", "reason": "cancelled by contradicting event"}
    from src.rag_indexer import remove_document
    result = await remove_document(file_path, vault_id)
    await _git_version_remove(file_path, vault_id)
    return result


@broker.task(
    task_name="move_document_task",
    retry_on_error=True,
    max_retries=3,
)
async def move_document_task(src_path: str, dest_path: str,
                             vault_id: str = DEFAULT_VAULT) -> dict:
    """Update the RAG index for a moved/renamed document."""
    if await _is_cancelled("move_document_task", dest_path, vault_id):
        print(f"move_document_task: cancelled for {src_path} -> {dest_path}")
        return {"status": "skipped", "reason": "cancelled by contradicting event"}
    if not _file_exists(dest_path, vault_id):
        print(f"move_document_task: skipping {src_path} -> {dest_path} (dest gone before execution)")
        return {"status": "skipped", "reason": "dest file gone before execution"}
    from src.rag_indexer import move_document
    result = await move_document(src_path, dest_path, vault_id)
    await _git_version_move(src_path, dest_path, vault_id)
    return result


@broker.task(task_name="reembed_all_task", retry_on_error=False)
async def reembed_all_task() -> dict:
    """Re-embed all documents with NULL embeddings. Manually triggerable."""
    from src.embedding_config import get_docs_needing_embedding, _get_pg_connection
    conn = _get_pg_connection()
    try:
        doc_ids = get_docs_needing_embedding(conn)
    finally:
        conn.close()
    r = get_async_redis()
    try:
        for vault_id, doc_id in doc_ids:
            task_id = f"reembed:{vault_id}:{doc_id}"
            await _record_enqueue(r, task_id, "embed_document_task")
            await embed_document_task.kicker().with_task_id(task_id).kiq(
                file_path=doc_id, vault_id=vault_id)
    finally:
        await r.close()
    return {"enqueued": len(doc_ids)}


@broker.task(task_name="update_commit_graph_task", retry_on_error=False)
async def update_commit_graph_task() -> dict:
    """Regenerate the git commit-graph file for each vault's repo (faster history)."""
    if not USE_GIT_VERSIONING:
        return {"status": "skipped", "reason": "git versioning disabled"}
    from src import vault_registry
    updated = 0
    # include_system: the system vault has a real git repo that benefits from
    # commit-graph maintenance even though it is excluded from content loops.
    for v in vault_registry.list_vaults(include_system=True):
        try:
            vt = _vault_tracker(v["vault_id"])
            await asyncio.to_thread(vt._update_commit_graph)
            updated += 1
        except Exception as e:
            print(f"update_commit_graph_task: failed for {v['vault_id']}: {e}")
    return {"status": "ok", "vaults": updated}


#######################################################
# File watcher startup/shutdown hooks
#######################################################

_file_observer = None
_agent_api_task = None
_agent_scheduler_task = None
_maintenance_task = None


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def _start_file_watcher(state):
    print("Starting worker")
    global _file_observer, _agent_api_task
    loop = asyncio.get_running_loop()

    # Schema reconcile runs HERE too, not just in the server: compose starts the
    # worker first (tzaraserver depends_on tzaraworker), so on an upgraded
    # install the watcher could write a timestamp into a still-naive column
    # before the server ever connects. Serialized with the server's pass by an
    # advisory lock, and a no-op once the schema is current.
    from src.schema_upgrade import reconcile_schema
    await asyncio.to_thread(reconcile_schema)

    # Reap IN_PROGRESS rows left by the PREVIOUS worker life, before the scheduler
    # or the event dispatcher can read one as a live run and coalesce against it
    # forever. Nothing else ever clears them: post_execute is the only other
    # remover and it cannot run for a task the worker died mid-way through.
    from src.task_tracker import clear_stale_in_progress
    _r = get_async_redis()
    try:
        _stale = await clear_stale_in_progress(_r)
        if _stale:
            print(f"cleared {_stale} stale in-progress task(s) from a previous worker")
    except Exception as e:
        print(f"failed to clear stale in-progress tasks: {e}")
    finally:
        await _r.close()

    # The agent-API: the token-gated callback surface for the agent kernel's
    # `wiki` proxy. Lives in this process (its only client is a kernel running
    # a tool call THIS worker initiated); reachable only over agent-net.
    from src import agent_api
    _agent_api_task = asyncio.create_task(agent_api.serve())

    # The agent scheduler: fires `schedule:`-carrying agents when due and GCs
    # stale staged batches. Rescans the agent files each tick (reconcile=rescan).
    global _agent_scheduler_task, _maintenance_task
    from src import agent_scheduler
    _agent_scheduler_task = asyncio.create_task(agent_scheduler.scheduler_loop())
    # Housekeeping runs whether or not agents are scheduled: the failure drain, its
    # 30-day prune, the hourly disk-vs-index reconcile, and staged-batch GC. Kept
    # OUT of scheduler_loop, which returns early on AGENT_SCHEDULER_ENABLED=false -
    # a monitoring feature that stops recording when you disable something
    # unrelated is worse than no monitoring.
    _maintenance_task = asyncio.create_task(agent_scheduler.maintenance_loop())
    # Guarantee the default vault exists on disk (dir + separated git repo) before the
    # watcher starts, so indexing of the default vault always has somewhere to commit.
    # The system vault must also exist (and its system flag be registered) before the
    # first event so the watcher's is_system_vault skip is authoritative immediately.
    from src import vault_registry
    vault_registry.ensure_default_vault()
    vault_registry.ensure_system_vault()
    # Materialize `.tzara/config.json` (authoritative metadata) for any vault missing it,
    # from the DB cache -- idempotent, mirrors the server's startup reconcile.
    vault_registry.reconcile_vault_configs()
    from src.file_watcher import start_watcher
    from src.task_broker import REDIS_URL
    _file_observer = start_watcher(loop, REDIS_URL)

    # Check embedding model configuration against DB
    from src.embedding_config import check_and_migrate_embedding_config
    try:
        result = await check_and_migrate_embedding_config()
        print(f"Embedding config: {result['status']}")
        if result["status"] in ("migrated", "partial_reindex"):
            doc_ids = result.get("doc_ids", [])
            print(f"Queuing re-embed for {len(doc_ids)} documents")
            r = get_async_redis()
            try:
                for vault_id, doc_id in doc_ids:
                    task_id = f"startup-reembed:{vault_id}:{doc_id}"
                    await _record_enqueue(r, task_id, "embed_document_task")
                    await embed_document_task.kicker().with_task_id(task_id).kiq(
                        file_path=doc_id, vault_id=vault_id)
            finally:
                await r.close()
    except Exception as e:
        print(f"WARNING: Embedding config check failed: {e}")


@broker.on_event(TaskiqEvents.WORKER_SHUTDOWN)
async def _stop_file_watcher(state):
    print("Shutdown worker")
    global _file_observer, _agent_api_task
    if _file_observer is not None:
        from src.file_watcher import stop_watcher
        stop_watcher(_file_observer)
        _file_observer = None
    if _agent_api_task is not None:
        from src import agent_api
        agent_api.shutdown()
        try:
            await asyncio.wait_for(_agent_api_task, timeout=5)
        except Exception:
            _agent_api_task.cancel()
        _agent_api_task = None
    global _agent_scheduler_task, _maintenance_task
    if _agent_scheduler_task is not None:
        _agent_scheduler_task.cancel()
        _agent_scheduler_task = None
    if _maintenance_task is not None:
        _maintenance_task.cancel()
        _maintenance_task = None
