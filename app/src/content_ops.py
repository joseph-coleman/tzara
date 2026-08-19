# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Document content-management operations that span filesystem + git + the link
graph -- i.e. the things that make "a document" more than a single file.

Currently: move / rename. A move on disk is cheap, but a document also owns RAG
records (chunks, embeddings, edges) and is the target of other documents'
``[[wikilinks]]``. This module handles the parts that must originate *inside*
tzara when its own UI moves a document:

  1. rewrite the inbound ``[[...]]`` text in referring files, and
  2. perform the filesystem rename (+ a git move commit).

It deliberately does NOT touch the RAG database. Mirroring save_document /
delete_document in main.py, the database is reconciled by the file watcher: the
os.rename fires ``on_moved`` -> ``move_document_task`` -> ``reconcile_rename``
(in-place re-key, no re-embed), and each rewritten referrer fires ``on_modified``
-> reindex (rebuilding its edges). Keeping a single DB writer (the worker) avoids
a race between the web process and the task worker.

The watcher path (external moves, e.g. Obsidian) skips step 1 on purpose --
external tools rewrite their own links.
"""

import asyncio
import errno
import logging
import os
import re
import time
from contextvars import ContextVar

from config import (DEFAULT_VAULT, USE_GIT_VERSIONING,
                    vault_abs_root, vault_root)
from src.chunker import _key_segments, resolve_linkpath, wikilink_key
from src.rag_indexer import _get_pg_connection

logger = logging.getLogger("content_ops")

# The vault a content op is acting on. Set by each public op (move/delete/batch) and
# read by the path helpers below. A ContextVar (not a thread-local) because these ops
# are async coroutines -- it is isolated per asyncio task, so concurrent ops in
# different vaults never clobber each other's active vault.
_active_vault: ContextVar[str] = ContextVar("content_ops_vault", default=DEFAULT_VAULT)


def _git_tracker():
    """Per-vault git tracker for the active vault (ensures the separated repo exists)."""
    from src.docversioning import MarkdownGitVersioning
    from src import vault_registry
    v = _active_vault.get()
    vault_registry.init_vault_repo(v)
    return MarkdownGitVersioning(vault_root(v))

# A wikilink or doc-embed: optional leading '!' (embed), then [[ ... ]]. The inner
# group may carry a |display alias and/or a #anchor; we split those out so only the
# target portion is rewritten.
_LINK_RE = re.compile(r"(!?)\[\[([^\]]+)\]\]")


def iter_wikilinks(text: str):
    """Yield one entry per ``[[wikilink]]`` / ``![[embed]]`` in ``text``.

    Each entry is ``(match, target, anchor, alias, is_embed)`` with the same
    decomposition ``_rewrite_text`` performs -- the ``|alias`` and ``#anchor``
    split out, leaving the bare target. ``match`` is the re.Match, so callers can
    use ``.start()``/``.group(0)`` to locate or replace the link in place.

    Exists so link-inspecting code (agent capabilities' add/remove-wikilink)
    shares this module's canonical _LINK_RE instead of growing another one.
    """
    for m in _LINK_RE.finditer(text):
        bang, inner = m.group(1), m.group(2)
        target_part, _, alias = inner.partition("|")
        target, _, anchor = target_part.partition("#")
        yield m, target.strip(), anchor.strip(), alias.strip(), bang == "!"


def link_targets_doc(link_target: str, doc_id: str) -> bool:
    """Whether the wikilink target text points at ``doc_id``.

    Applies resolve_linkpath's matching rule without needing a vault index: a
    leading '/' anchors the match at the vault root (full path equality),
    otherwise the link's segments must be a SUFFIX of the document's. Folding is
    per-segment via chunker._key_segments, so '[[Ceres]]', '[[/World Map/Ceres]]'
    and '[[world_map/ceres]]' all match 'World Map/Ceres.md' while
    '[[Ceres Station]]' does not.

    Unlike resolve_linkpath this cannot break a tie between two same-basename
    documents -- with no candidate pool there is nothing to rank. It answers
    "could this link mean that document", which is the right question for
    idempotency ("don't add a link that may already be here") and for removing a
    link the caller located by name.
    """
    t = (link_target or "").strip().rstrip("/")
    if not t or not doc_id:
        return False
    anchored = t.startswith("/")
    tgt_segs = _key_segments(t.lstrip("/"))
    doc_segs = _key_segments(doc_id.lstrip("/"))
    if not tgt_segs or not doc_segs:
        return False
    if anchored:
        return doc_segs == tgt_segs
    return doc_segs[-len(tgt_segs):] == tgt_segs


def _abs(rel_path: str) -> str:
    """Absolute on-disk path for a vault-relative doc_id (e.g. "notes/p.md"), in the
    active vault (see _active_vault)."""
    return os.path.join(vault_abs_root(_active_vault.get()), rel_path)


def _ensure_md(rel_path: str) -> str:
    return rel_path if rel_path.endswith(".md") else rel_path + ".md"


def _default_page_md() -> str:
    """The active vault's protected start page as a doc_id. Which page that is, is
    per-vault, so it must be resolved against _active_vault rather than read off a
    site-wide constant."""
    from src.vault_registry import vault_default_page
    return _ensure_md(vault_default_page(_active_vault.get()))


def _by_stem(paths) -> dict:
    """{wikilink_key(basename stem): [path, ...]} -- the candidate index that
    chunker.resolve_linkpath consumes (the final link segment is always the stem)."""
    idx: dict[str, list] = {}
    for p in paths:
        stem = (p[:-3] if p.endswith(".md") else p).split("/")[-1]
        idx.setdefault(wikilink_key(stem), []).append(p)
    return idx


def _dir(doc_id: str) -> str:
    """Vault-relative folder of a doc_id ('' at the vault root)."""
    return doc_id.rsplit("/", 1)[0] if "/" in doc_id else ""


def _shortest_link(new_id: str, source_dir: str, post_by_stem: dict, had_root: bool) -> str:
    """Shortest link text that resolves to ``new_id`` from ``source_dir`` under the
    post-move vault -- Obsidian's "shortest path when possible". A link that was
    written absolute stays absolute; otherwise try the bare basename, then ever
    longer path-suffixes, falling back to an absolute (root-anchored) path."""
    link_path = new_id[:-3] if new_id.endswith(".md") else new_id  # drop .md for docs
    if had_root:
        return "/" + link_path
    segs = link_path.split("/")
    for i in range(1, len(segs) + 1):
        cand = "/".join(segs[-i:])
        if resolve_linkpath(cand, source_dir, by_stem=post_by_stem) == new_id:
            return cand
    return "/" + link_path


def _rewrite_text(text, pre_by_stem, post_by_stem, pre_dir, post_dir, rename_map, moved):
    """Rewrite links in ``text`` that point at a moved item but would stop resolving
    to it after the move; return (new_text, count).

    Obsidian-faithful: a link is touched only when it actually resolves to a moved
    document *and* the same text would no longer resolve there post-move. A bare
    [[Name]] with a unique basename survives any folder move untouched; a path-suffix
    or absolute link that breaks is rewritten to the shortest form that still points
    at the moved document's new location. ``|alias``, ``#anchor`` and the embed ``!``
    are preserved."""
    count = 0

    def _sub(m: re.Match) -> str:
        nonlocal count
        bang, inner = m.group(1), m.group(2)
        target_part, pipe, display = inner.partition("|")
        target, hashsep, anchor = target_part.partition("#")

        resolved_old = resolve_linkpath(target, pre_dir, by_stem=pre_by_stem)
        if resolved_old not in moved:
            return m.group(0)  # doesn't point at anything we're moving
        new_id = rename_map[resolved_old]
        if resolve_linkpath(target, post_dir, by_stem=post_by_stem) == new_id:
            return m.group(0)  # still resolves to the moved doc -- leave it alone

        new_t = _shortest_link(new_id, post_dir, post_by_stem, target.strip().startswith("/"))
        rebuilt = new_t + (hashsep + anchor if hashsep else "") + (pipe + display if pipe else "")
        count += 1
        return f"{bang}[[{rebuilt}]]"

    return _LINK_RE.sub(_sub, text), count


def _rewrite_inbound_links_sync(rename_map: dict[str, str]) -> list[str]:
    """Rewrite inbound link text in every file that references a moved item.

    ``rename_map`` is {old_id: new_id}; several entries (a folder move) are rewritten
    in a single pass per referrer. Resolution is Obsidian-style and shared with the
    renderer (chunker.resolve_linkpath), so a link is rewritten exactly when it would
    otherwise break. Candidate file lists are taken before and after the move -- the
    rename hasn't touched disk yet, so the current scan is the pre-move state and
    applying ``rename_map`` to it gives the post-move state (all file types, so asset
    embeds resolve too). Markdown referrers are discovered via the ``edges`` table;
    asset referrers via ``asset_refs``. Returns the referrer doc_ids whose content
    changed.
    """
    old_ids = list(rename_map)
    moved = set(old_ids)

    pre_paths = _walk_vault_files(_vault_root())
    post_paths = [rename_map.get(p, p) for p in pre_paths]
    pre_by_stem = _by_stem(pre_paths)
    post_by_stem = _by_stem(post_paths)

    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT source_doc_id FROM edges WHERE target_doc_id = ANY(%s)",
            (old_ids,),
        )
        referrers = {r[0] for r in cur.fetchall()}
        # Asset embeds: resolve each stored embed target from its referrer's folder
        # and include the referrer if it points at a moved asset.
        cur.execute("SELECT DISTINCT asset_name, doc_id FROM asset_refs")
        for asset_name, doc_id in cur.fetchall():
            if resolve_linkpath(asset_name, _dir(doc_id), by_stem=pre_by_stem) in moved:
                referrers.add(doc_id)
    finally:
        conn.close()

    from src.wikidoc import WikiDoc
    vault = _active_vault.get()
    changed: list[str] = []
    for ref_id in referrers:
        pair = WikiDoc.read_text(vault, ref_id)  # (content_LF, eol) | None
        if pair is None:
            logger.warning("rewrite_inbound_links: referrer missing on disk: %s", ref_id)
            continue
        text, eol = pair
        ref_new = rename_map.get(ref_id, ref_id)  # a referrer can itself be moving
        new_text, n = _rewrite_text(
            text, pre_by_stem, post_by_stem, _dir(ref_id), _dir(ref_new), rename_map, moved
        )
        if n and new_text != text:
            # Canonical write preserves the referrer's own EOL - a CRLF page whose
            # link text changed must not get rewritten to LF (that was the churn).
            WikiDoc.write_text(vault, ref_id, new_text, eol=eol)
            changed.append(ref_id)
            logger.info("rewrite_inbound_links: updated %d link(s) in %s", n, ref_id)
    return changed


async def _set_git_debounce(rel_path: str):
    """Skip the watcher's duplicate git commit for a path we just committed.
    Delegates to WikiDoc.set_debounce (the single key constructor); async callers
    wrap the sync primitive in a thread."""
    from src.wikidoc import WikiDoc
    await asyncio.to_thread(WikiDoc.set_debounce, _active_vault.get(), rel_path)


async def _apply_renames(rename_map: dict[str, str]) -> list[str]:
    """The single move engine shared by ``move_document_op`` (rename) and
    ``batch_move_op`` (move-into-folder).

    Rewrites inbound ``[[links]]``/``![[embeds]]`` once for the whole batch, then
    moves each file on disk with a debounced git move commit. Callers own all
    validation and any pre/post steps (collision checks, empty-dir pruning); this
    primitive assumes ``rename_map`` is already resolved and safe. Returns the
    referrer doc_ids whose link text changed. The RAG database is reconciled by the
    file watcher (see module docstring) -- this never touches the DB.
    """
    referrers = await asyncio.to_thread(_rewrite_inbound_links_sync, rename_map)

    vt = _git_tracker() if USE_GIT_VERSIONING else None
    vroot = vault_root(_active_vault.get())

    for src_rel, dest_rel in rename_map.items():
        src_abs, dest_abs = _abs(src_rel), _abs(dest_rel)

        def _move_file(s=src_abs, d=dest_abs):
            os.makedirs(os.path.dirname(d), exist_ok=True)
            os.rename(s, d)

        await asyncio.to_thread(_move_file)

        if vt is not None:
            try:
                await asyncio.to_thread(
                    vt.move_file,
                    os.path.join(vroot, src_rel),
                    os.path.join(vroot, dest_rel),
                )
                await _set_git_debounce(dest_rel)
            except Exception as e:  # git issues shouldn't fail the user's move
                logger.error("apply_renames: git move commit failed: %s", e)

    return referrers


async def move_document_op(src_rel: str, dest_rel: str, vault_id: str = DEFAULT_VAULT) -> dict:
    """Move/rename a document from ``src_rel`` to ``dest_rel`` (vault-relative) within
    ``vault_id``.

    Rewrites inbound wikilink text in referring files, moves the file on disk, and
    records a git move commit. The RAG database is reconciled by the file watcher
    (see module docstring). Returns a status dict.
    """
    _active_vault.set(vault_id)
    src_rel = _ensure_md(src_rel)
    dest_rel = _ensure_md(dest_rel)

    # --- validation -------------------------------------------------------
    if src_rel == dest_rel:
        return {"status": "noop", "reason": "source and destination are the same"}
    if src_rel == _default_page_md():
        return {"status": "refused", "reason": "cannot move the default page"}

    src_abs, dest_abs = _abs(src_rel), _abs(dest_rel)
    if not await asyncio.to_thread(os.path.isfile, src_abs):
        return {"status": "error", "reason": f"source not found: {src_rel}"}
    if await asyncio.to_thread(os.path.exists, dest_abs):
        return {"status": "error", "reason": f"destination already exists: {dest_rel}"}

    # Rewrite inbound links + move on disk + git commit, all via the shared engine.
    # The watcher reconciles the RAG DB and commits the referrer edits.
    referrers = await _apply_renames({src_rel: dest_rel})

    logger.info(
        "move_document_op: %s -> %s (%d referrer file(s) updated)",
        src_rel, dest_rel, len(referrers),
    )
    return {
        "status": "ok",
        "src": src_rel,
        "dest": dest_rel,
        "referrers_updated": referrers,
    }


# ---------------------------------------------------------------------------
# Batch move (multiple files / whole folders into a destination folder)
# ---------------------------------------------------------------------------

def _vault_root() -> str:
    return vault_abs_root(_active_vault.get())


def _rel_from_path(path: str) -> str:
    """Vault-relative path (no ``wiki/`` prefix) for any file type, extension
    preserved. Folder paths come back as e.g. ``"Projects/Sub"``."""
    from src.wikidoc import WikiDoc

    d = WikiDoc.parse_url_path(path)
    if d["is_default_page_name"]:
        return ""  # bare root
    parts = [p for p in d["path_list"] if p] + [d["file_name"]]
    return "/".join(parts)


def _walk_vault_files(abs_dir: str) -> list[str]:
    """Every regular file under ``abs_dir`` as vault-relative paths."""
    root_abs = _vault_root()
    out: list[str] = []
    for cur_root, _dirs, files in os.walk(abs_dir):
        for fn in files:
            rel = os.path.relpath(os.path.join(cur_root, fn), root_abs)
            out.append(rel.replace(os.sep, "/"))
    return out


# rmdir of a GENUINELY-EMPTY dir can transiently fail with EACCES/EBUSY on the
# Docker bind-mount over the Windows drive: the file watcher's inotify watch (or
# another container sharing the mount) momentarily holds the directory. The dir
# is empty, so this is transient contention, not a logic error -- a bounded retry
# is the correct fix (was an intermittent "folder not pruned" flake).
_RMDIR_TRANSIENT = {errno.EACCES, errno.EBUSY, errno.ENOTEMPTY, errno.EPERM}


def _rmdir_if_empty(abs_dir: str, attempts: int = 6, backoff: float = 0.05) -> bool:
    """Remove abs_dir if empty, retrying briefly on transient FS contention.
    Returns True if removed (or already gone), False if genuinely non-empty or
    the failure persisted."""
    for i in range(attempts):
        try:
            if not os.path.isdir(abs_dir):
                return True
            if os.listdir(abs_dir):
                return False  # genuinely non-empty -> nothing to prune
            os.rmdir(abs_dir)
            return True
        except OSError as e:
            if e.errno in _RMDIR_TRANSIENT and i < attempts - 1:
                time.sleep(backoff)
                continue
            logger.warning("prune_empty_dirs: could not remove %s: %s", abs_dir, e)
            return False
    return False


def _prune_empty_dirs(rel_dirs: set[str]) -> None:
    """Clean up directories emptied by a folder move.

    For each moved source folder: remove any empty subdirectories left behind
    (bottom-up, so a fully-emptied subtree collapses), then remove now-empty
    ancestors up the chain. Stops at the first non-empty directory.
    """
    for rel in rel_dirs:
        abs_dir = _abs(rel)
        if os.path.isdir(abs_dir):
            for root, _dirs, _files in os.walk(abs_dir, topdown=False):
                _rmdir_if_empty(root)

        rel = os.path.dirname(rel)
        while rel:
            abs_dir = _abs(rel)
            if os.path.isdir(abs_dir) and not os.listdir(abs_dir):
                if not _rmdir_if_empty(abs_dir):
                    break
                rel = os.path.dirname(rel)
            else:
                break


async def batch_move_op(items: list[str], destination: str, vault_id: str = DEFAULT_VAULT) -> dict:
    """Move a set of files and/or whole folders into ``destination`` (a folder), within
    ``vault_id``.

    Folders are expanded to every contained file, preserving sub-tree structure
    under ``destination/<folder name>/...``. Inbound ``[[links]]`` and
    ``![[embeds]]`` across the whole batch are rewritten in a single pass, then
    each file is moved on disk with its own git commit (the watcher reconciles the
    RAG DB; non-markdown files are re-keyed only via their referrers' reindex).

    Returns ``{status, moved: [{src, dest}], skipped: [{src, reason}],
    referrers_updated: [...]}``.
    """
    _active_vault.set(vault_id)
    dest_dir = _rel_from_path(destination)
    default_md = _default_page_md()

    rename_map: dict[str, str] = {}
    skipped: list[dict] = []
    claimed: dict[str, str] = {}          # dest_rel -> src_rel (collision guard)
    source_dirs: set[str] = set()         # folder items, for empty-dir pruning

    def _skip(src: str, reason: str):
        skipped.append({"src": src, "reason": reason})

    # Drop selections that sit inside another selected folder: the folder move
    # already carries them (preserving structure). Keeping them would re-process
    # the same source with its own immediate dir as the keep-root, flattening it
    # into the destination and overwriting the folder's structure-preserving entry.
    rels = [_rel_from_path(r) for r in items]
    kept = [
        raw
        for raw, rel in zip(items, rels)
        if not (rel and any(rel != p and rel.startswith(p + "/") for p in rels if p))
    ]

    for raw in kept:
        item_rel = _rel_from_path(raw)
        if not item_rel:
            _skip(raw, "invalid path")
            continue
        abs_item = _abs(item_rel)

        # Resolve the item into a concrete list of files to move.
        if await asyncio.to_thread(os.path.isdir, abs_item):
            # Refuse moving a folder into itself or a descendant.
            if dest_dir == item_rel or dest_dir.startswith(item_rel + "/"):
                _skip(item_rel, "cannot move a folder into itself")
                continue
            files = await asyncio.to_thread(_walk_vault_files, abs_item)
            source_dirs.add(item_rel)
            # Carry the sibling folder-note (`<name>.md` next to `<name>/`) so the
            # whole folder entry as shown in the index moves as one unit. The
            # parent-keep logic below lands it at `dest/<name>.md`.
            sibling_md = _ensure_md(item_rel)
            if await asyncio.to_thread(os.path.isfile, _abs(sibling_md)):
                files.append(sibling_md)
        elif await asyncio.to_thread(os.path.isfile, abs_item):
            files = [item_rel]
        elif await asyncio.to_thread(os.path.isfile, _abs(_ensure_md(item_rel))):
            files = [_ensure_md(item_rel)]  # extensionless markdown doc id
        else:
            _skip(item_rel, "not found")
            continue

        # Preserve everything from the item's parent downward, so a file keeps its
        # basename and a folder keeps its own name + internal structure.
        parent = os.path.dirname(item_rel)
        for f in files:
            keep = os.path.relpath(f, parent) if parent else f
            keep = keep.replace(os.sep, "/")
            dest_rel = "/".join(p for p in (dest_dir, keep) if p)

            if f == default_md:
                _skip(f, "cannot move the default page")
                continue
            if dest_rel == f:
                _skip(f, "already in destination")
                continue
            if dest_rel in claimed:
                _skip(f, f"destination collides with {claimed[dest_rel]}")
                continue
            if await asyncio.to_thread(os.path.exists, _abs(dest_rel)):
                _skip(f, f"destination already exists: {dest_rel}")
                continue
            claimed[dest_rel] = f
            rename_map[f] = dest_rel

    if not rename_map:
        return {"status": "noop", "moved": [], "skipped": skipped,
                "referrers_updated": []}

    # --- 1+2. rewrite inbound links + move every file via the shared engine
    referrers = await _apply_renames(rename_map)
    moved = [{"src": s, "dest": d} for s, d in rename_map.items()]

    # --- 3. prune source folders left empty by the move -------------------
    if source_dirs:
        await asyncio.to_thread(_prune_empty_dirs, source_dirs)

    logger.info(
        "batch_move_op: moved %d file(s) into %s (%d skipped, %d referrer(s) updated)",
        len(moved), dest_dir or "<root>", len(skipped), len(referrers),
    )
    return {
        "status": "ok",
        "moved": moved,
        "skipped": skipped,
        "referrers_updated": referrers,
    }


# ---------------------------------------------------------------------------
# Delete (single document / batch of files + folders)
# ---------------------------------------------------------------------------

async def _apply_deletes(rels: list[str]) -> None:
    """The single delete engine shared by ``delete_document_op`` and
    ``batch_delete_op``.

    Removes each file on disk and records a debounced git removal. Callers own
    validation, default-page guarding, and empty-dir pruning. Unlike the move
    engine, this deliberately does NOT rewrite inbound links (see
    ``delete_document_op``). The RAG database is reconciled by the file watcher.
    """
    # Canonical delete: checkpoint-before-delete -> os.remove -> git removal ->
    # debounce, with git best-effort (a git hiccup never blocks the removal).
    from src.wikidoc import WikiDoc
    vault = _active_vault.get()
    for rel in rels:
        await asyncio.to_thread(WikiDoc.delete_file, vault, rel)


async def delete_document_op(rel: str, vault_id: str = DEFAULT_VAULT) -> dict:
    """Delete a single file (vault-relative ``doc_id``) from disk + git, in ``vault_id``.

    Inbound links are intentionally left untouched: a ``[[link]]`` to a removed
    page should degrade to an unresolved/ghost edge (so the page can be recreated
    or the dangling reference surfaced), not silently vanish from other documents.
    The RAG database is reconciled by the file watcher (``on_deleted``). Returns a
    status dict.
    """
    _active_vault.set(vault_id)
    if _ensure_md(rel) == _default_page_md():
        return {"status": "refused", "reason": "cannot delete the default page"}
    if not await asyncio.to_thread(os.path.isfile, _abs(rel)):
        return {"status": "error", "reason": f"not found: {rel}"}

    await _apply_deletes([rel])
    logger.info("delete_document_op: %s", rel)
    return {"status": "ok", "deleted": [rel]}


async def batch_delete_op(items: list[str], vault_id: str = DEFAULT_VAULT) -> dict:
    """Delete a set of files and/or whole folders within ``vault_id``.

    Folders are expanded to every contained file (assets included) and empty source
    folders are pruned afterward. Like ``delete_document_op``, inbound links are
    intentionally left to become ghost edges. Returns ``{status, deleted: [...],
    skipped: [{src, reason}]}``.
    """
    _active_vault.set(vault_id)
    default_md = _default_page_md()
    to_delete: list[str] = []
    skipped: list[dict] = []
    source_dirs: set[str] = set()

    def _skip(src: str, reason: str):
        skipped.append({"src": src, "reason": reason})

    for raw in items:
        item_rel = _rel_from_path(raw)
        if not item_rel:
            _skip(raw, "invalid path")
            continue
        abs_item = _abs(item_rel)

        if await asyncio.to_thread(os.path.isdir, abs_item):
            files = await asyncio.to_thread(_walk_vault_files, abs_item)
            source_dirs.add(item_rel)
        elif await asyncio.to_thread(os.path.isfile, abs_item):
            files = [item_rel]
        elif await asyncio.to_thread(os.path.isfile, _abs(_ensure_md(item_rel))):
            files = [_ensure_md(item_rel)]  # extensionless markdown doc id
        else:
            _skip(item_rel, "not found")
            continue

        for f in files:
            if f == default_md:
                _skip(f, "cannot delete the default page")
                continue
            to_delete.append(f)

    if not to_delete:
        return {"status": "noop", "deleted": [], "skipped": skipped}

    await _apply_deletes(to_delete)

    if source_dirs:
        await asyncio.to_thread(_prune_empty_dirs, source_dirs)

    logger.info(
        "batch_delete_op: deleted %d file(s) (%d skipped)",
        len(to_delete), len(skipped),
    )
    return {"status": "ok", "deleted": to_delete, "skipped": skipped}
