# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The agent kernel: where agent-file custom tools execute.

A SECOND jupyter server (`jupyter-agent` compose service) with NO vault mount
and membership in agent-net only - it reaches the open internet (for pinned-
destination tools like yfinance) and the worker's agent-API, but has no route
to Postgres/Ollama/the browser server and no filesystem vault access. The
sandbox holds arbitrary-code tools only; the loop, LLM calls, and internal
tools stay worker-side (gap #3).

Per run: a fresh kernel (page_id = run_id) is seeded with (a) the `wiki`
REST proxy pinned to the agent-API base + this run's HMAC token, and (b) the
agent file's python source. Each tool call executes `fn(**args)` and captures
text output; a per-call deadline INTERRUPTS the kernel (clean tool error, the
kernel survives). The kernel is deleted at run end - restart-between-runs
means no namespace bleed between agents/runs. A prune at session start reaps
any kernels orphaned by a dead worker.
"""

import asyncio
import inspect
import json
import logging

from config import (
    AGENT_API_URL,
    AGENT_TOOL_TIMEOUT_S,
    JUPYTER_AGENT_HOST,
    JUPYTER_AGENT_WS,
)
from src import arg_coercion
from src.jupyter_client import AsyncJupyterManager

logger = logging.getLogger("agent_kernel")

# Separate manager instance = separate server + separate kernel/connection
# caches. The interactive `jupyter_manager` singleton is untouched.
agent_jupyter_manager = AsyncJupyterManager(host=JUPYTER_AGENT_HOST,
                                            ws=JUPYTER_AGENT_WS)

# The in-kernel `wiki` proxy for AGENT kernels: pure stdlib, pinned to the
# worker-hosted agent-API with a Bearer token. Unlike the interactive twin it
# carries read/write (both server-enforced: reads see the run's staged
# overlay; writes go through the write gate - staged for human space, direct
# only inside the agent's own output folder, refused for system vaults).
_AGENT_WIKI_BODY = '''
class _AgentWiki:
    """This run's wiki access. search/related/tagged/backlinks/frontmatter
    query the vault index; read(path) returns page text (including your own
    staged edits); write(path, content, note="") STAGES a proposal for human
    review unless the path is inside your own output folder;
    write_file(path, data, note="") saves a binary attachment (png/csv/...)
    inside your own output folder ONLY - link it from your pages.

    For TARGETED changes, prefer the section/link methods over write():
    outline(path) and readSection(path, heading) to look, then
    editSection/insertSection/deleteSection to change one named section, or
    addLink/removeLink for a single '## Related' entry. They leave the rest of
    the page byte-identical, so a tool can encode an exact recipe rather than
    regenerating a document it only partly understands - and the human reviews
    a diff the size of the actual change. Same gate as write().

    For composing analytics in Python (instead of chaining tools yourself),
    queryDocuments()/queryEdges()/queryDocumentTags() return whole-vault
    metadata tables as row lists (wrap in pandas.DataFrame(...) to filter/join),
    and list_orphans/find_near_duplicates/find_missing_links/list_stale_stubs
    return the same structured findings the analysis capabilities use.

    as_int(v, default)/as_float(v, default)/as_str(v, default) coerce malformed
    model args (bracket-wrapped scalars, one-element lists, ...). See the
    help/wiki-object.md page for the authoritative reference to this object and
    its interactive page-kernel twin."""
    def __init__(self, base, token):
        self._base, self._token = base, token
    def _post(self, route, payload):
        import json as _json
        import urllib.request as _req
        import urllib.error as _err
        body = _json.dumps(payload).encode("utf-8")
        request = _req.Request(
            self._base + route, data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + self._token})
        try:
            with _req.urlopen(request, timeout=30) as resp:
                return _json.load(resp).get("result")
        except _err.HTTPError as e:
            try:
                msg = _json.load(e).get("error", str(e))
            except Exception:
                msg = str(e)
            raise RuntimeError("wiki call failed: " + str(msg)) from None
    def _query(self, op, **args):
        return self._post("/query", {"op": op, "args": args})
    def search(self, query, top_k=10):
        return self._query("search", query=query, top_k=top_k)
    def related(self, path, top_k=10):
        return self._query("related", path=path, top_k=top_k)
    def tagged(self, tag):
        return self._query("tagged", tag=tag)
    def backlinks(self, path):
        return self._query("backlinks", path=path)
    def frontmatter(self, path):
        return self._query("frontmatter", path=path)
    def _query_all(self, op):
        """Whole-table read, KEYSET-paginated to COMPLETENESS. The server returns
        one page + an opaque cursor; we loop on it and return the full row list
        (DataFrame-safe), so vault size never silently caps the result."""
        rows, cursor, pages = [], None, 0
        while True:
            page = self._query(op, after=cursor)
            if not isinstance(page, dict):        # defensive: legacy bare list
                return page
            rows.extend(page.get("rows", []))
            pages += 1
            if not page.get("has_more") or pages >= 10000:  # runaway guard
                if pages >= 10000:
                    import sys as _sys
                    print(f"[wiki] WARNING: {op} stopped after {len(rows)} rows "
                          f"(pagination guard hit) - result may be incomplete.",
                          file=_sys.stderr)
                break
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        return rows
    def queryDocuments(self):
        return self._query_all("queryDocuments")
    def queryEdges(self):
        return self._query_all("queryEdges")
    def queryDocumentTags(self):
        return self._query_all("queryDocumentTags")
    def list_orphans(self, path_prefix="", limit=50):
        return self._query("list_orphans", path_prefix=path_prefix, limit=limit)
    def find_near_duplicates(self, path_prefix="", threshold=0.88, limit=30):
        return self._query("find_near_duplicates", path_prefix=path_prefix,
                           threshold=threshold, limit=limit)
    def find_missing_links(self, path_prefix="", low=0.62, high=0.88, limit=40):
        return self._query("find_missing_links", path_prefix=path_prefix,
                           low=low, high=high, limit=limit)
    def list_stale_stubs(self, path_prefix="", max_chars=400, stale_days=180, limit=40):
        return self._query("list_stale_stubs", path_prefix=path_prefix,
                           max_chars=max_chars, stale_days=stale_days, limit=limit)
    def read(self, path):
        return self._post("/read", {"path": path})
    def write(self, path, content, note=""):
        return self._post("/write", {"path": path, "content": content, "note": note})
    def write_file(self, path, data, note=""):
        import base64 as _b64
        if isinstance(data, str):
            data = data.encode("utf-8")
        return self._post("/write", {"path": path, "note": note,
                                     "content_b64": _b64.b64encode(data).decode("ascii")})

    # --- targeted edits -------------------------------------------------
    # write() replaces a whole page. These change ONE named part of it and
    # leave the rest byte-identical, so a tool can state an exact recipe
    # instead of rebuilding a document it only partly understands.
    # `heading` takes the heading text with or without '#' marks, matched
    # case-insensitively; '(top)' is the text above the first heading; pass
    # `index` (from outline()) when a page repeats a heading.
    def _edit(self, op, path, **kw):
        payload = {"op": op, "path": path}
        payload.update({k: v for k, v in kw.items() if v is not None})
        return self._post("/edit", payload)
    def outline(self, path):
        """Heading tree with the indices the other methods accept."""
        return self._edit("outline", path)
    def readSection(self, path, heading, index=None):
        """Just that section's body text (reads through your staged edits)."""
        return self._edit("readSection", path, heading=heading, index=index)
    def editSection(self, path, heading, content, index=None, note=""):
        """Replace one section's body; its heading and the rest of the page stay."""
        return self._edit("sectionEdit", path, heading=heading,
                          content=content, index=index, note=note)
    def insertSection(self, path, heading, content, position="after",
                      reference=None, reference_index=None, note=""):
        """Add a section before/after `reference` (end of page if omitted)."""
        return self._edit("sectionInsert", path, heading=heading, content=content,
                          position=position, reference=reference,
                          reference_index=reference_index, note=note)
    def deleteSection(self, path, heading, index=None, note=""):
        """Remove a section - heading, body, and anything nested under it."""
        return self._edit("sectionDelete", path, heading=heading,
                          index=index, note=note)
    def addLink(self, path, target, reason=""):
        """Add `- [[target]]` under `path`'s '## Related' section (idempotent)."""
        return self._edit("addLink", path, target=target, reason=reason)
    def removeLink(self, path, target, reason=""):
        """Remove a '## Related' bullet linking to `target`. Only plain link
        bullets - prose mentions and task items are reported, never edited."""
        return self._edit("removeLink", path, target=target, reason=reason)

wiki = _AgentWiki(_AGENT_API_BASE, _AGENT_TOKEN)
del _AgentWiki, _AGENT_API_BASE, _AGENT_TOKEN
'''


# Custom tools run in this isolated kernel and CANNOT import project code, so
# the arg-coercion helpers can't be `import`ed here - they're lifted verbatim
# from the canonical src/arg_coercion.py via inspect.getsource (single source of
# truth, no drift) and attached to the `wiki` object as as_int/as_float/as_str.
# The functions recurse on their own names, so they stay as kernel globals
# (json/re too - both stdlib, already available in the sandbox); not deleted.
_ARG_COERCION_SETUP = (
    "# --- shared arg-coercion helpers (canonical: src/arg_coercion.py) ---\n"
    "import json\n"
    "import re\n"
    + inspect.getsource(arg_coercion.arg_as_str)
    + inspect.getsource(arg_coercion.arg_as_int)
    + inspect.getsource(arg_coercion.arg_as_float)
    + "wiki.as_str = arg_as_str\n"
    "wiki.as_int = arg_as_int\n"
    "wiki.as_float = arg_as_float\n"
)


def _wiki_setup(token: str) -> str:
    return ("_AGENT_API_BASE = " + repr(AGENT_API_URL) + "\n"
            "_AGENT_TOKEN = " + repr(token) + "\n"
            + _AGENT_WIKI_BODY
            + _ARG_COERCION_SETUP)


# Tool-call wrapper executed in the kernel: json args -> fn(**kwargs) ->
# print result (str passthrough, else repr). Errors surface as tracebacks via
# the normal error message, which the drain loop captures.
_CALL_TEMPLATE = """
import json as _aj
_a_args = _aj.loads({args_json!r})
_a_res = {fn}(**_a_args)
print(_a_res if isinstance(_a_res, str) else repr(_a_res))
del _aj, _a_args, _a_res
"""


class AgentKernelSession:
    """One kernel for one agent run. start() -> call_tool()* -> close()."""

    def __init__(self, run_id: str, agent_slug: str, vault_id: str,
                 py_source: str, token: str, extra_setup: str = ""):
        self.run_id = run_id
        self.agent_slug = agent_slug
        self.vault_id = vault_id
        self.py_source = py_source
        self.token = token
        # Optional silent setup injected AFTER the `wiki` proxy but BEFORE the
        # author's py_source, so the source's functions can reference whatever it
        # defines (the editor path uses it to inject the `editor` data object).
        self.extra_setup = extra_setup
        self.conn = None
        self.kernel_id = None

    async def start(self) -> None:
        await prune_agent_kernels()
        # cwd/vault deliberately omitted: no chdir, no interactive-wiki seeding.
        self.conn = await agent_jupyter_manager.get_or_create_connection(self.run_id)
        self.kernel_id = agent_jupyter_manager.kernels.get(self.run_id)
        await self.conn.run_setup(_wiki_setup(self.token))
        if self.extra_setup:
            await self.conn.run_setup(self.extra_setup)
        await self.conn.run_setup(self.py_source)
        logger.info("agent kernel %s started for %s:%s",
                    self.kernel_id, self.agent_slug, self.vault_id)

    async def call_tool(self, name: str, args: dict,
                        timeout: float = AGENT_TOOL_TIMEOUT_S) -> str:
        """Execute one custom tool call; always returns a result string."""
        if self.conn is None:
            return f"{name}: agent kernel is not running."
        code = _CALL_TEMPLATE.format(args_json=json.dumps(args or {}), fn=name)
        msg_id, queue = await self.conn.execute(code)

        stdout: list[str] = []
        results: list[str] = []
        error: list[str] = []
        interrupted = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    # Two-layer timeouts, layer 1: INTERRUPT the running cell.
                    # The kernel survives; the aborted execution produces a
                    # KeyboardInterrupt error reply that we don't wait for.
                    interrupted = True
                    if self.kernel_id:
                        await agent_jupyter_manager.interrupt_kernel(self.kernel_id)
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue
                mtype = msg.get("msg_type")
                content = msg.get("content", {})
                if mtype == "stream":
                    stdout.append(content.get("text", ""))
                elif mtype in ("execute_result", "display_data"):
                    text = (content.get("data") or {}).get("text/plain")
                    if text:
                        results.append(text)
                elif mtype == "error":
                    from src.agent_python import _strip_ansi
                    error.append(_strip_ansi("\n".join(content.get("traceback", []))))
                elif mtype == "input_request":
                    await self.conn.send_input_reply("")
                elif mtype == "status" and content.get("execution_state") == "idle":
                    break
        finally:
            self.conn.remove_pending_execution(msg_id)

        if interrupted:
            # The KeyboardInterrupt error makes Jupyter ABORT whatever execute
            # request follows it (stop_on_error semantics). Absorb that penalty
            # on a throwaway execution so the NEXT real tool call runs clean.
            await self._flush_after_interrupt()
            return (f"{name}: timed out after {timeout:.0f}s and was interrupted. "
                    "The call produced no result.")
        if error:
            return f"{name}: error -\n" + "\n".join(error)
        out = "".join(stdout).strip()
        extra = "\n".join(results).strip()
        combined = "\n".join(x for x in (out, extra) if x).strip()
        return combined or f"{name}: (no output)"

    async def _flush_after_interrupt(self) -> None:
        msg_id, queue = await self.conn.execute("pass")
        try:
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 10
            while loop.time() < deadline:
                try:
                    msg = await asyncio.wait_for(queue.get(),
                                                 timeout=deadline - loop.time())
                except asyncio.TimeoutError:
                    break
                if (msg.get("msg_type") == "status"
                        and msg.get("content", {}).get("execution_state") == "idle"):
                    break
        finally:
            self.conn.remove_pending_execution(msg_id)

    async def close(self) -> None:
        """Delete the kernel - restart-between-runs, no namespace bleed."""
        if self.kernel_id is None:
            return
        try:
            import httpx
            async with httpx.AsyncClient() as client:
                await agent_jupyter_manager._delete_kernel(client, self.kernel_id)
        except Exception:
            logger.exception("agent kernel cleanup failed for %s", self.kernel_id)
        finally:
            self.conn = None
            self.kernel_id = None


async def prune_agent_kernels(max_age_seconds: int = 7200) -> None:
    """Safety net at run start: reap agent kernels orphaned by a dead worker.
    Trivial thanks to the serializing queue - at most one run's kernel should
    exist at any time."""
    try:
        await agent_jupyter_manager.prune_stale_kernels(max_age_seconds)
    except Exception as e:
        logger.debug("prune_agent_kernels: %s (agent jupyter may be starting)", e)
