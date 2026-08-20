# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Background-agents framework: headless agents that drive the shared engine
(src.agent_runner.run_agent_loop) on a directive + tool set, with no human in
the loop.

A background agent is data - `{directive, tool_defs, tool_names, execute, sink}`.
Definitions live as markdown files in the system vault and are loaded/built by
src.agent_registry (the hardcoded Vault Gardener literal that used to live here
is now `vaults/{SYSTEM_VAULT}/agents/vault-gardener.md`). The engine and this
runner stay agent-agnostic.

Output ownership: agents write ONLY into their owned area,
`{AGENT_OUTPUT_DIR}/{agent}/...` inside the target vault (see
write_agent_output). That location is RAG-excluded by path and banner-marked in
the UI; the write commits itself to git because the watcher deliberately
ignores the whole area. Every run also appends a run-log page under
`{AGENT_OUTPUT_DIR}/{agent}/logs/`.

Concurrency: this runs in the taskiq worker process. Because agents are
*multi-step* loops (not a single generate/embed), each LLM call is gated
individually (run_agent_loop's ``llm_gate``) so a run interleaves fairly with
other background work instead of holding the single background slot throughout.
Interactive chat (separate web process) is deliberately ungated. See
src/llm_gate.py.
"""

import asyncio
import json
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from config import (
    AGENT_LEDGER_CONTEXT_FRACTION,
    AGENT_LEDGER_MAX_CHARS,
    AGENT_LEDGER_MAX_COUNT,
    AGENT_LEDGER_MAX_ITEMS,
    AGENT_LEDGER_TURN_TIMEOUT_S,
    AGENT_LEDGER_TURN_TRANSCRIPT_CHARS,
    AGENT_LEDGERS_FILE,
    AGENT_MEMORY_FILE,
    AGENT_MEMORY_INJECT_CHARS,
    AGENT_MEMORY_CONTEXT_FRACTION,
    AGENT_MEMORY_MAX_CHARS,
    AGENT_MEMORY_MIN_CHARS,
    AGENT_OUTPUT_COLLAPSE_FLOOR,
    AGENT_OUTPUT_COLLAPSE_RATIO,
    CHARS_PER_TOKEN,
    LLM_AGENT_NO_PROMPT_CACHE,
    AGENT_YIELD_MAX_S,
    AGENT_YIELD_POLL_S,
    AGENT_YIELD_TO_HUMAN,
    AGENT_MEMORY_TURN_TIMEOUT_S,
    AGENT_OUTPUT_DIR,
    AGENT_RUN_TIMEOUT_S,
    AGENT_TOOL_THINK,
    LLM_CONTEXT_BUDGET,
    LLM_KEEP_ALIVE,
    LLM_MODEL,
    LLM_NUM_CTX,
    LLM_URL,
)
from src.agent_capabilities import uses_ledgers
from src.agent_runner import MAX_AGENT_ITERATIONS, AgentRunResult, run_agent_loop
from src.context_providers import (
    ContextProvider,
    DirectiveProvider,
    LedgerProvider,
    MemoryProvider,
    assemble_system_prompt,
)
from src.llm_manager import NativeLLMManager
from src import llm_backend
from src.llm_backend import create_llm_backend
from src import timefmt

logger = logging.getLogger("background_agents")


class AgentCancelled(Exception):
    """A run was stopped by an external cancel request (not a failure). Kept
    distinct from ordinary exceptions so the log page, the fan-out accounting,
    and the activity surface can show "cancelled" rather than "failed"."""


class AgentOutputSuspect(Exception):
    """The run finished cleanly but its final message failed the output-collapse
    guard, so nothing durable was written.

    Rides the ordinary failure path (no output write, no reserved memory/ledger
    turns, recorded to the durable failure log behind /manage/monitor) because
    every one of those behaviors is already what a suspect run wants. It stays a
    DISTINCT type for the same reason AgentCancelled does: an agent that keeps
    run logs renders it as "suspect" with the rejected text intact, rather than
    as a traceback. An agent with `log:` off gets no page - its diagnosis is the
    excerpt carried in this exception's message."""


# ---------------------------------------------------------------------------
# Worker-side LLM manager
# ---------------------------------------------------------------------------

def make_worker_llm() -> NativeLLMManager:
    """Build a worker-side LLM manager from config (the worker has none today).

    Per-call concurrency gating is applied by the engine (run_agent_loop's
    ``llm_gate`` param), not by a manager subclass - see run_background_agent.

    Returns the configured LLM backend (LLM_PROVIDER); all backends subclass
    NativeLLMManager, so the annotation and every downstream call site hold."""
    mgr = create_llm_backend(url=LLM_URL, model=LLM_MODEL,
                             keep_alive=LLM_KEEP_ALIVE, num_ctx_request=LLM_NUM_CTX,
                             context_budget=LLM_CONTEXT_BUDGET)
    # Scoped to the worker on purpose: this is where a stale server-side KV slot
    # costs a whole unattended run, and where the reprocess latency is free. The
    # ollama-native fallback has no /v1 body to carry the field; setattr is
    # harmless there.
    mgr.no_prompt_cache = LLM_AGENT_NO_PROMPT_CACHE
    return mgr


# ---------------------------------------------------------------------------
# Background agent definition
# ---------------------------------------------------------------------------

# execute(name, args, vault_id, status_callback) -> result string
ExecuteTool = Callable[[str, dict, str, object], Awaitable[str]]
# async sink(vault_id, body) -> vault-relative path written
Sink = Callable[[str, str], Awaitable[str]]


@dataclass
class BackgroundAgent:
    name: str
    # Owned-area prefix "agents/{slug}" (mirrors editors' "editors/{slug}"); used for the
    # _dada path, token claim + staging attribution. `name` stays the bare identity slug
    # (def lookup, job_id, events, logging). Required - only build_background_agent builds this.
    owner: str
    directive: str          # system-prompt instruction (the goal + output format)
    kickoff: str            # the seed user turn that starts the loop
    tools_text: str         # human-readable tool list for the prompt
    tool_defs: list         # native tool schemas passed to the model
    tool_names: set         # allowed tool-name set
    execute: ExecuteTool    # async dispatcher bound per-vault at run time
    sink: Sink              # where the final text goes (async, commits itself)
    max_iterations: int | None = None   # None = engine default
    def_hash: str = ""      # git short-hash of the definition file at build time
    py_source: str = ""     # human-authored custom tool code (runs in the agent kernel)
    custom_tool_names: set = field(default_factory=set)
    mode: str = "propose"   # autonomy ceiling from the blessed file (write_gate.gated_write)
    log: bool = False       # opt-in (`log:` frontmatter): write a per-run log page
    output_rel: str = ""    # vault-relative path of the persistent output page (human-facing report)
    memory: bool = False    # opt-in (`memory:` frontmatter): reserved memory turn + injection
    memory_prompt: str = ""  # `# Memory Prompt` body; "" = the shared default
    memory_rel: str = ""    # vault-relative path of the cross-run memory page (_dada/{slug}/memory.md)


def _build_tools_text(tool_defs: list) -> str:
    """Human-readable tool list for the system prompt, parameters included -
    belt-and-suspenders for small local models (the native tool_defs already
    carry the schemas). Kept terse: one sub-bullet per parameter."""
    lines = ["## Available Tools"]
    for tc in tool_defs:
        fn = tc["function"]
        lines.append(f"- **{fn['name']}**: {fn['description']}")
        params = (fn.get("parameters") or {}).get("properties") or {}
        required = set((fn.get("parameters") or {}).get("required") or [])
        for pname, spec in params.items():
            kind = spec.get("type", "string")
            if pname in required:
                kind += ", required"
            elif "default" in spec:
                kind += f", default {spec['default']!r}"
            desc = spec.get("description", "")
            lines.append(f"    - {pname} ({kind}): {desc}")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Activity narration (what a tool call DID, for debuggable run logs)
# ---------------------------------------------------------------------------

def _summarize_arg(value, limit: int = 60) -> str:
    """One-line, length-capped rendering of a single tool-argument value.

    Whitespace/newlines collapse to single spaces so a page-length `content`
    arg becomes a readable snippet instead of a multi-line dump."""
    if isinstance(value, str):
        text = " ".join(value.split())
        return f"'{text[:limit].rstrip()}…'" if len(text) > limit else f"'{text}'"
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text[:limit].rstrip() + "…" if len(text) > limit else text


def _narrate_tool_call(name: str, args: dict) -> str:
    """Activity-log line for one tool call: the tool name PLUS a compact,
    length-capped rendering of its arguments. Names alone were undebuggable -
    you couldn't tell what an agent searched for or tried to create; this makes
    the per-run log enough to reconstruct the function-calling."""
    if not args:
        return f"Ran {name}()"
    inner = ", ".join(f"{k}={_summarize_arg(v)}" for k, v in args.items())
    return f"Ran {name}({inner})"


# ---------------------------------------------------------------------------
# Owned-output writes (location IS the ownership fact)
# ---------------------------------------------------------------------------

async def write_agent_output(vault_id: str, agent_slug: str, rel_path: str,
                             body: str, title: str | None = None,
                             index: bool = False) -> str:
    """Write a page into the agent's owned area and git-commit it.

    Path: `{AGENT_OUTPUT_DIR}/{agent_slug}/{rel_path}` under the target vault.
    Everything under AGENT_OUTPUT_DIR is RAG-excluded by location (watcher
    ignore + indexer guard) and banner-marked in the document view - that
    location is the whole ownership mechanism, so this writer must commit
    itself (the watcher never will). The `origin:` frontmatter is a
    non-load-bearing provenance breadcrumb only.

    Returns the vault-relative path written.
    """
    rel = f"{AGENT_OUTPUT_DIR}/{agent_slug}/{rel_path.lstrip('/')}"
    generated = timefmt.iso_local()
    front = (
        "---\n"
        f"title: {title or os.path.splitext(os.path.basename(rel_path))[0]}\n"
        f"origin: {agent_slug}\n"
        f"generated: {generated}\n"
        "---\n\n"
    )
    # Canonical write (DEFAULT_ENCODING, xmlcharrefreplace, traversal-checked,
    # makedirs). Physical-only: the git commit is the separate _git_version_save
    # below, kept as-is until Phase 2 folds this owned-output path onto commit().
    from src.wikidoc import WikiDoc
    WikiDoc.write_text(vault_id, rel, front + body + "\n")

    # The watcher ignores AGENT_OUTPUT_DIR entirely, so this write is the sole
    # commit source. Lazy import: task_definitions imports this module's runner.
    from src.task_definitions import _git_version_save
    await _git_version_save(rel, vault_id)

    # Opt-in RAG indexing : the grant lives in the BLESSED agent file
    # (index_output frontmatter), never on the output page itself. The watcher
    # still ignores _dada, so this direct ingest is the sole indexing path -
    # the agent's overwrite-and-reindex each run is the source of truth.
    if index:
        try:
            from src.rag_indexer import ingest_document
            await ingest_document(rel, force=True, skip_frontmatter_gen=True,
                                  vault_id=vault_id, allow_excluded=True)
        except Exception:
            logger.exception("opt-in index failed for %s:%s", vault_id, rel)
    return rel


async def write_agent_attachment(vault_id: str, agent_slug: str, rel_path: str,
                                 data: bytes) -> str:
    """Write a binary attachment (chart PNG, data file, …) into the agent's
    owned area and git-commit it.

    The extension must be in ATTACHMENT_FILE_TYPES - the same allowlist the
    serving route uses, so anything written here is immediately servable next
    to the agent's pages (e.g. `![](chart.png)` in the output page). No
    frontmatter, no RAG: attachments must NOT go through write_agent_output,
    which prepends a markdown frontmatter header.

    Returns the vault-relative path written.
    """
    from config import ATTACHMENT_FILE_TYPES
    ext = os.path.splitext(rel_path)[1].lstrip(".").lower()
    if ext not in ATTACHMENT_FILE_TYPES:
        raise ValueError(
            f"attachment extension '.{ext}' is not allowed "
            f"(allowed: {', '.join(sorted(ATTACHMENT_FILE_TYPES))})")
    rel = f"{AGENT_OUTPUT_DIR}/{agent_slug}/{rel_path.lstrip('/')}"
    # Canonical binary write: traversal-checked (safe_rel) - the agent supplies
    # rel_path, so this path MUST be validated, not just extension-checked.
    from src.wikidoc import WikiDoc
    WikiDoc.write_bytes(vault_id, rel, data)

    from src.task_definitions import _git_version_save
    await _git_version_save(rel, vault_id)
    return rel


# ---------------------------------------------------------------------------
# Cross-run memory (opt-in `memory:`) - reserved-turn consolidation + storage
# ---------------------------------------------------------------------------

def _read_agent_memory(vault_id: str, memory_rel: str) -> str:
    """Return the agent's stored memory body (frontmatter stripped), or "" if none."""
    from src.wikidoc import WikiDoc
    raw = WikiDoc.read_text(vault_id, memory_rel)
    if not raw:
        return ""
    return WikiDoc.strip_frontmatter(raw[0]).strip()


def _collapsed_against_previous(vault_id: str, output_rel: str,
                                body: str) -> int | None:
    """Length of the previous output page when `body` collapses against it, else None.

    The whole of the output-collapse guard's test (thresholds in
    AGENT_OUTPUT_COLLAPSE_*). Reads the previous page through the same canonical
    WikiDoc path every other agent-owned read uses. No previous page, no
    comparison, so a first run is never suspect - and a guard must never be the
    thing that fails a run, so an unreadable page means "not suspect" too.

    THREE conditions, and the first is what keeps the guard out of the way of
    agents that write short pages by design: an agent whose report has never
    reached the floor is never judged at all, however far a given run falls.
    Of the other two, the ratio is the signal (a collapse against THIS agent's
    own scale) and the floor is the brake (a run still over it is a short report,
    not a broken one). Nothing here reads the text: the failure being guarded
    against is fluent, correct prose answering somebody else's prompt, which no
    phrase list can separate from the real thing.
    """
    if not output_rel or AGENT_OUTPUT_COLLAPSE_RATIO <= 0:
        return None
    try:
        previous = _read_agent_memory(vault_id, output_rel)
    except Exception:       # noqa: BLE001 - see docstring
        logger.debug("collapse guard: could not read %s:%s", vault_id, output_rel)
        return None
    if len(previous) < AGENT_OUTPUT_COLLAPSE_FLOOR:
        return None
    if (len(body) < AGENT_OUTPUT_COLLAPSE_FLOOR
            and len(body) < len(previous) * AGENT_OUTPUT_COLLAPSE_RATIO):
        return len(previous)
    return None


async def write_agent_memory(vault_id: str, agent_slug: str, text: str) -> str:
    """Overwrite the agent's cross-run memory page and git-commit it.

    Path: `{AGENT_OUTPUT_DIR}/{agent_slug}/{AGENT_MEMORY_FILE}` in the target vault.
    Like write_agent_output it lives in the RAG-excluded, banner-marked owned area
    and commits itself (the watcher ignores the area). No RAG indexing - memory is
    for the agent, not the corpus. No in-body banner: the _dada location already
    banner-marks it agent-owned in the UI, and an in-body banner would pollute the
    memory the agent reads back next run."""
    rel = f"{AGENT_OUTPUT_DIR}/{agent_slug}/{AGENT_MEMORY_FILE}"
    generated = timefmt.iso_local()
    front = (
        "---\n"
        f"title: {agent_slug} memory\n"
        f"origin: {agent_slug}\n"
        f"generated: {generated}\n"
        "---\n\n"
    )
    from src.wikidoc import WikiDoc
    WikiDoc.write_text(vault_id, rel, front + text.strip() + "\n")

    from src.task_definitions import _git_version_save
    await _git_version_save(rel, vault_id)
    return rel


def read_agent_ledgers(vault_id: str, owner: str) -> dict[str, list[str]]:
    """Parsed ledgers for `owner` (e.g. "agents/math_of_the_day"), {} if none.

    Owner-scoped rather than vault-scoped: ledgers live beside memory.md in the
    owner's directory, so one agent can never read or clobber another's.
    """
    from src import ledgers as ledger_ops
    from src.wikidoc import WikiDoc
    raw = WikiDoc.read_text(vault_id, f"{AGENT_OUTPUT_DIR}/{owner}/{AGENT_LEDGERS_FILE}")
    if not raw:
        return {}
    return ledger_ops.parse(WikiDoc.strip_frontmatter(raw[0]).strip())


async def write_agent_ledgers(vault_id: str, owner: str,
                              ledgers: dict[str, list[str]]) -> str:
    """Overwrite the owner's ledgers page and git-commit it.

    Same contract as write_agent_memory: owned area, RAG-excluded by location,
    self-committed (the watcher ignores the area), no in-body banner so the page
    reads back clean.
    """
    from src import ledgers as ledger_ops
    from src.wikidoc import WikiDoc
    rel = f"{AGENT_OUTPUT_DIR}/{owner}/{AGENT_LEDGERS_FILE}"
    front = (
        "---\n"
        f"title: {owner} ledgers\n"
        f"origin: {owner}\n"
        f"generated: {timefmt.iso_local()}\n"
        "---\n\n"
    )
    WikiDoc.write_text(vault_id, rel, front + ledger_ops.render(ledgers).strip() + "\n")

    from src.task_definitions import _git_version_save
    await _git_version_save(rel, vault_id)
    return rel


# Run-scoped collection of what ledger operations actually DID. The activity
# list narrates a tool CALL and its arguments; it cannot show that a row was
# deduped away, landed in a differently-spelled ledger, or was refused by a full
# one - and the reserved ledger turn makes no tool call at all, so it left no
# trace in a run log whatsoever. A ContextVar because the agent path reaches
# apply_ledger_ops through the generic capability dispatch, which has nowhere to
# thread a sink; the editor path posts its writes to the worker and so passes a
# list explicitly instead.
_ledger_activity: ContextVar[list[str] | None] = ContextVar(
    "agent_ledger_activity", default=None)


def start_ledger_activity() -> tuple[list[str], object]:
    """Begin collecting ledger activity for this run. Returns (sink, token).

    Child tasks (the reserved turn runs under asyncio.wait_for) inherit a COPY
    of the context, which still holds this same list object - appends are
    visible to the caller because nothing rebinds the variable.
    """
    sink: list[str] = []
    return sink, _ledger_activity.set(sink)


def reset_ledger_activity(token) -> None:
    _ledger_activity.reset(token)


def record_ledger_activity(notes: list[str]) -> None:
    """Record applied-ledger notes to the active sink, if a run is collecting."""
    sink = _ledger_activity.get()
    if sink is not None and notes:
        sink.extend(notes)


# Prefix marking an applied note as a capacity REFUSAL rather than ordinary
# bookkeeping. A text marker rather than a richer note type because the editor
# path carries these back from the worker as plain JSON strings, and because the
# same string is handed to the model as a tool result, where the glyph reads as
# the signal to prune or forget.
LEDGER_REFUSAL_MARK = "\u26a0 "


def count_ledger_refusals(during: list[str], reserved: list[str]) -> int:
    """How many applied notes are refusals."""
    return sum(1 for n in (*during, *reserved) if n.startswith(LEDGER_REFUSAL_MARK))


def render_ledger_activity(during: list[str], reserved: list[str],
                           injection: str = "") -> list[str]:
    """Markdown lines for a run log's ledger section, shared by the agent and
    editor logs so the two read the same.

    Split by phase because the halves answer different questions: rows recorded
    DURING the run are the agent's own bookkeeping, rows from the reserved turn
    are the backstop catching what it did not record itself.

    `injection` reports that the ledgers page was too big to inject whole. That
    belongs here and not in a log line nobody reads: an owner working from a
    partial view is the one condition that degrades a run without failing it,
    which is exactly the kind of thing that otherwise gets noticed months later
    as "it started repeating itself".
    """
    lines = ["", "## Ledger operations"]
    if injection:
        lines += ["", injection]
    if not during and not reserved:
        return lines + ["", "- (no ledger operations)"]
    refusals = [n for n in (*during, *reserved) if n.startswith(LEDGER_REFUSAL_MARK)]
    if refusals:
        # A callout rather than one more bullet: a refusal is the only line in
        # this section that needs acting on, and the run around it still reports
        # ok, so nothing else on the page marks it. The phase bullets below keep
        # their copy - this is the summary, not a move.
        lines += ["", "> [!warning] Ledger capacity reached",
                  "> Prune rows or `forget` a ledger - new rows are being refused.",
                  ">",
                  *(f"> - {n.removeprefix(LEDGER_REFUSAL_MARK)}" for n in refusals)]
    # The blank line before each list is load-bearing: without it markdown reads
    # the rows as a lazy continuation of the label and renders a literal "- ".
    if during:
        lines += ["", "During the run:", "", *(f"- {n}" for n in during)]
    if reserved:
        lines += ["", "Reserved ledger turn:", "", *(f"- {n}" for n in reserved)]
    return lines


async def apply_ledger_ops(vault_id: str, owner: str, ops: list[dict]) -> list[str]:
    """Apply `remember`/`forget` operations and persist. Returns activity lines.

    One read-modify-write per CALL, however many ops it carries. Batching is the
    caller's to exploit: the reserved ledger turn hands over every op it decided
    on at once (agent side `_record_ledgers_from_run`, editor side the
    `/editor/memory` post) and gets one commit. The mid-loop `remember`/`forget`
    tools pass exactly one op each and the loop dispatches tool calls serially,
    so those commit per call - by design, since a run that dies mid-way keeps
    what it already recorded. Refusals are returned as text (and logged), never
    raised: a full ledger must not fail the run that reported to it. They carry
    LEDGER_REFUSAL_MARK so the run log can tell them from ordinary notes.
    """
    from src import ledgers as ledger_ops
    if not ops:
        return []
    book = read_agent_ledgers(vault_id, owner)
    notes: list[str] = []
    dirty = False
    for op in ops:
        name = (op.get("ledger") or "").strip()
        if op.get("forget"):
            if ledger_ops.forget(book, name):
                dirty = True
                notes.append(f"Forgot ledger '{name}'")
            else:
                notes.append(f"No ledger '{name}' to forget")
            continue
        added, dupes, refusal = ledger_ops.append(
            book, name, op.get("items") or [],
            AGENT_LEDGER_MAX_ITEMS, AGENT_LEDGER_MAX_COUNT)
        # Report the ledger the rows actually landed in, not the caller's spelling
        # of it - name matching is case-insensitive, so those can differ.
        canon = ledger_ops.find(book, name) or name
        if added:
            dirty = True
            notes.append(f"Recorded to '{canon}': {', '.join(added)}")
        if dupes:
            notes.append(f"Already on '{canon}': {', '.join(dupes)}")
        if refusal:
            logger.warning("ledger refusal for %s in %s: %s", owner, vault_id, refusal)
            notes.append(LEDGER_REFUSAL_MARK + refusal)
    if dirty:
        await write_agent_ledgers(vault_id, owner, book)
    # One chokepoint for every ledger write, so the run log sees mid-loop tool
    # calls and the reserved turn alike without either caller opting in.
    record_ledger_activity(notes)
    return notes


async def memory_budget(llm_mgr) -> tuple[int, int]:
    """(inject_chars, generate_tokens) for agent memory, derived TOGETHER.

    THE single source of truth for the memory size budget. Both numbers come from
    one figure so they cannot disagree: text generated past what will be injected
    is thrown away, and because injection is a TAIL cap, what gets thrown away is
    the note's opening. Setting them independently is how 1168 chars of every
    verbose consolidation went missing until 2026-08-03.

    Scales with the model's context window rather than a fixed char count - the
    same 6000 chars is 1.3% of a 128K window and 42% of a 4K one. Falls back to
    AGENT_MEMORY_INJECT_CHARS when the server does not report a context length.
    """
    ctx = 0
    try:
        ctx = int(await llm_mgr.get_context_length() or 0)
    except Exception:
        ctx = 0

    if ctx > 0:
        chars = int(ctx * AGENT_MEMORY_CONTEXT_FRACTION * CHARS_PER_TOKEN)
    else:
        chars = AGENT_MEMORY_INJECT_CHARS
    chars = max(AGENT_MEMORY_MIN_CHARS, min(chars, AGENT_MEMORY_MAX_CHARS))
    return chars, int(chars / CHARS_PER_TOKEN)


async def ledger_budget(llm_mgr) -> int:
    """Injection cap for the ledgers page, scaled to the context window.

    Its own fraction rather than a share of the memory budget: the two are
    injected together but a long ledger must not squeeze out the handoff note,
    nor the reverse. No generation counterpart - ledger rows arrive as tool
    arguments, not as generated prose.
    """
    ctx = 0
    try:
        ctx = int(await llm_mgr.get_context_length() or 0)
    except Exception:
        ctx = 0
    chars = (int(ctx * AGENT_LEDGER_CONTEXT_FRACTION * CHARS_PER_TOKEN) if ctx > 0
             else AGENT_MEMORY_INJECT_CHARS)
    return max(AGENT_MEMORY_MIN_CHARS, min(chars, AGENT_LEDGER_MAX_CHARS))


def _transcript_tail(messages: list[dict], max_chars: int) -> str:
    """The newest WHOLE turns fitting `max_chars`, with the cut declared.

    Slicing the rendered transcript by character hands the model a fragment
    whose opening line has lost its `assistant:` / `tool[x]:` label, and says
    nothing about how much went missing - the same two failures the ledgers page
    itself used to have. Dropping whole turns keeps every surviving line
    attributable, which is why the memory turn windows by message group
    (compaction.apply_sliding_window) rather than by character.
    """
    from src.compaction import render_messages_for_summary
    if not messages:
        return ""
    rendered = [render_messages_for_summary([m]) for m in messages]
    kept, used = 0, 0
    for text in reversed(rendered):
        # Always keep at least the last turn, even if it alone busts the budget:
        # a ledger turn shown nothing of the run has nothing to record.
        if kept and used + len(text) + 1 > max_chars:
            break
        used += len(text) + 1
        kept += 1
    tail = rendered[len(rendered) - kept:]
    if len(tail) == 1 and len(tail[0]) > max_chars:
        # One oversized turn (a fat tool result) still has to be bounded. Cut on
        # a line boundary and say so, rather than passing off a fragment as whole.
        cut = tail[0][-max_chars:]
        nl = cut.find("\n")
        tail = ["[start of this turn omitted]\n" + (cut[nl + 1:] if nl >= 0 else cut)]
    body = "\n".join(tail)
    dropped = len(rendered) - kept
    return f"[{dropped} earlier turn(s) omitted, {kept} shown]\n{body}" if dropped else body


async def ledger_ops_from_run(book: dict, messages: list[dict], note: str,
                              llm_mgr, label: str = "") -> list[dict]:
    """Memory step, call 2: a tool-only turn that decides what to record.

    Returns ops for apply_ledger_ops; performs NO writes, so the agent path can
    apply them directly while the editor path brokers them to the worker (the
    owned-area write is the worker's job - see editor_kernel.http_memory).

    Exactly ONE call, and the model never sees its result: `remember` is
    write-only, so there is nothing to feed back and no reason to iterate. Split
    from the note-writing call because this model emits prose XOR tool calls -
    see .test/probe_memory_turn_tools.py.
    """
    from src.agent_capabilities import _registry, _validate_args
    from src.agent_runner import _tool_call_name, _normalize_tool_call
    from src.llm_gate import get_llm_gate
    from src.memory_prompts import ledger_turn_prompt
    from src import ledgers as ledger_ops

    transcript = _transcript_tail(messages, AGENT_LEDGER_TURN_TRANSCRIPT_CHARS)
    # The book is rendered WHOLE here, unlike the injected view the loop sees.
    # Not an oversight: the injection fraction is small because the system prompt
    # is paid on every turn, and this call happens once. More to the point, code
    # dedup is LEXICAL (ledgers.item_key) - judging whether a new row is
    # meaningfully distinct from an existing one is delegated to the prompt, and
    # a model cannot make that judgement against rows it was not shown. Trimming
    # here would break the one thing this turn is for.
    prompt = ledger_turn_prompt(ledger_ops.render(book), note, transcript)

    # The one registered schema, not a copy - the tool the model is offered here
    # must be the tool it is offered in the loop.
    spec = _registry()["remember"]
    tool_defs = [spec["def"]]

    calls, stream_error = None, None
    stream = llm_mgr.chat_stream_with_tools(
        [{"role": "user", "content": prompt}], tool_defs, think=False)
    try:
        async with get_llm_gate("agent:ledger"):
            async for chunk in stream:
                if chunk.done:
                    calls, stream_error = chunk.tool_calls, chunk.error
                    break
    finally:
        try:
            await stream.aclose()
        except Exception:
            pass
    if stream_error:
        logger.warning("ledger turn for %s ended abnormally: %s", label, stream_error)

    ops = []
    for tc in calls or []:
        if _tool_call_name(tc) != "remember":
            continue
        args = _normalize_tool_call(tc)["function"]["arguments"]
        kwargs, errors = _validate_args(spec["def"], args)
        if errors:
            # A call naming a ledger but carrying no items is how the model says
            # "nothing new this run" - benign, and common enough that warning on
            # it would cry wolf every quiet run. Anything else is a real defect.
            empty_only = all("items" in e for e in errors)
            log = logger.debug if empty_only else logger.warning
            log("ledger turn for %s produced no usable call: %s",
                label, "; ".join(errors))
            continue
        ops.append({"ledger": kwargs["ledger"], "items": kwargs["items"]})
    return ops


async def _record_ledgers_from_run(agent: BackgroundAgent, vault_id: str,
                                   messages: list[dict], note: str,
                                   llm_mgr) -> list[str]:
    """Agent-side call 2: decide what to record, then record it.

    The BACKSTOP half of ledger recording. An agent granted `remember` records as
    it works, which is better (a run that dies half way keeps what it finished);
    this catches what the run did not record, and needs no grant, because
    requiring one would mean predicting in advance which agents turn out to need
    a ledger - the prediction that cannot be made for behavior that emerges
    mid-run.
    """
    book = read_agent_ledgers(vault_id, agent.owner)
    ops = await ledger_ops_from_run(
        book, messages, note, llm_mgr, label=f"{agent.name}:{vault_id}")
    if not ops:
        return []
    return await apply_ledger_ops(vault_id, agent.owner, ops)


async def _consolidate_agent_memory(agent: BackgroundAgent, vault_id: str,
                                    messages: list[dict], prior_memory_text: str,
                                    llm_mgr) -> None:
    """The reserved memory turn: one tool-free LLM call that rewrites memory.md.

    NEVER clobbers on failure: summarize_conversation returns "" on error/overflow
    (and background runs don't compact, so a heavy transcript can overflow the
    summarizer) - on empty output we PRESERVE prior memory rather than wipe it.
    Prior memory is passed in the INPUT because the summarizer runs as a bare
    generate() with no agent system prompt, so the injected MemoryProvider is
    invisible to it. Gated (worker citizen) and bounded by a dedicated sub-timeout
    so a hung consolidation can't run past the reserved single call."""
    from src.compaction import (
        apply_sliding_window, estimate_tokens, summarize_conversation)
    from src.llm_gate import get_llm_gate
    from src.memory_prompts import agent_instruction

    tool_list = ", ".join(sorted(agent.tool_names)) or "your tools"
    instruction = agent_instruction(
        agent.memory_prompt, agent.name, tool_list, prior_memory_text)
    # Bound the INPUT before spending the call. The docstring above notes that a
    # heavy transcript can overflow the summarizer - in which case it returns ""
    # and we preserve prior memory, so an oversized run costs a long LLM call and
    # produces NOTHING. Trimming oldest groups to fit is strictly better than
    # overflowing; atomic groups keep tool results with their calls.
    bounded = messages
    try:
        ctx = await llm_mgr.get_context_length()
        if ctx:
            instr_tokens = estimate_tokens([{"role": "user", "content": instruction}])
            bounded, trimmed = apply_sliding_window(
                messages, max_messages=len(messages),
                system_prompt_tokens=instr_tokens, context_length=ctx,
            )
            if trimmed:
                logger.info("memory turn: trimmed transcript for %s:%s to fit %d ctx",
                            agent.name, vault_id, ctx)
    except Exception as e:
        logger.debug("memory turn: could not bound transcript (%s); sending as-is", e)

    inject_chars, gen_tokens = await memory_budget(llm_mgr)

    # Labeled separately from the agent's own turns: memory consolidation is a
    # distinct kind of LLM spend, and worth being able to see on its own.
    async with get_llm_gate("agent:memory"):
        text = await asyncio.wait_for(
            summarize_conversation(bounded, instruction, llm_mgr,
                                   max_tokens=gen_tokens),
            timeout=AGENT_MEMORY_TURN_TIMEOUT_S,
        )
    text = (text or "").strip()
    # Hitting the ceiling means the note was cut off mid-thought, which is
    # otherwise invisible - the overflow is simply dropped at injection time.
    if text and len(text) >= inject_chars * 0.95:
        logger.warning(
            "memory turn for %s:%s produced %d chars against a %d budget - "
            "likely truncated; raise AGENT_MEMORY_CONTEXT_FRACTION or tighten "
            "the agent's `# Memory Prompt`",
            agent.name, vault_id, len(text), inject_chars)
    if not text:
        logger.warning(
            "memory turn produced no output for %s:%s - preserving prior memory",
            agent.name, vault_id)
    else:
        rel = await write_agent_memory(vault_id, agent.owner, text)
        logger.info("background agent %s consolidated memory for vault %s -> %s",
                    agent.name, vault_id, rel)

    # Call 2. Runs even when call 1 produced nothing: the two writes are
    # independent, and a run whose note failed to generate is exactly the run
    # whose facts most need recording somewhere durable.
    try:
        notes = await asyncio.wait_for(
            _record_ledgers_from_run(agent, vault_id, messages, text, llm_mgr),
            timeout=AGENT_LEDGER_TURN_TIMEOUT_S)
        if notes:
            logger.info("ledger turn for %s:%s - %s",
                        agent.name, vault_id, "; ".join(notes))
    except Exception:           # noqa: BLE001 - never costs the run its memory
        logger.exception("ledger turn failed for %s:%s", agent.name, vault_id)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_background_agent(agent: BackgroundAgent, vault_id: str, llm_mgr,
                               cancel_check=None, kickoff_extra: str | None = None,
                               trigger_events: list[dict] | None = None,
                               trigger_source: str = "manual") -> dict:
    """Run one background agent against one vault. Returns a small status dict.

    Always writes a per-run log page (success or failure) into the agent's
    owned area: `{AGENT_OUTPUT_DIR}/{agent}/logs/{timestamp}.md`.

    Event-triggered runs pass ``kickoff_extra`` (the human-readable trigger
    batch, appended to the kickoff turn - prompt-interpreted context, gap #7
    style) and ``trigger_events`` (the raw envelopes, recorded in the run log).
    """
    providers: list[ContextProvider] = [
        DirectiveProvider(agent.directive, name="directive", priority=10),
        DirectiveProvider(agent.tools_text, name="tools", priority=30),
    ]
    # Cross-run memory (opt-in `memory:`): the agent's self-curated handoff note,
    # written by the reserved memory turn at end-of-run (below) and injected here
    # for the NEXT run. Read once and reused below as the reserved turn's
    # prior-memory carry-forward. Decoupled from `log:`, which now governs only the
    # per-run audit pages. Storage/injection is agent policy; the summarizer and
    # the provider are shared (chat can reuse them later).
    prior_memory_text = ""
    if agent.memory and agent.memory_rel:
        prior_memory_text = _read_agent_memory(vault_id, agent.memory_rel)
        # Same budget the memory TURN generates against, so nothing written last
        # run is silently dropped on the way back in this run.
        _inject_chars, _ = await memory_budget(llm_mgr)
        providers.append(MemoryProvider(
            prior_memory_text, name="memory", priority=20,
            char_cap=_inject_chars))
    # Ledgers are gated SEPARATELY from memory. The run has to SEE what it
    # already recorded before it can avoid repeating it, or decide a ledger has
    # outlived its purpose - and the write tools need only a run context, so an
    # agent granted `remember` with memory off was recording rows every run and
    # never reading one back. Separate provider as well as a separate gate: the
    # two carry different guarantees, the note being rewritten each run and
    # these rows not.
    ledger_injection_note = ""
    if uses_ledgers(agent.memory, agent.tool_names):
        book = read_agent_ledgers(vault_id, agent.owner)
        ledgers_provider = LedgerProvider(
            book, name="ledgers", priority=21,
            char_cap=await ledger_budget(llm_mgr))
        providers.append(ledgers_provider)
        if ledgers_provider.stubbed:
            ledger_injection_note = (
                "> [!info] Ledgers too large to inject whole\n"
                "> Shown in part this run: "
                + ", ".join(f"`{n}` ({len(book[n])} rows)"
                            for n in ledgers_provider.stubbed)
                + ".\n> The agent could read the rest with `recall`; prune or "
                  "`forget` a ledger to restore the full view.")
    system_prompt, _ = assemble_system_prompt(providers)

    kickoff = agent.kickoff + ("\n\n" + kickoff_extra if kickoff_extra else "")
    messages: list[dict] = [{"role": "user", "content": kickoff}]

    from functools import partial

    from src import write_gate
    from src.llm_gate import get_llm_gate, human_active

    # The run id keys everything durable this run produces: staged shadows +
    # manifest rows (write_gate), apply-commit attribution, the run log, and
    # (for custom tools) the kernel + its scoped token.
    # Session boundary. Redundant while LLM_AGENT_NO_PROMPT_CACHE is on (every
    # worker call is already cold), and the point of stating it anyway: turning
    # that knob off must not silently give up the isolation guarantee at the one
    # boundary where a run picks up whatever the server was last doing.
    llm_backend.begin_cold_session()

    run_id = f"{agent.name}-{vault_id}-{timefmt.file_stamp()}"
    ctx_token = write_gate.set_run_context(run_id, agent.owner, agent.mode)
    # Outlives the write-gate context above: the reserved ledger turn runs after
    # the loop is torn down and its rows belong in the same log section.
    ledger_sink, ledger_token = start_ledger_activity()

    # Custom tools execute in the isolated agent kernel; internal capabilities
    # stay worker-side. The kernel session and its HMAC token live exactly as
    # long as this run.
    kernel_session = None
    if agent.custom_tool_names:
        from src import agent_tokens
        from src.agent_kernel import AgentKernelSession
        token = agent_tokens.mint(agent.owner, vault_id, run_id, mode=agent.mode)
        kernel_session = AgentKernelSession(
            run_id, agent.name, vault_id, agent.py_source, token)

    async def _exec(name, args, status_cb):
        if kernel_session is not None and name in agent.custom_tool_names:
            return await kernel_session.call_tool(name, args or {})
        return await agent.execute(name, args, vault_id, status_cb)

    started = time.monotonic()
    run_result = AgentRunResult()
    error: Exception | None = None

    async def _drive():
        nonlocal run_result
        async for evt in run_agent_loop(
            messages=messages,
            system_prompt=system_prompt,
            tool_defs=agent.tool_defs,
            tool_names=agent.tool_names,
            llm_mgr=llm_mgr,
            execute_tool=_exec,
            status_label=lambda name: f"Running {name}…",
            activity_narration=_narrate_tool_call,
            needs_approval=frozenset(),       # staged writes replaced per-call approval
            parse_tool_call_from_text=None,   # native tool calls only
            stream_markers=(),
            # per-call background gating (worker citizen); partial keeps it a
            # ZERO-ARG factory, which is how agent_runner calls it, while still
            # attributing the LLM time to this specific agent.
            llm_gate=partial(get_llm_gate, f"agent:{agent.name}"),
            # Stand aside between turns while a person is using the wiki. Priority
            # on the gate cannot help here: interactive chat runs in the WEB
            # process and never touches this gate, and the LLM server itself is
            # serial - so the only way to keep a human from waiting out a 26s turn
            # is for the agent not to start the next one.
            yield_check=(human_active if AGENT_YIELD_TO_HUMAN else None),
            yield_poll_s=AGENT_YIELD_POLL_S,
            yield_max_s=AGENT_YIELD_MAX_S,
            cancel_check=cancel_check,        # cooperative stop, checked between steps
            max_iterations=agent.max_iterations or MAX_AGENT_ITERATIONS,
            think=AGENT_TOOL_THINK,   # gpt-oss reasoning-channel interleave can break tool-call parsing
            log_label=f"{agent.name}:{vault_id} ",
        ):
            if evt["type"] == "result" and isinstance(evt["result"], AgentRunResult):
                run_result = evt["result"]

    try:
        if kernel_session is not None:
            await kernel_session.start()
        # Two-layer timeouts, layer 2: whole-run wall clock (the time-analog of
        # max-steps). On expiry the kernel is deleted in the finally below.
        await asyncio.wait_for(_drive(), timeout=AGENT_RUN_TIMEOUT_S)
    except asyncio.TimeoutError:
        error = TimeoutError(f"run exceeded AGENT_RUN_TIMEOUT_S ({AGENT_RUN_TIMEOUT_S:.0f}s)")
    except Exception as e:      # noqa: BLE001 - the run log must record failures too
        error = e
    finally:
        write_gate.reset_run_context(ctx_token)
        if kernel_session is not None:
            await kernel_session.close()

    # A cooperative cancel reuses the failure path's plumbing (no output
    # clobber; a log page is written) but carries a DISTINCT type so it renders
    # as "cancelled", not "failed".
    if error is None and run_result.cancelled:
        error = AgentCancelled("run stopped by cancel request")

    # A run that exhausted its malformed-stream retries made no clean stop and no
    # progress on that step - record it as a failure so the log renders "failed" (not
    # a misleading "ok") and the /agents inbox surfaces it, instead of silently
    # truncating the activity list like the old swallow-the-error path did.
    if error is None and run_result.stream_error:
        error = RuntimeError(
            f"model stream aborted on a malformed tool call: {run_result.stream_error}")

    duration = time.monotonic() - started
    try:
        n_staged = await asyncio.to_thread(write_gate.staged_count, run_id)
        n_applied = await asyncio.to_thread(write_gate.applied_count, run_id)
    except Exception:
        n_staged, n_applied = 0, 0

    body = (run_result.final_text or "").strip()
    output_path = None

    # Output-collapse guard. Runs BEFORE the write block so a suspect run takes
    # the failure path whole: the page is preserved, the reserved memory and
    # ledger turns below never fire, and the run surfaces as a failure instead of
    # being found tomorrow as the agent's report.
    if error is None and body:
        previous_len = _collapsed_against_previous(vault_id, agent.output_rel, body)
        if previous_len is not None:
            logger.warning(
                "output-collapse guard rejected %s:%s - %d chars against a %d-char "
                "previous page; output, memory and ledgers left untouched",
                agent.name, vault_id, len(body), previous_len)
            # The excerpt is load-bearing, not decoration: with `log:` off there
            # is no page holding the rejected text, and this string is what
            # reaches /manage/monitor (failure_log truncates to DETAIL_CHARS).
            # Seeing that the text answers a DIFFERENT prompt is the diagnosis.
            error = AgentOutputSuspect(
                f"final output collapsed to {len(body)} chars against a "
                f"{previous_len}-char previous page: {body[:100]!r}")

    if error is None:
        # The output page is the human-facing report; cross-run memory now lives in
        # memory.md (reserved turn), so we no longer preserve the output page AS
        # memory. But an empty run (no final message, e.g. max-steps) must still be
        # SURFACED. `log:` gates only which channel does that: when it is ON the
        # per-run log page records the miss, so we skip the write and keep the last
        # good report; when it is OFF there is no log page, so the output stub is the
        # sole channel that surfaces the miss. (This use of `log:` is observability,
        # not the memory-durability coupling that was removed.)
        empty_run = not body or body == "(Reached maximum steps.)"
        if empty_run:
            if agent.log:
                body = ""       # log page records the miss; preserve last report
            else:
                body = (
                    f"_The {agent.name} agent did not produce output this run "
                    f"(steps exhausted: {run_result.reached_max_steps}). "
                    f"Tools run: {', '.join(run_result.activity_log) or 'none'}._"
                )
        if body:
            output_path = await agent.sink(vault_id, body)
            logger.info("background agent %s wrote output for vault %s -> %s",
                        agent.name, vault_id, output_path)

    # Reserved memory turn (+1 iteration above the working budget): one tool-free
    # consolidation call that writes the agent's cross-run memory. It is reserved,
    # so step-exhaustion can never starve it - this is what makes memory advance on
    # EVERY run, including the max-steps/timeout runs that used to freeze it. Fires
    # on any run that actually ran; hard errors, cancels and collapse-guard
    # rejections skip it (they have no trustworthy state to consolidate - a suspect
    # run's transcript is the very thing under suspicion, and consolidating it is
    # how one bad turn rewrote the note AND appended a ledger row for an article
    # that was never written). A timeout is the time-analog of
    # max-steps, so it still consolidates. A stream-error failure (the model kept
    # emitting malformed tool calls) likewise did real, completed tool work before
    # the break, so its message history IS trustworthy - consolidate it too rather
    # than freezing memory. Never clobbers on failure (see helper).
    stream_error_run = bool(run_result.stream_error)
    # Everything the sink holds up to here came from the agent's own mid-loop
    # `remember`/`forget` calls; anything after is the reserved turn's backstop.
    n_mid_run = len(ledger_sink)
    if agent.memory and (error is None or isinstance(error, TimeoutError) or stream_error_run):
        try:
            await _consolidate_agent_memory(
                agent, vault_id, messages, prior_memory_text, llm_mgr)
        except Exception:       # noqa: BLE001 - memory must never fail the run
            logger.exception(
                "memory consolidation failed for %s:%s (prior memory preserved)",
                agent.name, vault_id)
    ledger_during, ledger_reserved = ledger_sink[:n_mid_run], ledger_sink[n_mid_run:]
    reset_ledger_activity(ledger_token)

    # Per-run log pages are opt-in (`log:` frontmatter) and that is the WHOLE of
    # it - a failure does not earn the right to write a page into a vault whose
    # owner asked for none. Failures reach a human without one: run_agent_task
    # records the error per vault, failure_log.classify_result turns that into a
    # durable `agent_run` row, and /manage/monitor renders it under "Failures"
    # with the vault, the agent and the error text. That surface is the reason
    # this branch can be the plain reading of `log:`.
    if agent.log:
        # The section is worth its two lines whenever the agent keeps ledgers at
        # all - "recorded nothing" is itself the answer being looked for.
        show_ledgers = bool(uses_ledgers(agent.memory, agent.tool_names)
                            or ledger_during or ledger_reserved)
        await _write_run_log(agent, vault_id, llm_mgr, run_result, duration,
                             output_path, error, run_id, n_staged, n_applied,
                             trigger_events, trigger_source,
                             (ledger_during, ledger_reserved) if show_ledgers else None,
                             ledger_injection_note)
    if error is not None:
        raise error

    return {
        "agent": agent.name,
        "vault_id": vault_id,
        "run_id": run_id,
        "output_path": output_path,
        "staged_count": n_staged,
        "applied_count": n_applied,
        "tools_run": run_result.activity_log,
        "reached_max_steps": run_result.reached_max_steps,
        "duration_s": round(duration, 1),
    }


async def _write_run_log(agent: BackgroundAgent, vault_id: str, llm_mgr,
                         run_result: AgentRunResult, duration: float,
                         output_path: str | None, error: Exception | None,
                         run_id: str = "", n_staged: int = 0,
                         n_applied: int = 0,
                         trigger_events: list[dict] | None = None,
                         trigger_source: str = "manual",
                         ledger_activity: tuple[list[str], list[str]] | None = None,
                         ledger_injection: str = "") -> None:
    """Per-run log page (crude store-full rendering; view-layer collapsing is a
    later refinement). Log failures must never mask the run's own outcome."""
    from src.events import format_trigger_summary   # lazy: import cycle
    ts = timefmt.file_stamp()
    cancelled = isinstance(error, AgentCancelled)
    suspect = isinstance(error, AgentOutputSuspect)
    status = ("CANCELLED" if cancelled else "SUSPECT" if suspect
              else "FAILED" if error else "ok")
    led_during, led_reserved = ledger_activity or ([], [])
    n_refused = count_ledger_refusals(led_during, led_reserved)
    lines = [
        f"| status | {status} |",
        "|---|---|",
        f"| run_id | `{run_id or '?'}` |",
        f"| vault | {vault_id} |",
        f"| model | {getattr(llm_mgr, 'model', '?')} |",
        f"| definition | `{agent.def_hash or '?'}` |",
        f"| duration | {duration:.1f}s |",
        f"| tool calls | {len(run_result.activity_log)} |",
        f"| reached_max_steps | {run_result.reached_max_steps} |",
        f"| output | {output_path or '-'} |",
        f"| mode | {agent.mode} |",
        f"| staged for review | {n_staged} |",
        f"| applied directly | {n_applied} |",
        f"| kernel | {'agent kernel (custom tools)' if agent.custom_tool_names else '-'} |",
        f"| triggered by | {format_trigger_summary(trigger_events, trigger_source)} |",
        *([f"| ledger ops | {len(led_during) + len(led_reserved)}"
           f"{f' - **{n_refused} REFUSED**' if n_refused else ''} |"]
          if ledger_activity is not None else []),
        "",
        "## Activity",
        *([f"- {a}" for a in run_result.activity_log] or ["- (no tool calls)"]),
    ]
    if ledger_activity is not None:
        lines += render_ledger_activity(led_during, led_reserved, ledger_injection)
    if trigger_events:
        lines += ["", "## Triggered by"]
        lines += [f"- {ev.get('type', '?')}: `{ev.get('subject', '')}` "
                  f"(vault {ev.get('vault', '?')}, by {ev.get('actor', '?')}, "
                  f"depth {ev.get('depth', 0)}, {ev.get('ts', '?')})"
                  for ev in trigger_events]
    if cancelled:
        lines += ["", "## Cancelled", f"{error}"]
    elif suspect:
        lines += [
            "", "> [!warning] Output rejected by the collapse guard",
            f"> {error}. The output page, cross-run memory and ledgers were left",
            "> exactly as the previous run left them. Rerun the agent; if the text",
            "> below looks like it answers a DIFFERENT prompt, suspect the inference",
            "> server's prompt cache rather than the agent definition.",
            "", "## Rejected output", "", run_result.final_text or "(empty)"]
    elif error is not None:
        lines += ["", "## Error", f"```\n{error!r}\n```"]
    elif run_result.final_text:
        lines += ["", "## Final output", "", run_result.final_text]
    try:
        await write_agent_output(vault_id, agent.owner, f"logs/{ts}.md",
                                 "\n".join(lines), title=f"Run {ts}")
    except Exception:
        logger.exception("failed to write run log for %s:%s", agent.name, vault_id)
