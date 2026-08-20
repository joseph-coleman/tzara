# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generic agent loop engine - the reusable core shared by interactive chat
and headless background agents.

This module owns ONLY engine concerns: call the LLM with tools, parse/validate
tool calls, execute them, feed results back, and repeat until the model stops,
an approval-gated tool is hit, or the step budget runs out.

It is deliberately free of any surface-specific code (no SSE formatting, no
ChatSession, no scratchpad). Callers drive it as an async generator that yields
structured event dicts and supply an ``execute_tool`` callable plus small
profile hooks (status labels, narration, approval policy). The interactive chat
loop translates the yielded events into SSE; a background agent just consumes
(or logs) them and reads the final ``AgentRunResult``.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from src.compaction import log_history_stats

logger = logging.getLogger("agent_runner")


@asynccontextmanager
async def _null_cm():
    """No-op async context manager (used when no per-call LLM gate is supplied)."""
    yield

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_AGENT_ITERATIONS = 10

# How many times to re-request a single step that ended on a malformed-stream error
# (Ollama could not parse a tool call) before giving up and surfacing it as a failure.
MAX_STREAM_ERROR_RETRIES = 2
STREAM_PREFIX_BUFFER_SIZE = 200  # chars to buffer before streaming for garbage detection


# ---------------------------------------------------------------------------
# Stream prefix filter
# ---------------------------------------------------------------------------

class StreamPrefixFilter:
    """Buffer first N chars of a stream for garbage detection before forwarding.

    ``markers`` are substrings that, if found in the buffered prefix, indicate
    the model is regurgitating the system prompt; such streams are suppressed.
    """

    def __init__(self, buffer_size: int = STREAM_PREFIX_BUFFER_SIZE, markers=()):
        self._buffer = ""
        self._buffer_size = buffer_size
        self._markers = tuple(markers)
        self._flushed = False
        self._suppressed = False

    def feed(self, token: str) -> list[str]:
        """Feed a token. Returns list of tokens to emit (may be empty while buffering)."""
        if self._suppressed:
            return []
        if not self._flushed:
            self._buffer += token
            if len(self._buffer) >= self._buffer_size:
                if self._is_garbage(self._buffer):
                    self._suppressed = True
                    return []
                self._flushed = True
                return [self._buffer]
            return []
        return [token]

    def flush_remaining(self) -> list[str]:
        """Call after stream ends. Returns any unflushed buffer content."""
        if self._suppressed:
            return []
        if not self._flushed and self._buffer:
            if self._is_garbage(self._buffer):
                self._suppressed = True
                return []
            self._flushed = True
            return [self._buffer]
        return []

    @property
    def is_suppressed(self) -> bool:
        return self._suppressed

    def _is_garbage(self, text: str) -> bool:
        for marker in self._markers:
            if marker in text:
                return True
        return False


# ---------------------------------------------------------------------------
# Tool-call normalization helpers (pure)
# ---------------------------------------------------------------------------

def _tool_call_name(tc) -> str:
    """Extract function name from an Ollama tool call object or dict."""
    func = getattr(tc, "function", None) or (tc.get("function", {}) if hasattr(tc, "get") else {})
    return getattr(func, "name", "") or (func.get("name", "") if hasattr(func, "get") else "")


def _normalize_tool_call(tc, index: int = 0) -> dict:
    """Convert Ollama ToolCall object or dict to a serializable dict."""
    func = getattr(tc, "function", None) or (tc.get("function", {}) if hasattr(tc, "get") else {})
    name = getattr(func, "name", "") or (func.get("name", "") if hasattr(func, "get") else "")
    args = getattr(func, "arguments", None) or (func.get("arguments", {}) if hasattr(func, "get") else {})
    return {
        "type": "function",
        "function": {"index": index, "name": name, "arguments": dict(args) if args else {}},
    }


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class AgentRunResult:
    """Outcome of a run_agent_loop pass, read by the caller after iteration."""
    final_text: str = ""
    already_streamed: bool = False
    any_tools_executed: bool = False
    reached_max_steps: bool = False
    cancelled: bool = False               # cooperative cancel tripped between steps
    stream_error: str | None = None       # last unrecovered malformed-stream error, if the run ended on one
    pending_approval: dict | None = None  # {"name": str, "args": dict} when a gated tool was hit
    activity_log: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

async def run_agent_loop(
    *,
    messages: list,
    system_prompt: str,
    tool_defs: list,
    tool_names: set,
    llm_mgr,
    execute_tool,                      # async (name, args, status_callback) -> str
    status_label=lambda name: f"Executing {name}...",
    activity_narration=lambda name, args: name,
    needs_approval=frozenset(),        # tool names that pause (return pending_approval) instead of executing
    approval_narration=lambda name, args: f"Proposed {name}",
    parse_tool_call_from_text=None,    # optional callable(text, tool_names) -> {"name","arguments"} | None
    stream_markers=(),
    llm_gate=None,                     # optional zero-arg callable -> async CM, entered around each LLM call
    cancel_check=None,                 # optional async () -> bool; True at a step boundary stops the run
    yield_check=None,                  # optional async () -> bool; True at a step boundary PAUSES the run
    yield_poll_s: float = 5.0,         # how often to re-check while paused
    yield_max_s: float = 300.0,        # give up standing aside after this long
    max_iterations: int = MAX_AGENT_ITERATIONS,
    think: bool = True,
    log_label: str = "",
):
    """Run the iterative tool-calling loop, yielding structured event dicts.

    Mutates ``messages`` in place (appends assistant/tool turns). The terminal
    event is ``{"type": "result", "result": AgentRunResult}``; the caller reads
    that to drive change-detection, approval prompts, compaction, etc.

    Event types yielded: ``status``, ``token``, ``retract``, ``activity``,
    ``result``.
    """
    result = AgentRunResult()

    # Per-RUN yield budget: see the yield block below for why this must not live
    # inside the loop. `announced_pause` keeps the status message to once per run
    # rather than once per step boundary.
    yielded_total = 0.0
    announced_pause = False

    for iteration in range(max_iterations):
        # Cooperative cancel: checked at each step boundary (between LLM turns),
        # so it stops a run grinding through steps - but NOT one wedged inside a
        # single generation (that needs the per-run wall clock / an asyncio cancel).
        if cancel_check is not None and await cancel_check():
            result.cancelled = True
            result.final_text = "(Run cancelled.)"
            yield {"type": "result", "result": result}
            return

        # Stand aside for a person, at the same step boundary as the cancel check.
        # BETWEEN turns, never during one: the LLM server is serial and a turn in
        # flight cannot be preempted, so this bounds how long a human waits to at
        # most one agent turn instead of a whole run.
        #
        # The budget is PER RUN (yielded_total lives outside this loop), not per
        # step. Per-step it multiplies by max_iterations, and an agent declaring
        # `max_iterations: 30` would then yield up to 30 x 300s = 150 min while
        # holding the GLOBAL agent run lock, whose TTL is 120 min - the lock would
        # expire mid-run and let a second mutating run start, which is exactly the
        # single-writer invariant it exists to enforce. A run's total delay must
        # fit inside that lock, and only a per-run cap makes that true by
        # construction rather than by arithmetic on a value set in a markdown file.
        if yield_check is not None:
            while yielded_total < yield_max_s and await yield_check():
                if cancel_check is not None and await cancel_check():
                    result.cancelled = True
                    result.final_text = "(Run cancelled.)"
                    yield {"type": "result", "result": result}
                    return
                if not announced_pause:
                    yield {"type": "status", "text": "Pausing while you're working…"}
                    announced_pause = True
                await asyncio.sleep(yield_poll_s)
                yielded_total += yield_poll_s

        yield {"type": "status", "text": f"Thinking... (step {iteration + 1})"}

        # Stream response with tools - tokens arrive live, tool_calls on final chunk.
        # An ABNORMAL stream exit (Ollama aborts on a tool call it can't parse; a
        # mid-stream network error also lands here) is retried in this INNER loop, so
        # a retry is the same step re-asked and does NOT consume the outer step budget.
        # Bounded so a model that reliably emits garbage can't spin forever.
        stream_attempt = 0
        while True:
            prefix_filter = StreamPrefixFilter(markers=stream_markers)
            accumulated_text = ""
            final_tool_calls = None
            any_tokens_streamed = False
            stream_error = None

            # Optional per-call gate: held only for the duration of THIS LLM stream
            # (released before tool execution), so a background caller interleaves
            # fairly with other gated work instead of holding a slot for the whole loop.
            stream_cm = llm_gate() if llm_gate is not None else _null_cm()
            # Hold a reference so we can deterministically aclose() the stream on early
            # break (we break on chunk.done) rather than leaving it for GC, which can
            # race into "aclose(): asynchronous generator is already running" at loop
            # teardown and leak suspended streams across runs in the worker.
            stream = llm_mgr.chat_stream_with_tools(
                messages, tool_defs, system=system_prompt, think=think
            )
            try:
                async with stream_cm:
                    async for chunk in stream:
                        accumulated_text += chunk.token
                        if chunk.done:
                            final_tool_calls = chunk.tool_calls
                            stream_error = chunk.error
                            for t in prefix_filter.flush_remaining():
                                yield {"type": "token", "text": t}
                                any_tokens_streamed = True
                            break
                        for t in prefix_filter.feed(chunk.token):
                            yield {"type": "token", "text": t}
                            any_tokens_streamed = True
            except Exception as e:
                # Any streaming failure must route through stream_error - NOT just the
                # ResponseError that chat_stream_with_tools converts to chunk.error.
                # Otherwise the empty turn below is silently misread as "the model
                # chose to stop" (the original silent-truncation bug, general case).
                logger.warning("Error during LLM streaming (iteration %d): %s", iteration, e)
                final_tool_calls = None
                stream_error = f"stream failed: {e!r}"
            finally:
                try:
                    await stream.aclose()
                except Exception:
                    pass

            # Retry only when the stream ended abnormally AND left nothing usable; a
            # stream_error that still carried tool_calls is trusted as-is. Re-sending
            # the identical request usually succeeds because sampling is nondeterministic.
            if stream_error is not None and not final_tool_calls:
                stream_attempt += 1
                if stream_attempt <= MAX_STREAM_ERROR_RETRIES:
                    logger.warning(
                        "Retrying step %d stream (attempt %d/%d) after abnormal exit: %s",
                        iteration + 1, stream_attempt, MAX_STREAM_ERROR_RETRIES, stream_error,
                    )
                    if any_tokens_streamed:
                        yield {"type": "retract"}
                    yield {"type": "status", "text": "Recovering from a malformed model response…"}
                    continue
            break

        text_content = accumulated_text
        tool_calls = final_tool_calls

        # Validate structured tool call names against allowed set
        if tool_calls:
            valid = [tc for tc in tool_calls if _tool_call_name(tc) in tool_names]
            if len(valid) < len(tool_calls):
                dropped = [_tool_call_name(tc) for tc in tool_calls if _tool_call_name(tc) not in tool_names]
                logger.warning("Dropping unrecognized tool calls: %s", dropped)
            tool_calls = valid or None

        # Fallback: try to parse tool call from text
        parsed_from_text = None
        if not tool_calls and text_content and parse_tool_call_from_text is not None:
            parsed_from_text = parse_tool_call_from_text(text_content, tool_names)

        if not tool_calls and not parsed_from_text:
            if stream_error is not None:
                # Exhausted the stream retries above with nothing usable: surface a
                # FAILURE, not a clean finish, so callers can tell "the model stopped"
                # from "the stream kept breaking". The salvaged reasoning text (if any)
                # is preserved verbatim in stream_error.
                logger.error("Giving up after %d stream retries: %s", stream_attempt, stream_error)
                result.stream_error = stream_error
                result.final_text = "(The model returned a malformed response and the run could not continue.)"
                yield {"type": "result", "result": result}
                return
            # Model is done - text was already streamed (if prefix was clean)
            result.final_text = text_content
            if any_tokens_streamed and not prefix_filter.is_suppressed:
                result.already_streamed = True
            yield {"type": "result", "result": result}
            return

        # Tool call path - retract any accidentally streamed tokens
        if any_tokens_streamed:
            yield {"type": "retract"}

        # Build list of tool calls to process
        if parsed_from_text:
            calls_to_process = [{"name": parsed_from_text["name"], "arguments": parsed_from_text["arguments"]}]
            tc_for_history = [{
                "type": "function",
                "function": {"index": 0, "name": parsed_from_text["name"], "arguments": parsed_from_text["arguments"]},
            }]
            text_content = ""
        else:
            tc_for_history = [_normalize_tool_call(tc, index=i) for i, tc in enumerate(tool_calls)]
            calls_to_process = [{"name": n["function"]["name"], "arguments": n["function"]["arguments"]} for n in tc_for_history]

        # Append assistant message once before tool results
        assistant_msg = {"role": "assistant", "content": text_content or ""}
        assistant_msg["tool_calls"] = tc_for_history
        messages.append(assistant_msg)

        # Execute each tool call and append results
        result.any_tools_executed = True
        approval_hit = False
        for ci, call in enumerate(calls_to_process):
            func_name = call["name"]
            func_args = call["arguments"]

            # Approval-gated tool: don't execute here. Stash the call, pair every
            # remaining tool_call in this batch with a placeholder result so the
            # chat template stays valid, and break to let the caller propose it.
            if func_name in needs_approval:
                result.pending_approval = {"name": func_name, "args": func_args}
                messages.append({
                    "role": "tool", "tool_name": func_name,
                    "content": "Code execution is pending user approval.",
                })
                for skipped in calls_to_process[ci + 1:]:
                    messages.append({
                        "role": "tool", "tool_name": skipped["name"],
                        "content": "Not executed - pending approval of the proposed code.",
                    })
                narration = approval_narration(func_name, func_args)
                yield {"type": "activity", "text": narration}
                result.activity_log.append(narration)
                approval_hit = True
                break

            # Emit status event
            yield {"type": "status", "text": status_label(func_name)}

            # Execute tool with real-time status draining via queue
            status_queue = asyncio.Queue()

            async def emit_status(text):
                await status_queue.put(text)

            tool_task = asyncio.create_task(execute_tool(func_name, func_args, emit_status))

            # Drain status events in real-time while tool runs
            while not tool_task.done():
                try:
                    status_text = await asyncio.wait_for(status_queue.get(), timeout=0.1)
                    yield {"type": "status", "text": status_text}
                except asyncio.TimeoutError:
                    continue

            tool_result = await tool_task

            # Drain any remaining status events
            while not status_queue.empty():
                yield {"type": "status", "text": status_queue.get_nowait()}

            # Emit activity narration and track in activity log
            narration = activity_narration(func_name, func_args)
            if "not found" in tool_result:
                for verb in ("Edited", "Read", "Deleted"):
                    narration = narration.replace(verb, f"Failed to {verb.lower()}")
            yield {"type": "activity", "text": narration}
            result.activity_log.append(narration)

            # Append tool result with tool_name for multi-turn
            messages.append({"role": "tool", "tool_name": func_name, "content": tool_result})

        log_history_stats(f"{log_label}tool-step {iteration + 1}", messages, system_prompt)

        # The agent proposed an approval-gated call; pause and let the caller propose it.
        if approval_hit:
            yield {"type": "result", "result": result}
            return

    else:
        # Max iterations reached - the model still wanted to call tools when the
        # budget ran out. Flag for the caller's resume affordance.
        result.reached_max_steps = True
        result.final_text = "(Reached maximum steps.)"

    yield {"type": "result", "result": result}
