# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Server-side execution of agent-authored Python in a page's shared kernel.

The chat agent's `run_python` tool runs here. The agent runs inside
`tzaraserver`, but the kernel lives in the separate `jupyterserver` container;
we reach it through the same `AsyncJupyterManager` the browser uses
(`get_or_create_connection` → `execute`), so the agent shares the EXACT kernel the
open page uses - including the `wiki` query object injected at spawn and any
variables the user's own cells already produced.

Kernels are keyed by the browser's `page_id` (its `window.location.pathname`,
e.g. `/wiki/main/Sports/Baseball`). For sharing to actually happen, we must use
that identical string, which the frontend now sends with the chat request and we
stash on the session (`session.page_id`). Reconstructing it from the stored
doc path is avoided because percent-encoding/`.md`-suffix mismatches would key a
DIFFERENT kernel and silently break sharing.

Output handling mirrors the existing cell path: text (stdout / `text/plain` /
tracebacks) is returned to the model as the tool result; every `image/png` is a
base64 payload handed to `emit_artifact` so the chat UI can render it inline as a
`data:` URL - nothing is written to disk.
"""

import asyncio
import logging
import os
import re
import time

from config import vault_abs_root
from src import vault_registry

logger = logging.getLogger("agent_python")

# Wall-clock cap so a runaway/blocking cell can never hang the chat SSE stream.
DEFAULT_TIMEOUT = 45.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Tracebacks arrive with ANSI color codes; strip them for the model."""
    return _ANSI_RE.sub("", text or "")


def _page_kernel_target(session) -> "tuple[str, str, str] | None":
    """Resolve (page_id, cwd, vault) for the session's open page.

    Prefers the exact `page_id` the frontend sent (so the kernel key matches the
    one the page's own cells use). Falls back to reconstructing it from the
    session's vault + doc path. Returns None for non-vault / global sessions.
    """
    page_id = (getattr(session, "page_id", "") or "").strip()
    if not page_id and session.vault and session.document_url_path:
        rel = session.document_url_path
        if rel.lower().endswith(".md"):
            rel = rel[:-3]
        page_id = f"/wiki/{session.vault}/{rel}"
    if not page_id:
        return None

    parts = [p for p in page_id.split("/") if p]
    if len(parts) < 2 or parts[0] != "wiki":
        return None
    vault = parts[1]
    if not vault_registry.vault_exists(vault):
        return None
    # chdir into the folder that CONTAINS the page (drop the final segment), so
    # the kernel resolves the page's attachments by their relative link name.
    folder_parts = parts[2:-1]
    cwd = os.path.join(vault_abs_root(vault), *folder_parts)
    if not os.path.isdir(cwd):
        cwd = vault_abs_root(vault)
    return page_id, cwd, vault


async def execute_in_page_kernel(
    code: str,
    *,
    session,
    jupyter_manager,
    emit_artifact=None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Run `code` in the session page's shared kernel; return model-facing text.

    `emit_artifact`, if given, is an async callable invoked once per generated
    figure with `{"type": "image", "data": <base64 png>}`. The returned string is
    the textual transcript (stdout + results + traceback + one note per figure)
    for the model; it never contains the base64 image data.
    """
    code = (code or "").strip()
    if not code:
        return "Error: no code was provided to run."

    target = _page_kernel_target(session)
    if target is None:
        return (
            "Error: run_python is only available in a document chat on a vault "
            "page (no page kernel to attach to)."
        )
    page_id, cwd, vault = target

    try:
        conn = await jupyter_manager.get_or_create_connection(page_id, cwd, vault)
    except Exception as e:  # kernel container down, spawn failed, etc.
        logger.warning("run_python: could not get kernel for %s: %s", page_id, e)
        return f"Error: could not reach the page's Python kernel ({e})."

    try:
        msg_id, queue = await conn.execute(code)
    except Exception as e:
        logger.warning("run_python: execute failed on %s: %s", page_id, e)
        return f"Error: failed to send code to the kernel ({e})."

    stdout_parts: list[str] = []
    result_parts: list[str] = []
    images: list[str] = []
    error_text: str | None = None
    timed_out = False

    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                msg = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                timed_out = True
                break

            msg_type = msg.get("msg_type")
            content = msg.get("content", {}) or {}

            if msg_type == "stream":
                stdout_parts.append(content.get("text", ""))
            elif msg_type in ("execute_result", "display_data"):
                data = content.get("data", {}) or {}
                png = data.get("image/png")
                if png:
                    images.append(png)
                    if emit_artifact is not None:
                        try:
                            await emit_artifact({"type": "image", "data": png})
                        except Exception as e:
                            logger.warning("run_python: emit_artifact failed: %s", e)
                elif data.get("text/plain"):
                    result_parts.append(data["text/plain"])
            elif msg_type == "error":
                tb = content.get("traceback") or []
                if tb:
                    error_text = _strip_ansi("\n".join(tb))
                else:
                    error_text = f"{content.get('ename', 'Error')}: {content.get('evalue', '')}"
            elif msg_type == "input_request":
                # Agent code shouldn't prompt; answer with EOF-ish empty input so
                # input() returns instead of hanging the drain (timeout still caps
                # a loop that keeps prompting).
                try:
                    await conn.send_input_reply("")
                except Exception:
                    pass
            elif msg_type == "status" and content.get("execution_state") == "idle":
                break
    finally:
        # The listener registers a queue per execution; drop ours so it doesn't
        # leak (the browser path is long-lived, but these are one-shot).
        conn._pending_executions.pop(msg_id, None)

    # Assemble the model-facing transcript.
    parts: list[str] = []
    stdout = "".join(stdout_parts).strip()
    if stdout:
        parts.append(stdout)
    results = "\n".join(p for p in result_parts if p.strip())
    if results.strip():
        parts.append(results.strip())
    if error_text:
        parts.append("Traceback:\n" + error_text.strip())
    for i in range(len(images)):
        parts.append(f"[figure {i + 1} generated and shown to the user]")
    if timed_out:
        parts.append(
            f"[execution stopped after {int(timeout)}s timeout; the code may still "
            "be running in the kernel]"
        )
    if not parts:
        return "(code ran with no output)"
    return "\n".join(parts)
