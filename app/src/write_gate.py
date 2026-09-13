# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The agent write gate: the single chokepoint deciding what happens when an
agent writes a page, plus the durable staging substrate behind "propose" mode.

Decision (classify_write), derived from LOCATION only:
- target vault is a SYSTEM vault        -> refused (blessing store is human-only)
- path's first segment is a control dir  -> refused (RESERVED_CONTROL_DIRS:
  (.git / .obsidian / .tzara)              executable/privileged config a diff
                                           review is too thin a backstop for)
- path under AGENT_OUTPUT_DIR (_dada/)  -> direct write (agent-owned area;
                                           background_agents.write_agent_output)
- anything else (human space)           -> STAGED: the write lands as a shadow
                                           copy + manifest row; only a human
                                           applies it from the /agents inbox.

Staging model (externalizes chat's DocumentScratchpad to survive the run):
- Shadow BODIES: files under  vault-history/.staging/{run_id}/{vault}/{rel_path}
  - inside the mount shared rw by server+worker, OUTSIDE vaults/ so the watcher
  and Dropbox never see them.
- MANIFEST: the agent_staging Postgres table (run_id, vault, path, base_hash,
  note, op, dest_path, status). base_hash freezes the file state the proposal
  was computed against; promotion refuses on drift instead of blind-clobbering.
  ``op`` is write (a shadow body), delete, or move (to dest_path) - page deletes
  and moves carry no body, just the decision.

Promotion (promote_file) is checkpoint-before-mutate: the pre-image is committed
first (a no-op when the file is clean at HEAD - docversioning.save_version's
content-equality short-circuit), then the shadow is written and committed with
an attributed message `agent({slug}/{vault}/{run_id}): ...`, then the watcher's
duplicate commit is suppressed via the standard git:debounce key (the watcher
still reindexes the changed page - wanted).

Everything here is synchronous (psycopg2 / filesystem / git subprocess / sync
redis), matching vault_analysis; async callers wrap in asyncio.to_thread.
"""

import hashlib
import html
import logging
import os
import shutil
from contextvars import ContextVar
from difflib import unified_diff

from config import (
    AGENT_OUTPUT_DIR,
    HISTORY_DIR,
)

logger = logging.getLogger("write_gate")

# Dotfolders that are not merely non-content but ACTIVE control surfaces, so an
# agent must never even STAGE into them (a human diff-review is too thin a
# backstop for an executable/privileged config):
#   .git      - a planted hook executes on the host's next git op (RCE)
#   .obsidian - community-plugin code Obsidian loads on vault open
#   .tzara    - config.json carries `system:true`, the `seeded` list, the
#               display metadata, and the vault's `default_page` / `template`;
#               a flipped flag hides a vault / locks writes
#
# .obsidian is write-blocked AND never read. Honouring Obsidian's settings was
# considered and declined: Obsidian persists only the keys you have changed from
# its defaults, so a real vault's app.json is typically `{}` and there is almost
# nothing there to act on. `.tzara/config.json` stays the single source of truth
# for per-vault settings (vault_registry), which keeps one file authoritative
# rather than two that can disagree.
# Deliberately NARROW: other non-content dirs the watcher ignores (.trash,
# __pycache__) hold no exec/privilege surface, so they stay ordinary staged
# writes -- and _dada/ is the agent-OWNED area, gated separately below.
RESERVED_CONTROL_DIRS = {".git", ".obsidian", ".tzara"}

# (run_id, agent_slug, mode, depth) for the currently executing agent run in this
# asyncio task - same idiom as content_ops._active_vault. Tools read it
# implicitly so their signatures stay clean. `mode` is the per-agent autonomy
# ceiling from the BLESSED file ("propose" | "act-with-checkpoint"); it decides
# whether gated_write stages or applies, and it can never come from a tool call.
# `depth` is the run's event-chain depth, carried onto the document events its
# direct writes cause so trigger chains through page edits hit EVENT_MAX_DEPTH.
_run_ctx: ContextVar[tuple[str, str, str, int] | None] = ContextVar("agent_run_ctx", default=None)


def set_run_context(run_id: str, agent_slug: str, mode: str = "propose", depth: int = 0):
    return _run_ctx.set((run_id, agent_slug, mode, int(depth or 0)))


def reset_run_context(token) -> None:
    _run_ctx.reset(token)


def current_run() -> tuple[str, str, str, int] | None:
    return _run_ctx.get()


def current_mode() -> str:
    """The active run's autonomy mode; safe-closed to 'propose' outside a run."""
    ctx = _run_ctx.get()
    return ctx[2] if ctx is not None else "propose"


def current_depth() -> int:
    """The active run's event-chain depth; 0 outside a run (a human applying a
    staged batch starts no chain)."""
    ctx = _run_ctx.get()
    return ctx[3] if ctx is not None else 0


def _mark_agent_writer(vault_id: str, rel: str, agent_slug: str, run_id: str) -> None:
    """Attribute the page change about to happen to the agent that wrote it -
    also when a human is applying the agent's staged proposal: the text is the
    agent's, and the approval already has its own event (staging.approved)."""
    from src.events import mark_writer
    mark_writer(vault_id, rel, f"agent:{agent_slug}", cause_run_id=run_id,
                depth=current_depth())


# ---------------------------------------------------------------------------
# Paths / hashing
# ---------------------------------------------------------------------------

def _staging_root() -> str:
    return os.path.join(os.getcwd(), HISTORY_DIR, ".staging")


def _shadow_path(run_id: str, vault_id: str, rel_path: str) -> str:
    return os.path.join(_staging_root(), run_id, vault_id, rel_path)


def _validate_rel_path(rel_path: str) -> str:
    """Delegates to WikiDoc.safe_rel (the single rel-path validator)."""
    from src.wikidoc import WikiDoc
    return WikiDoc.safe_rel(rel_path)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_disk(vault_id: str, rel_path: str) -> str | None:
    """Canonical read (EOL-preserving, LF-normalized) via WikiDoc.read_text.
    Returns just the content - callers here hash/diff it and never need the eol
    (WikiDoc.commit re-derives eol from the file when it writes)."""
    from src.wikidoc import WikiDoc
    pair = WikiDoc.read_text(vault_id, rel_path)
    return pair[0] if pair else None


def _get_pg_connection():
    from config import get_pg_connection
    return get_pg_connection()


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------

def classify_write(vault_id: str, rel_path: str) -> str:
    """'refuse_system' | 'refuse_reserved' | 'owned_direct' | 'staged' - location
    is the whole fact."""
    from src import vault_registry
    if vault_registry.is_system_vault(vault_id):
        return "refuse_system"
    rel = _validate_rel_path(rel_path)
    first = rel.split("/")[0]
    if first in RESERVED_CONTROL_DIRS:
        # Structural refusal beats trusting a human (or auto-apply) to catch a
        # privileged/executable config diff in review. See RESERVED_CONTROL_DIRS.
        return "refuse_reserved"
    if first == AGENT_OUTPUT_DIR:
        return "owned_direct"
    return "staged"


# ---------------------------------------------------------------------------
# Staging (called from agent tools, inside a run context)
# ---------------------------------------------------------------------------

def stage_write(vault_id: str, rel_path: str, new_content: str, note: str = "") -> str:
    """Stage a human-space write as a shadow copy for later human review.

    Returns a short status string suitable as a tool result. Requires an active
    run context. Repeated stages of the same file in one run overwrite the
    shadow (accumulating edits) but keep the ORIGINAL base_hash - the drift
    check is always against what the run first saw.
    """
    ctx = current_run()
    if ctx is None:
        raise RuntimeError("stage_write called outside an agent run context")
    run_id, agent_slug = ctx[0], ctx[1]

    verdict = classify_write(vault_id, rel_path)
    if verdict == "refuse_system":
        return f"stage_write: refused - {vault_id!r} is a system vault (human-only)."
    if verdict == "refuse_reserved":
        return (f"stage_write: refused - {rel_path!r} is a reserved control path "
                "(dotfolder, non-content) and is not agent-writable.")
    if verdict == "owned_direct":
        raise RuntimeError(
            f"stage_write called for the agent-owned area ({rel_path!r}) - "
            "use write_agent_output for owned pages")

    rel = _validate_rel_path(rel_path)
    conflict = _write_conflict(run_id, vault_id, rel)
    if conflict:
        return f"stage_write: refused - {conflict}"
    base = _read_disk(vault_id, rel)
    base_hash = _content_hash(base) if base is not None else ""

    shadow = _shadow_path(run_id, vault_id, rel)
    from src.wikidoc import WikiDoc
    WikiDoc._write_raw(shadow, new_content)  # DEFAULT_ENCODING + makedirs, verbatim

    _record_row(run_id, agent_slug, vault_id, rel, base_hash, note)
    logger.info("staged %s:%s for run %s", vault_id, rel, run_id)
    return f"Staged proposed change to '{rel}' for human review."


def _record_row(run_id: str, agent_slug: str, vault_id: str, rel: str,
                base_hash: str, note: str, *, op: str = "write",
                dest_path: str = "", applied: bool = False) -> None:
    """Upsert one manifest row: pending (staged) or applied (act-mode audit).
    A repeat for the same (run, vault, path) merges the note and keeps the FIRST
    base_hash - the drift check is always against what the run first saw."""
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_staging (run_id, agent_slug, vault_id, rel_path,
                                       base_hash, note, op, dest_path, status,
                                       decided_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CASE WHEN %s THEN NOW() END)
            ON CONFLICT (run_id, vault_id, rel_path) DO UPDATE
                SET note = CASE
                        WHEN EXCLUDED.note = '' OR agent_staging.note = EXCLUDED.note
                            THEN agent_staging.note
                        WHEN agent_staging.note = '' THEN EXCLUDED.note
                        ELSE agent_staging.note || ' | ' || EXCLUDED.note
                    END,
                    op = EXCLUDED.op, dest_path = EXCLUDED.dest_path,
                    status = EXCLUDED.status, decided_at = EXCLUDED.decided_at
            """,
            (run_id, agent_slug, vault_id, rel, base_hash, note, op, dest_path,
             "applied" if applied else "pending", applied),
        )
        conn.commit()
    finally:
        conn.close()


def gated_write(vault_id: str, rel_path: str, new_content: str, note: str = "") -> str:
    """The tool-facing human-space write chokepoint.

    In "propose" mode (the default and the floor outside any run context) this
    is stage_write. In "act-with-checkpoint" mode - granted per-agent in the
    BLESSED file, carried by the run context, never by the tool call - the
    write applies immediately via the same checkpoint-before-mutate path human
    promotion uses, and an `applied` audit row lands in agent_staging so
    "what did this run change" stays queryable.
    """
    if current_mode() != "act-with-checkpoint":
        return stage_write(vault_id, rel_path, new_content, note=note)

    ctx = current_run()  # not None: current_mode() above came from it
    run_id, agent_slug = ctx[0], ctx[1]

    verdict = classify_write(vault_id, rel_path)
    if verdict == "refuse_system":
        return f"gated_write: refused - {vault_id!r} is a system vault (human-only)."
    if verdict == "refuse_reserved":
        return (f"gated_write: refused - {rel_path!r} is a reserved control path "
                "(dotfolder, non-content) and is not agent-writable.")
    if verdict == "owned_direct":
        raise RuntimeError(
            f"gated_write called for the agent-owned area ({rel_path!r}) - "
            "use write_agent_output for owned pages")

    rel = _validate_rel_path(rel_path)
    current = _read_disk(vault_id, rel)
    base_hash = _content_hash(current) if current is not None else ""
    _apply_to_disk(vault_id, rel, new_content, agent_slug, run_id, current)

    # Audit row: same manifest table, pre-decided. Re-writes of the same file
    # in one run keep the FIRST base_hash (matching stage_write's semantics).
    _record_row(run_id, agent_slug, vault_id, rel, base_hash, note, applied=True)
    logger.info("act-applied %s:%s (run %s)", vault_id, rel, run_id)
    return (f"Applied change to '{rel}' directly "
            "(act-with-checkpoint; pre-image checkpointed).")


# ---------------------------------------------------------------------------
# Page deletes and moves (same gate, same modes, no shadow body)
# ---------------------------------------------------------------------------

def _pending_involving(run_id: str, vault_id: str, rel: str) -> list[tuple[str, str, str]]:
    """(op, rel_path, dest_path) of this run's pending rows that touch ``rel`` -
    as their page or as a move's destination."""
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT op, rel_path, dest_path FROM agent_staging
            WHERE run_id = %s AND vault_id = %s AND status = 'pending'
              AND (lower(rel_path) = lower(%s)
                   OR (op = 'move' AND lower(dest_path) = lower(%s)))
            """, (run_id, vault_id, rel, rel))
        return cur.fetchall()
    finally:
        conn.close()


def _describe_pending(rel: str, op: str, src: str, dest: str) -> str:
    if op == "delete":
        return f"you proposed deleting '{src}' earlier in this run"
    if op == "move" and src.casefold() == rel.casefold():
        return (f"you proposed moving '{src}' to '{dest}' earlier in this run - "
                "change it in a later run, once the move is applied")
    if op == "move":
        return (f"'{dest}' is the destination of a move you proposed earlier in "
                f"this run (from '{src}')")
    return f"you already staged an edit to '{src}' in this run"


def _write_conflict(run_id: str, vault_id: str, rel: str) -> str:
    """Why a write to ``rel`` can't be staged in this run, or ''. Repeated edits
    to one page accumulate; an edit to a page this run proposes deleting or
    moving (or to a move's destination) cannot be applied in a sensible order."""
    for op, src, dest in _pending_involving(run_id, vault_id, rel):
        if op != "write":
            return _describe_pending(rel, op, src, dest)
    return ""


def _page_op_conflict(run_id: str, vault_id: str, rel: str) -> str:
    """Why a delete/move involving ``rel`` can't be staged in this run, or ''.
    A page carries one kind of proposal per run."""
    for op, src, dest in _pending_involving(run_id, vault_id, rel):
        return (_describe_pending(rel, op, src, dest)
                + " - a page takes one kind of proposal per run")
    return ""


def _refusal(tool: str, vault_id: str, rel_path: str) -> str:
    """Location refusal for a page delete/move, or ''."""
    verdict = classify_write(vault_id, rel_path)
    if verdict == "refuse_system":
        return f"{tool}: refused - {vault_id!r} is a system vault (human-only)."
    if verdict == "refuse_reserved":
        return (f"{tool}: refused - {rel_path!r} is a reserved control path "
                "(dotfolder, non-content) and is not agent-writable.")
    if verdict == "owned_direct":
        return (f"{tool}: refused - {rel_path!r} is in the agent-owned area; "
                "these tools act on the human's pages.")
    return ""


def _dest_taken(vault_id: str, rel: str) -> bool:
    from src.wikidoc import WikiDoc
    return os.path.exists(WikiDoc._abs_checked(vault_id, rel))


def _delete_on_disk(vault_id: str, rel: str, agent_slug: str, run_id: str) -> None:
    """Checkpoint-before-delete removal with agent attribution (git + events)."""
    from src.wikidoc import WikiDoc
    _mark_agent_writer(vault_id, rel, agent_slug, run_id)
    WikiDoc.delete_file(vault_id, rel,
                        message=f"agent({agent_slug}/{vault_id}/{run_id}): delete {rel}")


def _move_on_disk(vault_id: str, src: str, dest: str, agent_slug: str,
                  run_id: str) -> dict:
    """The human move engine (inbound links rewritten), agent-attributed."""
    from src import content_ops
    return content_ops.move_document_sync(
        src, dest, vault_id,
        message=f"agent({agent_slug}/{vault_id}/{run_id}): move {src} -> {dest}",
        writer=(f"agent:{agent_slug}", run_id, current_depth()))


def gated_delete(vault_id: str, rel_path: str, note: str = "") -> tuple[bool, str]:
    """Delete a human-space page through the gate: staged in propose mode,
    applied (checkpoint first) with an audit row in act-with-checkpoint mode.
    Returns (ok, message). Requires an active run context."""
    ctx = current_run()
    if ctx is None:
        raise RuntimeError("gated_delete called outside an agent run context")
    run_id, agent_slug = ctx[0], ctx[1]

    refused = _refusal("propose_delete", vault_id, rel_path)
    if refused:
        return False, refused
    rel = _validate_rel_path(rel_path)
    from src import content_ops
    if content_ops.is_default_page(vault_id, rel):
        return False, f"propose_delete: refused - '{rel}' is the vault's start page."
    conflict = _page_op_conflict(run_id, vault_id, rel)
    if conflict:
        return False, f"propose_delete: refused - {conflict}."
    current = _read_disk(vault_id, rel)
    if current is None:
        return False, f"propose_delete: '{rel}' not found."
    base_hash = _content_hash(current)

    if current_mode() == "act-with-checkpoint":
        _delete_on_disk(vault_id, rel, agent_slug, run_id)
        _record_row(run_id, agent_slug, vault_id, rel, base_hash, note,
                    op="delete", applied=True)
        logger.info("act-deleted %s:%s (run %s)", vault_id, rel, run_id)
        return True, (f"Deleted '{rel}' directly "
                      "(act-with-checkpoint; pre-image checkpointed).")
    _record_row(run_id, agent_slug, vault_id, rel, base_hash, note, op="delete")
    logger.info("staged delete %s:%s for run %s", vault_id, rel, run_id)
    return True, f"Staged deletion of '{rel}' for human review."


def gated_move(vault_id: str, src_path: str, dest_path: str,
               note: str = "") -> tuple[bool, str]:
    """Move/rename a human-space page through the gate. Applying rewrites the
    links that point at it, exactly as a human move does. Returns (ok, message).
    Requires an active run context."""
    ctx = current_run()
    if ctx is None:
        raise RuntimeError("gated_move called outside an agent run context")
    run_id, agent_slug = ctx[0], ctx[1]

    for p in (src_path, dest_path):
        refused = _refusal("propose_move", vault_id, p)
        if refused:
            return False, refused
    src, dest = _validate_rel_path(src_path), _validate_rel_path(dest_path)
    if src == dest:
        return False, "propose_move: source and destination are the same."
    from src import content_ops
    if content_ops.is_default_page(vault_id, src):
        return False, f"propose_move: refused - '{src}' is the vault's start page."
    for p in (src, dest):
        conflict = _page_op_conflict(run_id, vault_id, p)
        if conflict:
            return False, f"propose_move: refused - {conflict}."
    current = _read_disk(vault_id, src)
    if current is None:
        return False, f"propose_move: '{src}' not found."
    if _dest_taken(vault_id, dest):
        return False, f"propose_move: '{dest}' already exists - pick another path."
    base_hash = _content_hash(current)

    if current_mode() == "act-with-checkpoint":
        result = _move_on_disk(vault_id, src, dest, agent_slug, run_id)
        if result.get("status") != "ok":
            return False, f"propose_move: {result.get('reason', 'move failed')}."
        _record_row(run_id, agent_slug, vault_id, src, base_hash, note,
                    op="move", dest_path=dest, applied=True)
        logger.info("act-moved %s:%s -> %s (run %s)", vault_id, src, dest, run_id)
        return True, f"Moved '{src}' to '{dest}' directly (act-with-checkpoint)."
    _record_row(run_id, agent_slug, vault_id, src, base_hash, note,
                op="move", dest_path=dest)
    logger.info("staged move %s:%s -> %s for run %s", vault_id, src, dest, run_id)
    return True, f"Staged move of '{src}' to '{dest}' for human review."


def read_through(vault_id: str, rel_path: str) -> str | None:
    """Disk content, overlaid by THIS run's shadow if one exists (an agent must
    see its own staged edits - self-consistency without a human in the loop)."""
    rel = _validate_rel_path(rel_path)
    ctx = current_run()
    if ctx is not None:
        shadow = _shadow_path(ctx[0], vault_id, rel)
        if os.path.isfile(shadow):
            from src.wikidoc import WikiDoc
            return WikiDoc._read_raw(shadow)
    return _read_disk(vault_id, rel)


def staged_count(run_id: str) -> int:
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM agent_staging WHERE run_id = %s AND status = 'pending'",
                    (run_id,))
        return cur.fetchone()[0]
    finally:
        conn.close()


def applied_count(run_id: str) -> int:
    """Writes this run applied directly (act-with-checkpoint audit rows)."""
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM agent_staging WHERE run_id = %s AND status = 'applied'",
                    (run_id,))
        return cur.fetchone()[0]
    finally:
        conn.close()


def pending_summary() -> dict:
    """Counts for the nav alert badge: how many staged runs and files across
    ALL agents/vaults await human review (drift rows included - they still
    need a reject/rebase decision). One aggregate over the same predicate
    `get_pending_batches` groups on, so it is cheap enough to poll from the
    header on every page load."""
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT count(DISTINCT run_id) AS batches, count(*) AS files
            FROM agent_staging
            WHERE status IN ('pending', 'drift')
            """)
        row = cur.fetchone()
        return {"batches": row[0], "files": row[1]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Review-side queries (the /agents inbox)
# ---------------------------------------------------------------------------

def get_run_meta(run_id: str) -> dict | None:
    """{agent_slug, vault_id} for a staged run, or None. Callers that emit
    staging events MUST fetch this BEFORE acting - apply/discard cleanup can
    remove the rows it reads."""
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT agent_slug, vault_id FROM agent_staging WHERE run_id = %s LIMIT 1",
            (run_id,))
        row = cur.fetchone()
        return {"agent_slug": row[0], "vault_id": row[1]} if row else None
    finally:
        conn.close()


def get_pending_batches() -> list[dict]:
    """Pending (and drift-flagged) work grouped by run, newest first."""
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT run_id, agent_slug, vault_id, min(created_at) AS created_at,
                   count(*) FILTER (WHERE status = 'pending') AS pending,
                   count(*) FILTER (WHERE status = 'drift')   AS drift
            FROM agent_staging
            WHERE status IN ('pending', 'drift')
            GROUP BY run_id, agent_slug, vault_id
            ORDER BY min(created_at) DESC
            """)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()


def get_batch_files(run_id: str) -> list[dict]:
    """All undecided rows of a run, each with shadow/current content + live
    drift flag (current disk hash != base_hash)."""
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, run_id, agent_slug, vault_id, rel_path, base_hash, note,
                   op, dest_path, status
            FROM agent_staging
            WHERE run_id = %s AND status IN ('pending', 'drift')
            ORDER BY rel_path
            """, (run_id,))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    finally:
        conn.close()

    from src.wikidoc import WikiDoc
    for row in rows:
        op = row.get("op") or "write"
        shadow = _shadow_path(run_id, row["vault_id"], row["rel_path"])
        row["staged_content"] = (WikiDoc._read_raw(shadow)
                                 if op == "write" and os.path.isfile(shadow) else None)
        current = _read_disk(row["vault_id"], row["rel_path"])
        row["current_content"] = current
        current_hash = _content_hash(current) if current is not None else ""
        row["drifted"] = current_hash != row["base_hash"]
        row["drift_reason"] = ("the page changed since this was staged"
                               if row["drifted"] else "")
        if op == "move" and not row["drifted"] \
                and _dest_taken(row["vault_id"], row["dest_path"]):
            row["drifted"] = True
            row["drift_reason"] = f"'{row['dest_path']}' now exists"
    return rows


def _get_row(row_id: int) -> dict | None:
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, run_id, agent_slug, vault_id, rel_path, base_hash, note,
                      op, dest_path, status
               FROM agent_staging WHERE id = %s""", (row_id,))
        r = cur.fetchone()
        if r is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, r))
    finally:
        conn.close()


def _set_status(row_id: int, status: str) -> None:
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE agent_staging SET status = %s, decided_at = NOW() WHERE id = %s",
            (status, row_id))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Promotion / rejection (called from the inbox API, human-initiated)
# ---------------------------------------------------------------------------


def _apply_to_disk(vault_id: str, rel: str, new_content: str,
                   agent_slug: str, run_id: str, current: str | None,
                   edited: bool = False) -> None:
    """Checkpoint-before-mutate write: pre-image commit -> EOL-preserving write
    -> attributed commit -> watcher debounce. Shared by human promotion
    (promote_file) and act-with-checkpoint runs (gated_write).

    Routed through WikiDoc.commit (the canonical versioned-mutation primitive):
    it re-reads the file's EOL so a CRLF page stays CRLF, and skips the
    checkpoint for brand-new files on its own (`current is None` <=> not existed).
    """
    from src.wikidoc import WikiDoc
    _mark_agent_writer(vault_id, rel, agent_slug, run_id)
    WikiDoc.commit(vault_id, rel, new_content,
                   message=(f"agent({agent_slug}/{vault_id}/{run_id}): {rel}"
                            + (" (edited on apply)" if edited else "")),
                   checkpoint=current is not None)


def promote_file(row_id: int) -> str:
    """Apply one staged proposal: drift-check -> checkpoint pre-image -> write,
    delete or move -> attributed commit -> watcher debounce. Returns
    'applied' | 'drift' | error."""
    row = _get_row(row_id)
    if row is None or row["status"] not in ("pending", "drift"):
        return "not_pending"

    op = row.get("op") or "write"
    vault_id, rel = row["vault_id"], row["rel_path"]
    shadow = _shadow_path(row["run_id"], vault_id, rel)
    if op == "write" and not os.path.isfile(shadow):
        _set_status(row_id, "rejected")
        return "shadow_missing"

    current = _read_disk(vault_id, rel)
    current_hash = _content_hash(current) if current is not None else ""
    if current_hash != row["base_hash"]:
        # The file changed since the agent computed this proposal. Never
        # blind-clobber: flag it and let the human discard / re-run the agent.
        _set_status(row_id, "drift")
        return "drift"

    if op == "delete":
        _delete_on_disk(vault_id, rel, row["agent_slug"], row["run_id"])
    elif op == "move":
        if _dest_taken(vault_id, row["dest_path"]):
            _set_status(row_id, "drift")
            return "drift"
        result = _move_on_disk(vault_id, rel, row["dest_path"],
                               row["agent_slug"], row["run_id"])
        if result.get("status") != "ok":
            return f"move_failed: {result.get('reason', '')}"
    else:
        from src.wikidoc import WikiDoc
        staged = WikiDoc._read_raw(shadow)
        _apply_to_disk(vault_id, rel, staged, row["agent_slug"], row["run_id"], current)

    _set_status(row_id, "applied")
    if op == "write":
        os.remove(shadow)
    _maybe_cleanup_run(row["run_id"])
    logger.info("applied staged %s %s:%s (run %s)", op, vault_id, rel, row["run_id"])
    return "applied"


def get_staged_write(row_id: int) -> dict | None:
    """One undecided staged WRITE plus its proposed text ("staged_content"), or
    None - what the editor loads to review it ("Accept with edits")."""
    row = _get_row(row_id)
    if row is None or row["status"] not in ("pending", "drift") \
            or (row.get("op") or "write") != "write":
        return None
    shadow = _shadow_path(row["run_id"], row["vault_id"], row["rel_path"])
    if not os.path.isfile(shadow):
        return None
    from src.wikidoc import WikiDoc
    row["staged_content"] = WikiDoc._read_raw(shadow)
    return row


def promote_reviewed(row_id: int, run_id: str, content: str,
                     expected_hash: str) -> dict:
    """Apply a staged WRITE as reviewed in the editor ("Accept with edits").

    `content` is what the reviewer accepted: the proposal, possibly with some of
    its changes dropped or edited. The drift check compares the page against
    `expected_hash` - the page as the reviewer saw it when the review opened -
    not the row's base_hash, because the reviewer reconciled the proposal
    against that text; so a proposal the page has since outgrown ('drift') can
    still be accepted. A change DURING the review is refused, with the current
    text for the editor to rebase onto.

    Returns {"status": "applied", "edited": bool}, {"status": "drift",
    "current", "base_hash"}, or {"status": "not_reviewable"}. The agent stays
    the recorded author; an edit only marks the commit message.
    """
    row = get_staged_write(row_id)
    if row is None or row["run_id"] != run_id:
        return {"status": "not_reviewable"}
    vault_id, rel = row["vault_id"], row["rel_path"]
    current = _read_disk(vault_id, rel)
    current_hash = _content_hash(current) if current is not None else ""
    if current_hash != expected_hash:
        return {"status": "drift", "current": current or "", "base_hash": current_hash}
    content = content.replace("\r\n", "\n")
    edited = content != row["staged_content"].replace("\r\n", "\n")
    _apply_to_disk(vault_id, rel, content, row["agent_slug"], run_id, current,
                   edited=edited)
    _set_status(row_id, "applied")
    shadow = _shadow_path(run_id, vault_id, rel)
    if os.path.isfile(shadow):
        os.remove(shadow)
    _maybe_cleanup_run(run_id)
    logger.info("applied reviewed %s:%s (run %s, edited=%s)", vault_id, rel, run_id, edited)
    return {"status": "applied", "edited": edited}


# Writes first: a staged edit to a page that links to a moving one was computed
# against the pre-move text, and the move's link rewrite would drift it.
_APPLY_ORDER = {"write": 0, "move": 1, "delete": 2}


def reject_file(row_id: int) -> str:
    row = _get_row(row_id)
    if row is None or row["status"] not in ("pending", "drift"):
        return "not_pending"
    shadow = _shadow_path(row["run_id"], row["vault_id"], row["rel_path"])
    if os.path.isfile(shadow):
        os.remove(shadow)
    _set_status(row_id, "rejected")
    _maybe_cleanup_run(row["run_id"])
    return "rejected"


def apply_batch(run_id: str, only_ids: list[int] | None = None) -> dict:
    counts = {"applied": 0, "drift": 0, "other": 0}
    rows = sorted(get_batch_files(run_id),
                  key=lambda r: _APPLY_ORDER.get(r.get("op") or "write", 0))
    for row in rows:
        if only_ids is not None and row["id"] not in only_ids:
            continue
        outcome = promote_file(row["id"])
        counts["applied" if outcome == "applied" else
               "drift" if outcome == "drift" else "other"] += 1
    return counts


def reject_batch(run_id: str, only_ids: list[int] | None = None) -> dict:
    rejected = 0
    for row in get_batch_files(run_id):
        if only_ids is not None and row["id"] not in only_ids:
            continue
        if reject_file(row["id"]) == "rejected":
            rejected += 1
    return {"rejected": rejected}


def discard_run(run_id: str) -> dict:
    """Reject everything undecided in a run and remove its shadow dir - the
    manual GC for crashed/stale batches."""
    out = reject_batch(run_id)
    run_dir = os.path.join(_staging_root(), run_id)
    if os.path.isdir(run_dir):
        shutil.rmtree(run_dir, ignore_errors=True)
    return out


def gc_stale_batches(ttl_days: int) -> list[str]:
    """Auto-GC: discard staged batches whose undecided rows are older than the
    TTL - crashed or forgotten proposals don't haunt the inbox forever. Runs on
    the scheduler tick. Returns the discarded run_ids."""
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT DISTINCT run_id FROM agent_staging
               WHERE status IN ('pending', 'drift')
                 AND created_at < NOW() - make_interval(days => %s)""",
            (ttl_days,))
        stale = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()
    for run_id in stale:
        discard_run(run_id)
        logger.info("gc_stale_batches: discarded %s (older than %sd)", run_id, ttl_days)
    return stale


def _maybe_cleanup_run(run_id: str) -> None:
    """Remove the run's shadow directory once nothing undecided remains."""
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) FROM agent_staging WHERE run_id = %s AND status IN ('pending','drift')",
            (run_id,))
        remaining = cur.fetchone()[0]
    finally:
        conn.close()
    if remaining == 0:
        run_dir = os.path.join(_staging_root(), run_id)
        if os.path.isdir(run_dir):
            shutil.rmtree(run_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Diff rendering (server-side, reuses the chat-diff CSS classes)
# ---------------------------------------------------------------------------

def unified_diff_html(current: str | None, staged: str, rel_path: str) -> str:
    """Unified diff of staged-vs-CURRENT-disk as .chat-diff HTML (the honest
    preview: what applying would change right now)."""
    diff_lines = list(unified_diff(
        (current or "").splitlines(), staged.splitlines(),
        fromfile=f"current/{rel_path}", tofile=f"staged/{rel_path}", lineterm=""))
    if not diff_lines:
        return '<div class="chat-diff"><span class="diff-ctx">(no changes)</span></div>'
    spans = []
    for line in diff_lines:
        esc = html.escape(line)
        if line.startswith("@@"):
            cls = "diff-hunk"
        elif line.startswith("+"):
            cls = "diff-add"
        elif line.startswith("-"):
            cls = "diff-del"
        else:
            cls = "diff-ctx"
        spans.append(f'<span class="{cls}">{esc}</span>')
    return '<div class="chat-diff">' + "\n".join(spans) + "</div>"
