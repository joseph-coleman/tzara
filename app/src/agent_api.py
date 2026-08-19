# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The agent-API: the token-gated surface the agent kernel's `wiki` proxy
calls back into. Hosted ON THE WORKER (a small uvicorn task started at
WORKER_STARTUP), reachable only over agent-net - the browser-facing server is
not on that network, so the kernel cannot reach browser routes at all
(isolation = network membership, not routing; per the gap-#3 design review the
listener's only client is a kernel executing a tool call THIS worker initiated,
so colocation is lifecycle-correct and there is no wait-cycle: these handlers
never await the kernel).

Every request carries a Bearer token minted for the current run
(src.agent_tokens). Vault and run identity come from the VERIFIED CLAIMS,
never the request body - the kernel can't ask for another vault. Handlers are
async with blocking work pushed to threads so a slow query can't starve the
worker's event loop (kernel heartbeats included).

Endpoints (POST, JSON):
- /query {op, args}            -> kernel_api.run_query (search/related/tagged/
                                  backlinks/frontmatter), vault from claims
- /read  {path}                -> write_gate.read_through (the run's staged
                                  overlay - an agent sees its own proposals)
- /write {path, content, note} -> the write gate decides: owned area = direct
                                  (write_agent_output), human space = gated
                                  (gated_write: staged in propose mode, applied
                                  with a checkpoint in act-with-checkpoint mode
                                  - mode from token claims), system = refused.
  Binary: {path, content_b64, note} - owned area ONLY, extension must be in
  ATTACHMENT_FILE_TYPES (write_agent_attachment); never stageable.
"""

import asyncio
import base64
import logging

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from config import AGENT_API_PORT

logger = logging.getLogger("agent_api")


def _claims(request: Request) -> dict | None:
    from src import agent_tokens
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    return agent_tokens.verify(auth[7:])


async def _query(request: Request):
    claims = _claims(request)
    if claims is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from src.kernel_api import KernelApiError, run_query
    data = await request.json()
    try:
        result = await asyncio.to_thread(
            run_query, data.get("op", ""), data.get("args") or {}, claims["vault"])
        return JSONResponse({"result": result})
    except KernelApiError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        logger.exception("agent-api query failed")
        return JSONResponse({"error": "internal error"}, status_code=500)


async def _read(request: Request):
    claims = _claims(request)
    if claims is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from src import write_gate
    data = await request.json()
    path = (data.get("path") or "").strip().lstrip("/")
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)

    def _do_read():
        # Reads must see THIS run's staged overlay: restore the run context in
        # the worker thread (ContextVars don't cross to_thread from here since
        # the HTTP request is a different asyncio task than the agent loop).
        token = write_gate.set_run_context(claims["run_id"], claims["agent"],
                                           claims.get("mode", "propose"))
        try:
            return write_gate.read_through(claims["vault"], path)
        finally:
            write_gate.reset_run_context(token)

    try:
        content = await asyncio.to_thread(_do_read)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if content is None:
        return JSONResponse({"error": f"not found: {path}"}, status_code=404)
    return JSONResponse({"result": content})


async def _write(request: Request):
    claims = _claims(request)
    if claims is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from src import write_gate
    from src.background_agents import write_agent_attachment, write_agent_output
    data = await request.json()
    path = (data.get("path") or "").strip().lstrip("/")
    content = data.get("content")
    content_b64 = data.get("content_b64")
    note = (data.get("note") or "").strip()
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)
    if (content is None) == (content_b64 is None):
        return JSONResponse(
            {"error": "exactly one of 'content' (text) or 'content_b64' (binary) required"},
            status_code=400)
    if content is not None and not isinstance(content, str):
        return JSONResponse({"error": "'content' must be a string"}, status_code=400)

    try:
        verdict = write_gate.classify_write(claims["vault"], path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    if verdict == "refuse_system":
        return JSONResponse(
            {"error": "system vaults are human-only (blessing store)"}, status_code=403)

    if verdict == "refuse_reserved":
        return JSONResponse(
            {"error": "reserved control paths (dotfolders like .tzara/) are "
                      "non-content and not agent-writable"}, status_code=403)

    if verdict == "owned_direct":
        # Direct write, but ONLY inside this agent's own subfolder of the
        # owned area - the token's agent claim is the boundary.
        from config import AGENT_OUTPUT_DIR
        expected_prefix = f"{AGENT_OUTPUT_DIR}/{claims['agent']}/"
        if not path.startswith(expected_prefix):
            return JSONResponse(
                {"error": f"owned-area writes must stay under {expected_prefix}"},
                status_code=403)
        rel_inside = path[len(expected_prefix):]
        if content_b64 is not None:
            # Binary attachment (chart PNGs etc.). ~20 MB raw cap; extension
            # allowlist enforced by write_agent_attachment.
            if not isinstance(content_b64, str) or len(content_b64) > 28 * 1024 * 1024:
                return JSONResponse({"error": "binary payload too large (20 MB cap)"},
                                    status_code=413)
            try:
                blob = base64.b64decode(content_b64, validate=True)
            except Exception:
                return JSONResponse({"error": "invalid base64 in content_b64"},
                                    status_code=400)
            try:
                written = await write_agent_attachment(
                    claims["vault"], claims["agent"], rel_inside, blob)
            except ValueError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            return JSONResponse({"result": {"status": "written", "path": written}})
        written = await write_agent_output(
            claims["vault"], claims["agent"], rel_inside, content)
        return JSONResponse({"result": {"status": "written", "path": written}})

    if content_b64 is not None:
        return JSONResponse(
            {"error": "binary content cannot be staged into human space "
                      "(no binary diff review); write it under your owned folder "
                      f"(_dada/{claims['agent']}/...) and link it from your pages"},
            status_code=400)

    def _do_gated():
        # Mode comes from verified token claims (minted from the blessed file);
        # a stale token without the claim degrades safe-closed to propose.
        mode = claims.get("mode", "propose")
        token = write_gate.set_run_context(claims["run_id"], claims["agent"], mode)
        try:
            return mode, write_gate.gated_write(claims["vault"], path, content, note=note)
        finally:
            write_gate.reset_run_context(token)

    mode, msg = await asyncio.to_thread(_do_gated)
    status = "applied" if mode == "act-with-checkpoint" else "staged"
    return JSONResponse({"result": {"status": status, "detail": msg}})


# ---------------------------------------------------------------------------
# Section- and link-scoped edits
#
# /write replaces a whole file. That is fine when the tool composed the whole
# file, but a human-authored tool usually wants a RECIPE: "rewrite the Status
# section", "file this link", "drop that link" - stated exactly, with the rest
# of the page provably untouched. These ops give it that.
#
# Every op delegates to the SAME src.agent_capabilities function the declarative
# capability menu calls, so the kernel surface and the tool menu cannot drift:
# identical addressing, identical splicing, identical gate, identical messages.
# ---------------------------------------------------------------------------

_EDIT_READ_OPS = {"outline", "readSection"}
_EDIT_WRITE_OPS = {"sectionEdit", "sectionInsert", "sectionDelete",
                   "addLink", "removeLink"}


def _edit_dispatch(op: str, vault: str, data: dict):
    """Run one /edit op. Called inside the run context (see _edit)."""
    from src import agent_capabilities as ac
    from src import md_sections, write_gate

    path = (data.get("path") or "").strip().lstrip("/")
    note = (data.get("note") or "").strip()

    if op == "outline":
        return ac.get_outline(vault, path)

    if op == "readSection":
        text = write_gate.read_through(vault, ac._doc_id(path))
        if text is None:
            raise ValueError(f"not found: {path}")
        sections = md_sections.parse_sections(text)
        section, err = ac._resolve_section(sections, data.get("heading") or "",
                                           data.get("index"))
        if err:
            raise ValueError(err)
        return md_sections.section_body(text, section)

    if op == "sectionEdit":
        return ac.propose_section_edit(
            vault, path, data.get("heading") or "", data.get("content") or "",
            section_index=data.get("index"), note=note)

    if op == "sectionInsert":
        return ac.propose_section_insert(
            vault, path, data.get("heading") or "", data.get("content") or "",
            position=data.get("position") or "after",
            reference_section=data.get("reference") or "",
            reference_section_index=data.get("reference_index"), note=note)

    if op == "sectionDelete":
        return ac.propose_section_delete(
            vault, path, data.get("heading") or "",
            section_index=data.get("index"), note=note)

    if op == "addLink":
        return ac.apply_wikilink(vault, path, data.get("target") or "",
                                 reason=data.get("reason") or "")

    if op == "removeLink":
        return ac.remove_wikilink(vault, path, data.get("target") or "",
                                  reason=data.get("reason") or "")

    raise ValueError(f"unknown edit op: {op}")


async def _edit(request: Request):
    claims = _claims(request)
    if claims is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    from src import write_gate
    data = await request.json()
    op = (data.get("op") or "").strip()
    if op not in _EDIT_READ_OPS | _EDIT_WRITE_OPS:
        return JSONResponse({"error": f"unknown edit op: {op}"}, status_code=400)
    path = (data.get("path") or "").strip().lstrip("/")
    if not path:
        return JSONResponse({"error": "path required"}, status_code=400)

    # Location gate for writes, identical to /write's. Reads are already
    # confined to this vault by the token's vault claim.
    if op in _EDIT_WRITE_OPS:
        try:
            verdict = write_gate.classify_write(claims["vault"], path)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if verdict == "refuse_system":
            return JSONResponse(
                {"error": "system vaults are human-only (blessing store)"},
                status_code=403)
        if verdict == "refuse_reserved":
            return JSONResponse(
                {"error": "reserved control paths (dotfolders like .tzara/) are "
                          "non-content and not agent-writable"}, status_code=403)
        if verdict == "owned_direct":
            return JSONResponse(
                {"error": "section/link ops target reviewable vault pages; your "
                          "own output folder is written whole with wiki.write()"},
                status_code=400)

    def _run():
        # Same context restoration as /read - these ops read through this run's
        # staged overlay and write back through the gate.
        mode = claims.get("mode", "propose")
        token = write_gate.set_run_context(claims["run_id"], claims["agent"], mode)
        try:
            return mode, _edit_dispatch(op, claims["vault"], data)
        finally:
            write_gate.reset_run_context(token)

    try:
        mode, result = await asyncio.to_thread(_run)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception:
        logger.exception("agent-api edit failed (op=%s)", op)
        return JSONResponse({"error": "internal error"}, status_code=500)

    if op in _EDIT_READ_OPS:
        return JSONResponse({"result": result})
    status = "applied" if mode == "act-with-checkpoint" else "staged"
    return JSONResponse({"result": {"status": status, "detail": result}})


# Editor Tier 2: the same embedded worker server also brokers editor custom-tool
# execution for the interactive server (service-authed, not per-run-token gated).
# Kept on THIS app so the worker still runs exactly one listener; the handlers
# live in editor_kernel to keep the token-gated agent surface above separate from
# the service-gated editor surface.
from src import editor_kernel  # noqa: E402

app = Starlette(routes=[
    Route("/query", _query, methods=["POST"]),
    Route("/read", _read, methods=["POST"]),
    Route("/write", _write, methods=["POST"]),
    Route("/edit", _edit, methods=["POST"]),
    Route("/editor/session/start", editor_kernel.http_start, methods=["POST"]),
    Route("/editor/session/tool", editor_kernel.http_tool, methods=["POST"]),
    Route("/editor/session/close", editor_kernel.http_close, methods=["POST"]),
    Route("/editor/note", editor_kernel.http_note, methods=["POST"]),
    Route("/editor/memory", editor_kernel.http_memory, methods=["POST"]),
    Route("/editor/log", editor_kernel.http_log, methods=["POST"]),
])


_server: uvicorn.Server | None = None


async def serve() -> None:
    """Run the agent-API inside the worker's event loop (WORKER_STARTUP task).

    uvicorn is EMBEDDED here - the taskiq worker owns the process. Two
    consequences (learned the hard way):
    - uvicorn must NOT capture SIGTERM/SIGINT: it would swallow taskiq's
      process-management signals, leaving a zombie worker holding the port
      while the manager spawns replacements that can't bind (crash loop).
    - a bind failure must NOT be fatal: uvicorn raises SystemExit, which
      would take the whole worker down; the worker is useful without the
      agent-API (only kernel wiki calls need it).
    """
    import contextlib

    global _server
    config = uvicorn.Config(app, host="0.0.0.0", port=AGENT_API_PORT,
                            log_level="warning", loop="none")
    _server = uvicorn.Server(config)
    # Neutralize signal handling across uvicorn versions: older versions call
    # install_signal_handlers(), newer wrap serve() in capture_signals().
    _server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    if hasattr(_server, "capture_signals"):
        _server.capture_signals = contextlib.nullcontext  # type: ignore[assignment]
    logger.info("agent-api listening on :%d", AGENT_API_PORT)
    try:
        await _server.serve()
    except (SystemExit, Exception) as e:  # noqa: BLE001
        logger.error("agent-api server exited: %r (worker continues without it)", e)


def shutdown() -> None:
    if _server is not None:
        _server.should_exit = True
