# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Streaming chat with agent loop - supports single-document and global corpus modes."""

import asyncio
import difflib
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from collections.abc import AsyncGenerator

from config import (
    AGENT_TOOL_THINK,
    CHARS_PER_TOKEN,
    CHAT_ENABLE_RUN_PYTHON,
    DEFAULT_VAULT,
    MIN_MESSAGES,
)
from src.compaction import (
    log_trim,
    log_history_stats,
    run_compaction_pipeline,
    select_checkpoint_span,
    apply_checkpoint,
    render_messages_for_summary,
    log_checkpoint_fold,
    CHECKPOINT_PREFIX,
)
from src.context_providers import (
    assemble_system_prompt,
    CoreInstructionsProvider,
    ToolDescriptionProvider,
    InstructionsProvider,
    DocumentContentProvider,
    PageDataFilesProvider,
)
from src import chunker
from src import llm_backend
from src.wikidoc import WikiDoc
from src.agent_runner import (
    MAX_AGENT_ITERATIONS,
    AgentRunResult,
    run_agent_loop,
)

logger = logging.getLogger("chat")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_TTL = 30 * 60   # 30 minutes
TOOL_RESULT_TRUNCATE = 2000
CONTENT_SIZE_LIMIT = 100 * 1024  # 100 KB
SHORT_DOC_THRESHOLD = 3000  # include full content in prompt if under this

# MAX_AGENT_ITERATIONS is re-exported from src.agent_runner (imported above) so
# existing references and test patch targets (chat.MAX_AGENT_ITERATIONS) keep working.

# Markers that indicate the model is repeating the system prompt
PROMPT_MARKERS = ["## Available Tools", "## Instructions", "## Document Outline"]


# ---------------------------------------------------------------------------
# Dynamic message-history trimming
# ---------------------------------------------------------------------------

def _trim_message_history(
    messages: list[dict],
    max_messages: int,
    system_prompt: str,
    context_length: int,
) -> list[dict]:
    """Trim message history to fit within both message-count and token-budget limits.

    First applies the max_messages cap (derived from model context size),
    then drops oldest messages if the total estimated character count
    still exceeds the context window budget. Response reserve scales
    proportionally with context size (~12.5%, floor of 512 tokens).
    """
    # 1. Apply message-count cap
    if len(messages) > max_messages:
        logger.info(f"Apply message count cap {len(messages)} > {max_messages}")
        messages = messages[-max_messages:]

    # 2. Character-budget check using actual content sizes
    response_reserve = max(512, context_length // 8)
    budget_chars = int((context_length - response_reserve) * CHARS_PER_TOKEN)
    system_chars = len(system_prompt)

    while len(messages) > MIN_MESSAGES:
        msg_chars = sum(len(m.get("content", "")) for m in messages)
        if system_chars + msg_chars <= budget_chars:
            break
        logger.info(f"Remove Message:")
        logger.info(messages[0])
        logger.info("End of Remove Message")
        messages = messages[1:]

    return messages


# ---------------------------------------------------------------------------
# Stream prefix filter
# ---------------------------------------------------------------------------

# StreamPrefixFilter moved to src.agent_runner (the shared engine). chat.py now
# passes PROMPT_MARKERS to run_agent_loop, which constructs the filter internally.


# ---------------------------------------------------------------------------
# Document Scratchpad
# ---------------------------------------------------------------------------

class DocumentScratchpad:
    """In-memory copy of a document for agent loop modifications."""

    def __init__(self, document_url_path: str, vault: str = DEFAULT_VAULT,
                 revision: str = ""):
        self.document_url_path = document_url_path
        self.vault = vault or DEFAULT_VAULT
        # Git commit the user is viewing (from ?revision= on the page). When set,
        # the baseline is that commit's content instead of the working tree, so
        # every content/section tool operates on what the user actually sees.
        self.revision = (revision or "").strip()
        self._original_content: str = ""
        self._working_content: str = ""
        self._title: str = ""
        self._loaded = False

    def load(self):
        """Snapshot the document as the baseline: the viewed git revision when one
        is set, otherwise the working tree."""
        wd = WikiDoc(self.document_url_path, vault=self.vault)
        rel = wd.relative_file_path()

        historic = None
        if self.revision and rel:
            historic = WikiDoc.read_text_at_revision(self.vault, rel, self.revision)
            if historic is None:
                # Unknown sha, or the file did not exist at that commit. Degrade to
                # the current version rather than erroring, matching what the view
                # route does when a revision fails to resolve.
                self.revision = ""

        if historic is not None:
            self._original_content = historic
            self._title = wd.file_name_no_ext() or self.document_url_path
        elif wd.exists():
            self._original_content = wd.get_content() or ""
            self._title = wd.file_name_no_ext() or self.document_url_path
        else:
            self._original_content = ""
            self._title = self.document_url_path
        self._working_content = self._original_content
        self._loaded = True

    @property
    def is_historical(self) -> bool:
        """True when the baseline came from a git revision, not the working tree.
        Only meaningful after load() - a revision that failed to resolve is cleared."""
        return bool(self.revision)

    def revision_info(self) -> dict:
        """Label data for the viewed revision ({} when not historical)."""
        if not self.is_historical:
            return {}
        wd = WikiDoc(self.document_url_path, vault=self.vault)
        rel = wd.relative_file_path()
        if not rel:
            return {}
        return WikiDoc.revision_info(self.vault, rel, self.revision)

    def head_drift(self) -> dict | None:
        """Describe the gap between the viewed revision and the current file.

        Applying a historical edit overwrites the CURRENT file, so when the two
        have diverged the user is approving more than the diff they were shown.
        Returns None when there is nothing to warn about (not historical, or the
        current file still matches the revision); otherwise the commit count since
        the revision plus the unified diff that will actually land on disk.
        """
        if not self.is_historical:
            return None
        wd = WikiDoc(self.document_url_path, vault=self.vault)
        rel = wd.relative_file_path()
        if not rel:
            return None
        pair = WikiDoc.read_text(self.vault, rel)
        head_content = pair[0] if pair else ""
        if head_content == self._original_content:
            return None

        info = WikiDoc.revision_info(self.vault, rel, self.revision)
        diff = difflib.unified_diff(
            head_content.splitlines(), self._working_content.splitlines(),
            fromfile='current', tofile='proposed', lineterm=''
        )
        return {
            "commits_since": info.get("commits_since", 0),
            "short_sha": info.get("short_sha", self.revision[:10]),
            "date_str": info.get("date_str", ""),
            "head_diff": '\n'.join(diff),
        }

    @property
    def content(self) -> str:
        return self._working_content

    @content.setter
    def content(self, value: str):
        self._working_content = value

    @property
    def original(self) -> str:
        return self._original_content

    @property
    def title(self) -> str:
        return self._title

    @property
    def has_changes(self) -> bool:
        return self._working_content != self._original_content

    def get_unified_diff(self) -> str:
        """Full unified diff between original and working content."""
        old_lines = self._original_content.splitlines()
        new_lines = self._working_content.splitlines()
        diff = difflib.unified_diff(
            old_lines, new_lines,
            fromfile='original', tofile='modified',
            lineterm=''
        )
        return '\n'.join(diff)

    async def apply_to_disk(self):
        """Write working content to disk and commit via the canonical primitive:
        checkpoint-before-mutate -> EOL-preserving write -> attributed git commit
        -> watcher debounce. Replaces the former set_content/save + inline git
        triple + hand-built debounce key."""
        wd = WikiDoc(self.document_url_path, vault=self.vault)
        rel = wd.relative_file_path()
        if not rel:
            return
        await asyncio.to_thread(
            WikiDoc.commit, self.vault, rel.replace(os.sep, "/"),
            self._working_content)


def _revision_label(scratchpad: DocumentScratchpad) -> str:
    """Human-readable revision label for the prompt ("" when on the current
    version), e.g. "a1b2c3d4 (2026-06-14 09:12:03)"."""
    if not scratchpad.is_historical:
        return ""
    info = scratchpad.revision_info()
    short = info.get("short_sha") or scratchpad.revision[:10]
    date_str = info.get("date_str") or ""
    return f"{short} ({date_str})" if date_str else short


class ScratchpadCollection:
    """Manages multiple DocumentScratchpads for global chat mode (one vault)."""

    def __init__(self, vault: str = DEFAULT_VAULT):
        self.vault = vault or DEFAULT_VAULT
        self._pads: dict[str, DocumentScratchpad] = {}

    def get_or_load(self, doc_url_path: str) -> DocumentScratchpad:
        """Get existing pad or create+load one for the given path."""
        if doc_url_path not in self._pads:
            pad = DocumentScratchpad(doc_url_path, self.vault)
            pad.load()
            self._pads[doc_url_path] = pad
        return self._pads[doc_url_path]

    def create_new(self, doc_url_path: str, initial_content: str) -> DocumentScratchpad:
        """Create a pad for a new (not-yet-on-disk) document."""
        pad = DocumentScratchpad(doc_url_path, self.vault)
        pad._original_content = ""
        pad._working_content = initial_content
        pad._title = doc_url_path.rsplit("/", 1)[-1] if "/" in doc_url_path else doc_url_path
        pad._loaded = True
        self._pads[doc_url_path] = pad
        return pad

    @property
    def has_changes(self) -> bool:
        return any(p.has_changes for p in self._pads.values())

    def get_all_diffs(self) -> list[dict]:
        """Return list of {path, diff_preview} for all modified pads."""
        diffs = []
        for path, pad in self._pads.items():
            if pad.has_changes:
                diffs.append({"path": path, "diff_preview": pad.get_unified_diff()})
        return diffs

    async def apply_all_to_disk(self):
        """Write all modified pads to disk and optionally git commit each."""
        for pad in self._pads.values():
            if pad.has_changes:
                await pad.apply_to_disk()


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

@dataclass
class ChatSession:
    session_id: str
    document_url_path: str = ""   # vault-relative doc path, empty for global mode
    vault: str = DEFAULT_VAULT    # the single vault this session is bound to
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_activity: float = field(default_factory=time.time)
    mode: str = "document"
    # The browser's window.location.pathname for the open page (e.g.
    # "/wiki/main/Sports/Baseball"). Sent with the chat request so the run_python
    # tool can attach to the EXACT same Jupyter kernel the page's own cells use
    # (kernels are keyed by this string); reconstructing it risks an encoding/.md
    # mismatch that would key a different, unshared kernel.
    page_id: str = ""
    # Git commit sha from ?revision= on the page being viewed, or "" for the
    # current version. Refreshed per turn like page_id (the user can navigate
    # between revisions without starting a new chat), so it is a property of the
    # request rather than of the session.
    revision: str = ""
    pending_scratchpad: DocumentScratchpad | None = None
    pending_collection: ScratchpadCollection | None = None
    # Code proposed by the agent's run_python tool, awaiting user approval.
    # {"code": str}. Set when the loop proposes; consumed on confirm.
    pending_python: dict | None = None
    # True when the agent loop had active tool calls before proposing changes
    # (signals that continuation after confirm may be needed)
    had_tool_calls_before_propose: bool = False
    # True when the most recent agent loop exhausted its iteration budget with no
    # pending changes (signals that a manual "Continue" resume is available)
    reached_max_steps: bool = False
    # Activity log from the most recent agent loop run (for continuation context)
    last_activity_log: list[str] = field(default_factory=list)
    # Rolling checkpoint summary of folded-away early turns. Non-empty implies
    # messages[0] is the CHECKPOINT_PREFIX summary message.
    checkpoint_summary: str = ""


_sessions: dict[str, ChatSession] = {}


def _reap_stale_sessions():
    """Remove sessions that haven't been used within SESSION_TTL."""
    now = time.time()
    stale = [sid for sid, s in _sessions.items() if now - s.last_activity > SESSION_TTL]
    for sid in stale:
        del _sessions[sid]


def create_session(document_url_path: str, mode: str = "document",
                   vault: str = DEFAULT_VAULT) -> ChatSession:
    _reap_stale_sessions()
    session = ChatSession(
        session_id=str(uuid.uuid4()),
        document_url_path=document_url_path,
        vault=vault or DEFAULT_VAULT,
        mode=mode,
    )
    _sessions[session.session_id] = session
    return session


def get_session(session_id: str) -> ChatSession | None:
    _reap_stale_sessions()
    session = _sessions.get(session_id)
    if session:
        session.last_activity = time.time()
    return session


def delete_session(session_id: str):
    _sessions.pop(session_id, None)
    return None


# ---------------------------------------------------------------------------
# Section parsing
# ---------------------------------------------------------------------------
# The parser, lookup and splice primitives moved to src.md_sections so the agent
# capabilities, the editor outline and transclusion can share ONE implementation
# instead of each carrying its own heading regex.  Aliased to the historical
# private names to keep call sites in this module unchanged (same pattern as the
# arg_coercion extraction below).
from src.md_sections import (
    build_outline as _build_document_outline,
    delete_section as _delete_section,
    describe_sections as _describe_sections,
    insert_section as _insert_section,
    lookup_section as _lookup_section,
    parse_sections as _parse_document_sections,
    replace_section as _replace_section,
    section_body as _section_body,
)


# ---------------------------------------------------------------------------
# Robust argument extraction
# ---------------------------------------------------------------------------
# These coercion helpers moved to src.arg_coercion so the agent capabilities
# dispatcher can share the exact same malformed-output recovery.  Aliased to
# the historical private names to keep call sites in this module unchanged.
from src.arg_coercion import arg_as_str as _arg_as_str, arg_as_int as _arg_as_int


# ---------------------------------------------------------------------------
# Fallback: parse tool calls from text output
# ---------------------------------------------------------------------------

_TOOL_NAMES = {
    "get_document_outline", "get_section", "search_wiki", "read_document",
    "edit_section", "append_to_document", "insert_section", "delete_section",
}

_GLOBAL_TOOL_NAMES = {
    "search_wiki", "read_document", "get_document_outline",
    "edit_document", "create_document", "list_documents_by_tag",
}

# The computational-RAG tool is opt-in (LLM-authored code execution) and only in
# document mode, where there is a single page kernel to attach to.
if CHAT_ENABLE_RUN_PYTHON:
    _TOOL_NAMES.add("run_python")

_ALL_TOOL_NAMES = _TOOL_NAMES | _GLOBAL_TOOL_NAMES


def _try_parse_tool_call_from_text(text: str, tool_names: set[str] | None = None) -> dict | None:
    """Attempt to extract a tool call from plain-text LLM output.

    Small models often emit the tool call as JSON-ish text instead of using
    the structured tool_calls field.  Handles patterns like:
      {"name": edit_section, "parameters": {...}}
      {"name": "edit_section", "parameters": {...}}
      ```json\n{...}\n```
    Returns {"name": str, "arguments": dict} or None.
    """
    if not text:
        return None

    names = tool_names or _ALL_TOOL_NAMES

    # Strip markdown code fences if present
    cleaned = text.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r'\s*```\s*$', '', cleaned, flags=re.MULTILINE)
    cleaned = cleaned.strip()

    # Quick check: does the text contain any known tool name?
    if not any(name in cleaned for name in names):
        return None

    # Fix common invalid JSON: unquoted tool name values
    # e.g.  "name": edit_section  →  "name": "edit_section"
    for name in names:
        cleaned = re.sub(
            r'("name"\s*:\s*)(' + name + r')(\s*[,}])',
            r'\1"\2"\3',
            cleaned,
        )

    # Try to find the outermost JSON object containing "name"
    # Walk through to find balanced braces
    start = cleaned.find('{')
    if start == -1:
        return None

    depth = 0
    end = -1
    in_string = False
    escape_next = False
    for i in range(start, len(cleaned)):
        ch = cleaned[i]
        if escape_next:
            escape_next = False
            continue
        if ch == '\\' and in_string:
            escape_next = True
            continue
        if ch == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if end == -1:
        return None

    json_str = cleaned[start:end]

    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    # Extract the tool name. Small models disagree on where it lives:
    #   {"name": "x", ...}                    (Ollama text fallback)
    #   {"function": "x", "arguments": {...}} (granite-dense flat form)
    #   {"function": {"name": "x", ...}}      (OpenAI nested form)
    func = parsed.get("function")
    func_name = parsed.get("name") or ""
    if not func_name and isinstance(func, str):
        func_name = func
    elif not func_name and isinstance(func, dict):
        func_name = func.get("name", "")

    # Validate structure: must have a name in known tools
    if func_name not in names:
        return None

    # Arguments may be under "parameters" or "arguments", at the top level
    # or (for the nested form) inside the "function" object.
    args = parsed.get("parameters") or parsed.get("arguments") or {}
    if not args and isinstance(func, dict):
        args = func.get("parameters") or func.get("arguments") or {}

    return {"name": func_name, "arguments": args}


# ---------------------------------------------------------------------------
# Tool definitions (Ollama format) - 7 tools
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    # --- Read tools ---
    {
        "type": "function",
        "function": {
            "name": "get_document_outline",
            "description": "Get the section outline of the current document. Returns a list of section headings with their index numbers.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_section",
            "description": "Get the content of a specific section from the current document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "section_heading": {
                        "type": "string",
                        "description": "The heading of the section to retrieve, e.g. 'Introduction' or '## Introduction'.",
                    },
                    "section_index": {
                        "type": "integer",
                        "description": "Optional index number from the document outline, for disambiguating duplicate headings.",
                    },
                },
                "required": ["section_heading"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_wiki",
            "description": "Search across all wiki documents for relevant content. Returns matching snippets from other documents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Read the full content or a specific section of ANOTHER wiki document (e.g. one surfaced by search_wiki). Use this to follow up on search results - the current document is already provided to you.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_path": {
                        "type": "string",
                        "description": "The wiki path of the document to read, e.g. 'Programming' or 'notes/Meeting Notes'.",
                    },
                    "section_heading": {
                        "type": "string",
                        "description": "Optional: heading of a specific section to read. If omitted, returns the full document.",
                    },
                    "section_index": {
                        "type": "integer",
                        "description": "Optional index number to disambiguate duplicate headings.",
                    },
                },
                "required": ["document_path"],
            },
        },
    },
    # --- Write tools ---
    {
        "type": "function",
        "function": {
            "name": "edit_section",
            "description": "Replace the body of an existing section in the current document. The heading is preserved; only the body content is replaced.",
            "parameters": {
                "type": "object",
                "properties": {
                    "section_heading": {
                        "type": "string",
                        "description": "The heading of the section to edit, e.g. 'Introduction' or '## Introduction'.",
                    },
                    "new_content": {
                        "type": "string",
                        "description": "The new body text for the section. The heading line is kept automatically.",
                    },
                    "section_index": {
                        "type": "integer",
                        "description": "Optional index number from the document outline, for disambiguating duplicate headings.",
                    },
                },
                "required": ["section_heading", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_to_document",
            "description": "Append text to the end of the current document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The text to append to the end of the document.",
                    },
                },
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "insert_section",
            "description": "Insert a new section into the current document. By default appends at the end. Use reference_section with position to insert before or after a specific existing section.",
            "parameters": {
                "type": "object",
                "properties": {
                    "heading": {
                        "type": "string",
                        "description": "The heading for the new section, e.g. '# New Section' or '### New Section'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The body text for the new section.",
                    },
                    "reference_section": {
                        "type": "string",
                        "description": "Optional: an existing section heading to insert relative to. If omitted, appends at end.",
                    },
                    "reference_section_index": {
                        "type": "integer",
                        "description": "Optional index to disambiguate the reference_section heading.",
                    },
                    "position": {
                        "type": "string",
                        "enum": ["before", "after"],
                        "description": "Whether to insert before or after the reference_section. Defaults to 'after'.",
                    },
                },
                "required": ["heading", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_section",
            "description": "Delete a section (heading and body) from the current document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "section_heading": {
                        "type": "string",
                        "description": "The heading of the section to delete.",
                    },
                    "section_index": {
                        "type": "integer",
                        "description": "Optional index number from the document outline, for disambiguating duplicate headings.",
                    },
                },
                "required": ["section_heading"],
            },
        },
    },
]

# Computational-RAG tool (opt-in via CHAT_ENABLE_RUN_PYTHON). Appended rather than
# inlined so it never reaches the model when disabled.
if CHAT_ENABLE_RUN_PYTHON:
    TOOL_DEFINITIONS.append({
        "type": "function",
        "function": {
            "name": "run_python",
            "description": (
                "Run Python in THIS page's live Jupyter kernel to COMPUTE an "
                "answer (counts, aggregations, tables, charts) instead of "
                "guessing. A `wiki` object is already available for querying the "
                "vault index: wiki.search(query, top_k=10), wiki.tagged(tag), "
                "wiki.related(path), wiki.backlinks(path), wiki.frontmatter(path) "
                "- each returns plain lists/dicts that drop into pandas. pandas "
                "and matplotlib are available; any figure you create is shown to "
                "the user automatically. The kernel's WORKING DIRECTORY is the "
                "folder that contains THIS page, so read files attached to this "
                "page by their bare filename - e.g. pd.read_csv('data.csv') - NOT "
                "a path that includes this page's name. The user must approve your "
                "code before it runs, so write the COMPLETE code in one call. "
                "Prefer read-only computation and fresh variable names - the kernel "
                "is shared with the user's own cells. Do NOT delete files or mutate "
                "their data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The complete Python source to execute.",
                    },
                },
                "required": ["code"],
            },
        },
    })


# ---------------------------------------------------------------------------
# Global corpus tool definitions (Ollama format) - 6 tools
# ---------------------------------------------------------------------------

GLOBAL_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_wiki",
            "description": "Search across all wiki documents for relevant content. Returns matching snippets with document paths and section headers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Read the full content or a specific section of any wiki document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_path": {
                        "type": "string",
                        "description": "The wiki path of the document, e.g. 'wiki/Programming' or 'wiki/notes/Meeting Notes'.",
                    },
                    "section_heading": {
                        "type": "string",
                        "description": "Optional: heading of a specific section to read. If omitted, returns the full document.",
                    },
                    "section_index": {
                        "type": "integer",
                        "description": "Optional index number to disambiguate duplicate headings.",
                    },
                },
                "required": ["document_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_document_outline",
            "description": "Get the section outline of any wiki document. Returns a list of section headings with their index numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_path": {
                        "type": "string",
                        "description": "The wiki path of the document.",
                    },
                },
                "required": ["document_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_document",
            "description": "Replace the body of an existing section in any wiki document. The heading is preserved; only the body content is replaced.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_path": {
                        "type": "string",
                        "description": "The wiki path of the document to edit.",
                    },
                    "section_heading": {
                        "type": "string",
                        "description": "The heading of the section to edit.",
                    },
                    "new_content": {
                        "type": "string",
                        "description": "The new body text for the section. The heading line is kept automatically.",
                    },
                    "section_index": {
                        "type": "integer",
                        "description": "Optional index number to disambiguate duplicate headings.",
                    },
                },
                "required": ["document_path", "section_heading", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_document",
            "description": "Create a new wiki document at the specified path with the given content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "document_path": {
                        "type": "string",
                        "description": "The wiki path for the new document, e.g. 'wiki/New Topic'.",
                    },
                    "content": {
                        "type": "string",
                        "description": "The full markdown content for the new document.",
                    },
                },
                "required": ["document_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents_by_tag",
            "description": "List all wiki documents that have a specific tag.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "The tag to search for, e.g. 'python' or 'math'.",
                    },
                },
                "required": ["tag"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

def _build_system_prompt(title: str, document_content: str) -> str:
    """System prompt for non-tool-capable models (streaming path)."""
    return (
        f'You are a writing assistant for a personal wiki. '
        f'You are viewing the page titled "{title}".\n\n'
        f'The full content of the current document is below:\n\n'
        f'---\n{document_content}\n---\n\n'
        f'Help the user understand, improve, or expand this document. '
        f'Keep answers concise and relevant.'
    )


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

def _truncate(text: str, limit: int = TOOL_RESULT_TRUNCATE) -> str:
    """Truncate text to limit, appending a note if truncated."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[truncated - content exceeds display limit]"


def _clean_agent_response(text: str) -> str:
    """Filter out system prompt regurgitation from model response."""
    if not text:
        return ""
    for marker in PROMPT_MARKERS:
        if marker in text:
            return ""
    return text.strip()


async def _contextualize_query(
    query: str, messages: list[dict], scratchpad, llm_mgr
) -> str:
    """Rewrite a search query using conversation context to make it self-contained."""
    # Skip if early in conversation (first user message = no ambiguity)
    user_messages = [m for m in messages if m.get("role") == "user"]
    
    logger.info("Contextual Query rewrite")
    
    if len(user_messages) <= 1:
        return query

    # Build context from recent conversation (last few exchanges)
    recent = messages[-6:]
    context_lines = []
    for m in recent:
        role = m.get("role", "")
        content = (m.get("content") or "")[:300]
        if role in ("user", "assistant") and content:
            context_lines.append(f"{role}: {content}")
    conversation_context = "\n".join(context_lines)

    doc_title = scratchpad.title if scratchpad else "unknown"

    prompt = (
        f"Current document: {doc_title}\n\n"
        f"Recent conversation:\n{conversation_context}\n\n"
        f"The user's search query: \"{query}\"\n\n"
        f"Rewrite this query as a self-contained search query that would work "
        f"without any conversation context. Only output the rewritten query, nothing else."
    )
    
    logger.info(prompt)

    try:
        result = await llm_mgr.generate(prompt)
        rewritten = result.strip().strip('"')
        if rewritten and len(rewritten) < 500:
            logger.info("Query rewrite: '%s' -> '%s'", query, rewritten)
            return rewritten
    except Exception as e:
        logger.warning("Query rewrite failed, using original: %s", e)

    return query


async def _do_search_wiki(
    tool_args: dict,
    session=None,
    llm_mgr=None,
    scratchpad=None,
    status_callback=None,
) -> str:
    """Shared search implementation used by both document-mode and global-mode tools."""
    logger.info("Search Wiki Tool Use")
    query = _arg_as_str(tool_args.get("query"))
    logger.info(query)
    if not query:
        return "Error: query is required"
    # Rewrite conversational queries for better retrieval
    if session and llm_mgr:
        if status_callback:
            await status_callback("Refining search query...")
        query = await _contextualize_query(
            query, session.messages, scratchpad, llm_mgr
        )
    else:
        logger.info("Contextual Search was bypassed.")
    try:
        if status_callback:
            await status_callback("Searching documents...")
        from src.rag_search import search as rag_search
        # Scope retrieval to the session's vault (hard isolation: chat never crosses
        # a vault boundary).
        vault_id = session.vault if session else DEFAULT_VAULT
        search_results = await asyncio.to_thread(
            rag_search, query, top_k=5, include_graph_expansion=True, vault_id=vault_id
        )
        results = search_results["chunk_results"]
        if not results:
            return "No results found."
        lines = []
        for r in results:
            doc_id = r.get("doc_id", "")
            wiki_path = doc_id[:-3] if doc_id.endswith(".md") else doc_id
            logger.info(f"{wiki_path=}")
            header = r.get("header_path", "")
            logger.info(f"{header=}")
            snippet = (r.get("content", "") or "")[:300]
            prefix = "[linked] " if r.get("source") == "graph" else ""
            logger.info(f"{prefix=}")
            logger.info(r.keys())
            lines.append(f"- {prefix}[[/{wiki_path}]] > {header}\n  {snippet}")
        return _truncate("\n".join(lines))
    except Exception as e:
        logger.warning("search_wiki failed: %s", e)
        return f"Search unavailable: {e}"


async def _execute_tool(
    tool_name: str,
    tool_args: dict,
    scratchpad: DocumentScratchpad,
    session=None,
    llm_mgr=None,
    status_callback=None,
) -> str:
    """Execute a tool call and return result text for the model."""

    if tool_name == "get_document_outline":
        sections = _parse_document_sections(scratchpad.content)
        outline = _build_document_outline(sections)
        return outline or "(document has no sections)"

    elif tool_name == "get_section":
        heading = _arg_as_str(tool_args.get("section_heading"))
        idx_raw = tool_args.get("section_index")
        idx = _arg_as_int(idx_raw)

        sections = _parse_document_sections(scratchpad.content)
        section = _lookup_section(sections, heading, idx)
        if not section:
            available = _describe_sections(sections)
            return f"Section '{heading}' not found. Available sections: {available}"

        body = _section_body(scratchpad.content, section)
        return _truncate(body)

    elif tool_name == "search_wiki":
        return await _do_search_wiki(
            tool_args, session=session, llm_mgr=llm_mgr,
            scratchpad=scratchpad, status_callback=status_callback,
        )

    elif tool_name == "read_document":
        doc_path = _arg_as_str(tool_args.get("document_path"))
        if not doc_path:
            return "Error: document_path is required"

        vault = scratchpad.vault if scratchpad else DEFAULT_VAULT
        target = WikiDoc(doc_path, vault=vault)

        # Prefer the scratchpad for the current document so in-progress (unconfirmed)
        # edits are reflected; otherwise load the other document read-only from disk.
        if scratchpad and target.relative_file_path() == WikiDoc(
            scratchpad.document_url_path, vault=vault
        ).relative_file_path():
            content = scratchpad.content
        elif target.exists():
            content = target.get_content() or ""
        else:
            return f"Document '{doc_path}' not found."

        if not content:
            return f"Document '{doc_path}' is empty."

        heading = tool_args.get("section_heading")
        if heading:
            heading = _arg_as_str(heading)
            idx_raw = tool_args.get("section_index")
            idx = _arg_as_int(idx_raw)
            sections = _parse_document_sections(content)
            section = _lookup_section(sections, heading, idx)
            if not section:
                available = _describe_sections(sections)
                return f"Section '{heading}' not found in {doc_path}. Available: {available}"
            body = _section_body(content, section)
            return _truncate(body)

        return _truncate(content)

    elif tool_name == "edit_section":
        heading = _arg_as_str(tool_args.get("section_heading"))
        new_content = _arg_as_str(tool_args.get("new_content"))
        idx_raw = tool_args.get("section_index")
        idx = _arg_as_int(idx_raw)

        sections = _parse_document_sections(scratchpad.content)
        section = _lookup_section(sections, heading, idx)
        if not section:
            available = _describe_sections(sections)
            return f"Section '{heading}' not found. Available sections: {available}"

        # _replace_section re-seats the body with one blank line on each seam,
        # so no whitespace fixing is needed here - report what actually landed.
        old_body = _section_body(scratchpad.content, section)
        scratchpad.content = _replace_section(scratchpad.content, section, new_content)
        new_body = _section_body(scratchpad.content,
                                 _lookup_section(_parse_document_sections(scratchpad.content),
                                                 heading, idx) or section)
        preview = new_content.strip()[:200]
        return f"Section '{section['heading']}' updated ({len(old_body)} chars → {len(new_body)} chars). Updated content starts with: {preview}"

    elif tool_name == "append_to_document":
        content = _arg_as_str(tool_args.get("content"))
        if not content:
            return "Error: content is required"

        if scratchpad.content and not scratchpad.content.endswith('\n'):
            scratchpad.content += '\n'
        scratchpad.content += content
        return f"Appended {len(content)} chars to document."

    elif tool_name == "insert_section":
        heading = _arg_as_str(tool_args.get("heading"))
        content = _arg_as_str(tool_args.get("content"))
        ref_section = tool_args.get("reference_section")
        ref_idx_raw = tool_args.get("reference_section_index")
        ref_idx = _arg_as_int(ref_idx_raw)
        position = _arg_as_str(tool_args.get("position")) or "after"
        position = position.lower().strip()
        if position not in ("before", "after"):
            position = "after"

        if not heading:
            return "Error: heading is required"

        target = None
        if ref_section:
            ref_heading = _arg_as_str(ref_section)
            sections = _parse_document_sections(scratchpad.content)
            target = _lookup_section(sections, ref_heading, ref_idx)
            if not target:
                available = _describe_sections(sections)
                return f"Section '{ref_heading}' not found for insertion point. Available sections: {available}"

        scratchpad.content = _insert_section(
            scratchpad.content, heading, content,
            reference=target, position=position,
        )

        # _insert_section promotes a bare heading to '##'; mirror that here so
        # the report names the heading the document actually got.
        if not heading.startswith('#'):
            heading = f"## {heading}"
        return f"Inserted new section '{heading}' ({position} '{_arg_as_str(ref_section) if ref_section else 'end'}')."

    elif tool_name == "delete_section":
        heading = _arg_as_str(tool_args.get("section_heading"))
        idx_raw = tool_args.get("section_index")
        idx = _arg_as_int(idx_raw)

        sections = _parse_document_sections(scratchpad.content)
        section = _lookup_section(sections, heading, idx)
        if not section:
            available = _describe_sections(sections)
            return f"Section '{heading}' not found. Available sections: {available}"

        before = len(scratchpad.content)
        scratchpad.content = _delete_section(scratchpad.content, section)
        removed = before - len(scratchpad.content)
        return f"Deleted section '{section['heading']}' ({removed} chars removed)."

    else:
        return f"Unknown tool: {tool_name}"


# ---------------------------------------------------------------------------
# Global corpus tool execution
# ---------------------------------------------------------------------------

def _global_status_label(tool_name: str) -> str:
    """Human-readable status label for a global tool call."""
    labels = {
        "search_wiki": "Searching wiki...",
        "read_document": "Reading document...",
        "get_document_outline": "Reading document outline...",
        "edit_document": "Editing document...",
        "create_document": "Creating document...",
        "list_documents_by_tag": "Browsing by tag...",
    }
    return labels.get(tool_name, f"Executing {tool_name}...")


def _global_activity_narration(tool_name: str, tool_args: dict) -> str:
    """Human-readable past-tense narration of a completed global tool call."""
    if tool_name == "search_wiki":
        query = _arg_as_str(tool_args.get("query", ""))
        return f"Searched wiki for '{query}'"
    elif tool_name == "read_document":
        path = _arg_as_str(tool_args.get("document_path", ""))
        heading = tool_args.get("section_heading")
        if heading:
            return f"Read section '{_arg_as_str(heading)}' from {path}"
        return f"Read document {path}"
    elif tool_name == "get_document_outline":
        path = _arg_as_str(tool_args.get("document_path", ""))
        return f"Read outline of {path}"
    elif tool_name == "edit_document":
        path = _arg_as_str(tool_args.get("document_path", ""))
        heading = _arg_as_str(tool_args.get("section_heading", ""))
        return f"Edited section '{heading}' in {path}"
    elif tool_name == "create_document":
        path = _arg_as_str(tool_args.get("document_path", ""))
        return f"Created document {path}"
    elif tool_name == "list_documents_by_tag":
        tag = _arg_as_str(tool_args.get("tag", ""))
        return f"Listed documents tagged '{tag}'"
    return f"Executed {tool_name}"


async def _execute_global_tool(
    tool_name: str,
    tool_args: dict,
    collection: ScratchpadCollection,
    session=None,
    llm_mgr=None,
    status_callback=None,
) -> str:
    """Execute a global-mode tool call and return result text for the model."""

    if tool_name == "search_wiki":
        return await _do_search_wiki(
            tool_args, session=session, llm_mgr=llm_mgr,
            scratchpad=None, status_callback=status_callback,
        )

    elif tool_name == "read_document":
        doc_path = _arg_as_str(tool_args.get("document_path"))
        if not doc_path:
            return "Error: document_path is required"
        pad = collection.get_or_load(doc_path)
        if not pad.content:
            return f"Document '{doc_path}' not found or is empty."

        heading = tool_args.get("section_heading")
        if heading:
            heading = _arg_as_str(heading)
            idx_raw = tool_args.get("section_index")
            idx = _arg_as_int(idx_raw)
            sections = _parse_document_sections(pad.content)
            section = _lookup_section(sections, heading, idx)
            if not section:
                available = _describe_sections(sections)
                return f"Section '{heading}' not found in {doc_path}. Available: {available}"
            body = _section_body(pad.content, section)
            return _truncate(body)

        return _truncate(pad.content)

    elif tool_name == "get_document_outline":
        doc_path = _arg_as_str(tool_args.get("document_path"))
        if not doc_path:
            return "Error: document_path is required"
        pad = collection.get_or_load(doc_path)
        if not pad.content:
            return f"Document '{doc_path}' not found or is empty."
        sections = _parse_document_sections(pad.content)
        outline = _build_document_outline(sections)
        return outline or "(document has no sections)"

    elif tool_name == "edit_document":
        doc_path = _arg_as_str(tool_args.get("document_path"))
        heading = _arg_as_str(tool_args.get("section_heading"))
        new_content = _arg_as_str(tool_args.get("new_content"))
        idx_raw = tool_args.get("section_index")
        idx = _arg_as_int(idx_raw)

        if not doc_path:
            return "Error: document_path is required"
        if not heading:
            return "Error: section_heading is required"

        pad = collection.get_or_load(doc_path)
        if not pad.content:
            return f"Document '{doc_path}' not found or is empty. Use create_document instead."

        sections = _parse_document_sections(pad.content)
        section = _lookup_section(sections, heading, idx)
        if not section:
            available = _describe_sections(sections)
            return f"Section '{heading}' not found in {doc_path}. Available: {available}"

        # Same shared splice the single-document edit_section uses, so global
        # mode gets identical seam normalization instead of its own arithmetic.
        old_body = _section_body(pad.content, section)
        pad.content = _replace_section(pad.content, section, new_content)
        new_body = _section_body(
            pad.content,
            _lookup_section(_parse_document_sections(pad.content), heading, idx) or section)
        preview = new_content.strip()[:200]
        return f"Section '{section['heading']}' in {doc_path} updated ({len(old_body)} chars -> {len(new_body)} chars). Updated content starts with: {preview}"

    elif tool_name == "create_document":
        doc_path = _arg_as_str(tool_args.get("document_path"))
        content = _arg_as_str(tool_args.get("content"))
        if not doc_path:
            return "Error: document_path is required"
        if not content:
            return "Error: content is required"

        # Check if document already exists (within the collection's vault)
        wd = WikiDoc(doc_path, vault=collection.vault)
        if wd.exists():
            return f"Document '{doc_path}' already exists. Use edit_document to modify it."

        collection.create_new(doc_path, content)
        return f"Document '{doc_path}' created ({len(content)} chars). Changes are pending confirmation."

    elif tool_name == "list_documents_by_tag":
        tag = _arg_as_str(tool_args.get("tag"))
        if not tag:
            return "Error: tag is required"
        try:
            from config import get_pg_connection
            conn = get_pg_connection()
            cur = conn.cursor()
            cur.execute(
                """SELECT d.doc_id, d.title
                   FROM documents d
                   JOIN document_tags dt ON d.doc_id = dt.doc_id
                   WHERE dt.tag = %s AND d.doc_exists = TRUE
                   ORDER BY d.title
                   LIMIT 50""",
                (tag,)
            )
            rows = cur.fetchall()
            cur.close()
            conn.close()
            if not rows:
                return f"No documents found with tag '{tag}'."
            lines = []
            for doc_id, title in rows:
                wiki_path = doc_id[:-3] if doc_id.endswith(".md") else doc_id
                lines.append(f"- [[/{wiki_path}]] ({title})")
            return "\n".join(lines)
        except Exception as e:
            logger.warning("list_documents_by_tag failed: %s", e)
            return f"Tag lookup unavailable: {e}"

    else:
        return f"Unknown tool: {tool_name}"


# ---------------------------------------------------------------------------
# Confirm / reject pending scratchpad changes
# ---------------------------------------------------------------------------

async def confirm_action(session: ChatSession, confirmed: bool) -> dict:
    """Handle user confirm/deny of pending scratchpad changes."""
    result = None

    # Single-document scratchpad (document mode)
    if session.pending_scratchpad is not None:
        scratchpad = session.pending_scratchpad
        session.pending_scratchpad = None

        if not confirmed:
            result = {"action_rejected": True}
        else:
            was_historical = scratchpad.is_historical
            try:
                await scratchpad.apply_to_disk()
                wd = WikiDoc(scratchpad.document_url_path, vault=scratchpad.vault)
                url = "/" + (wd.display_url_path() or scratchpad.document_url_path)
                result = {"action_executed": {"success": True, "message": "Changes applied.", "url": url}}
                if was_historical:
                    # What we just wrote IS the current version now; the rest of the
                    # conversation continues against it, and the browser has to stop
                    # claiming to show history.
                    session.revision = ""
                    result["action_executed"]["revision_cleared"] = True
            except Exception as e:
                result = {"action_executed": {"success": False, "message": str(e), "url": ""}}

    # Multi-document collection (global mode)
    elif session.pending_collection is not None:
        collection = session.pending_collection
        session.pending_collection = None

        if not confirmed:
            result = {"action_rejected": True}
        else:
            try:
                await collection.apply_all_to_disk()
                diffs = collection.get_all_diffs()
                paths = [d["path"] for d in diffs] if diffs else []
                msg = f"Changes applied to {len(paths)} document(s)." if paths else "Changes applied."
                result = {"action_executed": {"success": True, "message": msg, "paths": paths}}
            except Exception as e:
                result = {"action_executed": {"success": False, "message": str(e)}}

    else:
        return {"error": "No pending action"}

    # Inject confirmation context into message history so the model knows
    # what happened if the loop resumes (prevents repeated actions)
    if confirmed:
        activity_summary = ""
        if session.last_activity_log:
            activity_summary = " Completed actions: " + "; ".join(session.last_activity_log) + "."
        context = (
            "The user approved and the following changes have been applied to disk."
            + activity_summary
            + " Do NOT repeat these actions. Continue only with remaining steps that have not yet been completed."
        )
    else:
        context = "The user rejected the proposed changes. They were not applied."
    session.messages.append({"role": "user", "content": context})
    log_history_stats("confirm-context-added", session.messages)

    return result


async def _run_pending_python_generator(
    session: ChatSession, confirmed: bool, llm_mgr
) -> AsyncGenerator[str, None]:
    """Execute (or decline) the agent's approval-gated run_python code.

    Lives in a generator (not confirm_action) because running the code emits
    `artifact` SSE events for any figures and then resumes the agent loop so the
    model can interpret the output. The captured text is fed back as the loop's
    next user message; the base64 figures go only to the browser, never the model.
    """
    pending = session.pending_python or {}
    session.pending_python = None
    code = pending.get("code", "")

    if not confirmed:
        yield f"data: {json.dumps({'action_rejected': True})}\n\n"
        continuation = (
            "The user DECLINED to run the proposed code; it was not executed. "
            "Do not propose the same code again unless they ask. Answer using what "
            "you already know, or ask how they'd like to proceed."
        )
        async for event in _agent_loop(session, continuation, llm_mgr):
            yield event
        return

    # Approved: run the code in the page's shared kernel, streaming any figures.
    from src import agent_python
    from src.jupyter_client import jupyter_manager

    artifact_queue: asyncio.Queue = asyncio.Queue()

    async def emit_artifact(art):
        await artifact_queue.put(art)

    exec_task = asyncio.create_task(agent_python.execute_in_page_kernel(
        code, session=session, jupyter_manager=jupyter_manager,
        emit_artifact=emit_artifact,
    ))
    while not exec_task.done():
        try:
            art = await asyncio.wait_for(artifact_queue.get(), timeout=0.1)
            yield f"data: {json.dumps({'artifact': art})}\n\n"
        except asyncio.TimeoutError:
            continue
    while not artifact_queue.empty():
        yield f"data: {json.dumps({'artifact': artifact_queue.get_nowait()})}\n\n"

    output = await exec_task
    yield f"data: {json.dumps({'action_executed': {'success': True, 'message': 'Code executed.'}})}\n\n"

    continuation = (
        "The user APPROVED and the code you proposed was executed. Its output "
        "follows. Use it to answer the user's question; do NOT re-run the same "
        "code. Any figures have already been shown to the user.\n\n"
        "--- output ---\n" + output
    )
    async for event in _agent_loop(session, continuation, llm_mgr):
        yield event


async def confirm_and_continue_generator(
    session: ChatSession, confirmed: bool, llm_mgr
) -> AsyncGenerator[str, None]:
    """Confirm/reject pending action, then optionally continue the agent loop.

    Yields SSE events: the confirmation result first, then (if the model had
    more work planned and the user confirmed) a full continuation of the agent
    loop with its own status/token/activity/action_proposed events.
    """
    # run_python approval is handled specially (executes code + streams figures).
    if session.pending_python is not None:
        async for event in _run_pending_python_generator(session, confirmed, llm_mgr):
            yield event
        return

    result = await confirm_action(session, confirmed)

    # Emit the confirmation result as an SSE event
    if result.get("action_executed"):
        yield f"data: {json.dumps({'action_executed': result['action_executed']})}\n\n"
    elif result.get("action_rejected"):
        yield f"data: {json.dumps({'action_rejected': True})}\n\n"
    elif result.get("error"):
        yield f"data: {json.dumps({'error': result['error']})}\n\n"
        yield f"data: {json.dumps({'done': True, 'session_id': session.session_id})}\n\n"
        return

    # If the agent was mid-plan and the user confirmed, continue the loop
    should_continue = confirmed and session.had_tool_calls_before_propose
    session.had_tool_calls_before_propose = False  # reset

    if should_continue:
        continuation_msg = (
            "The previous changes have been applied successfully. "
            "Continue with any remaining steps that have NOT already been completed. "
            "Do not re-read or re-edit documents that were already modified."
        )
        async for event in _agent_loop(session, continuation_msg, llm_mgr):
            yield event
    else:
        yield f"data: {json.dumps({'done': True, 'session_id': session.session_id})}\n\n"


async def continue_generator(
    session: ChatSession, llm_mgr
) -> AsyncGenerator[str, None]:
    """Resume the agent loop after it hit its iteration budget (manual Continue).

    Triggered by the "Continue" affordance the frontend shows on a `max_steps`
    event. Re-enters `_agent_loop` with a fresh iteration budget on top of the
    already-finalized (summary + compacted) session history, so the message
    sequence handed to Ollama stays template-valid. No cap - the user gates each
    resume by clicking, so the loop can be continued as many times as needed.
    """
    if not session.reached_max_steps:
        # Nothing to resume (stale click, expired session, or already continued).
        yield f"data: {json.dumps({'done': True, 'session_id': session.session_id})}\n\n"
        return

    session.reached_max_steps = False  # reset before re-entry

    continuation_msg = (
        "Continue working on the previous request. Pick up exactly where you "
        "left off. Do NOT repeat searches, reads, or edits you have already "
        "performed - refer to the completed-actions note in the prior message."
    )
    async for event in _agent_loop(session, continuation_msg, llm_mgr):
        yield event


# ---------------------------------------------------------------------------
# SSE streaming generator (no tools - original path)
# ---------------------------------------------------------------------------

async def _chat_response_generator_streaming(
    session: ChatSession, user_message: str, llm_mgr
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted tokens from a streaming Ollama chat response."""
    try:
        # 1. Reload document content: the viewed git revision when one is set,
        #    otherwise the working tree. Goes through the scratchpad loader so this
        #    path can't drift from the agent loop's notion of "the document".
        pad = DocumentScratchpad(
            session.document_url_path, session.vault, session.revision)
        pad.load()
        session.revision = pad.revision
        doc_content = pad.content or "(Document not found)"
        title = pad.title

        # 2. Build system prompt
        system_prompt = _build_system_prompt(title, doc_content)

        # 3. Append user message
        session.messages.append({"role": "user", "content": user_message})
        log_history_stats("streaming/user-added", session.messages, system_prompt)

        # 3b. Checkpoint-summarize the oldest span when history grows large.
        #     This is the remove-and-replace compaction (logged in detail by
        #     _maybe_checkpoint); the sliding-window trim below is the
        #     append-only safety net that only drops, never summarizes.
        system_prompt_tokens = int(len(system_prompt) / CHARS_PER_TOKEN)
        async for evt in _maybe_checkpoint(session, system_prompt_tokens, llm_mgr):
            yield evt

        # 4. Trim to sliding window (dynamic based on model context size)
        msgs_before_trim = list(session.messages)
        session.messages = _trim_message_history(
            session.messages,
            max_messages=llm_mgr.compute_max_messages(),
            system_prompt=system_prompt,
            context_length=llm_mgr._context_length or 4096,
        )
        log_trim("streaming_trim", msgs_before_trim, session.messages)
        log_history_stats("streaming/post-trim", session.messages, system_prompt)

        # 5. Stream from Ollama
        full_response = ""
        async for token in llm_mgr.chat_stream(session.messages, system=system_prompt):
            full_response += token
            yield f"data: {json.dumps({'token': token})}\n\n"

        # 6. Append assistant response to session history
        session.messages.append({"role": "assistant", "content": full_response})
        log_history_stats("streaming/assistant-added", session.messages, system_prompt)

        # 7. Done event
        yield f"data: {json.dumps({'done': True, 'session_id': session.session_id})}\n\n"

    except Exception as e:
        yield f"data: {json.dumps({'error': str(e)})}\n\n"


# ---------------------------------------------------------------------------
# Agent loop (replaces _chat_response_generator_with_tools)
# ---------------------------------------------------------------------------

# _tool_call_name and _normalize_tool_call moved to src.agent_runner.


def _status_label(tool_name: str) -> str:
    """Human-readable status label for a tool call."""
    labels = {
        "get_document_outline": "Reading document outline...",
        "get_section": "Reading section...",
        "search_wiki": "Searching wiki...",
        "read_document": "Reading document...",
        "edit_section": "Editing section...",
        "append_to_document": "Appending to document...",
        "insert_section": "Inserting section...",
        "delete_section": "Deleting section...",
        "run_python": "Preparing Python to run...",
    }
    return labels.get(tool_name, f"Executing {tool_name}...")


def _activity_narration(tool_name: str, tool_args: dict) -> str:
    """Human-readable past-tense narration of a completed tool call."""
    if tool_name == "search_wiki":
        query = _arg_as_str(tool_args.get("query", ""))
        return f"Searched wiki for '{query}'"
    elif tool_name == "read_document":
        path = _arg_as_str(tool_args.get("document_path", ""))
        heading = tool_args.get("section_heading")
        if heading:
            return f"Read section '{_arg_as_str(heading)}' from {path}"
        return f"Read document {path}"
    elif tool_name == "get_document_outline":
        return "Read the document outline"
    elif tool_name == "get_section":
        heading = _arg_as_str(tool_args.get("section_heading", ""))
        return f"Read section '{heading}'"
    elif tool_name == "edit_section":
        heading = _arg_as_str(tool_args.get("section_heading", ""))
        return f"Edited section '{heading}'"
    elif tool_name == "append_to_document":
        return "Appended content to document"
    elif tool_name == "insert_section":
        heading = _arg_as_str(tool_args.get("heading", ""))
        return f"Inserted new section '{heading}'"
    elif tool_name == "delete_section":
        heading = _arg_as_str(tool_args.get("section_heading", ""))
        return f"Deleted section '{heading}'"
    return f"Executed {tool_name}"


async def _summarize_span(span_text: str, llm_mgr) -> str:
    """Summarize a folded span of conversation using the active chat model.

    Uses the SAME model as the chat (via llm_mgr.generate) so the span never
    exceeds the summarizer's context window. Returns "" on failure so the caller
    can skip the checkpoint and fall back to the sliding-window safety net.
    """
    prompt = (
        "Summarize the earlier portion of a conversation between a user and a "
        "personal-wiki writing assistant. Preserve: the user's goals and requests, "
        "key facts and decisions, and any actions the assistant already completed "
        "(edits, searches, document reads) so they are not repeated. Be concise "
        "and factual.\n\n"
        "Conversation to summarize:\n"
        f"{span_text}\n\n"
        "Output ONLY the summary text. No preamble, no headings, no code fences."
    )
    try:
        result = await llm_mgr.generate(prompt)
        return (result or "").strip()
    except Exception as e:
        logger.warning("checkpoint summarization failed: %s", e)
        return ""


async def _maybe_checkpoint(
    session: ChatSession, system_prompt_tokens: int, llm_mgr
) -> AsyncGenerator[str, None]:
    """Fold the oldest span of history into one summary message when it grows large.

    Append-only consolidation: the kept (recent) suffix is preserved verbatim;
    only the oldest prefix is replaced by an LLM-generated summary. A prior
    checkpoint message is itself folded into the refreshed summary (it sits at
    the front, so it is part of the folded prefix).

    Async generator so it can surface SSE progress the same way tool calls do:
    a transient `status` while the summarization LLM call runs, then a
    persistent `activity` log entry once the fold lands. Yields nothing (and so
    is invisible to the client) on the common case where no checkpoint fires.
    Callers drain it with `async for evt in _maybe_checkpoint(...): yield evt`.
    """
    budget = llm_mgr.compute_history_budget_tokens(system_prompt_tokens)
    span = select_checkpoint_span(session.messages, budget)
    if not span:
        return
    fold_indices, keep_indices = span
    fold_msgs = [session.messages[i] for i in fold_indices]
    span_text = render_messages_for_summary(fold_msgs)
    # Surface "compaction happening" before the (potentially slow) summarize call.
    yield f"data: {json.dumps({'status': 'Compacting conversation history…'})}\n\n"
    summary = await _summarize_span(span_text, llm_mgr)
    if not summary:
        # Summarization failed - leave history intact for run_compaction_pipeline.
        return
    summary_msg = {
        "role": "user",
        "content": (
            f"{CHECKPOINT_PREFIX}\n\n{summary}\n\n"
            "(The above summarizes earlier turns that are no longer shown "
            "verbatim. Do not repeat actions described as already completed.)"
        ),
    }
    before = len(session.messages)
    log_checkpoint_fold(fold_msgs, summary_msg)
    session.messages = apply_checkpoint(session.messages, keep_indices, summary_msg)
    session.checkpoint_summary = summary
    logger.info(
        "compaction [checkpoint]: folded %d msgs into 1 summary (%d -> %d messages)",
        len(fold_indices), before, len(session.messages),
    )
    log_history_stats("post-checkpoint", session.messages)
    yield f"data: {json.dumps({'activity': f'Compacted {len(fold_indices)} earlier messages into a summary'})}\n\n"


async def _agent_loop(
    session: ChatSession, user_message: str, llm_mgr
) -> AsyncGenerator[str, None]:
    """Agent loop: iterative tool calling with scratchpad. Yields SSE events.

    Supports both document mode (single scratchpad) and wiki/global mode
    (ScratchpadCollection for multi-document operations).
    """
    try:
        # 1. Mode-dependent setup
        is_global = session.mode == "wiki"

        if is_global:
            collection = ScratchpadCollection(session.vault)
            scratchpad = None
            providers = [
                CoreInstructionsProvider(mode="wiki", vault=session.vault),
                ToolDescriptionProvider(mode="wiki"),
                InstructionsProvider(mode="wiki"),
            ]
            tool_defs = GLOBAL_TOOL_DEFINITIONS
            tool_names = _GLOBAL_TOOL_NAMES
        else:
            collection = None
            scratchpad = DocumentScratchpad(
                session.document_url_path, session.vault, session.revision)
            scratchpad.load()
            # load() clears a revision it could not resolve; mirror that back onto
            # the session so the rest of the turn agrees on which version is live.
            session.revision = scratchpad.revision
            rev_label = _revision_label(scratchpad)
            sections = _parse_document_sections(scratchpad.content)
            outline = _build_document_outline(sections)
            data_files = chunker.extract_data_file_refs(scratchpad.content)
            providers = [
                CoreInstructionsProvider(mode="document", title=scratchpad.title,
                                         url_path=session.document_url_path,
                                         revision=rev_label),
                DocumentContentProvider(scratchpad.content, outline, revision=rev_label),
                PageDataFilesProvider(data_files),
                ToolDescriptionProvider(mode="document"),
                InstructionsProvider(mode="document"),
            ]
            tool_defs = TOOL_DEFINITIONS
            tool_names = _TOOL_NAMES

        context_length = llm_mgr._context_length or 4096
        system_prompt, system_prompt_tokens = assemble_system_prompt(providers)

        # 3. Append user message, then checkpoint-summarize old history if large,
        #    then run the append-only sliding-window safety net.
        session.messages.append({"role": "user", "content": user_message})
        log_history_stats("user-added", session.messages, system_prompt)
        async for evt in _maybe_checkpoint(session, system_prompt_tokens, llm_mgr):
            yield evt
        session.messages = run_compaction_pipeline(
            session.messages,
            system_prompt_tokens=system_prompt_tokens,
            context_length=context_length,
            max_messages=llm_mgr.compute_max_messages(),
        )
        log_history_stats("post-compaction/pre-loop", session.messages, system_prompt)

        # 4. Agent loop - delegate to the shared engine (src.agent_runner), translating
        #    its structured events into SSE. Engine concerns (LLM calls, tool dispatch,
        #    approval gating, step budget) live there; chat owns the surface translation
        #    plus the change-detection / proposal logic below.
        status_label = _global_status_label if is_global else _status_label
        narration_fn = _global_activity_narration if is_global else _activity_narration

        if is_global:
            async def execute_tool(name, args, status_callback):
                assert collection is not None
                return await _execute_global_tool(
                    name, args, collection,
                    session=session, llm_mgr=llm_mgr,
                    status_callback=status_callback,
                )
            needs_approval = frozenset()
        else:
            async def execute_tool(name, args, status_callback):
                assert scratchpad is not None
                return await _execute_tool(
                    name, args, scratchpad,
                    session=session, llm_mgr=llm_mgr,
                    status_callback=status_callback,
                )
            # run_python is the only approval-gated tool, and only in document mode.
            needs_approval = frozenset({"run_python"})

        run_result = AgentRunResult()
        async for evt in run_agent_loop(
            messages=session.messages,
            system_prompt=system_prompt,
            tool_defs=tool_defs,
            tool_names=tool_names,
            llm_mgr=llm_mgr,
            execute_tool=execute_tool,
            status_label=status_label,
            activity_narration=narration_fn,
            needs_approval=needs_approval,
            approval_narration=lambda name, args: "Proposed Python to run",
            parse_tool_call_from_text=_try_parse_tool_call_from_text,
            stream_markers=PROMPT_MARKERS,
            max_iterations=MAX_AGENT_ITERATIONS,
            think=AGENT_TOOL_THINK,   # gpt-oss reasoning-channel interleave can break tool-call parsing
        ):
            etype = evt["type"]
            if etype == "result":
                res = evt["result"]
                if isinstance(res, AgentRunResult):
                    run_result = res
            elif etype == "token":
                yield f"data: {json.dumps({'token': evt['text']})}\n\n"
            elif etype == "status":
                yield f"data: {json.dumps({'status': evt['text']})}\n\n"
            elif etype == "activity":
                yield f"data: {json.dumps({'activity': evt['text']})}\n\n"
            elif etype == "retract":
                yield f"data: {json.dumps({'retract': True})}\n\n"

        # Unpack engine result into the locals the proposal logic below expects.
        # pending_python_code is the run_python case of the generic approval gate.
        final_text = run_result.final_text
        already_streamed = run_result.already_streamed
        any_tools_executed = run_result.any_tools_executed
        reached_max_steps = run_result.reached_max_steps
        activity_log = run_result.activity_log
        pending_python_code = (
            _arg_as_str(run_result.pending_approval["args"].get("code"))
            if run_result.pending_approval is not None else None
        )

        # 5. Check for changes and emit response
        has_pending_changes = False

        if pending_python_code is not None:
            # Agent proposed code: gate it behind explicit user approval. Reuse the
            # pending-changes continuation machinery so confirm resumes the loop.
            session.pending_python = {"code": pending_python_code}
            has_pending_changes = True

            if not already_streamed:
                clean_text = _clean_agent_response(final_text)
                if clean_text:
                    yield f"data: {json.dumps({'token': clean_text})}\n\n"

            action = {"action": "run_python", "code": pending_python_code}
            yield f"data: {json.dumps({'action_proposed': action})}\n\n"

        elif is_global and collection.has_changes:
            diffs = collection.get_all_diffs()
            session.pending_collection = collection
            has_pending_changes = True

            if not already_streamed:
                clean_text = _clean_agent_response(final_text)
                if clean_text:
                    yield f"data: {json.dumps({'token': clean_text})}\n\n"

            if len(diffs) == 1:
                # Single document changed - use existing action format
                action = {
                    "action": "scratchpad_changes",
                    "path": diffs[0]["path"],
                    "diff_preview": diffs[0]["diff_preview"],
                }
            else:
                # Multiple documents changed
                action = {
                    "action": "multi_document_changes",
                    "diffs": diffs,
                }
            yield f"data: {json.dumps({'action_proposed': action})}\n\n"

        elif not is_global and scratchpad.has_changes:
            diff_preview = scratchpad.get_unified_diff()
            session.pending_scratchpad = scratchpad
            has_pending_changes = True

            if not already_streamed:
                clean_text = _clean_agent_response(final_text)
                if clean_text:
                    yield f"data: {json.dumps({'token': clean_text})}\n\n"

            action = {
                "action": "scratchpad_changes",
                "path": session.document_url_path,
                "diff_preview": diff_preview,
            }
            # Applying a historical edit overwrites the CURRENT file, so the diff
            # above (revision -> edited) is not the whole story when the file has
            # moved on since. head_drift is None unless there is something to warn
            # about, leaving the ordinary card byte-identical.
            if scratchpad.is_historical:
                action["revision"] = scratchpad.revision
                action["head_drift"] = scratchpad.head_drift()
            yield f"data: {json.dumps({'action_proposed': action})}\n\n"

        # Set continuation flag so confirm endpoint knows to resume the loop
        session.had_tool_calls_before_propose = has_pending_changes and any_tools_executed
        # Store activity log on session so confirm_action can reference it
        session.last_activity_log = activity_log

        if has_pending_changes:
            pass  # Action card emitted above
        elif reached_max_steps:
            # Dead-end exhaustion with no pending changes: offer a manual resume
            # instead of the bare "(Reached maximum steps.)" token. The summary +
            # compaction steps that run just below (before the done event) leave
            # the session message history resume-ready for continue_generator.
            session.reached_max_steps = True
            yield f"data: {json.dumps({'max_steps': True})}\n\n"
        elif run_result.stream_error:
            # The model kept returning malformed responses and the loop gave up (see
            # run_agent_loop). Offer the SAME manual resume as max-steps - reuse
            # reached_max_steps as the resume gate - so the user can re-ask for another
            # round instead of hitting a silent dead end. A distinct signal lets the
            # frontend explain what happened.
            session.reached_max_steps = True
            yield f"data: {json.dumps({'stream_error': True})}\n\n"
        elif already_streamed:
            pass  # Response was already streamed live
        elif final_text:
            clean_text = _clean_agent_response(final_text)
            if clean_text:
                yield f"data: {json.dumps({'token': clean_text})}\n\n"
            else:
                fallback = "I was not able to process that request. Could you try rephrasing?"
                yield f"data: {json.dumps({'token': fallback})}\n\n"

        # 6. Append summary and compact (preserves intermediate messages)
        # Include activity log so the model retains context of completed actions
        activity_prefix = ""
        if activity_log:
            activity_prefix = "[Completed actions: " + "; ".join(activity_log) + "]\n\n"
        if has_pending_changes:
            summary_content = activity_prefix + (final_text or "(changes proposed, awaiting confirmation)")
        elif final_text:
            summary_content = activity_prefix + final_text
        else:
            summary_content = activity_prefix + "(no response)" if activity_prefix else ""

        if summary_content:
            session.messages.append({"role": "assistant", "content": summary_content})

        # Run compaction pipeline - tool results from this loop get shrunk immediately,
        # older turns get aged, and sliding window is the safety net for small contexts
        session.messages = run_compaction_pipeline(
            session.messages,
            system_prompt_tokens=system_prompt_tokens,
            context_length=context_length,
            max_messages=llm_mgr.compute_max_messages(),
        )

        # 7. Done event
        yield f"data: {json.dumps({'done': True, 'session_id': session.session_id})}\n\n"

    except Exception as e:
        logger.exception("Agent loop error")
        yield f"data: {json.dumps({'error': str(e)})}\n\n"


# ---------------------------------------------------------------------------
# Public entry point - routes to agent loop or streaming path
# ---------------------------------------------------------------------------

async def chat_response_generator(
    session: ChatSession, user_message: str, llm_mgr
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted events. Routes to agent loop or streaming path."""
    # Session boundary: a conversation's FIRST turn shares no history with
    # anything, so it is exactly the request that could otherwise start on a
    # server slot still holding another vault's context. Later turns extend this
    # conversation's own prefix and keep the cache. Armed here, in the generator
    # the request task iterates, so it lands in the same context as the calls below.
    if not session.messages:
        llm_backend.begin_cold_session()
    # Global mode always uses the agent loop (tools are essential)
    if session.mode == "wiki" or await llm_mgr.supports_tools():
        async for event in _agent_loop(session, user_message, llm_mgr):
            yield event
    else:
        async for event in _chat_response_generator_streaming(session, user_message, llm_mgr):
            yield event
