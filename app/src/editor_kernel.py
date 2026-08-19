# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Worker-side broker for editor custom-Python tools (editor Tier 2).

The editor LLM loop runs on the SERVER (it owns the SSE stream to the browser),
but the server is on tzara-net only and cannot reach the isolated jupyter-agent
kernel (agent-net). The WORKER can. So the server drives editor custom-tool
CALLS through this broker over the worker's existing agent-API app: a synchronous
`/editor/session/start` -> `/editor/session/tool`* -> `/editor/session/close`
lifecycle (service-authenticated - the trusted server calling the worker, NOT
the per-run kernel token).

The kernel itself is the SAME isolated sandbox agents use (AgentKernelSession on
`agent_jupyter_manager`), so editor tools inherit the `wiki` read/write proxy,
the HMAC token, the agent-API write-gate, `_dada` ownership, and RAG-exclusion.
The one net-new piece is the injected `editor` object (current selection /
document / frontmatter) - data the server holds and passes in at start.

Concurrency: at most ONE active editor session (one warm kernel). It's process-
local (this dict + the kernel live in the worker's embedded uvicorn), so an
in-process lock is the right primitive - NOT the global Redis agent run-lock,
which would make an interactive `/` command block behind a background agent. A
new start EVICTS any prior session (a fresh edit supersedes an abandoned one).
"""

import asyncio
import logging

from config import AGENT_TOOL_TIMEOUT_S, EDITOR_SERVICE_SECRET
from src import agent_tokens
from src import timefmt
from src.agent_kernel import AgentKernelSession

logger = logging.getLogger("editor_kernel")


# The `editor` object injected into the kernel namespace: a read-only snapshot of
# the document being edited, plus WHERE IN IT the user is. Net-new - no existing
# wiki surface exposes the current selection/buffer/caret (they address everything
# by explicit path), and an `operation: insert` tool that can't see its insertion
# point can only guess. `_ED_DATA` is injected as a repr'd literal (injection-safe:
# repr of str/dict/int escapes fully, exactly as _wiki_setup does with the token).
_EDITOR_OBJECT_BODY = '''
class _Editor:
    """The document currently being edited (read-only snapshot for THIS call):
      editor.selection   - the highlighted text ("" when nothing is selected)
      editor.document    - the text your tool operates in: the whole unsaved
                           buffer, minus the frontmatter block for a
                           `scope: document` tool (use .frontmatter for that)
      editor.frontmatter - the buffer's parsed YAML frontmatter, as a dict
      editor.path        - the document's vault path (may be "" for a new doc)

    Where the user is, as offsets into `editor.document`:
      editor.cursor          - the caret
      editor.selection_start - start of the selection
      editor.selection_end   - end of the selection
      editor.before          - everything before the selection/caret
      editor.after           - everything after it
      editor.before_cursor   - everything before the CARET specifically
      editor.after_cursor    - everything after it

    A caret is just a ZERO-WIDTH SELECTION: with nothing selected,
    selection_start == selection_end == cursor and `selection` is "", so
    .before/.after mean "before/after the cursor" without a special case - and
    the _cursor pair is identical to the plain pair.

    The two pairs differ only when the range isn't the caret. A `scope: document`
    tool's range is the whole buffer, so its .before/.after are empty while
    .before_cursor/.after_cursor still say where the user was standing when they
    invoked it - which is what an insert needs in order to write something that
    belongs at that spot.

    Corpus access (search/read/write other pages) is on the `wiki` object."""
    def __init__(self, data):
        self.selection = data.get("selection", "")
        self.document = data.get("document", "")
        self.frontmatter = data.get("frontmatter") or {}
        self.path = data.get("path", "")
        n = len(self.document)
        self.selection_start = max(0, min(int(data.get("selection_start", 0) or 0), n))
        self.selection_end = max(self.selection_start,
                                 min(int(data.get("selection_end", 0) or 0), n))
        self.cursor = max(0, min(int(data.get("cursor", 0) or 0), n))

    # Derived, not shipped: the document crosses the wire once, and these stay
    # consistent with it instead of being a second copy that can disagree.
    @property
    def before(self):
        return self.document[:self.selection_start]

    @property
    def after(self):
        return self.document[self.selection_end:]

    @property
    def before_cursor(self):
        return self.document[:self.cursor]

    @property
    def after_cursor(self):
        return self.document[self.cursor:]

editor = _Editor(_ED_DATA)
del _Editor, _ED_DATA
'''


def _editor_setup(editor_data: dict) -> str:
    return "_ED_DATA = " + repr(editor_data) + "\n" + _EDITOR_OBJECT_BODY


# --- one-active-session state (process-local) ------------------------------
_op_lock = asyncio.Lock()          # serializes start/close/evict, NOT call_tool
_session: AgentKernelSession | None = None
_session_run_id: str | None = None


async def _evict_locked() -> None:
    """Close the current session if any. Caller holds _op_lock."""
    global _session, _session_run_id
    if _session is not None:
        try:
            await _session.close()
        except Exception:
            logger.exception("editor kernel evict/close failed")
        _session = None
        _session_run_id = None


async def start_session(run_id: str, slug: str, vault: str, py_source: str,
                        editor_data: dict, mode: str = "propose") -> None:
    """Start (evicting any prior) the one warm editor kernel for this run."""
    global _session, _session_run_id
    async with _op_lock:
        await _evict_locked()
        # agent_slug = "editors/{slug}" so the token's owned area is
        # _dada/editors/{slug}/ (the agent-API /write prefix check + the
        # write_agent_output path both key off this claim). One reused code path.
        owner = f"editors/{slug}"
        token = agent_tokens.mint(owner, vault, run_id, mode=mode)
        session = AgentKernelSession(
            run_id=run_id, agent_slug=owner, vault_id=vault,
            py_source=py_source, token=token,
            extra_setup=_editor_setup(editor_data))
        await session.start()
        _session = session
        _session_run_id = run_id
        logger.info("editor kernel session started: %s (%s:%s)", run_id, slug, vault)


async def call_session_tool(run_id: str, name: str, args: dict) -> str:
    """Execute one custom tool call in the active editor kernel."""
    if _session is None or _session_run_id != run_id:
        return f"{name}: editor kernel session not found (it may have been superseded)."
    return await _session.call_tool(name, args or {}, timeout=AGENT_TOOL_TIMEOUT_S)


async def close_session(run_id: str) -> None:
    global _session, _session_run_id
    async with _op_lock:
        if _session is not None and _session_run_id == run_id:
            await _evict_locked()


# --- op:note external digest -----------------------------------------------

async def note_append(vault: str, slug: str, output: str, source: str,
                      text: str) -> str:
    """Append `text` as a timestamped entry to the editor's owned digest page
    (`_dada/editors/{slug}/{output}`), GROWING it across calls (and across the
    different documents the tool is run from). Reuses the agent owned-area writer,
    so RAG-exclusion + git-commit-of-_dada come for free. Session-independent: a
    pure-prompt (no-python) editor can use op:note too.
    """
    from config import AGENT_OUTPUT_DIR
    from src.background_agents import write_agent_output
    from src.wikidoc import WikiDoc

    owner = f"editors/{slug}"
    rel = f"{AGENT_OUTPUT_DIR}/{owner}/{output}"
    # WikiDoc.read_text returns (content_LF, eol) or None (absent on first call).
    raw = WikiDoc.read_text(vault, rel)
    prior_body = WikiDoc.strip_frontmatter(raw[0]).strip() if raw else ""
    ts = timefmt.iso_local()
    entry = f"## {source or 'note'} - {ts}\n\n{text.strip()}\n"
    new_body = (prior_body + "\n\n" + entry) if prior_body else entry
    return await write_agent_output(vault, owner, output, new_body)


async def http_note(request):
    from starlette.responses import JSONResponse
    if not _service_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await request.json()
    try:
        written = await note_append(
            vault=data["vault"], slug=data["slug"],
            output=data.get("output") or "Notes.md",
            source=data.get("source", ""), text=data.get("text", ""))
    except Exception as e:
        logger.exception("editor note append failed")
        return JSONResponse({"error": f"note write failed: {e}"}, status_code=500)
    return JSONResponse({"result": {"path": written}})


# --- cross-invocation memory (opt-in `memory:`) ----------------------------

async def http_memory(request):
    """Persist an editor's cross-invocation memory: the consolidated note, the
    append-only ledger operations, or both.

    The reserved consolidation turn and the ledger turn both run on the SERVER
    (it has the loop transcript + ollama); only the owned-area WRITE needs the
    worker (git + RAG-exclusion). Reuses the agent writers verbatim -
    `editors/{slug}` lands at `_dada/editors/{slug}/`.

    Both halves are optional and independent: a mid-loop `remember` posts ops
    with no text, and a consolidation that produced nothing must not blank the
    note (the no-clobber rule) while its ledger ops still land."""
    from starlette.responses import JSONResponse
    if not _service_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from src.background_agents import apply_ledger_ops, write_agent_memory
    data = await request.json()
    owner = f"editors/{data['slug']}"
    result = {}
    try:
        text = (data.get("text") or "").strip()
        if text:
            result["path"] = await write_agent_memory(data["vault"], owner, text)
        ops = data.get("ledger_ops") or []
        if ops:
            result["ledgers"] = await apply_ledger_ops(data["vault"], owner, ops)
    except Exception as e:
        logger.exception("editor memory write failed")
        return JSONResponse({"error": f"memory write failed: {e}"}, status_code=500)
    return JSONResponse({"result": result})


async def http_log(request):
    """Write a per-invocation editor log page. The SERVER assembles the markdown
    body (it holds the run's input/activity/memory-before-after); the worker just
    lands it in the owned area at _dada/editors/{slug}/logs/{ts}.md (RAG-excluded,
    git-committed). Unique timestamp filename => no overwrite."""
    from starlette.responses import JSONResponse
    if not _service_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from src.background_agents import write_agent_output
    data = await request.json()
    ts = data.get("ts") or ""
    try:
        written = await write_agent_output(
            data["vault"], f"editors/{data['slug']}", f"logs/{ts}.md",
            data.get("body", ""), title=f"Run {ts}")
    except Exception as e:
        logger.exception("editor log write failed")
        return JSONResponse({"error": f"log write failed: {e}"}, status_code=500)
    return JSONResponse({"result": {"path": written}})


# --- HTTP handlers (mounted on the worker's agent_api app) ------------------

def _service_authed(request) -> bool:
    return request.headers.get("x-editor-service-secret", "") == EDITOR_SERVICE_SECRET


async def http_start(request):
    from starlette.responses import JSONResponse
    if not _service_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await request.json()
    try:
        await start_session(
            run_id=data["run_id"], slug=data["slug"], vault=data["vault"],
            py_source=data.get("py_source", ""),
            editor_data=data.get("editor_data") or {},
            mode=data.get("mode", "propose"))
    except Exception as e:
        logger.exception("editor session start failed")
        return JSONResponse({"error": f"start failed: {e}"}, status_code=500)
    return JSONResponse({"result": {"status": "started"}})


async def http_tool(request):
    from starlette.responses import JSONResponse
    if not _service_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await request.json()
    result = await call_session_tool(
        data["run_id"], data.get("name", ""), data.get("args") or {})
    return JSONResponse({"result": result})


async def http_close(request):
    from starlette.responses import JSONResponse
    if not _service_authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    data = await request.json()
    await close_session(data["run_id"])
    return JSONResponse({"result": {"status": "closed"}})
