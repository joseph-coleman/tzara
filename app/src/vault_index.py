# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Filesystem-backed index of vault files, for Obsidian-style wikilink resolution
at render time.

The renderer must resolve a ``[[link]]`` against *every* note in the vault (not just
the current folder), and it must work on a vault that has never been RAG-indexed --
so the candidate set comes from the **filesystem**, not Postgres. This module keeps a
process-local cache of that file list, rebuilt on a short TTL.

Why a TTL rather than push-invalidation: the file watcher runs in the *worker*
process (``task_definitions._start_file_watcher``), so it cannot reach into the web
process to invalidate a cache here. A short TTL is self-healing and -- crucially for a
"drop-in Obsidian frontend" -- also picks up edits made directly in the vault by
Obsidian, which never touch our endpoints. The scan is O(files); for a personal wiki
that is trivial. If it ever isn't, swap the body of ``_scan``/``get_index`` for a
dir-mtime check or a Redis version counter behind this same API.
"""

import os
import threading
import time

from config import DEFAULT_VAULT, vault_abs_root
from src.chunker import resolve_linkpath, wikilink_key

# How long a built index is trusted before the next access rebuilds it. Matches the
# watcher's poll interval so staleness windows line up.
_TTL_SECONDS = 3.0

_lock = threading.Lock()
# Per-vault caches: {vault_id: {"built_at", "paths", "by_stem"}}. Each vault has its
# own candidate set so a [[wikilink]] only ever resolves within its own vault.
_caches: dict[str, dict] = {}


def _vault_root(vault: str) -> str:
    return vault_abs_root(vault)


def _scan(vault: str) -> tuple[list, dict]:
    """Walk the vault and return (all vault-relative paths, {stem_key: [paths]}).

    All file types are indexed, not just markdown: a wikilink can target a
    non-markdown file by its full basename (e.g. [[Some Diagram.canvas]]), which the
    renderer must resolve the way the old extension-aware existence check did. The
    stem key strips only a trailing .md, so a bare [[Name]] still matches notes while
    an extension-qualified [[Name.canvas]] matches the canvas -- mirroring how
    Obsidian addresses notes without an extension but other files with one."""
    root = _vault_root(vault)
    paths: list[str] = []
    by_stem: dict[str, list[str]] = {}
    for cur_root, dirs, files in os.walk(root):
        # Don't descend into dot-dirs (.git, .obsidian, .trash, ...).
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for fn in files:
            rel = os.path.relpath(os.path.join(cur_root, fn), root).replace(os.sep, "/")
            paths.append(rel)
            stem = (rel[:-3] if rel.endswith(".md") else rel).split("/")[-1]
            by_stem.setdefault(wikilink_key(stem), []).append(rel)
    return paths, by_stem


def get_index(vault: str = DEFAULT_VAULT, force: bool = False) -> tuple[list, dict]:
    """Cached (paths, by_stem) for ``vault``, rebuilt when older than the TTL."""
    now = time.monotonic()
    with _lock:
        cache = _caches.setdefault(vault, {"built_at": 0.0, "paths": [], "by_stem": {}})
        if force or (now - cache["built_at"]) > _TTL_SECONDS:
            paths, by_stem = _scan(vault)
            cache.update(built_at=now, paths=paths, by_stem=by_stem)
        return cache["paths"], cache["by_stem"]


def resolve(target: str, source_dir: str, vault: str = DEFAULT_VAULT) -> str | None:
    """Resolve a wikilink/embed target (no #anchor/|alias) to a vault-relative .md
    path, Obsidian-style, or None, scoped to ``vault``. ``source_dir`` is the source
    document's folder."""
    _, by_stem = get_index(vault)
    return resolve_linkpath(target, source_dir, by_stem=by_stem)


def invalidate(vault: str | None = None) -> None:
    """Force the next access to rebuild. Pass a vault to invalidate just that one, or
    None to invalidate all. Optional: web-process mutations (save / move / delete) can
    call this for immediate freshness instead of waiting out the TTL."""
    with _lock:
        if vault is None:
            _caches.clear()
        elif vault in _caches:
            _caches[vault]["built_at"] = 0.0
