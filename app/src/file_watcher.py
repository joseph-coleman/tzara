# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
File system watcher for wiki directory changes.

Uses watchdog's PollingObserver to detect file create/modify/delete/move
events and enqueue corresponding RAG indexing tasks via taskiq.

PollingObserver is used instead of native inotify because Docker Desktop
on Windows/macOS does not reliably propagate inotify events through
volume mounts.

Redis debounce keys prevent duplicate task enqueueing from multiple
watchers and feedback loops from self-generated file writes.
"""

import asyncio
import logging
import os
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver
from src.task_tracker import PENDING_KEY, preseed_pending

logger = logging.getLogger("file_watcher")

# Watch the VAULTS_DIR *parent*: every immediate subdirectory is a vault, and the vault
# slug is the first path segment of each event (see _split_vault). This lets one watcher
# serve all vaults; events are routed to vault-scoped tasks/Redis keys.
from config import AGENT_OUTPUT_DIR, EXCLUDED_FOLDERS
from config import VAULTS_DIR as WATCH_DIR  # = "vaults"
POLL_INTERVAL = 3

# AGENT_OUTPUT_DIR (agent-owned pages, e.g. "_dada"): no tasks fire at all - the
# area is RAG-excluded by location and the agent's writer commits its own files.
#
# Built FROM the canonical RAG exclusion set (which already contains
# AGENT_OUTPUT_DIR) rather than restating it: these two lists disagreeing is what
# leaked `.tzara/config.json` into the index. The extras are dot-dirs this watcher
# must also skip for git/commit purposes; matching by NAME here rather than by a
# leading-dot rule is deliberate, because _should_ignore sees ABSOLUTE paths and a
# checkout under e.g. ~/.local would otherwise ignore every event in the vault.
IGNORED_DIRS = set(EXCLUDED_FOLDERS) | {".git", "__pycache__", ".obsidian", ".tzara", ".trash"}
IGNORED_FILES = {".DS_Store", "Thumbs.db"}
IGNORED_SUFFIXES = {".swp", ".swo", ".tmp", ".bak", ".crswap", "~"}
IGNORED_PREFIXES = ("~", ".")

DEBOUNCE_TTL = 10
# Suppression window for a write the wiki ITSELF just made (metadata/frontmatter
# writes during a bulk run). Longer than DEBOUNCE_TTL because the poll that sees
# the write can land well after it, and the whole point is that the watcher never
# enqueues a task for our own edit.
WRITE_DEBOUNCE_TTL = 120

CANCELLED_SET = "watcher:cancelled"
CANCELLED_TTL = 120


def watcher_task_id(task_name: str, vault: str, rel_path: str) -> str:
    """THE watcher task id. Build it here, never by hand.

    This string is a CONTRACT between three parties that never call each other:
    the watcher enqueues under it, `_cancel_contradicting` looks up the pending
    row by it, and `task_definitions._is_cancelled` re-derives it inside the
    running task to check the cancelled set. A mismatch is SILENT - no error,
    the lookup simply never matches and cancellation quietly stops working."""
    return f"watcher:{task_name}:{vault}:{rel_path}"


def watcher_debounce_key(task_name: str, vault: str, rel_path: str) -> str:
    """THE watcher debounce key. Build it here, never by hand.

    Also a cross-module contract: the wiki's own write paths pre-set this key
    (with WRITE_DEBOUNCE_TTL) so the watcher skips the modify event they are
    about to cause. This has already drifted once - the vault segment was added
    for multi-vault and the pre-setters were missed, so every metadata write
    fired a spurious reindex until it was tracked down."""
    return f"watcher:debounce:{task_name}:{vault}:{rel_path}"

# When a new event fires, which pending task types does it contradict?
_CONTRADICTIONS = {
    "index_document_task": ["remove_document_task"],
    "update_document_task": ["remove_document_task"],
    "remove_document_task": ["index_document_task", "update_document_task"],
    "move_document_task": ["index_document_task", "update_document_task"],
}


class WikiFileEventHandler(FileSystemEventHandler):

    def __init__(self, loop, wiki_root, redis_url):
        super().__init__()
        self._loop = loop
        self._wiki_root = os.path.abspath(wiki_root)
        self._redis_url = redis_url

    def _should_ignore(self, path):
        parts = Path(path).parts
        for part in parts:
            if part in IGNORED_DIRS:
                return True
        filename = os.path.basename(path)
        if filename in IGNORED_FILES:
            return True
        if any(filename.endswith(s) for s in IGNORED_SUFFIXES):
            return True
        if any(filename.startswith(p) for p in IGNORED_PREFIXES):
            return True
        return False

    def _split_vault(self, path):
        """Map an absolute event path to (vault_slug, vault_relative_path).

        The watcher root is the VAULTS_DIR parent, so a path is
        ``<root>/<vault>/<rel...>``: the first segment is the vault, the rest is the
        doc_id used by the tasks. Returns (None, None) for a path with no in-vault
        remainder (e.g. a stray file dropped directly in the parent, or the vault dir
        itself), which callers skip."""
        rel = os.path.relpath(path, self._wiki_root).replace(os.sep, "/")
        parts = rel.split("/")
        if len(parts) < 2 or parts[0] in ("", ".", ".."):
            return None, None
        # System vaults (agent definitions, help docs) flow through the normal
        # pipeline: watcher git commits + RAG ingest/embed, so their content is
        # versioned and searchable WITHIN the vault (search is hard vault-scoped;
        # agents never run against system vaults). The one thing they never get
        # is LLM frontmatter generation - blessed files are human-only, enforced
        # in rag_indexer. The RAG-pollution concern lives at the OUTPUT location
        # (AGENT_OUTPUT_DIR in content vaults), not here.
        return parts[0], "/".join(parts[1:])

    def _enqueue(self, coro):
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    async def _cancel_contradicting(self, r, task_name, vault, rel_path):
        """Cancel pending tasks contradicted by a new event on the same (vault, path)."""
        to_cancel = _CONTRADICTIONS.get(task_name, [])
        for contra_task in to_cancel:
            task_id = watcher_task_id(contra_task, vault, rel_path)
            if await r.hexists(PENDING_KEY, task_id):
                await r.sadd(CANCELLED_SET, task_id)
                await r.expire(CANCELLED_SET, CANCELLED_TTL)
                await r.hdel(PENDING_KEY, task_id)
                logger.info("cancelled contradicting task: %s (superseded by %s)", task_id, task_name)

    async def _debounced_enqueue(self, task_func, vault, rel_path, cancel_path=None, **kwargs):
        # get_async_redis (not a bare from_url) so this path inherits the shared
        # socket timeout + command retry - see its docstring: an unretried drop
        # here is exactly what stranded a task on 2026-07-29.
        from src.task_broker import get_async_redis

        r = get_async_redis()
        task_id = None
        preseeded = False
        try:
            # Cancel contradicting tasks for the affected (vault, path). Keys are
            # vault-scoped to match task_definitions._is_cancelled.
            await self._cancel_contradicting(r, task_func.task_name, vault, cancel_path or rel_path)

            key = watcher_debounce_key(task_func.task_name, vault, rel_path)
            was_set = await r.set(key, "1", nx=True, ex=DEBOUNCE_TTL)
            if not was_set:
                logger.debug("debounce skip: %s/%s", vault, rel_path)
                return
            logger.info("enqueueing %s for %s/%s", task_func.task_name, vault, rel_path)
            task_id = watcher_task_id(task_func.task_name, vault, rel_path)
            await preseed_pending(r, task_id, task_func.task_name)
            preseeded = True
            await task_func.kicker().with_task_id(task_id).kiq(vault_id=vault, **kwargs)
        except Exception:
            logger.exception("failed to enqueue task for %s/%s", vault, rel_path)
            if preseeded:
                # The pre-seed has no TTL: orphaned, it reads as "queued" FOREVER
                # on /manage/tasks while no broker message exists to run, fail or
                # clear it - and the document silently never gets indexed.
                try:
                    await r.hdel(PENDING_KEY, task_id)
                except Exception:
                    logger.exception("failed to clean pre-seed %s", task_id)
        finally:
            await r.close()

    def on_created(self, event):
        if event.is_directory:
            return
        if self._should_ignore(event.src_path):
            return
        vault, rel = self._split_vault(event.src_path)
        if vault is None:
            return
        logger.info("file created: %s/%s", vault, rel)
        from src.task_definitions import index_document_task

        self._enqueue(
            self._debounced_enqueue(index_document_task, vault, rel, file_path=rel)
        )

    def on_modified(self, event):
        if event.is_directory:
            return
        if self._should_ignore(event.src_path):
            return
        vault, rel = self._split_vault(event.src_path)
        if vault is None:
            return
        logger.info("file modified: %s/%s", vault, rel)
        from src.task_definitions import update_document_task

        self._enqueue(
            self._debounced_enqueue(update_document_task, vault, rel, file_path=rel)
        )

    def on_deleted(self, event):
        if event.is_directory:
            return
        if self._should_ignore(event.src_path):
            return
        vault, rel = self._split_vault(event.src_path)
        if vault is None:
            return
        logger.info("file deleted: %s/%s", vault, rel)
        from src.task_definitions import remove_document_task

        self._enqueue(
            self._debounced_enqueue(remove_document_task, vault, rel, file_path=rel)
        )

    def on_moved(self, event):
        if event.is_directory:
            return
        if self._should_ignore(event.src_path) and self._should_ignore(event.dest_path):
            return
        src_vault, src_rel = self._split_vault(event.src_path)
        dest_vault, dest_rel = self._split_vault(event.dest_path)
        if dest_vault is None:
            return
        from src.task_definitions import (
            move_document_task, remove_document_task, index_document_task,
        )
        if src_vault is not None and src_vault != dest_vault:
            # Cross-vault filesystem move (e.g. Obsidian drag between vault folders).
            # Hard isolation: it's not a re-key -- remove from the source vault and
            # index fresh into the destination vault.
            logger.info("cross-vault move: %s/%s -> %s/%s", src_vault, src_rel, dest_vault, dest_rel)
            self._enqueue(
                self._debounced_enqueue(remove_document_task, src_vault, src_rel, file_path=src_rel)
            )
            self._enqueue(
                self._debounced_enqueue(index_document_task, dest_vault, dest_rel, file_path=dest_rel)
            )
            return
        logger.info("file moved: %s/%s -> %s", dest_vault, src_rel, dest_rel)
        self._enqueue(
            self._debounced_enqueue(
                move_document_task, dest_vault, dest_rel,
                cancel_path=src_rel,
                src_path=src_rel, dest_path=dest_rel,
            )
        )

def start_watcher(loop, redis_url):
    """
    loop - asyncio running loop
    redis_url - redis:// from configs
    """
    os.makedirs(WATCH_DIR, exist_ok=True)
    wiki_root = os.path.abspath(WATCH_DIR)
    handler = WikiFileEventHandler(loop, wiki_root, redis_url)
    observer = PollingObserver(timeout=POLL_INTERVAL)
    observer.schedule(handler, wiki_root, recursive=True)
    observer.start()
    logger.info("started watching vaults parent (all vaults): %s", wiki_root)
    return observer


def stop_watcher(observer):
    try:
        observer.stop()
        observer.join(timeout=10)
        logger.info("file watcher stopped")
    except Exception:
        logger.exception("error stopping file watcher")
