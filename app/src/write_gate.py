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
  note, status). base_hash freezes the file state the proposal was computed
  against; promotion refuses on drift instead of blind-clobbering.

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

# (run_id, agent_slug, mode) for the currently executing agent run in this
# asyncio task - same idiom as content_ops._active_vault. Tools read it
# implicitly so their signatures stay clean. `mode` is the per-agent autonomy
# ceiling from the BLESSED file ("propose" | "act-with-checkpoint"); it decides
# whether gated_write stages or applies, and it can never come from a tool call.
_run_ctx: ContextVar[tuple[str, str, str] | None] = ContextVar("agent_run_ctx", default=None)


def set_run_context(run_id: str, agent_slug: str, mode: str = "propose"):
    return _run_ctx.set((run_id, agent_slug, mode))


def reset_run_context(token) -> None:
    _run_ctx.reset(token)


def current_run() -> tuple[str, str, str] | None:
    return _run_ctx.get()


def current_mode() -> str:
    """The active run's autonomy mode; safe-closed to 'propose' outside a run."""
    ctx = _run_ctx.get()
    return ctx[2] if ctx is not None else "propose"


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
    base = _read_disk(vault_id, rel)
    base_hash = _content_hash(base) if base is not None else ""

    shadow = _shadow_path(run_id, vault_id, rel)
    from src.wikidoc import WikiDoc
    WikiDoc._write_raw(shadow, new_content)  # DEFAULT_ENCODING + makedirs, verbatim

    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_staging (run_id, agent_slug, vault_id, rel_path,
                                       base_hash, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (run_id, vault_id, rel_path) DO UPDATE
                SET note = CASE
                        WHEN EXCLUDED.note = '' OR agent_staging.note = EXCLUDED.note
                            THEN agent_staging.note
                        WHEN agent_staging.note = '' THEN EXCLUDED.note
                        ELSE agent_staging.note || ' | ' || EXCLUDED.note
                    END,
                    status = 'pending', decided_at = NULL
            """,
            (run_id, agent_slug, vault_id, rel, base_hash, note),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("staged %s:%s for run %s", vault_id, rel, run_id)
    return f"Staged proposed change to '{rel}' for human review."


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
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO agent_staging (run_id, agent_slug, vault_id, rel_path,
                                       base_hash, note, status, decided_at)
            VALUES (%s, %s, %s, %s, %s, %s, 'applied', NOW())
            ON CONFLICT (run_id, vault_id, rel_path) DO UPDATE
                SET note = CASE
                        WHEN EXCLUDED.note = '' OR agent_staging.note = EXCLUDED.note
                            THEN agent_staging.note
                        WHEN agent_staging.note = '' THEN EXCLUDED.note
                        ELSE agent_staging.note || ' | ' || EXCLUDED.note
                    END,
                    status = 'applied', decided_at = NOW()
            """,
            (run_id, agent_slug, vault_id, rel, base_hash, note),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info("act-applied %s:%s (run %s)", vault_id, rel, run_id)
    return (f"Applied change to '{rel}' directly "
            "(act-with-checkpoint; pre-image checkpointed).")


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
            SELECT id, run_id, agent_slug, vault_id, rel_path, base_hash, note, status
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
        shadow = _shadow_path(run_id, row["vault_id"], row["rel_path"])
        row["staged_content"] = (WikiDoc._read_raw(shadow)
                                 if os.path.isfile(shadow) else None)
        current = _read_disk(row["vault_id"], row["rel_path"])
        row["current_content"] = current
        current_hash = _content_hash(current) if current is not None else ""
        row["drifted"] = current_hash != row["base_hash"]
    return rows


def _get_row(row_id: int) -> dict | None:
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT id, run_id, agent_slug, vault_id, rel_path, base_hash, note, status
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
                   agent_slug: str, run_id: str, current: str | None) -> None:
    """Checkpoint-before-mutate write: pre-image commit -> EOL-preserving write
    -> attributed commit -> watcher debounce. Shared by human promotion
    (promote_file) and act-with-checkpoint runs (gated_write).

    Routed through WikiDoc.commit (the canonical versioned-mutation primitive):
    it re-reads the file's EOL so a CRLF page stays CRLF, and skips the
    checkpoint for brand-new files on its own (`current is None` <=> not existed).
    """
    from src.wikidoc import WikiDoc
    WikiDoc.commit(vault_id, rel, new_content,
                   message=f"agent({agent_slug}/{vault_id}/{run_id}): {rel}",
                   checkpoint=current is not None)


def promote_file(row_id: int) -> str:
    """Apply one staged file: drift-check -> checkpoint pre-image -> write ->
    attributed commit -> watcher debounce. Returns 'applied' | 'drift' | error."""
    row = _get_row(row_id)
    if row is None or row["status"] not in ("pending", "drift"):
        return "not_pending"

    vault_id, rel = row["vault_id"], row["rel_path"]
    shadow = _shadow_path(row["run_id"], vault_id, rel)
    if not os.path.isfile(shadow):
        _set_status(row_id, "rejected")
        return "shadow_missing"

    current = _read_disk(vault_id, rel)
    current_hash = _content_hash(current) if current is not None else ""
    if current_hash != row["base_hash"]:
        # The file changed since the agent computed this proposal. Never
        # blind-clobber: flag it and let the human discard / re-run the agent.
        _set_status(row_id, "drift")
        return "drift"

    from src.wikidoc import WikiDoc
    staged = WikiDoc._read_raw(shadow)
    _apply_to_disk(vault_id, rel, staged, row["agent_slug"], row["run_id"], current)

    _set_status(row_id, "applied")
    os.remove(shadow)
    _maybe_cleanup_run(row["run_id"])
    logger.info("applied staged %s:%s (run %s)", vault_id, rel, row["run_id"])
    return "applied"


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
    for row in get_batch_files(run_id):
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
