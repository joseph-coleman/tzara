# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Writing-assistance commands for the /edit/ view.

Architecture:
  - WritingCommand describes one invokable operation: id, label, which range
    it operates on (cursor or selection), what it does to that range
    (insert or replace), how to build the LLM messages, and an optional
    tuple of context_providers - async callables that fetch extra context
    (e.g. retrieved corpus passages) before the prompt is built.
  - AssistContext bundles everything a provider might need in one frozen
    record (cursor neighborhood, document identity, frontmatter, app handles).
  - COMMANDS is the registry. Adding a command is one entry; the slash
    menu picks it up automatically via GET /api/edit/commands.
  - stream_assist is the shared SSE-streaming dispatcher: runs providers,
    emits a one-shot `sources` event for the UI, then streams tokens.

SSE protocol matches chat.py: data: {json}\\n\\n with keys
{token, done, error, sources}. The `sources` event (if emitted) precedes
the first token and carries a list of {path, title, header, linked} dicts.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass, replace
from typing import Any, AsyncGenerator, Awaitable, Callable

from config import (
    AGENT_TOOL_THINK,
    DEFAULT_VAULT,
    LLM_EDIT_CONTEXT_BUDGET,
    LLM_MODEL,
)
from src import chunker
from src import llm_backend
from src import timefmt
from src.wikidoc import WikiDoc

logger = logging.getLogger("edit_assist")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _voice_hint(frontmatter: dict | None) -> str:
    if not frontmatter:
        return ""
    parts = []
    for key in ("audience", "voice", "tone", "style"):
        v = frontmatter.get(key)
        if v:
            parts.append(f"{key}: {v}")
    if not parts:
        return ""
    return "\n\nDocument metadata to honor: " + "; ".join(parts) + "."


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _path_to_doc_id(path: str | None) -> str | None:
    """Normalize the editor's file_path to the wiki-relative `.md`-suffixed
    doc_id used as the chunks/documents primary key.

    Delegates to `WikiDoc.parse_url_path` so we get reserved-prefix stripping
    (`wiki/`, `edit/`, etc.), URL decoding, and traversal/slash normalization
    for free. Examples:
      "wiki/RAG_PLAN.md"            -> "RAG_PLAN.md"
      "RAG_PLAN"                    -> "RAG_PLAN.md"
      "wiki/Computer/AMD_Strix.md"  -> "Computer/AMD_Strix.md"
      "Foo%20Bar"                   -> "Foo Bar.md"
    """
    if not path:
        return None
    parsed = WikiDoc.parse_url_path(path)
    parts = [p for p in parsed["path_list"] if p]
    file_name = parsed["file_name"]
    if parsed["file_ext"] != "md":
        file_name = file_name + ".md"
    parts.append(file_name)
    return "/".join(parts)


def _build_retrieval_query(before: str) -> str:
    """Pick the text to drive corpus retrieval from the cursor neighborhood.

    Heuristic for v1: last 500 chars of `before`, biased to the last
    paragraph when a clear `\\n\\n` break is present. Tunable.
    """
    if not before:
        return ""
    tail = before[-500:].strip()
    idx = tail.rfind("\n\n")
    if idx >= 0:
        last_para = tail[idx + 2:].strip()
        if last_para:
            return last_para
    return tail


_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _approx_tokens(text: str) -> int:
    """Cheap chars/4 token estimator. Good enough for tier selection."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _build_outline(content: str, max_lines: int = 60) -> str:
    """Compact markdown heading outline, indented by level.

    Headings come from md_sections.parse_sections, so a `## Heading` inside a
    fenced code block or a `$$` math block is correctly NOT treated as document
    structure - the local regex this replaced had no fence tracking and reported
    those as real headings.
    """
    if not content:
        return ""
    from src.md_sections import parse_sections
    lines = []
    for section in parse_sections(content):
        if section["level"] == 0:  # text above the first heading isn't a heading
            continue
        lines.append("  " * (section["level"] - 1) + "- " + section["heading_text"])
        if len(lines) >= max_lines:
            lines.append("  ... (outline truncated)")
            break
    return "\n".join(lines)


_CURSOR_BEFORE_WINDOW = 4000
_CURSOR_AFTER_WINDOW = 1000


def _build_doc_context(
    content: str,
    cursor_offset: int,
    budget_tokens: int,
) -> tuple[str, str]:
    """Return (tier_name, rendered_doc_context) for cursor-mode commands.

    Tiers, in order of preference:
      - "full":           whole document with a <<CURSOR>> marker
      - "outline+window": heading outline + before/after windows
      - "window":         just before/after - last resort

    `budget_tokens` is the model's full context window. Internal overhead
    (system prompt, retrieved passages, completion) is subtracted before
    deciding tier.
    """
    overhead = 2000
    available = max(1000, budget_tokens - overhead)
    pos = max(0, min(cursor_offset, len(content)))

    # Tier 1: full doc with cursor marker. Wording is DIRECTIVE-NEUTRAL - both the
    # built-in continuation commands and cursor-scope editor tools render their doc
    # context through here and append their own instruction after this block.
    if _approx_tokens(content) <= available:
        marked = content[:pos] + "<<CURSOR>>" + content[pos:]
        return "full", (
            "Full document (the caret is marked <<CURSOR>>; write your text for "
            "that position and do not output the marker itself):\n```\n" + marked + "\n```"
        )

    # Cursor-neighborhood window, used by tiers 2 and 3
    before = content[max(0, pos - _CURSOR_BEFORE_WINDOW):pos]
    after = content[pos:pos + _CURSOR_AFTER_WINDOW]
    window_parts = [f"Text before the cursor:\n```\n{before}\n```"]
    if after.strip():
        window_parts.append(
            f"Text after the cursor (do not repeat or overlap with this):\n"
            f"```\n{after}\n```"
        )
    window_block = "\n\n".join(window_parts)

    # Tier 2: heading outline + window
    outline = _build_outline(content)
    if outline:
        candidate = "Document outline (for orientation):\n" + outline + "\n\n" + window_block
        if _approx_tokens(candidate) <= available:
            return "outline+window", candidate

    # Tier 3: window only
    return "window", window_block


def _format_passages(chunks: list[dict]) -> str:
    """Render retrieved chunks as a markdown bullet list for the LLM prompt.

    Format mirrors the chat-side convention (`app/src/chat.py:_do_search_wiki`)
    so the model sees the same shape across `/wiki/` chat and `/edit/`
    grounded commands.
    """
    lines = []
    for r in chunks:
        doc_id = r.get("doc_id", "") or ""
        wiki_path = doc_id[:-3] if doc_id.endswith(".md") else doc_id
        header = r.get("header_path", "") or ""
        snippet = (r.get("content", "") or "")[:300]
        prefix = "[linked] " if r.get("source") == "graph" else ""
        lines.append(f"- {prefix}[[/{wiki_path}]] > {header}\n  {snippet}")
    return "\n".join(lines)


def _to_source(r: dict) -> dict:
    """Project a retrieval result into the small dict the UI consumes."""
    doc_id = r.get("doc_id", "") or ""
    wiki_path = doc_id[:-3] if doc_id.endswith(".md") else doc_id
    return {
        "path": wiki_path,
        "title": r.get("doc_title", "") or wiki_path,
        "header": r.get("header_path", "") or "",
        "linked": r.get("source") == "graph",
    }


# ---------------------------------------------------------------------------
# Context object + provider type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AssistContext:
    """Everything a context-provider might need to assemble extra prompt context."""
    before: str
    selection: str
    after: str
    path: str | None         # editor file_path (URL-style, may lack .md)
    doc_id: str | None       # .md-suffixed doc_id for DB lookups
    vault: str               # the vault this document lives in (retrieval scope)
    frontmatter: dict | None
    llm_mgr: Any          # opaque app handle; providers may need embeddings/chat
    # Full live editor content (None when frontend hasn't sent it - fall back
    # to before/after windows). cursor_offset is the byte/char index of the
    # caret in `content`; required to render a `<<CURSOR>>` marker for
    # full-doc context tier.
    content: str | None = None
    cursor_offset: int | None = None
    # The working range, as offsets into `content` (NOT into the whole buffer -
    # document-scope commands strip the frontmatter first, and the client sends
    # offsets already rebased onto what it sent). A caret is a ZERO-WIDTH
    # selection: for cursor-source commands start == end == cursor_offset, and
    # `before`/`after` are then simply the text on either side of the caret.
    # Invariant the client maintains: content[selection_start:selection_end]
    # == selection.
    selection_start: int | None = None
    selection_end: int | None = None


def _windows_at(content: str, pos: int) -> tuple[str, str]:
    """(before, after): the text on either side of one offset in `content`.

    Capped at the _CURSOR_* sizes so a long document can't blow the prompt, and
    clamped so a stale or out-of-range offset can never raise.
    """
    n = len(content)
    p = max(0, min(pos, n))
    return content[max(0, p - _CURSOR_BEFORE_WINDOW):p], content[p:p + _CURSOR_AFTER_WINDOW]


def _range_windows(actx: "AssistContext") -> tuple[str, str]:
    """(before, after): the text bracketing the command's working range.

    Prefers exact slices of the live buffer, since the client sends the range
    offsets in `content` coordinates; falls back to the smaller windows the
    client shipped when the buffer isn't available.

    One code path for all three scopes, because the caret is just a zero-width
    selection - collapse start and end together and "before/after the selection"
    becomes "before/after the cursor" with no special case.
    """
    if actx.content is None or actx.selection_start is None or actx.selection_end is None:
        return actx.before, actx.after
    start = max(0, min(actx.selection_start, len(actx.content)))
    end = max(start, actx.selection_end)
    return _windows_at(actx.content, start)[0], _windows_at(actx.content, end)[1]


# Providers return a {"ctx": dict, "sources": list[dict]} envelope.
# `ctx` is merged into the dict that user_template receives. `sources` is
# concatenated across providers and emitted to the UI as a one-shot event.
ContextProvider = Callable[[AssistContext], Awaitable[dict]]


# ---------------------------------------------------------------------------
# Built-in providers
# ---------------------------------------------------------------------------

def corpus_passages(top_k: int = 5) -> ContextProvider:
    """Provider that retrieves wiki-corpus passages relevant to the cursor.

    Self-excludes the document being edited so the LLM isn't "grounded" on
    text the user just wrote. Result is exposed to the user_template as
    `ctx["passages_block"]` (formatted markdown) and to the UI as a list of
    source records (path, title, header, linked).
    """
    async def provider(actx: AssistContext) -> dict:
        query = _build_retrieval_query(actx.before)
        if not query:
            return {"ctx": {"passages_block": ""}, "sources": []}

        # Live overrides: derive wikilinks/tags from the editor's current
        # content (not the saved index) so removing/adding links during an
        # edit immediately reflects in graph-expanded retrieval.
        overrides: dict | None = None
        if actx.doc_id:
            body = actx.content or ""
            if body.startswith("---\n"):
                close = body.find("\n---", 4)
                if close != -1:
                    body = body[close + 4:]
            overrides = {
                "doc_id": actx.doc_id,
                "wikilinks": chunker.extract_page_links(body),
                "tags": chunker.extract_tags(body),
            }

        # Late import to mirror chat.py and avoid pulling rag_search at
        # module load (it touches psycopg2 / Ollama on its own imports).
        from src import rag_search
        result = await asyncio.to_thread(
            rag_search.search,
            query,
            top_k=top_k,
            include_graph_expansion=True,
            exclude_doc_id=actx.doc_id,
            current_doc_overrides=overrides,
            vault_id=actx.vault,
        )
        chunks = result.get("chunk_results") or []
        if not chunks:
            return {"ctx": {"passages_block": ""}, "sources": []}

        # The LLM benefits from seeing multiple passages from the same doc
        # (different sections). The UI does not - collapse the source list to
        # one entry per path. chunk_results is already RRF-ordered, so the
        # first-seen header is the highest-ranked one.
        seen: set[str] = set()
        sources: list[dict] = []
        for c in chunks:
            s = _to_source(c)
            if s["path"] in seen:
                continue
            seen.add(s["path"])
            sources.append(s)

        return {
            "ctx": {"passages_block": _format_passages(chunks)},
            "sources": sources,
        }
    return provider


# ---------------------------------------------------------------------------
# User-message templates
# ---------------------------------------------------------------------------
# All templates take (before, selection, after, ctx). The ctx dict carries
# whatever the command's providers produced; commands without providers
# simply ignore it.

def _continue_user(before: str, selection: str, after: str, ctx: dict) -> str:
    parts = [ctx["doc_context"], "Write the continuation now."]
    return "\n\n".join(parts)


def _continue_sources_user(before: str, selection: str, after: str, ctx: dict) -> str:
    parts = [ctx["doc_context"]]
    block = ctx.get("passages_block") or ""
    if block:
        parts.append(
            "Relevant passages from your other notes (use as factual grounding; "
            "paraphrase, do not quote verbatim, do not list inline):\n" + block
        )
    parts.append("Write the continuation now.")
    return "\n\n".join(parts)


def _surrounding_blocks(before: str, after: str, caveat: str) -> str:
    """Render the text bracketing the working range as labeled reference blocks
    (or "" when there is nothing on either side).

    `caveat` is load-bearing, not decoration: without an explicit "do not do X
    with this" a small model cheerfully rewrites the reference text back at you
    and the result overwrites the surrounding paragraphs.
    """
    blocks = []
    if before.strip():
        blocks.append(f"Before:\n```\n{before}\n```")
    if after.strip():
        blocks.append(f"After:\n```\n{after}\n```")
    if not blocks:
        return ""
    return f"Surrounding text ({caveat}):\n\n" + "\n\n".join(blocks)


def _rewrite_user(before: str, selection: str, after: str, ctx: dict) -> str:
    parts = []
    ctx_block = _surrounding_blocks(
        before, after, "for voice/tone reference only - do not rewrite this")
    if ctx_block:
        parts.append(ctx_block)
    parts.append(f"Text to rewrite:\n```\n{selection}\n```")
    parts.append("Output only the rewritten text.")
    return "\n\n".join(parts)


def _voice_rewrite_user(verb: str):
    """Builder for tighten/loosen-style commands.

    Includes surrounding context purely as a voice/tone reference,
    explicitly telling the model not to copy it.
    """
    def template(before: str, selection: str, after: str, ctx: dict) -> str:
        parts = []
        ctx_block = _surrounding_blocks(
            before, after, "for voice/tone reference only - do not copy")
        if ctx_block:
            parts.append(ctx_block)
        parts.append(f"Text to {verb}:\n```\n{selection}\n```")
        parts.append(f"Output only the {verb}ed text.")
        return "\n\n".join(parts)
    return template


def _structural_user(label: str, target: str):
    """Builder for pure structural transforms (list↔table, outline↔prose).

    Surrounding context is irrelevant; only the selection matters.
    """
    def template(before: str, selection: str, after: str, ctx: dict) -> str:
        return (
            f"Selected {label}:\n```\n{selection}\n```\n\n"
            f"Output only the {target}."
        )
    return template


# ---------------------------------------------------------------------------
# Command definition
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WritingCommand:
    id: str
    label: str
    range_source: str   # "cursor" | "selection"
    operation: str      # "insert" | "replace"
    # system_prompt + user_template are only used by kind="llm" commands.
    # Non-LLM kinds (e.g. "autolink") leave them None.
    system_prompt: str | None = None
    user_template: Callable[[str, str, str, dict], str] | None = None
    context_providers: tuple = ()
    model: str | None = None
    context_budget_tokens: int | None = None
    # Dispatch discriminator. "llm" routes through the token-stream path;
    # other values route through their own pipeline inside stream_assist
    # (e.g. "autolink" runs lexical+semantic candidate search and emits a
    # one-shot `candidates` SSE event). Frontend reads this from
    # /api/edit/commands to choose how to invoke the command.
    kind: str = "llm"


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_CONTINUE_SYS = (
    "You are continuing a markdown document mid-flow. "
    "Write 1-3 sentences that follow naturally from the preceding text. "
    "Match the document's voice, tone, and level of formality. "
    "If the preceding text ends mid-sentence, finish the sentence first. "
    "Output ONLY the continuation text. No preamble, no quotes, no markdown fences, "
    "no commentary."
)

# _CONTINUE_SOURCES_SYS = (
#     "You are continuing a markdown document mid-flow, with relevant passages "
#     "from the author's other notes available as background. Use the passages "
#     "as background context where relevant, but write in the voice and flow "
#     "of the current document. Do not quote retrieved passages verbatim; "
#     "paraphrase. Do not list sources inline (the UI surfaces them separately). "
#     "Write 1-3 sentences that follow naturally from the preceding text. "
#     "If the preceding text ends mid-sentence, finish the sentence first. "
#     "Output ONLY the continuation text. No preamble, no quotes, no markdown fences."
# )
_CONTINUE_SOURCES_SYS = (
    "You are continuing a markdown document mid-flow, with relevant passages "
    "from the author's other notes available as background. Use the passages "
    "as background context where relevant, but write in the voice and flow "
    "of the current document. Do not quote retrieved passages verbatim; "
    "paraphrase, and provide a wikilink, [[/path/file | link text]], to the source, "
    "with link text being a natural part of the sentence."
    "Do not list sources if they are not used. "
    "Strive for 1-3 sentences that follow naturally from the preceding text, but you may use more if necessary. "
    "If the preceding text ends mid-sentence, finish the sentence first. "
    "Output ONLY the continuation text. No preamble, no markdown fences."
)


_REWRITE_SYS = (
    "You are rewriting a selected passage of a markdown document. "
    "Rewrite ONLY the selected passage. Do not rewrite the surrounding context shown for reference. "
    "Preserve the original meaning and information; improve clarity, flow, and concision. "
    "Match the voice of the surrounding text. Keep markdown formatting (links, emphasis, code) "
    "intact unless changing it improves the result. "
    "Output ONLY the rewritten passage. No preamble, no quotes, no commentary."
)

_OUTLINE_TO_PROSE_SYS = (
    "You are converting a markdown bulleted outline into flowing prose. "
    "Preserve every point. Use connective phrasing so the result reads as continuous text "
    "rather than a list. Match the surrounding voice. "
    "Output ONLY the prose passage. No preamble, no commentary, no headings."
)

_PROSE_TO_OUTLINE_SYS = (
    "You are converting a passage of prose into a markdown bulleted outline. "
    "Capture every distinct point as its own top-level bullet. "
    "Use sub-bullets for supporting details. Be terse - bullets, not full sentences. "
    "Preserve concrete facts, names, and numbers exactly. "
    "Output ONLY the markdown outline. No preamble, no commentary."
)

_TIGHTEN_SYS = (
    "You are tightening a selected passage of a markdown document. "
    "Remove filler words, hedging, and repetition. Preserve every concrete fact, "
    "name, and number. Preserve the original voice. "
    "Aim for 60-80% of the original length. "
    "Keep markdown formatting intact. "
    "Output ONLY the tightened text. No preamble, no commentary."
)

_LOOSEN_SYS = (
    "You are expanding a selected passage of a markdown document. "
    "Add concrete examples, clarifying detail, or fuller explanation where it helps. "
    "Do NOT add filler or padding. Preserve the original voice and structure. "
    "Keep markdown formatting intact. "
    "Output ONLY the expanded text. No preamble, no commentary."
)

_LIST_TO_TABLE_SYS = (
    "You are converting a markdown bulleted list into a markdown table. "
    "Infer column headers from the bullet content. If sub-bullets indicate paired "
    "attributes, use them as columns. If the list cannot reasonably be tabulated, "
    "output the original list unchanged. "
    "Output ONLY the table (or list, if untabulable). No preamble, no commentary."
)

_TABLE_TO_LIST_SYS = (
    "You are converting a markdown table into a markdown bulleted list. "
    "Each row becomes one top-level bullet using the first column's value as its label. "
    "Remaining columns become sub-bullets formatted as `<column header>: <value>`. "
    "Output ONLY the list. No preamble, no commentary."
)

_PROSE_TO_MERMAID_SYS = (
    "You are converting a prose description into a Mermaid diagram. "
    "Choose the diagram type that best matches the prose:\n"
    "- flowchart (LR or TD): processes, decisions, pipelines\n"
    "- sequenceDiagram: actors interacting over time, message exchanges\n"
    "- classDiagram: object/type structures and inheritance\n"
    "- stateDiagram-v2: state machines, lifecycle transitions\n"
    "- erDiagram: data entities and their relationships\n"
    "- gantt: schedules, timelines with durations\n"
    "- pie: proportional breakdowns\n"
    "- mindmap: hierarchical concepts radiating from a center\n"
    "Capture every entity, relationship, and step the prose describes. "
    "Use short, clear node labels; quote labels containing spaces or punctuation "
    "(e.g. `A[\"Long label\"]`). Avoid characters that break Mermaid parsing inside "
    "unquoted labels (parentheses, colons, slashes). "
    "Output ONLY a fenced Mermaid code block - start with a line containing exactly "
    "```mermaid, then the diagram body, then a closing ``` line. "
    "No preamble, no explanation, no surrounding prose."
)

_MERMAID_TO_PROSE_SYS = (
    "You are converting a Mermaid diagram into flowing prose. "
    "The selection is a fenced ```mermaid code block. Read the first directive "
    "line to identify the diagram type and translate accordingly:\n"
    "- flowchart / stateDiagram-v2: describe nodes and the transitions between them in order\n"
    "- sequenceDiagram: narrate the actors and their message exchanges chronologically\n"
    "- classDiagram / erDiagram: describe each type or entity and how they relate\n"
    "- gantt: describe each task or phase with its start and duration\n"
    "- pie: describe the proportional breakdown across slices\n"
    "- mindmap: describe the hierarchy starting from the root and radiating outward\n"
    "Preserve every node label, every edge, and every numeric value exactly as they "
    "appear in the diagram - no facts may be dropped in translation. "
    "Use connective phrasing so the result reads as continuous prose, not a list. "
    "Match the surrounding document voice. "
    "Output ONLY the prose passage. No preamble, no commentary, no headings, no markdown fences."
)

_TABLE_TO_MERMAID_SYS = (
    "You are converting a markdown table into the best-fit Mermaid chart. "
    "Inspect the table's data shape and choose exactly one diagram type:\n"
    "- pie: one category-label column plus one numeric-value column per row\n"
    "  (e.g. `| Browser | Share |`). Emit `pie title <title>` then one\n"
    "  `\"<label>\" : <number>` line per row.\n"
    "- xychart-beta: a categorical or ordered x-axis with one or more numeric\n"
    "  value columns (bar chart). Emit `xychart-beta`, then `title \"<title>\"`,\n"
    "  then `x-axis [<cat1>, <cat2>, ...]`, then `y-axis \"<label>\" <min> --> <max>`,\n"
    "  then one `bar [v1, v2, ...]` line per numeric column.\n"
    "- gantt: the table has a task/phase label plus start and end (or start +\n"
    "  duration) date columns. Emit `gantt`, `dateFormat YYYY-MM-DD`, an optional\n"
    "  `title`, and one `<task> : <start>, <end>` (or `, <duration>`) line per row.\n"
    "If the table cannot reasonably be charted (purely textual, too many "
    "heterogeneous columns, no numeric or date data), output the original "
    "markdown table unchanged.\n"
    "Use short, clear labels; quote any label containing spaces or punctuation. "
    "Avoid parentheses, colons, and slashes inside unquoted labels. "
    "Output ONLY a fenced Mermaid code block - start with a line containing exactly "
    "```mermaid, then the diagram body, then a closing ``` line - or, in the "
    "untabulable fallback case, output ONLY the original markdown table. "
    "No preamble, no explanation, no surrounding prose."
)


# ---------------------------------------------------------------------------
# Shared output guardrail (custom prompts + editor tools)
# ---------------------------------------------------------------------------
# The user-authored instruction (custom prompt) or editor-tool `# Prompt` supplies
# the *intent*; this constant is appended to pin the *output contract* the ghost-text
# accept/reject UI depends on. Load-bearing for small local models - keep it verbatim.
_EDITOR_GUARD_BASE = (
    "\n\nYour reply is applied DIRECTLY to a markdown document. "
    "Output ONLY the resulting text - no preamble, no explanation, no commentary, "
    "no surrounding quotes, and no markdown code fences unless the text "
    "is itself meant to be a fenced code block. "
)

# Operations that ADD text rather than displace it. They share one fallback
# clause and one seam concern; only their anchor differs (see _SEAM_ANCHOR).
_ADDITIVE_OPS = ("prepend", "append", "insert")

# The fallback clause is PER-OPERATION and the variants are NOT interchangeable.
# "Output the original text unchanged" is a safe no-op only when the output
# REPLACES its input; for an additive operation the output is added alongside the
# input, so the same sentence tells a stuck model to duplicate the document into
# itself.
_ADDITIVE_FALLBACK = ("Output ONLY the new text to be added - never echo the "
                      "surrounding document back. If you cannot perform the "
                      "request, output nothing at all.")
_EDITOR_GUARD_FALLBACK = {
    "replace": "If you cannot perform the request, output the original text unchanged.",
    "note": ("Write the entry itself, not a report about writing it. If you "
             "cannot perform the request, output nothing at all."),
    **{op: _ADDITIVE_FALLBACK for op in _ADDITIVE_OPS},
}

# Appended whenever the user message carries before/after reference blocks.
_EDITOR_GUARD_CONTEXT = (
    " Any text shown to you as surrounding context or as the document around the "
    "caret is there for REFERENCE ONLY - do not reproduce, rewrite, or continue "
    "it in your output."
)


def _editor_output_guard(operation: str = "replace", has_context: bool = False) -> str:
    """The shared output-shape guard, specialized for how the result is applied."""
    guard = _EDITOR_GUARD_BASE + _EDITOR_GUARD_FALLBACK.get(
        operation, _EDITOR_GUARD_FALLBACK["replace"])
    return guard + _EDITOR_GUARD_CONTEXT if has_context else guard


_CUSTOM_SYS_PREFIX = (
    "You are a writing assistant embedded in a markdown editor. Apply the user's "
    "instruction to the supplied text precisely and literally, changing nothing the "
    "instruction does not call for.\n\n"
    "User instruction:\n"
)


def _strip_caret_marker(text: str) -> str:
    """Backstop: the caret marker must never reach the document. Prompts say not
    to emit it, but a model that echoes its context back would otherwise paste a
    literal `<<CURSOR>>` into the user's page."""
    return text.replace("<<CURSOR>>", "") if text else text


# Where an additive operation's payload lands, named as an AssistContext field.
# All three are already in `content` coordinates, so no rebasing is needed. The
# CLIENT resolves the same three-way choice in DOCUMENT coordinates to place the
# real edit (`insertAt` in edit_assist.js) - keep the two in step.
_SEAM_ANCHOR = {
    "prepend": "selection_start",   # immediately before the range
    "append": "selection_end",      # immediately after it
    "insert": "cursor_offset",      # the caret itself, whatever the scope
}


def _seam_windows(actx: "AssistContext", operation: str) -> tuple[str, str]:
    """(before, after) at the point where `operation`'s result will land.

    Falls back to the windows the client shipped when the buffer or the offset
    isn't available, so a partial payload degrades instead of raising.
    """
    attr = _SEAM_ANCHOR.get(operation)
    pos = getattr(actx, attr) if attr else None
    if actx.content is None or pos is None:
        return actx.before, actx.after
    return _windows_at(actx.content, pos)


def _pad_insert_seam(text: str, before: str, after: str) -> str:
    """Give an added block the blank lines it needs on BOTH sides of the seam.

    The payload lands at its anchor literally, with nothing added around it, so a
    new paragraph otherwise welds onto the neighbor it butts against. Asking the
    model for the blank line does NOT work: models strip leading/trailing
    whitespace regardless of instruction, so this is computed rather than
    prompted for.

    `before`/`after` are the text on either side of that anchor (see
    `_seam_windows`). Both sides matter, and which one is load-bearing flips per
    operation: a `document` + `append` lands at the end of the buffer with
    nothing after it, a `document` + `prepend` lands at offset 0 with nothing
    before it.

    Fires only when the insertion point is at a block boundary - a blank line
    (or the edge of the buffer) on at least one side. An insertion in the middle
    of a line, which is what "Continue Writing" mid-sentence looks like, is left
    exactly alone; padding there would break the sentence in half.
    """
    if not text.strip():
        return text
    at_block_start = (not before) or before.endswith("\n\n")
    at_block_end = (not after) or after.startswith("\n\n")
    if not (at_block_start or at_block_end):
        return text

    # A single newline is a soft break in markdown, so two are needed to open a
    # new block. Count what each side of the seam already contributes.
    def _runs(s: str, at_end: bool) -> int:
        return len(s) - (len(s.rstrip("\n")) if at_end else len(s.lstrip("\n")))

    if before.strip():
        have = _runs(before, True) + _runs(text, False)
        if have < 2:
            text = "\n" * (2 - have) + text
    if after.strip():
        have = _runs(text, True) + _runs(after, False)
        if have < 2:
            text = text + "\n" * (2 - have)
    return text


def _custom_system(instruction: str, frontmatter: dict | None,
                   operation: str = "replace", has_context: bool = False) -> str:
    return (_CUSTOM_SYS_PREFIX + instruction.strip()
            + _editor_output_guard(operation, has_context) + _voice_hint(frontmatter))


# Editor tools run a tool-calling loop; cap it low so a model that keeps
# searching (small vault, vague query) can't spin to the global agent ceiling.
_EDITOR_MAX_ITERATIONS = 4

# The closing instruction for a cursor-scope tool. Nothing is selected, so the
# doc context (caret marked <<CURSOR>>) is all the model has to go on - and what
# it should DO with that differs per operation. A single "produce the text to
# insert" would tell an op:note tool to write insertable prose for a page it
# never touches, and an op:replace tool that it has nothing to replace.
_CURSOR_TASK = {
    "insert": ("Apply your directive at the caret marked <<CURSOR>>. Produce ONLY "
               "the text to be inserted at that exact point - it may fall in the "
               "middle of a sentence."),
    "replace": ("Apply your directive to the block containing the caret marked "
                "<<CURSOR>> - the paragraph the caret sits in. Produce ONLY the "
                "replacement for that block, not for the rest of the document."),
    "prepend": ("Apply your directive to the block containing the caret marked "
                "<<CURSOR>> - the paragraph the caret sits in. Produce ONLY the "
                "text to be placed immediately BEFORE that block; the block "
                "itself stays as it is."),
    "append": ("Apply your directive to the block containing the caret marked "
               "<<CURSOR>> - the paragraph the caret sits in. Produce ONLY the "
               "text to be placed immediately AFTER that block; the block itself "
               "stays as it is."),
    "note": ("Apply your directive to the document around the caret marked "
             "<<CURSOR>>. Your result is filed to a separate note page rather "
             "than inserted here, so produce the note entry itself."),
}

# Placement hint for the RANGE scopes (selection/document). Only prepend/append
# get one: a model asked to write an introduction has no other signal that its
# output is an introduction rather than a continuation. Deliberately NOT added
# for replace/note/insert - those prompts stay byte-identical to what already
# authored tools see.
_PLACEMENT_HINT = {
    "prepend": "Your result will be placed immediately BEFORE this text.",
    "append": "Your result will be placed immediately AFTER this text.",
}

# A model told to "output only the text" often still wraps the whole reply in a
# bare ``` fence (quoting habit). Inserted literally that corrupts the document,
# so strip a single BARE outer fence (no language) that wraps the ENTIRE output.
# Language-tagged fences (```python, ```mermaid) are left intact - those are
# almost always intentional (an editor tool that emits a code block).
_OUTER_BARE_FENCE_RE = re.compile(r"^```[ \t]*\r?\n(.*)\r?\n```$", re.DOTALL)


def _strip_outer_fence(text: str) -> str:
    if not text:
        return text
    m = _OUTER_BARE_FENCE_RE.match(text.strip())
    return m.group(1) if m else text


# ---------------------------------------------------------------------------
# Auto-link candidate search (kind="autolink")
# ---------------------------------------------------------------------------
# Two-pass design: a cheap lexical title match first, then a semantic
# document_search() pass when no lexical hit is found. The semantic pass
# augments the embedding query with a window of surrounding text, since
# embedding short selections (e.g. a 2-token noun phrase) yields noisy
# vectors. The selection itself remains the link anchor; surrounding text
# only seeds the search.

_AUTOLINK_CONTEXT_WINDOW = 150          # chars on each side of the selection
_AUTOLINK_SEMANTIC_THRESHOLD = 0.45     # max cosine distance accepted in pass 2
_AUTOLINK_TOP_K = 3


def _lexical_link_candidates(
    selection: str,
    exclude_doc_id: str | None,
) -> list[dict]:
    """Pass 1: case-insensitive exact title match against documents.title.

    Returns one candidate per matching document. Multiple hits are possible
    when titles aren't unique across folders - the picker UI surfaces all
    of them so the user disambiguates rather than us guessing.
    """
    from src import rag_search
    s = (selection or "").strip()
    if not s:
        return []
    conn = rag_search._get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT doc_id, title FROM documents
            WHERE LOWER(title) = LOWER(%s)
              AND doc_exists = TRUE
              AND doc_id IS DISTINCT FROM %s
            ORDER BY doc_id
            LIMIT 5
            """,
            (s, exclude_doc_id),
        )
        out = []
        for doc_id, title in cur.fetchall():
            wiki_path = doc_id[:-3] if doc_id.endswith(".md") else doc_id
            out.append({
                "doc_id": doc_id,
                "title": title,
                "wiki_path": wiki_path,
                "match_type": "lexical",
                "confidence": 1.0,
            })
        return out
    finally:
        conn.close()


def _semantic_link_candidates(
    selection: str,
    surrounding_context: str,
    exclude_doc_id: str | None,
    vault_id: str = DEFAULT_VAULT,
    threshold: float = _AUTOLINK_SEMANTIC_THRESHOLD,
    top_k: int = _AUTOLINK_TOP_K,
) -> list[dict]:
    """Pass 2: document-summary cosine search, filtered by distance threshold.

    `document_search` operates on per-document summary embeddings - the
    right granularity for auto-link, since "this term is *about* that
    page" maps to document-level meaning, not chunk-level mentions.
    """
    from src import rag_search
    s = (selection or "").strip()
    if not s:
        return []
    query_text = (surrounding_context.strip() + " " + s) if surrounding_context else s
    # Over-fetch by one when excluding so a self-hit doesn't push valid
    # candidates out of top_k.
    fetch_k = top_k + (1 if exclude_doc_id else 0)
    results = rag_search.document_search(query_text, top_k=fetch_k, vault_id=vault_id)
    out = []
    for r in results:
        doc_id = r.get("doc_id")
        if not doc_id or doc_id == exclude_doc_id:
            continue
        distance = r.get("distance")
        if distance is None or distance > threshold:
            continue
        wiki_path = doc_id[:-3] if doc_id.endswith(".md") else doc_id
        out.append({
            "doc_id": doc_id,
            "title": r.get("title") or wiki_path,
            "wiki_path": wiki_path,
            "match_type": "semantic",
            "confidence": round(max(0.0, 1.0 - float(distance)), 3),
        })
        if len(out) >= top_k:
            break
    return out


def _surrounding_context(
    before: str,
    after: str,
    window: int = _AUTOLINK_CONTEXT_WINDOW,
) -> str:
    """Join ~window chars from each side into a single string for embedding."""
    b = (before or "")[-window:]
    a = (after or "")[:window]
    return (b + " " + a).strip()


async def _run_autolink(actx: AssistContext) -> AsyncGenerator[str, None]:
    """Pipeline for kind="autolink": lexical pass, then semantic on miss.

    Emits exactly one structured event:
      {"candidates": [...], "autolink_match_type": "lexical"|"semantic"|"none"}
    followed by {"done": True}. No token events.
    """
    sel = actx.selection.strip()
    if not sel:
        yield _sse({"error": "Empty selection"})
        return

    candidates = await asyncio.to_thread(
        _lexical_link_candidates, sel, actx.doc_id,
    )
    match_type = "lexical" if candidates else "none"

    if not candidates:
        ctx_text = _surrounding_context(actx.before, actx.after)
        candidates = await asyncio.to_thread(
            _semantic_link_candidates, sel, ctx_text, actx.doc_id, actx.vault,
        )
        if candidates:
            match_type = "semantic"

    yield _sse({"candidates": candidates, "autolink_match_type": match_type})
    yield _sse({"done": True})


# ---------------------------------------------------------------------------
# Cite-this-claim candidate search (kind="cite")
# ---------------------------------------------------------------------------
# Selection-mode command: take a sentence containing a factual claim,
# search the corpus at chunk granularity (a claim is supported by a
# specific paragraph, not a whole document), and surface up to top_k
# supporting passages for the user to pick. The frontend turns the chosen
# candidate into a markdown footnote ([^cite-N] inline + definition).
#
# Graph expansion is intentionally disabled here: citations want precision
# over recall, and surfacing graph-expanded neighbors would dilute the
# top-k with passages that are linked-but-not-supportive.

_CITE_TOP_K = 3
_CITE_DISTANCE_THRESHOLD = 0.55     # generous; user picks final
_CITE_SNIPPET_CHARS = 240           # picker preview
_CITE_QUOTE_CHARS = 120             # quoted in the footnote definition

_SLUG_NONWORD = re.compile(r"[^\w]+", re.UNICODE)
_WHITESPACE_RUN = re.compile(r"\s+")


def _slugify_heading(text: str) -> str:
    """Approximate python-markdown's TOC slug: lowercase, non-word -> '-'.

    Matches the toc extension's default slugify closely enough for deep-link
    anchors generated by the wiki's render pipeline. Edge cases (Unicode
    normalization, custom slug functions) aren't handled - if those become
    a problem we'd switch to importing `markdown.extensions.toc.slugify`.
    """
    s = _SLUG_NONWORD.sub("-", text.strip().lower()).strip("-")
    return s


def _clean_quote(content: str, max_chars: int) -> str:
    """Trim a chunk to a short, single-line quote suitable for a footnote def.

    Collapses whitespace runs (including newlines) so the quote doesn't
    break the `[^id]: ...` continuation, and escapes characters that would
    otherwise terminate the markdown link/footnote syntax.
    """
    if not content:
        return ""
    s = _WHITESPACE_RUN.sub(" ", content).strip()
    if len(s) > max_chars:
        s = s[:max_chars].rstrip()
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("]", "\\]")


def _cite_candidates(
    claim: str,
    surrounding_context: str,
    exclude_doc_id: str | None,
    vault_id: str = DEFAULT_VAULT,
    threshold: float = _CITE_DISTANCE_THRESHOLD,
    top_k: int = _CITE_TOP_K,
) -> list[dict]:
    """Hybrid chunk search for a claim, projected into citation candidates.

    Uses the same `rag_search.search()` entry point as the corpus_passages
    provider, but at smaller top_k and with graph expansion disabled. The
    surrounding sentence/paragraph is concatenated into the query so the
    embedding has enough context - short claims (e.g. "memory bandwidth is
    256 GB/s") embed poorly on their own.
    """
    print("[_cite_candidates] ", claim)
    print("[_cite_candidates] ", surrounding_context)
    from src import rag_search
    s = (claim or "").strip()
    if not s:
        return []
    query_text = (s + " " + surrounding_context.strip()) if surrounding_context else s
    result = rag_search.search(
        query_text,
        top_k=top_k + 1,
        include_graph_expansion=False,
        include_document_search=False,
        exclude_doc_id=exclude_doc_id,
        vault_id=vault_id,
    )
    chunks = result.get("chunk_results") or []
    out: list[dict] = []
    seen_docs: set[str] = set()
    #print("[_cite_candidates] ", chunks)
    for r in chunks:
        doc_id = r.get("doc_id")
        if (not doc_id) or (doc_id == exclude_doc_id):
            print(f"[_cite_candidates] Rejected {doc_id} because in exlude doc id {exclude_doc_id}")
            continue
            
        distance = r.get("vector_distance")
        if distance is not None and distance > threshold:
            print(f"[_cite_candidates] Rejected {doc_id} because distance {distance} > threshold {threshold} ")
            continue
            
        # One candidate per source doc - multiple chunks from the same
        # page in a 3-result picker would feel redundant.
        if doc_id in seen_docs:
            continue

        content = r.get("content", "") or ""
        header_path = r.get("header_path", [""]) or [""]
        print(f"[_cite_candidates] header_path = {header_path}")
        leaf_heading = header_path[-1] if header_path else ""
        wiki_path = doc_id[:-3] if doc_id.endswith(".md") else doc_id
        # vector distance isn't really comparable to rff_score
        # but it's something
        confidence = (
            round(max(0.0, 1.0 - float(distance)), 3)
            if distance is not None
            else round(float(r.get("rrf_score") or 0.0), 4)
        )
        out.append({
            "doc_id": doc_id,
            "title": r.get("doc_title", "") or wiki_path,
            "wiki_path": wiki_path,
            "header_path": header_path,
            "header_anchor": _slugify_heading(leaf_heading) if leaf_heading else "",
            "snippet": content[:_CITE_SNIPPET_CHARS],
            "quote": _clean_quote(content, _CITE_QUOTE_CHARS),
            "match_type": "semantic",
            "confidence": confidence,
            "chunk_id": r.get("chunk_id"),
        })
        seen_docs.add(doc_id)
        if len(out) >= top_k:
            break
        print("[_cite_candidates] ", out)
    return out


async def _run_cite(actx: AssistContext) -> AsyncGenerator[str, None]:
    """Pipeline for kind="cite": chunk-level retrieval, no LLM step.

    Emits exactly one structured event:
      {"candidates": [...], "cite_match_type": "semantic"|"none"}
    followed by {"done": True}. No token events.
    """
    sel = actx.selection.strip()
    if not sel:
        yield _sse({"error": "Empty selection"})
        return

    ctx_text = _surrounding_context(actx.before, actx.after)
    candidates = await asyncio.to_thread(
        _cite_candidates, sel, ctx_text, actx.doc_id, actx.vault,
    )
    match_type = "semantic" if candidates else "none"
    yield _sse({"candidates": candidates, "cite_match_type": match_type})
    yield _sse({"done": True})


# ---------------------------------------------------------------------------
# Wrap-as-admonition (kind="admonition")
# ---------------------------------------------------------------------------
# Selection-mode command: classify the selected passage into one Obsidian-
# style callout type, then wrap it as `> [!type]\n> body…`. The LLM only
# *picks* the type - wrapping is deterministic Python so prose is preserved
# byte-for-byte. Output is emitted as a single synthetic token event so the
# existing ghost-text accept/reject UI handles it with no frontend changes.
#
# Rendered downstream by ObsidianCalloutExtension
# (app/src/markdown_extensions.py); CSS for every type lives in
# app/template/default/tzara.css.

_ADMONITION_TYPES = (
    "note", "abstract", "summary", "tldr", "info", "todo",
    "tip", "hint", "important",
    "success", "check", "done",
    "question", "help", "faq",
    "warning", "caution", "attention",
    "failure", "fail", "missing",
    "danger", "error", "bug",
    "example", "quote",
)
_ADMONITION_TYPE_SET = frozenset(_ADMONITION_TYPES)
_ADMONITION_FALLBACK = "note"

_ADMONITION_SYS = (
    "You classify a short passage of markdown into one admonition type. "
    "Allowed types (lowercase, choose exactly one):\n"
    + ", ".join(_ADMONITION_TYPES) + ".\n"
    "Pick the type whose semantics best fit the passage. "
    "Output ONLY the single type word, lowercase, no punctuation, no explanation."
)

_ADMONITION_WORD_RE = re.compile(r"[a-z]+")


def _normalize_admonition_type(raw: str) -> str:
    """Pull the first lowercase alphabetic run from the LLM response and
    validate it against the whitelist. Falls back to `note` on miss.

    Handles common LLM failure modes: trailing punctuation, surrounding
    quotes, leading bullet/numbering, capitalization.
    """
    if not raw:
        return _ADMONITION_FALLBACK
    m = _ADMONITION_WORD_RE.search(raw.lower())
    if not m:
        return _ADMONITION_FALLBACK
    word = m.group(0)
    return word if word in _ADMONITION_TYPE_SET else _ADMONITION_FALLBACK


def _wrap_as_admonition(selection: str, type_: str) -> str:
    """Wrap `selection` as `> [!type]\\n> line…`.

    Empty lines render as bare `>` (no trailing space) so the blockquote
    stays contiguous - a fully blank line would otherwise terminate the
    blockquote and split the callout in the rendered output.
    """
    lines = selection.splitlines() or [""]
    body = "\n".join(("> " + ln) if ln else ">" for ln in lines)
    return f"> [!{type_}]\n{body}"


async def _run_admonition(actx: AssistContext) -> AsyncGenerator[str, None]:
    """Pipeline for kind="admonition": LLM-classify, server-wrap.

    Emits one synthetic token event carrying the full wrapped block, then
    `done`. The frontend's default LLM-streaming path handles this with no
    special-casing.
    """
    sel = actx.selection
    if not sel.strip():
        yield _sse({"error": "Empty selection"})
        return

    messages = [{"role": "user", "content": f"Passage:\n```\n{sel}\n```"}]
    model = LLM_MODEL
    parts: list[str] = []
    try:
        async for token in actx.llm_mgr.chat_stream(
            messages, system=_ADMONITION_SYS, model=model,
        ):
            parts.append(token)
    except Exception as e:
        yield _sse({"error": str(e)})
        return

    type_ = _normalize_admonition_type("".join(parts))
    wrapped = _wrap_as_admonition(sel, type_)
    yield _sse({"token": wrapped})
    yield _sse({"done": True})


# ---------------------------------------------------------------------------
# Checklist toggle (kind="checklist_toggle")
# ---------------------------------------------------------------------------
# Selection-mode commands: convert a plain markdown list to a GFM task
# list (`- [ ] foo`) and back. Pure syntactic transform - no LLM is
# involved. The dispatch coroutine emits a single synthetic `token`
# event so the frontend's default ghost-text accept/reject UI handles it
# unchanged, exactly like the admonition pipeline above.

# Matches a markdown list line: optional indent, bullet marker
# (-, *, +, or N.), one space, then the item body. The body is
# captured so we can inspect/rewrite its leading checkbox token.
_LIST_LINE_RE = re.compile(
    r"^(?P<indent>\s*)(?P<marker>[-*+]|\d+\.)\s+(?P<body>.*)$"
)
# Leading checkbox token inside a list-item body, e.g. "[ ] " or "[x] ".
_CHECKBOX_PREFIX_RE = re.compile(r"^\[[ xX]\]\s+")


def _list_to_checklist(text: str) -> str:
    """Add `[ ]` after the bullet marker on every list line.

    Idempotent: lines whose body already starts with `[ ]`/`[x]` are
    passed through untouched. Non-list lines (blank rows, paragraphs
    accidentally caught in the selection) are also untouched.
    """
    out = []
    for line in text.splitlines():
        m = _LIST_LINE_RE.match(line)
        if not m or _CHECKBOX_PREFIX_RE.match(m.group("body")):
            out.append(line)
            continue
        out.append(f"{m.group('indent')}{m.group('marker')} [ ] {m.group('body')}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def _checklist_to_list(text: str) -> str:
    """Strip the leading `[ ]`/`[x]`/`[X]` token from every list line.

    Completion state is dropped (per the agreed design); a finished
    `- [x] foo` becomes a plain `- foo`.
    """
    out = []
    for line in text.splitlines():
        m = _LIST_LINE_RE.match(line)
        if not m:
            out.append(line)
            continue
        body = _CHECKBOX_PREFIX_RE.sub("", m.group("body"))
        out.append(f"{m.group('indent')}{m.group('marker')} {body}")
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


async def _run_checklist_toggle(
    actx: AssistContext, command_id: str,
) -> AsyncGenerator[str, None]:
    """Pipeline for kind="checklist_toggle": deterministic regex transform.

    Emits the entire result as one synthetic `token` event, then `done`.
    No LLM call, no Ollama dependency.
    """
    sel = actx.selection
    if not sel.strip():
        yield _sse({"error": "Empty selection"})
        return
    if command_id == "list_to_checklist":
        result = _list_to_checklist(sel)
    else:
        result = _checklist_to_list(sel)
    yield _sse({"token": result})
    yield _sse({"done": True})


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


COMMANDS: dict[str, WritingCommand] = {
    "continue": WritingCommand(
        id="continue",
        label="Continue Writing",
        range_source="cursor",
        operation="insert",
        system_prompt=_CONTINUE_SYS,
        user_template=_continue_user,
        #model=temp_model,
    ),
    "continue_with_sources": WritingCommand(
        id="continue_with_sources",
        label="Continue (grounded in notes)",
        range_source="cursor",
        operation="insert",
        system_prompt=_CONTINUE_SOURCES_SYS,
        user_template=_continue_sources_user,
        context_providers=(corpus_passages(top_k=5),),
        #model=temp_model,
    ),
    "rewrite": WritingCommand(
        id="rewrite",
        label="Rewrite Selection",
        range_source="selection",
        operation="replace",
        system_prompt=_REWRITE_SYS,
        user_template=_rewrite_user,
        #model=temp_model,
    ),
    "outline_to_prose": WritingCommand(
        id="outline_to_prose",
        label="Outline → Prose",
        range_source="selection",
        operation="replace",
        system_prompt=_OUTLINE_TO_PROSE_SYS,
        user_template=_structural_user("outline", "prose"),
        #model=temp_model,
    ),
    "prose_to_outline": WritingCommand(
        id="prose_to_outline",
        label="Prose → Outline",
        range_source="selection",
        operation="replace",
        system_prompt=_PROSE_TO_OUTLINE_SYS,
        user_template=_structural_user("prose", "outline"),
        #model=temp_model,
    ),
    "tighten": WritingCommand(
        id="tighten",
        label="Tighten",
        range_source="selection",
        operation="replace",
        system_prompt=_TIGHTEN_SYS,
        user_template=_voice_rewrite_user("tighten"),
        #model=temp_model,
    ),
    "loosen": WritingCommand(
        id="loosen",
        label="Loosen",
        range_source="selection",
        operation="replace",
        system_prompt=_LOOSEN_SYS,
        user_template=_voice_rewrite_user("loosen"),
        #model=temp_model,
    ),
    "list_to_table": WritingCommand(
        id="list_to_table",
        label="List → Table",
        range_source="selection",
        operation="replace",
        system_prompt=_LIST_TO_TABLE_SYS,
        user_template=_structural_user("list", "table"),
        #model=temp_model,
    ),
    "table_to_list": WritingCommand(
        id="table_to_list",
        label="Table → List",
        range_source="selection",
        operation="replace",
        system_prompt=_TABLE_TO_LIST_SYS,
        user_template=_structural_user("table", "list"),
        #model=temp_model,
    ),
    "list_to_checklist": WritingCommand(
        id="list_to_checklist",
        label="List → Checklist",
        range_source="selection",
        operation="replace",
        kind="checklist_toggle",
        # No system_prompt / user_template - kind="checklist_toggle"
        # routes through _run_checklist_toggle (pure Python, no LLM).
    ),
    "checklist_to_list": WritingCommand(
        id="checklist_to_list",
        label="Checklist → List",
        range_source="selection",
        operation="replace",
        kind="checklist_toggle",
    ),
    "prose_to_mermaid": WritingCommand(
        id="prose_to_mermaid",
        label="Prose → Mermaid Diagram",
        range_source="selection",
        operation="replace",
        system_prompt=_PROSE_TO_MERMAID_SYS,
        user_template=_structural_user("prose description", "mermaid diagram"),
        model=LLM_MODEL,
    ),
    "mermaid_to_prose": WritingCommand(
        id="mermaid_to_prose",
        label="Mermaid → Prose",
        range_source="selection",
        operation="replace",
        system_prompt=_MERMAID_TO_PROSE_SYS,
        user_template=_structural_user("mermaid diagram", "prose description"),
        model=LLM_MODEL,
    ),
    "table_to_mermaid": WritingCommand(
        id="table_to_mermaid",
        label="Table → Mermaid Chart",
        range_source="selection",
        operation="replace",
        system_prompt=_TABLE_TO_MERMAID_SYS,
        user_template=_structural_user("markdown table", "mermaid diagram"),
        #model=LLM_MODEL,
    ),
    "autolink": WritingCommand(
        id="autolink",
        label="Auto-link selection",
        range_source="selection",
        operation="replace",
        kind="autolink",
        # system_prompt + user_template intentionally omitted - kind="autolink"
        # routes through _run_autolink, not the LLM token-stream path.
    ),
    "cite_claim": WritingCommand(
        id="cite_claim",
        label="Cite this claim",
        range_source="selection",
        operation="replace",
        kind="cite",
        # system_prompt + user_template intentionally omitted - kind="cite"
        # routes through _run_cite, not the LLM token-stream path. The
        # frontend custom-handles the actual edit (footnote marker +
        # definition), so `operation` here is informational only.
    ),
    "wrap_admonition": WritingCommand(
        id="wrap_admonition",
        label="Wrap as admonition",
        range_source="selection",
        operation="replace",
        kind="admonition",
        # system_prompt + user_template intentionally omitted - kind="admonition"
        # routes through _run_admonition. The LLM only classifies the type;
        # the wrapping is deterministic Python (preserves the selection
        # byte-for-byte). Frontend has no special-case: it sees a single
        # synthetic token event and a done event, like any LLM command.
    ),
    # Custom prompt: the user types a one-off instruction; it becomes a runtime
    # system prompt (see _run_custom). Three variants so the menu can offer the
    # right scope for the caret state. All route through kind="custom".
    #   selection present -> "custom" (transform the selection, replace it)
    #   no selection      -> "custom_replace" (transform the whole buffer)
    #                     -> "custom_insert"  (generate text, insert at the caret)
    "custom": WritingCommand(
        id="custom",
        label="Prompt",
        range_source="selection",
        operation="replace",
        kind="custom",
    ),
    "custom_replace": WritingCommand(
        id="custom_replace",
        label="Prompt (replace)",
        range_source="document",
        operation="replace",
        kind="custom",
    ),
    "custom_insert": WritingCommand(
        id="custom_insert",
        label="Prompt (insert)",
        range_source="cursor",
        operation="insert",
        kind="custom",
    ),
}


def list_commands(vault: str | None = None) -> list[dict]:
    """Public metadata for the /api/edit/commands endpoint.

    Excludes server-internal fields (system prompt, user template, providers).
    Appends human-authored editor tools discovered in the system vault's
    `editors/` folder (id `editor:<slug>`, kind `editor`); invalid ones are
    dropped so a malformed file never breaks the menu.

    `vault` = the vault of the file currently being edited. Editor tools whose
    `vaults:` whitelist excludes it are omitted (menu-visibility gate); passing
    None (unknown vault) shows all so the menu never silently empties.
    """
    out = [
        {
            "id": cmd.id,
            "label": cmd.label,
            "range_source": cmd.range_source,
            "operation": cmd.operation,
            "kind": cmd.kind,
        }
        for cmd in COMMANDS.values()
    ]
    try:
        from src import editor_registry
        for tool in editor_registry.list_editor_tools():
            if not tool.valid:
                continue
            if not tool.available_in(vault):
                continue
            out.append({
                "id": f"editor:{tool.slug}",
                "label": tool.label,
                "description": tool.description,
                # scope IS the range the frontend gathers & applies to - the two
                # vocabularies are 1:1 ("selection" | "document" | "cursor"), and
                # parse-time validation against _VALID_SCOPES already dropped
                # anything else (invalid tools are skipped above).
                "range_source": tool.scope,
                "operation": tool.operation,
                "kind": "editor",
            })
    except Exception:
        logger.exception("failed to enumerate editor tools")
    return out


# ---------------------------------------------------------------------------
# Custom prompt (kind="custom") + editor tools (id "editor:<slug>")
# ---------------------------------------------------------------------------

async def _run_custom(actx: "AssistContext", cmd: WritingCommand, instruction: str):
    """One-shot LLM transform driven by a user-typed instruction.

    The instruction becomes the system prompt (+ shared output guard); the input
    span is chosen by the command's scope: the selection (`custom`), the whole
    buffer (`custom_replace`), or the caret neighborhood for a generative insert
    (`custom_insert`). Streams tokens through the same ghost-text path as the
    built-in LLM commands."""
    instruction = (instruction or "").strip()
    if not instruction:
        yield _sse({"error": "Empty instruction"})
        return

    if cmd.range_source == "selection":
        user_msg = "Text to transform:\n```\n" + actx.selection + "\n```"
        has_context = False
    elif cmd.range_source == "document":
        user_msg = "Full document to transform:\n```\n" + (actx.content or "") + "\n```"
        has_context = False
    else:  # cursor -> generative insert at the caret
        user_msg = (
            "Document around the caret (the caret is marked <<CURSOR>>). "
            "Produce ONLY the text to insert at <<CURSOR>>:\n"
            "```\n" + actx.before + "<<CURSOR>>" + actx.after + "\n```"
        )
        has_context = True

    system = _custom_system(instruction, actx.frontmatter, cmd.operation, has_context)

    messages = [{"role": "user", "content": user_msg}]
    model = cmd.model or LLM_MODEL
    # Buffer the full reply so we can strip a bare wrapping fence before it lands
    # in the document (streaming can't retroactively remove a leading ``` line).
    # The status event keeps the ghost widget from looking dead while we wait.
    yield _sse({"status": "Working…"})
    parts: list[str] = []
    try:
        async for token in actx.llm_mgr.chat_stream(messages, system=system, model=model):
            parts.append(token)
    except Exception as e:
        yield _sse({"error": str(e)})
        return
    out = _strip_caret_marker(_strip_outer_fence("".join(parts)))
    if cmd.operation in _ADDITIVE_OPS:
        out = _pad_insert_seam(out, *_seam_windows(actx, cmd.operation))
    yield _sse({"token": out})
    yield _sse({"done": True})


def _build_editor_log(tool_def, slug, vault, path, input_text, run_result,
                      prior_memory, log_ctx, duration):
    """Assemble a per-invocation editor log page (markdown). Outcome/output are
    DERIVED from run_result + tool_def so the run path needs almost no threading;
    only the hard-error string and the consolidated memory are captured live. The
    memory before/after is the point of this - it's how the fuzzy consolidation
    behavior becomes inspectable. Ledger operations sit beside it as the other
    half of what a run remembered. Returns (ts, body)."""
    from src.agent_capabilities import uses_ledgers
    from src.background_agents import count_ledger_refusals, render_ledger_activity
    ts = timefmt.file_stamp(ms=True)

    def trunc(s, n=2000):
        s = s or ""
        return s if len(s) <= n else s[:n] + f"\n… (+{len(s) - n} more chars)"

    if log_ctx.get("error"):
        outcome = "error"
    elif run_result.stream_error:
        outcome = "stream_error"
    elif run_result.reached_max_steps:
        outcome = "reached_max_steps"
    elif not (run_result.final_text or "").strip():
        outcome = "empty_output"
    else:
        outcome = "ok"
    dest = "-"
    if outcome == "ok":
        dest = (f"note: _dada/editors/{slug}/{tool_def.output}"
                if tool_def.operation == "note"
                else f"document ({tool_def.operation})")

    led_during = log_ctx.get("ledger_during") or []
    led_reserved = log_ctx.get("ledger_reserved") or []
    # An editor granted the ledger tools keeps rows even with `memory:` off, so
    # the ledger half of the log is gated on either. Shared predicate: this used
    # to name only `memory` and worked by accident, via the activity lists.
    show_ledgers = bool(uses_ledgers(tool_def.memory, tool_def.capabilities)
                        or led_during or led_reserved)
    n_refused = count_ledger_refusals(led_during, led_reserved)
    lines = [
        f"| outcome | {outcome} |",
        "|---|---|",
        f"| vault | {vault} |",
        f"| document | {path or '-'} |",
        f"| scope | {tool_def.scope} |",
        f"| operation | {tool_def.operation} |",
        f"| output | {dest} |",
        f"| duration | {duration:.1f}s |",
        f"| tool calls | {len(run_result.activity_log)} |",
        f"| reached_max_steps | {run_result.reached_max_steps} |",
        f"| memory | {'on' if tool_def.memory else 'off'} |",
        *([f"| ledger ops | {len(led_during) + len(led_reserved)}"
           f"{f' - **{n_refused} REFUSED**' if n_refused else ''} |"]
          if show_ledgers else []),
        "",
        f"## Input ({tool_def.scope})",
        "```", trunc(input_text), "```",
        "",
        "## Activity",
        *([f"- {a}" for a in run_result.activity_log] or ["- (no tool calls)"]),
    ]
    if log_ctx.get("error"):
        lines += ["", "## Error", "```", str(log_ctx["error"]), "```"]
    if (run_result.final_text or "").strip():
        lines += ["", "## Result", "", trunc(run_result.final_text)]
    if tool_def.memory:
        lines += ["", "## Memory before", "", (trunc(prior_memory) or "(none)")]
        after = log_ctx.get("memory_after")
        lines += ["", "## Memory after",
                  "", (trunc(after) if after else "(unchanged - consolidation empty or failed)")]
    if show_ledgers:
        lines += render_ledger_activity(led_during, led_reserved,
                                        log_ctx.get("ledger_injection") or "")
    return ts, "\n".join(lines)


async def _broker_ledger_call(vault: str, slug: str, name: str, args: dict,
                              sink: list[str] | None = None) -> str:
    """Run a mid-loop `remember` / `forget` for an editor tool.

    Args are validated against the SAME registered schema the agent path uses, so
    the malformed shapes small models produce (a bare string for an array, a
    bracketed JSON string) are recovered identically; only the write is brokered.

    Applied notes are collected into `sink` for the run log. The agent path picks
    the same notes up from a run-scoped ContextVar inside apply_ledger_ops; here
    the write crosses a process boundary, so the reply is the only place they
    exist server-side.
    """
    from src.agent_capabilities import _registry, _validate_args

    spec = _registry().get(name)
    kwargs, errors = _validate_args(spec["def"], args or {})
    if errors:
        return f"{name}: " + "; ".join(errors)
    op = ({"ledger": kwargs["ledger"], "forget": True} if name == "forget"
          else {"ledger": kwargs["ledger"], "items": kwargs["items"]})
    resp = await _editor_broker_post(
        "/editor/memory", {"vault": vault, "slug": slug, "ledger_ops": [op]})
    notes = (resp.get("result") or {}).get("ledgers") or []
    if sink is not None:
        sink.extend(notes)
    return f"{name}: " + ("; ".join(notes) if notes else "done.")


def _editor_memory_rel(slug: str) -> str:
    """Vault-relative path of an editor's living memory store:
    `_dada/editors/{slug}/memory.md` (owned area, RAG-excluded)."""
    from config import AGENT_MEMORY_FILE, AGENT_OUTPUT_DIR
    return f"{AGENT_OUTPUT_DIR}/editors/{slug}/{AGENT_MEMORY_FILE}"


async def _editor_broker_post(route: str, payload: dict) -> dict:
    """POST to the worker's editor-kernel broker (the /editor/session/* routes on
    the worker's agent-API app). Service-authenticated (trusted server -> worker),
    reachable at AGENT_API_URL over tzara-net. Returns the parsed JSON body;
    raises on transport/HTTP error so callers can surface a clean failure."""
    import httpx

    from config import AGENT_API_URL, EDITOR_SERVICE_SECRET
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
        resp = await client.post(
            AGENT_API_URL + route, json=payload,
            headers={"X-Editor-Service-Secret": EDITOR_SERVICE_SECRET})
        resp.raise_for_status()
        return resp.json()


async def _run_editor_tool(llm_mgr, slug: str, actx: "AssistContext"):
    """Run a `type: editor` tool: a synchronous LLM tool-calling loop, streaming
    the transformed text into the ghost-text UI.

    Built-in read capabilities execute IN-PROCESS here (server-side, no kernel).
    Tier-2 custom Python tools execute in the isolated jupyter-agent kernel,
    BROKERED through the worker (`_editor_broker_post` -> editor_kernel), because
    the server can't reach agent-net directly. The loop itself stays here and owns
    the SSE stream; only the custom-tool CALLS cross to the worker.

    Loop event translation mirrors the chat path: `token`/`status`/`activity`/
    `retract` become the same SSE events, and the terminal turn's text is the
    accept/reject payload."""
    from src import editor_registry

    # Unpacked once: the rest of this function (capabilities, kernel session,
    # memory, note, log) addresses these individually.
    vault, path = actx.vault, actx.path
    selection, content, frontmatter = actx.selection, actx.content, actx.frontmatter

    tool_def = editor_registry.get_editor_tool(slug)
    if tool_def is None:
        yield _sse({"error": f"Unknown editor tool: {slug}"})
        return
    if not tool_def.valid:
        yield _sse({"error": f"Editor tool '{slug}' is misconfigured: "
                             + "; ".join(tool_def.errors)})
        return
    # Runtime guard: even though the menu already hides tools outside their
    # `vaults:` whitelist, re-check here so a direct/stale invocation can't run
    # an editor tool against a vault it wasn't authorized for.
    if not tool_def.available_in(vault):
        yield _sse({"error": f"Editor tool '{slug}' is not available in this vault"})
        return

    # Input span: selection tool -> the highlighted text; document tool -> the
    # whole unsaved buffer (the buffer is authoritative for the current doc,
    # which is why read_document/get_outline are NOT granted to editor tools).
    # A cursor tool has NO input span - nothing is selected - so it has nothing
    # to validate here; its context is the caret neighborhood, rendered below
    # through the same tiered _build_doc_context the built-in continuations use.
    scope = tool_def.scope
    if scope == "cursor":
        if content is None or actx.cursor_offset is None:
            yield _sse({"error": "Missing document content or cursor position"})
            return
        input_text = ""
    elif scope == "selection":
        input_text = selection
        if not input_text.strip():
            yield _sse({"error": "Empty selection"})
            return
    else:
        input_text = content or ""
        if not input_text.strip():
            yield _sse({"error": "Missing document content"})
            return

    import uuid

    from src.agent_capabilities import build_capability_map
    from src.agent_runner import AgentRunResult, run_agent_loop
    from src.background_agents import _build_tools_text, _narrate_tool_call
    from src.agent_capabilities import uses_ledgers
    from src.editor_registry import EDITOR_CAPABILITIES, EDITOR_LEDGER_CAPABILITIES

    cap_map = build_capability_map()
    # Defense-in-depth: parse already rejects out-of-set capabilities; re-filter
    # here so a stale/hand-edited def can never smuggle a write tool into an editor.
    granted = [c for c in tool_def.capabilities if c in EDITOR_CAPABILITIES and c in cap_map]
    # Symmetric with build_background_agent: reading your OWN ledgers is not a
    # granted capability, and the injected view below may have had to elide rows.
    if "recall" in cap_map and "recall" not in granted and uses_ledgers(
            tool_def.memory, tool_def.capabilities):
        granted.append("recall")
    tool_defs = [cap_map[c]["def"] for c in granted]
    tool_names = set(granted)

    # Tier 2 custom Python tools: schemas were derived statically at parse time
    # (agent_schema). They execute in the isolated jupyter-agent kernel, BROKERED
    # through the worker because the server can't reach agent-net (see
    # editor_kernel). Merge their schemas into the single tool list the model sees;
    # the built-in read capabilities keep running in-process here.
    custom_tool_names = {t["name"] for t in tool_def.custom_tools}
    tool_defs += [t["schema"] for t in tool_def.custom_tools]
    tool_names |= custom_tool_names

    editor_run_id = None
    if custom_tool_names:
        editor_run_id = f"editor-{uuid.uuid4().hex}"
        try:
            await _editor_broker_post("/editor/session/start", {
                "run_id": editor_run_id,
                "slug": slug,
                "vault": vault,
                "py_source": tool_def.py_source,
                "mode": "propose",
                # The range geometry travels with the text: a custom Python tool
                # that INSERTS needs to know where, and `document` alone can't
                # say. Offsets are in `document` coordinates (see AssistContext);
                # `editor.before`/`.after` are derived from them kernel-side
                # rather than shipped, so a large buffer crosses the wire once.
                "editor_data": {
                    "selection": selection or "",
                    "document": content or "",
                    "frontmatter": frontmatter or {},
                    "path": path or "",
                    "selection_start": actx.selection_start or 0,
                    "selection_end": actx.selection_end or 0,
                    "cursor": actx.cursor_offset or 0,
                },
            })
        except Exception as e:
            logger.exception("editor kernel session start failed for %s", slug)
            yield _sse({"error": f"Could not start the tool kernel: {e}"})
            return

    # Captured live; the rest of the log is derived in the finally. Declared here
    # because the tool dispatch below collects into it as the run proceeds.
    log_ctx = {"error": "", "memory_after": None,
               "ledger_during": [], "ledger_reserved": []}

    async def _execute(name, args, status_cb):
        # Custom Python tool -> the worker-brokered isolated kernel; ledger write
        # -> brokered too (owned-area writes belong to the worker, and the
        # in-process capability reads its owner from an agent run context this
        # path deliberately does not set); built-in read capability -> in-process.
        if name in custom_tool_names:
            resp = await _editor_broker_post("/editor/session/tool", {
                "run_id": editor_run_id, "name": name, "args": args or {}})
            return resp.get("result", f"{name}: (no result)")
        if name in EDITOR_LEDGER_CAPABILITIES:
            return await _broker_ledger_call(vault, slug, name, args,
                                             log_ctx["ledger_during"])
        if name == "recall":
            # Reads need no git and no worker, so this stays in-process - but it
            # cannot go through the shared capability, which resolves its owner
            # from the agent run context this path deliberately never sets. The
            # slug is applied here instead, where it is known and unforgeable.
            # Args still go through the ONE schema and the ONE coercer.
            from src.agent_capabilities import (RECALL_DEFAULT_ROWS, _registry,
                                                _validate_args)
            from src.background_agents import read_agent_ledgers
            from src.ledgers import recall_text
            kwargs, errors = _validate_args(_registry()["recall"]["def"], args or {})
            if errors:
                return "recall: " + "; ".join(errors)
            return recall_text(read_agent_ledgers(vault, f"editors/{slug}"),
                               kwargs.get("ledger", ""),
                               kwargs.get("max_rows", RECALL_DEFAULT_ROWS))
        return await cap_map[name]["execute"](name, args, vault, status_cb)

    # The user message is assembled BEFORE the system prompt because the output
    # guard depends on it: a model shown surrounding text has to be told not to
    # echo it back, and a model whose output is INSERTED must not be told to fall
    # back to "the original text unchanged".
    if scope == "cursor":
        # Same tiering the built-in continuations use: whole document with a
        # <<CURSOR>> marker when it fits, else outline + windows, else windows.
        # Clamped to the model's real window so an oversized configured budget
        # can't select a tier the backend then silently truncates.
        window = await llm_mgr.get_context_length()
        budget = min(LLM_EDIT_CONTEXT_BUDGET or window, window)
        tier, doc_block = _build_doc_context(content, actx.cursor_offset, budget)
        logger.debug("editor:%s cursor doc-context tier=%s", slug, tier)
        b, a = _range_windows(actx)
        log_input = f"{b}<<CURSOR>>{a}"
        user_msg = doc_block + "\n\n" + _CURSOR_TASK.get(
            tool_def.operation, _CURSOR_TASK["insert"])
        has_context = True
    else:
        label = "selected text" if scope == "selection" else "document"
        log_input = input_text
        user_msg = (f"Apply your directive to the following {label}:\n```\n"
                    + input_text + "\n```")
        hint = _PLACEMENT_HINT.get(tool_def.operation)
        if hint:
            user_msg += "\n\n" + hint
        has_context = False

    system = (tool_def.prompt.strip()
              + _editor_output_guard(tool_def.operation, has_context)
              + _voice_hint(frontmatter))
    if tool_defs:
        system += "\n\n" + _build_tools_text(tool_defs)

    # Cross-invocation memory (opt-in `memory:`): read the editor's living memory.md
    # and inject it so THIS run builds on what it has accumulated across prior
    # invocations/documents. Reading needs no git, so the server reads directly.
    # Imports here (function scope) are reused by the consolidation turn below.
    prior_memory = ""
    prior_ledgers: dict[str, list[str]] = {}
    mem_gen_tokens = 0
    if tool_def.memory:
        from config import AGENT_MEMORY_TURN_TIMEOUT_S
        from src.background_agents import _read_agent_memory, memory_budget
        from src.compaction import summarize_conversation
        from src.context_providers import MemoryProvider
        prior_memory = _read_agent_memory(vault, _editor_memory_rel(slug))
        # Same budget the agent path uses: the injection cap and the consolidation
        # turn's generation cap come from ONE figure, scaled to the model's context
        # window, so text generated past what will be injected is not thrown away.
        mem_inject_chars, mem_gen_tokens = await memory_budget(llm_mgr)
        # Editor-framed intro (NOT the agent "plan/decisions/use your tools" prose):
        # an editor keeps a running note across invocations, it doesn't crawl a vault.
        mem_section = MemoryProvider(
            prior_memory, char_cap=mem_inject_chars,
            heading="Your memory",
            intro=("This is the note you have kept across previous runs of this tool, "
                   "on this and other documents. Build on it - integrate it with the "
                   "passage you are working on now.")).render(10 ** 9)
        if mem_section:
            system += "\n\n" + mem_section
    # Ledgers are gated SEPARATELY from memory, exactly as on the agent side: the
    # tool has to SEE its durable rows before it can avoid repeating them, and the
    # write tools work without `memory:` - so an editor granted `remember` alone
    # was recording rows it could never read back. Reading needs no git, so the
    # server reads directly; only writing is brokered.
    if uses_ledgers(tool_def.memory, tool_def.capabilities):
        from src.background_agents import ledger_budget, read_agent_ledgers
        from src.context_providers import LedgerProvider
        prior_ledgers = read_agent_ledgers(vault, f"editors/{slug}")
        led_provider = LedgerProvider(
            prior_ledgers, char_cap=await ledger_budget(llm_mgr),
            heading="Your ledgers",
            intro=("Durable rows you recorded on earlier runs of this tool. "
                   "Unlike the note above they are maintained for you and cannot "
                   "be edited away by what you write now."))
        led_section = led_provider.render(10 ** 9)
        if led_section:
            system += "\n\n" + led_section
        if led_provider.stubbed:
            log_ctx["ledger_injection"] = (
                "> [!info] Ledgers too large to inject whole\n"
                "> Shown in part this run: "
                + ", ".join(f"`{n}` ({len(prior_ledgers[n])} rows)"
                            for n in led_provider.stubbed)
                + ".\n> The tool could read the rest with `recall`; prune or "
                  "`forget` a ledger to restore the full view.")

    messages = [{"role": "user", "content": user_msg}]

    run_result = AgentRunResult()
    import time
    _t0 = time.monotonic()
    try:
        try:
            async for evt in run_agent_loop(
                    messages=messages,
                    system_prompt=system,
                    tool_defs=tool_defs,
                    tool_names=tool_names,
                    llm_mgr=llm_mgr,
                    execute_tool=_execute,
                    status_label=lambda n: f"Running {n}…",
                    activity_narration=_narrate_tool_call,
                    stream_markers=("## Available Tools",),
                    think=AGENT_TOOL_THINK,
                    max_iterations=tool_def.max_iterations or _EDITOR_MAX_ITERATIONS,
                    log_label=f"editor:{slug} ",
            ):
                et = evt["type"]
                # Tool-loop tokens are NOT forwarded live: intermediate turns get
                # retracted, and the terminal text needs a bare-fence strip before
                # it can land in the document. We surface progress via status/
                # activity instead, and emit the cleaned terminal text once, below.
                if et in ("status", "activity"):
                    yield _sse({"status": evt["text"]})
                elif et == "result":
                    run_result = evt["result"]
        except Exception as e:
            logger.exception("editor tool %s failed", slug)
            log_ctx["error"] = str(e)
            yield _sse({"error": str(e)})
            return

        # Reserved memory turn (opt-in `memory:`): consolidate this run's transcript
        # into the editor's living memory.md. Fires on a normal completion and on
        # reached_max_steps (real work that just didn't finish - worth remembering),
        # but NOT on stream_error: a garbled model turn isn't worth remembering and
        # summarizing it risks OVERWRITING good memory with plausible-but-wrong text
        # (resiliency). Corruption backstop regardless: memory.md is git-committed on
        # every write, so a bad overwrite is always recoverable from history.
        # INDEPENDENT of the output operation; must NEVER fail the edit.
        if tool_def.memory and not run_result.stream_error:
            try:
                yield _sse({"status": "Updating memory…"})
                from src.memory_prompts import editor_instruction
                instruction = editor_instruction(
                    tool_def.memory_prompt, tool_def.label or slug, prior_memory)
                consolidated = await asyncio.wait_for(
                    summarize_conversation(messages, instruction, llm_mgr,
                                           max_tokens=mem_gen_tokens),
                    timeout=AGENT_MEMORY_TURN_TIMEOUT_S)
                if consolidated.strip():
                    log_ctx["memory_after"] = consolidated
                # Call 2 - the ledger backstop, same as the agent path. Runs even
                # when call 1 produced nothing (the two writes are independent),
                # and both ride ONE broker post so a run makes one commit.
                from config import AGENT_LEDGER_TURN_TIMEOUT_S
                from src.background_agents import ledger_ops_from_run
                ops = []
                try:
                    ops = await asyncio.wait_for(
                        ledger_ops_from_run(prior_ledgers, messages, consolidated,
                                            llm_mgr, label=f"editor:{slug}"),
                        timeout=AGENT_LEDGER_TURN_TIMEOUT_S)
                except Exception:   # noqa: BLE001 - never costs the note
                    logger.exception("editor ledger turn failed for %s", slug)
                if consolidated.strip() or ops:
                    resp = await _editor_broker_post("/editor/memory", {
                        "vault": vault, "slug": slug,
                        "text": consolidated, "ledger_ops": ops})
                    log_ctx["ledger_reserved"] = (
                        (resp.get("result") or {}).get("ledgers") or [])
            except Exception:
                logger.exception("editor memory consolidation failed for %s", slug)

        # Sentinels that must NEVER be inserted as the "transform".
        if run_result.stream_error:
            yield _sse({"error": "The model returned a malformed response."})
            return
        if run_result.reached_max_steps:
            yield _sse({"error": "The tool kept using its tools without producing a "
                                 "result (reached its step limit). Try a simpler request."})
            return

        clean = run_result.final_text or ""
        if "## Available Tools" in clean:   # tool-list regurgitation
            clean = ""
        clean = _strip_caret_marker(_strip_outer_fence(clean))
        if not clean.strip():
            yield _sse({"error": "The tool did not return any text."})
            return
        # An added block lands verbatim at its anchor; make sure it doesn't fuse
        # with the neighbors it now sits between. op:note goes to its own page and
        # op:replace swaps the span out - neither has a seam to protect. The
        # anchor comes from the operation alone, so this needs no scope branch.
        if tool_def.operation in _ADDITIVE_OPS:
            clean = _pad_insert_seam(
                clean, *_seam_windows(actx, tool_def.operation))

        # op:note routes the result to an external owned digest page (growing it
        # across calls) instead of applying it to the current document.
        if tool_def.operation == "note":
            try:
                resp = await _editor_broker_post("/editor/note", {
                    "vault": vault, "slug": slug, "output": tool_def.output,
                    "source": path or "note", "text": clean})
                written = (resp.get("result") or {}).get("path", tool_def.output)
            except Exception as e:
                logger.exception("op:note write failed for %s", slug)
                yield _sse({"error": f"Could not save the note: {e}"})
                return
            note_payload = {"note_saved": written}
            # If this editor also consolidated memory this run, offer a link to it
            # too (the digest is the append log; memory.md is the assimilated form).
            if log_ctx.get("memory_after"):
                note_payload["memory_saved"] = _editor_memory_rel(slug)
            yield _sse(note_payload)
            yield _sse({"done": True})
            return

        yield _sse({"token": clean})
        yield _sse({"done": True})
    finally:
        # Per-invocation log (opt-in `log:`) - covers every exit path (ok, error,
        # max-steps, empty). Records the memory before/after so the fuzzy
        # consolidation is inspectable. Must never mask the run's own outcome.
        if tool_def.log:
            try:
                ts, body = _build_editor_log(
                    tool_def, slug, vault, path, log_input, run_result,
                    prior_memory, log_ctx, time.monotonic() - _t0)
                await _editor_broker_post(
                    "/editor/log", {"vault": vault, "slug": slug, "ts": ts, "body": body})
            except Exception:
                logger.exception("editor log write failed for %s", slug)

        # Always tear down the isolated kernel (restart-between-runs, no bleed),
        # even on early return / client disconnect.
        if editor_run_id:
            try:
                await _editor_broker_post("/editor/session/close",
                                          {"run_id": editor_run_id})
            except Exception:
                logger.exception("editor kernel session close failed for %s", slug)


# ---------------------------------------------------------------------------
# Streaming dispatcher
# ---------------------------------------------------------------------------

async def stream_assist(
    llm_mgr,
    command_id: str,
    before: str = "",
    selection: str = "",
    after: str = "",
    frontmatter: dict | None = None,
    path: str | None = None,
    content: str | None = None,
    cursor_offset: int | None = None,
    selection_start: int | None = None,
    selection_end: int | None = None,
    vault: str = DEFAULT_VAULT,
    instruction: str = "",
) -> AsyncGenerator[str, None]:
    """Dispatch a writing-assistance command and stream tokens as SSE.

    Pipeline: validate → run providers → emit `sources` event → stream tokens.

    Cursor-mode commands require `content` + `cursor_offset` (full live
    document + caret). Selection-mode commands require `selection` plus
    `before`/`after` windows around it. Document-mode commands (whole-buffer
    replace) require `content`. `instruction` carries the user-typed directive
    for the custom-prompt commands.

    `selection_start`/`selection_end` locate the working range inside `content`;
    they coincide with `cursor_offset` when nothing is selected, which is what
    lets one code path serve both "text around the selection" and "text around
    the caret" (see `_range_windows`).
    """
    # Session boundary: every editor invocation is a one-shot with no history, so
    # each one is a fresh start on the server's slot - unlike chat, there is no
    # later turn to keep cheap, so arming unconditionally costs nothing beyond the
    # first-call reprocess it is buying.
    llm_backend.begin_cold_session()

    actx = AssistContext(
        before=before,
        selection=selection,
        after=after,
        path=path,
        doc_id=_path_to_doc_id(path),
        vault=vault,
        frontmatter=frontmatter,
        llm_mgr=llm_mgr,
        content=content,
        cursor_offset=cursor_offset,
        selection_start=selection_start,
        selection_end=selection_end,
    )

    # Editor tools are id `editor:<slug>`, resolved from the system-vault
    # registry (not the static COMMANDS dict). They run a synchronous
    # tool-calling loop over built-in read capabilities - see _run_editor_tool.
    # Their scope lives on the tool file rather than in COMMANDS, so they do
    # their own range validation and take the context object as-is.
    if command_id.startswith("editor:"):
        async for evt in _run_editor_tool(
                llm_mgr, command_id[len("editor:"):], actx):
            yield evt
        return

    cmd = COMMANDS.get(command_id)
    if not cmd:
        yield _sse({"error": f"Unknown command: {command_id}"})
        return

    if cmd.range_source == "selection" and not selection.strip():
        yield _sse({"error": "Empty selection"})
        return
    if cmd.range_source == "cursor" and (not content or cursor_offset is None):
        yield _sse({"error": "Missing document content or cursor position"})
        return
    if cmd.range_source == "document" and not (content or "").strip():
        yield _sse({"error": "Missing document content"})
        return

    # Cursor-mode: derive the `before`/`after` windows for retrieval-query
    # construction from the live document. Built-in SELECTION commands keep the
    # smaller windows the client shipped - their prompts are tuned around that
    # size - so this stays scoped to cursor mode rather than going through
    # _range_windows for everything.
    if cmd.range_source == "cursor":
        pos = max(0, min(cursor_offset, len(content)))
        actx = replace(
            actx,
            before=content[max(0, pos - _CURSOR_BEFORE_WINDOW):pos],
            after=content[pos:pos + _CURSOR_AFTER_WINDOW],
        )

    # Custom prompt: the user's instruction becomes the system prompt; one-shot
    # streaming through the same ghost-text path as the built-in LLM commands.
    if cmd.kind == "custom":
        async for evt in _run_custom(actx, cmd, instruction):
            yield evt
        return

    # Non-LLM kinds run their own pipeline and short-circuit the rest of
    # stream_assist (no providers, no doc_context, no Ollama call).
    if cmd.kind == "autolink":
        async for evt in _run_autolink(actx):
            yield evt
        return
    if cmd.kind == "cite":
        async for evt in _run_cite(actx):
            yield evt
        return
    if cmd.kind == "admonition":
        async for evt in _run_admonition(actx):
            yield evt
        return
    if cmd.kind == "checklist_toggle":
        async for evt in _run_checklist_toggle(actx, command_id):
            yield evt
        return

    ctx: dict = {}
    sources: list[dict] = []
    for provider in cmd.context_providers:
        try:
            out = await provider(actx)
        except Exception as e:
            yield _sse({"error": f"context provider failed: {e}"})
            return
        if not out:
            continue
        ctx.update(out.get("ctx") or {})
        sources.extend(out.get("sources") or [])

    if cmd.range_source == "cursor":
        # Edit context budget: per-command override > LLM_EDIT_CONTEXT_BUDGET opt-in >
        # the chat model's resolved window. Then clamp to that window so a stale/oversized
        # budget can't pick a doc-context tier the model silently truncates (mirrors the
        # chat-path measured-actual clamp). get_context_length() is cached + already
        # ask/measured/clamped, so the edit path inherits all of it for free.
        window = await llm_mgr.get_context_length()
        budget = cmd.context_budget_tokens or LLM_EDIT_CONTEXT_BUDGET or window
        budget = min(budget, window)
        tier, doc_block = _build_doc_context(content, cursor_offset, budget)
        ctx["doc_context"] = doc_block
        ctx["doc_context_tier"] = tier

    if sources:
        yield _sse({"sources": sources})

    system = cmd.system_prompt + _voice_hint(frontmatter)
    user_msg = cmd.user_template(actx.before, actx.selection, actx.after, ctx)
    messages = [{"role": "user", "content": user_msg}]

    model = cmd.model or LLM_MODEL
    try:
        async for token in llm_mgr.chat_stream(messages, system=system, model=model):
            yield _sse({"token": token})
        yield _sse({"done": True})
    except Exception as e:
        yield _sse({"error": str(e)})
