# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
RAG document ingestion pipeline.

Three public async functions:
  - ingest_document(file_path)   -- entry point from file watcher tasks
  - generate_frontmatter(file_path) -- LLM tag/summary generation
  - embed_document(file_path)    -- chunking + embedding + DB write
"""

import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path

from config import (
    DEFAULT_VAULT,
    EXCLUDED_FOLDERS,
    INDEX_DOCUMENT_FRONTMATTER_DEFAULT,
    OLLAMA_EMBED_MODEL,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_URL,
    truncate_for_embedding,
    vault_root,
)
from src import vault_registry
from src.chunker import resolve_linkpath, wikilink_key
from src.task_broker import get_async_redis
from src.task_tracker import preseed_pending
from src.wikidoc import WikiDoc

logger = logging.getLogger("rag_indexer")

EMBED_BATCH_SIZE = 8  # how many items to embed at once, more than 16 can lower quality
FRONTMATTER_PROCESSED_TTL = 60


async def _register_and_enqueue(task_func, task_id: str, **kwargs):
    """Record a task in the tracker pending hash, then enqueue it."""
    r = get_async_redis()
    try:
        await preseed_pending(r, task_id, task_func.task_name)
    finally:
        await r.close()
    await task_func.kicker().with_task_id(task_id).kiq(**kwargs)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_pg_connection():
    from config import get_pg_connection
    return get_pg_connection()


def _compute_content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _abs_path(rel_path: str, vault_id: str = DEFAULT_VAULT) -> str:
    return os.path.join(os.getcwd(), vault_root(vault_id), rel_path)


# A chunk needs at least one word character to be worth embedding. Whitespace was
# the original bar, but it lets through punctuation-only chunks -- a `---` rule
# between two fenced examples becomes its own `prose` chunk, and the resulting
# embedding is the same arbitrary-query noise an empty one would be. `\w` is
# Unicode-aware, so CJK and accented text still pass.
_WORD_CHAR_RE = re.compile(r"\w")


def has_indexable_text(content: str | None) -> bool:
    """Does this chunk carry text worth embedding?"""
    return bool(content) and _WORD_CHAR_RE.search(content) is not None


def is_excluded(rel_path: str) -> bool:
    """Does this vault-relative path fall under an excluded folder?

    THE exclusion gate - task_definitions imports this rather than keeping its own
    copy (it had a byte-identical one until 2026-07-31). Anything that decides
    "should this file be indexed?" must agree, or a reconcile reports phantom gaps.

    A DOT-DIRECTORY is excluded whatever EXCLUDED_FOLDERS lists. That is not new
    policy: enumerate_vault_markdown's glob has always skipped dot-dirs, so a file
    under one could never be enumerated - only reached by a per-file ingest. Naming
    them one at a time is what let `.tzara/config.json` in; it was in the watcher's
    IGNORED_DIRS and not in EXCLUDED_FOLDERS, so every path that did not go through
    the watcher indexed it. Matching the enumerator's rule beats maintaining a list.

    Takes a vault-RELATIVE path. Handing it an absolute one would test the mount
    point's own segments (`/home/me/.local/...`) and exclude everything.
    """
    parts = Path(rel_path).parts
    for part in parts:
        if part in EXCLUDED_FOLDERS or part.startswith("."):
            return True
    return False


# Back-compat alias for this module's existing private call sites.
_is_excluded = is_excluded


def enumerate_vault_markdown(vault_id: str | None = None,
                             include_system: bool = True) -> list[tuple[str, str, list[str]]]:
    """(vault_id, wiki_root, [vault-relative .md paths]) for the target vault(s).

    THE definition of "markdown the wiki knows about", shared by reindex_all_task,
    generate_all_metadata_task and find_unindexed_documents so a reconcile can
    never disagree with the bulk tasks about what exists.

    include_system defaults True: system vaults ARE RAG-indexed (search is
    vault-scoped, so they only surface when searching the system vault itself) -
    and a system vault is exactly where the 2026-07-29 lost index went unnoticed.
    The metadata bulk task passes False: blessed files are human-authored and must
    never receive LLM-generated frontmatter. That difference is REAL, so it stays
    a parameter rather than being flattened into one enumeration.

    Uses glob (not Path.rglob) deliberately: glob skips dot-directories, so
    `.tzara/` and friends never enter the candidate set in the first place.
    """
    import glob as glob_mod

    from src import vault_registry

    vaults = vault_registry.list_vaults(include_system=include_system)
    if vault_id is not None:
        vaults = [v for v in vaults if v["vault_id"] == vault_id]

    out: list[tuple[str, str, list[str]]] = []
    for v in vaults:
        vid = v["vault_id"]
        wiki_root = os.path.abspath(vault_root(vid))
        files = glob_mod.glob(os.path.join(wiki_root, "**", "*.md"), recursive=True)
        out.append((vid, wiki_root, [os.path.relpath(f, wiki_root) for f in files]))
    return out


def is_indexable(rel_path: str) -> bool:
    """Should this .md file have a `documents` row? Mirrors ingest_document's own
    skip gates (dotfile basename, excluded folder). The .canvas gate is moot here
    because callers feed this only *.md."""
    if os.path.basename(rel_path).startswith("."):
        return False
    return not is_excluded(rel_path)


def _enumerable(rel_path: str) -> bool:
    """Could enumerate_vault_markdown have seen this doc_id?

    Staleness is "row exists, file does not", so it may only be judged against paths
    the enumerator can actually see: `*.md`, and nothing under a dot-directory (glob
    skips those). Without this, `.tzara/config.json` rows read as deleted files.
    """
    return (rel_path.lower().endswith(".md")
            and not any(seg.startswith(".") for seg in rel_path.replace(os.sep, "/").split("/")))


async def find_unindexed_documents(vault_id: str | None = None) -> dict:
    """REPORT-ONLY reconcile, BOTH directions, from one enumeration of each vault.

    * `missing` - markdown on disk with no `documents` row. This direction had no
      check at all.
    * `stale` - a `documents` row whose file is gone. `prune_deleted_documents`
      fixes these, but it only runs inside a manual full reindex, so rows for
      files deleted or moved outside the app accumulate silently until someone
      remembers to reindex.

    Both directions share one disk walk so they can never disagree about what
    exists - the same reason enumerate_vault_markdown is shared with the bulk tasks.

    Deliberately convergent rather than transport-level: it asks "does the world
    look right?" from durable state, so it catches a failed enqueue, a worker
    killed mid-task, a bug not yet found, AND a file dropped in manually while
    the stack was down - none of which a queue could tell you about.

    Reports; never enqueues, never deletes. Returns
    {"checked": n, "missing": {vault: [path]}, "total_missing": n,
     "stale": {vault: [path]}, "total_stale": n}.
    """
    vault_files = enumerate_vault_markdown(vault_id)

    def _indexed(vid: str) -> set[str]:
        conn = _get_pg_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT doc_id FROM documents WHERE vault_id = %s AND doc_exists = TRUE",
                (vid,),
            )
            return {r[0] for r in cur.fetchall()}
        finally:
            conn.close()

    checked = 0
    missing: dict[str, list[str]] = {}
    stale: dict[str, list[str]] = {}
    for vid, _root, rel_paths in vault_files:
        # A vault whose volume failed to mount globs empty and is skipped here, so an
        # infrastructure fault can never be reported as "everything missing/stale".
        if not rel_paths:
            continue
        candidates = sorted(p for p in rel_paths if is_indexable(p))
        indexed = await asyncio.to_thread(_indexed, vid)
        checked += len(candidates)

        gaps = [p for p in candidates if p not in indexed]
        if gaps:
            missing[vid] = gaps
            logger.warning("find_unindexed_documents: %s has %d unindexed file(s)",
                           vid, len(gaps))

        # Compared against the UNFILTERED disk set: an opted-in `_dada` page is a
        # real file with a legitimate row, so exclusion must not make it look gone.
        on_disk = set(rel_paths)
        gone = sorted(d for d in indexed if _enumerable(d) and d not in on_disk)
        if gone:
            stale[vid] = gone
            logger.warning("find_unindexed_documents: %s has %d stale row(s)",
                           vid, len(gone))

    return {"checked": checked,
            "missing": missing, "total_missing": sum(len(v) for v in missing.values()),
            "stale": stale, "total_stale": sum(len(v) for v in stale.values())}


def _read_file(abs_path: str) -> str:
    # Canonical LF-normalized read (the indexer/chunker + frontmatter parsers
    # assume '\n'); matches the previous default-read behavior exactly.
    from src.wikidoc import WikiDoc
    return WikiDoc.read_text_at(abs_path)


def _extract_title(rel_path: str, frontmatter: dict) -> str:
    if "title" in frontmatter and frontmatter["title"]:
        return frontmatter["title"]
    return Path(rel_path).stem


def _get_stored_hash(doc_id: str, vault_id: str = DEFAULT_VAULT) -> str | None:
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT content_hash FROM documents WHERE vault_id = %s AND doc_id = %s",
            (vault_id, doc_id),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _upsert_document_row(doc_id: str, title: str, abs_file: str, vault_id: str = DEFAULT_VAULT):
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO documents (vault_id, doc_id, title, file_path, doc_exists)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (vault_id, doc_id) DO UPDATE
                SET title = EXCLUDED.title,
                    file_path = EXCLUDED.file_path,
                    doc_exists = TRUE,
                    updated_at = NOW()
            """,
            (vault_id, doc_id, title, abs_file),
        )
        conn.commit()
    finally:
        conn.close()


def _batch_embed(texts: list[str]) -> list[list[float]]:
    """Synchronously batch-embed texts via the configured LLM backend. Called
    inside asyncio.to_thread. Routes through llm_backend.embed_texts_sync so the
    embed surface follows LLM_PROVIDER (native /api/embed, or /v1/embeddings for a
    pure OpenAI server) instead of hardcoding an Ollama client."""
    from src.llm_backend import embed_texts_sync

    all_embeddings = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        # Clip each input so an oversized chunk can't hard-fail the whole embed call
        # against a strict llama.cpp backend (Ollama used to truncate silently).
        batch = [truncate_for_embedding(t) for t in texts[i : i + EMBED_BATCH_SIZE]]
        all_embeddings.extend(embed_texts_sync(batch))
    return all_embeddings


def _vector_literal(vec: list[float]) -> str:
    """Format a float list as a PostgreSQL vector literal string."""
    return "[" + ",".join(str(v) for v in vec) + "]"


def _build_name_index(cur, vault_id: str = DEFAULT_VAULT) -> dict:
    """Candidate index for Obsidian-style link resolution: map
    wikilink_key(filename stem) -> [doc_id, ...] over the documents IN ONE VAULT.

    The ``WHERE vault_id = %s`` filter here is THE link-isolation point: it is what
    makes a [[wikilink]] resolve only within its own vault. Keyed on basename only;
    chunker.resolve_linkpath does the path-suffix + proximity match over the doc_ids
    that share a basename. Resolution is by filename (the way Obsidian resolves).
    """
    cur.execute("SELECT doc_id FROM documents WHERE vault_id = %s", (vault_id,))
    by_stem: dict[str, list] = {}
    for row in cur.fetchall():
        doc_id = row[0]
        rel = doc_id[:-3] if doc_id.endswith(".md") else doc_id
        stem = rel.split("/")[-1]
        by_stem.setdefault(wikilink_key(stem), []).append(doc_id)
    return by_stem


def _resolve_target_doc_id(name_index: dict, target_title: str, source_doc_id: str = "") -> str | None:
    """doc_id a wikilink target resolves to, or None. Obsidian-style: a vault-global
    basename/path-suffix match, with the source document's folder breaking ties by
    proximity. Shares chunker.resolve_linkpath with the renderer so the graph and the
    rendered links never disagree."""
    source_dir = source_doc_id.rsplit("/", 1)[0] if "/" in source_doc_id else ""
    # resolve_linkpath resolves whole documents and requires an anchor-free target
    # (it does NOT strip these itself - that's the caller's job). A section/block
    # embed ![[Page#Heading]] / ![[Page#^blk]] and an anchored wikilink
    # [[Page#Heading]] both still link the whole Page, so drop any #anchor and
    # |alias here; otherwise the edge never resolves and shows as a ghost node.
    clean = target_title.split("|", 1)[0].split("#", 1)[0]
    return resolve_linkpath(clean, source_dir, by_stem=name_index)


def _insert_edge(cur, name_index, source_doc_id: str, target_title: str, edge_type: str,
                 vault_id: str = DEFAULT_VAULT):
    """Insert an edge, resolving target_doc_id via the candidate name index.

    The edge (and its resolved target) are confined to ``vault_id`` -- under hard
    isolation a link never crosses a vault boundary, so source and target share it.
    """
    target_doc_id = _resolve_target_doc_id(name_index, target_title, source_doc_id)
    resolved = target_doc_id is not None

    cur.execute(
        """
        INSERT INTO edges (vault_id, source_doc_id, target_title, target_doc_id, edge_type, resolved)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (vault_id, source_doc_id, target_title, edge_type) DO UPDATE
            SET target_doc_id = EXCLUDED.target_doc_id,
                resolved = EXCLUDED.resolved
        """,
        (vault_id, source_doc_id, target_title, target_doc_id, edge_type, resolved),
    )


def _resolve_ghost_edges(doc_id: str, title: str = "", vault_id: str = DEFAULT_VAULT) -> int:
    """Re-resolve unresolved edges within ``vault_id`` now that ``doc_id`` exists.

    A ghost edge is unresolved only because no file matched its target; when a new
    document appears, any such edge whose target now resolves (via the shared
    Obsidian-style resolver, honoring each edge's own source folder for proximity)
    is pointed at the matching document -- typically the one just created. Scoped to
    the vault so a new page only resolves ghosts in its own vault. ``title`` is unused
    now that resolution is by filename (kept for call-site compatibility).
    """
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        name_index = _build_name_index(cur, vault_id)
        cur.execute(
            "SELECT id, source_doc_id, target_title FROM edges "
            "WHERE resolved = FALSE AND target_doc_id IS NULL AND vault_id = %s",
            (vault_id,),
        )
        updates = [
            (tgt, eid)
            for eid, source_doc_id, target_title in cur.fetchall()
            if (tgt := _resolve_target_doc_id(name_index, target_title, source_doc_id)) is not None
        ]
        for tgt, eid in updates:
            cur.execute(
                "UPDATE edges SET resolved = TRUE, target_doc_id = %s WHERE id = %s",
                (tgt, eid),
            )
        count = len(updates)
        conn.commit()
        if count > 0:
            logger.info("_resolve_ghost_edges: resolved %d edges after %s", count, doc_id)
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reresolve_edges() -> int:
    """One-time backfill: recompute every edge's target_doc_id/resolved with the
    canonical resolver and persist rows that change. Returns the count updated.

    Lets a fix to wikilink resolution take effect on already-stored edges without
    re-indexing every document.
    """
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        # Per-vault name index cache so each edge resolves only within its own vault.
        name_indexes: dict[str, dict] = {}
        cur.execute(
            "SELECT id, vault_id, source_doc_id, target_title, target_doc_id, resolved FROM edges"
        )
        changed = 0
        for eid, vault_id, source_doc_id, target_title, target_doc_id, resolved in cur.fetchall():
            if vault_id not in name_indexes:
                name_indexes[vault_id] = _build_name_index(conn.cursor(), vault_id)
            name_index = name_indexes[vault_id]
            new_target = _resolve_target_doc_id(name_index, target_title, source_doc_id)
            new_resolved = new_target is not None
            if new_target != target_doc_id or new_resolved != resolved:
                cur.execute(
                    "UPDATE edges SET target_doc_id = %s, resolved = %s WHERE id = %s",
                    (new_target, new_resolved, eid),
                )
                changed += 1
        conn.commit()
        logger.info("reresolve_edges: updated %d edges", changed)
        return changed
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def index_links(doc_id: str, title: str, abs_file: str, body: str, content_hash: str,
                vault_id: str = DEFAULT_VAULT) -> int:
    """Write a document's link structure (edges) WITHOUT embedding it.

    Used for pages excluded from RAG (Index:False) so they still contribute to the
    knowledge graph / wiki link map. Mirrors the edge+asset half of _write_to_db but
    skips chunks/embeddings, and flags the documents row rag_indexed=FALSE. Returns
    the number of edges written.

    This is also the transition point INTO link-only status, so it must delete the
    RAG artifacts a previous indexing left behind - not just decline to write new
    ones. Toggling Index:False on an indexed page is the common way to get here.
    """
    from src.chunker import _classify_embed, extract_embeds, extract_page_links

    wikilinks = list(dict.fromkeys(extract_page_links(body)))
    embeds = list(dict.fromkeys(extract_embeds(body)))
    doc_embeds = [t for t in embeds if _classify_embed(t) == "doc"]
    asset_refs = [t for t in embeds if _classify_embed(t) == "asset"]

    conn = _get_pg_connection()
    try:
        cur = conn.cursor()
        # Upsert the row, marking it link-only (rag_indexed=FALSE). EXCLUDED.* works
        # in DO UPDATE so the same content_hash flows whether inserting or updating.
        cur.execute(
            """
            INSERT INTO documents
                (vault_id, doc_id, title, file_path, doc_exists, rag_indexed, content_hash, indexed_at)
            VALUES (%s, %s, %s, %s, TRUE, FALSE, %s, NOW())
            ON CONFLICT (vault_id, doc_id) DO UPDATE
                SET title = EXCLUDED.title,
                    file_path = EXCLUDED.file_path,
                    doc_exists = TRUE,
                    rag_indexed = FALSE,
                    content_hash = EXCLUDED.content_hash,
                    indexed_at = NOW(),
                    updated_at = NOW()
            """,
            (vault_id, doc_id, title, abs_file, content_hash),
        )
        # Shed everything RAG-derived. A page that flips to Index:False AFTER it was
        # indexed keeps its old chunks otherwise -- they stay searchable forever and
        # serve whatever the page said on the day it was last embedded.
        cur.execute("DELETE FROM chunks WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))
        cur.execute("DELETE FROM doc_embeddings WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))
        cur.execute("DELETE FROM document_tags WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))

        # Replace this doc's link structure (within its vault). Assets are link
        # structure too -- the RAG path collects them per chunk, but they are just
        # the non-doc half of the same embeds, so link-only indexing can keep
        # asset_refs current without chunking.
        cur.execute(
            "DELETE FROM edges WHERE vault_id = %s AND source_doc_id = %s",
            (vault_id, doc_id),
        )
        cur.execute(
            "DELETE FROM asset_refs WHERE vault_id = %s AND doc_id = %s",
            (vault_id, doc_id),
        )
        name_index = _build_name_index(cur, vault_id)
        for target_title in wikilinks:
            _insert_edge(cur, name_index, doc_id, target_title, "wikilink", vault_id)
        for target_title in doc_embeds:
            _insert_edge(cur, name_index, doc_id, target_title, "embed", vault_id)
        for asset_name in asset_refs:
            cur.execute(
                """
                INSERT INTO asset_refs (vault_id, asset_name, doc_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (vault_id, asset_name, doc_id) DO NOTHING
                """,
                (vault_id, asset_name, doc_id),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    # Resolve any ghost edges from other docs that point to this one by title.
    _resolve_ghost_edges(doc_id, title, vault_id)
    return len(wikilinks) + len(doc_embeds)


def _write_to_db(
    doc_id: str,
    title: str,
    abs_file: str,
    content_hash: str,
    summary: str,
    summary_embedding: list[float] | None,
    chunks_data: list[dict],
    chunk_embeddings: list[list[float]],
    all_wikilinks: list[tuple[str, str]],
    all_tags: list[tuple[str, str]],
    all_asset_refs: list[str],
    all_doc_embeds: list[str],
    vault_id: str = DEFAULT_VAULT,
):
    """Single-transaction write of all chunk/edge/tag/asset data (scoped to vault)."""
    conn = _get_pg_connection()
    try:
        cur = conn.cursor()

        # Delete old data for this doc (within its vault).
        cur.execute("DELETE FROM chunks WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))
        cur.execute("DELETE FROM edges WHERE vault_id = %s AND source_doc_id = %s", (vault_id, doc_id))
        cur.execute("DELETE FROM document_tags WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))
        cur.execute("DELETE FROM asset_refs WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))
        cur.execute("DELETE FROM doc_embeddings WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))

        # Insert chunks
        for chunk, embedding in zip(chunks_data, chunk_embeddings):
            vec_str = _vector_literal(embedding)
            cur.execute(
                """
                INSERT INTO chunks
                    (vault_id, doc_id, chunk_index, chunk_type, header_path,
                     content, context_content, wikilinks,
                     embedding, search_vector)
                VALUES
                    (%s, %s, %s, %s, %s, %s, %s, %s,
                     %s::vector, to_tsvector('english', %s))
                """,
                (
                    vault_id,
                    doc_id,
                    chunk["chunk_index"],
                    chunk["chunk_type"],
                    chunk.get("header_path", []),
                    chunk["content"],
                    chunk["context_content"],
                    chunk.get("wikilinks", []),
                    vec_str,
                    chunk["content"],
                ),
            )

        # Insert edges: wikilinks
        name_index = _build_name_index(cur, vault_id)
        for target_title, edge_type in all_wikilinks:
            _insert_edge(cur, name_index, doc_id, target_title, edge_type, vault_id)

        # Insert edges: document embeds
        for target_title in all_doc_embeds:
            _insert_edge(cur, name_index, doc_id, target_title, "embed", vault_id)

        # Insert tags
        for tag, source in all_tags:
            cur.execute(
                """
                INSERT INTO document_tags (vault_id, doc_id, tag, source)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (vault_id, doc_id, tag) DO UPDATE SET source = EXCLUDED.source
                """,
                (vault_id, doc_id, tag, source),
            )

        # Insert asset refs
        for asset_name in all_asset_refs:
            cur.execute(
                """
                INSERT INTO asset_refs (vault_id, asset_name, doc_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (vault_id, asset_name, doc_id) DO NOTHING
                """,
                (vault_id, asset_name, doc_id),
            )

        # Upsert doc_embeddings
        if summary_embedding and summary:
            vec_str = _vector_literal(summary_embedding)
            cur.execute(
                """
                INSERT INTO doc_embeddings (vault_id, doc_id, summary, embedding)
                VALUES (%s, %s, %s, %s::vector)
                ON CONFLICT (vault_id, doc_id) DO UPDATE
                    SET summary = EXCLUDED.summary,
                        embedding = EXCLUDED.embedding,
                        created_at = NOW()
                """,
                (vault_id, doc_id, summary, vec_str),
            )

        # Update documents row
        cur.execute(
            """
            UPDATE documents
            SET content_hash = %s, summary = %s, rag_indexed = TRUE,
                indexed_at = NOW(), updated_at = NOW()
            WHERE vault_id = %s AND doc_id = %s
            """,
            (content_hash, summary, vault_id, doc_id),
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Public pipeline functions
# ---------------------------------------------------------------------------

async def ingest_document(
    file_path: str, force: bool = False, skip_frontmatter_gen: bool = False,
    vault_id: str = DEFAULT_VAULT, allow_excluded: bool = False,
) -> dict:
    """Entry point: decide whether to index a document, then spawn child tasks.

    Args:
        file_path: vault-relative path (e.g. "subfolder/doc.md")
        vault_id: which vault this document belongs to (partition key)
        force: skip dedup checks (frontmatter:processed key and content hash)
        skip_frontmatter_gen: skip the LLM tag/summary step and go straight to
            embedding, regardless of the per-doc rag_frontmatter flag. Used by
            the bulk reindex path, which rebuilds the RAG DB only and leaves
            existing frontmatter untouched (LLM metadata is driven separately by
            the "Generate metadata" actions).
    """
    doc_id = file_path
    logger.info("ingest_document: %s", doc_id)

    # System vaults (agent definitions, help docs) ARE indexed - search is hard
    # vault-scoped, so their rows only surface when searching the system vault
    # itself. What they never get is LLM frontmatter generation: blessed files
    # are human-only, so ingest forces the straight-to-embed path below.
    system_vault = vault_registry.is_system_vault(vault_id)

    # Control/config dotfiles (.gitattributes, .gitignore, .editorconfig, ...) are
    # not wiki prose and must never be RAG-indexed or version-committed by the app.
    # The bulk reindex path already scopes itself to "**/*.md", but the file-watcher
    # hands ingest EVERY created file, so this is the chokepoint that keeps a
    # git-init'd vault's root .gitattributes out of search. Keyed on the basename so
    # a dot-prefixed file at any depth is caught; because the watcher only commits
    # when ingest returns "ok", skipping here also stops the spurious auto-commit.
    if os.path.basename(file_path).startswith("."):
        logger.info("ingest_document: skipping %s (control/config dotfile)", doc_id)
        return {"status": "skipped", "reason": "dotfile"}

    # Canvas files are JSON, not prose - embedding the raw blob pollutes the index
    # and (via the watcher) would fire an LLM frontmatter call + a git commit on
    # every auto-save. Skip entirely; because the watcher only commits when ingest
    # returns "ok", this also makes the canvas editor's explicit save_version
    # checkpoints the sole commit source for .canvas files.
    if file_path.endswith(".canvas"):
        logger.info("ingest_document: skipping %s (canvas not indexed)", doc_id)
        return {"status": "skipped", "reason": "canvas file"}

    # Check frontmatter:processed dedup key (vault-scoped so same-named docs in
    # different vaults don't suppress each other).
    r = get_async_redis()
    try:
        key = f"frontmatter:processed:{vault_id}:{doc_id}"
        if not force and await r.exists(key):
            logger.info("ingest_document: skipping %s (frontmatter recently processed)", doc_id)
            return {"status": "skipped", "reason": "frontmatter recently processed"}
    finally:
        await r.close()

    # Folder exclusion. allow_excluded bypasses ONLY this check - used by the
    # opt-in indexing of agent output (write_agent_output, index_output granted
    # in the blessed agent file). System-vault refusal above stays absolute.
    if _is_excluded(file_path) and not allow_excluded:
        logger.info("ingest_document: skipping %s (excluded folder)", doc_id)
        return {"status": "skipped", "reason": "excluded folder"}

    # Read file
    abs_file = _abs_path(file_path, vault_id)
    try:
        content = await asyncio.to_thread(_read_file, abs_file)
    except FileNotFoundError:
        logger.warning("ingest_document: file not found %s", abs_file)
        return {"status": "skipped", "reason": "file not found"}
    except Exception as e:
        logger.error("ingest_document: failed to read %s: %s", abs_file, e)
        return {"status": "failed", "error": str(e)}

    # Parse frontmatter, decide RAG-indexing vs link-only.
    frontmatter = WikiDoc.parse_frontmatter(content)
    index_flag = frontmatter.get("index", str(INDEX_DOCUMENT_FRONTMATTER_DEFAULT))
    rag_enabled = index_flag.lower() not in ("false", "no", "0")

    # Content hash check (applies to both paths).
    body = WikiDoc.strip_frontmatter(content)
    new_hash = _compute_content_hash(body)
    stored_hash = await asyncio.to_thread(_get_stored_hash, doc_id, vault_id)

    if not force and stored_hash == new_hash:
        logger.info("ingest_document: skipping %s (content unchanged)", doc_id)
        return {"status": "skipped", "reason": "content unchanged"}

    title = _extract_title(file_path, frontmatter)

    # Link-only path: pages excluded from RAG (Index:False) still contribute their
    # link structure to the knowledge graph (a documents row + edges, flagged
    # rag_indexed=FALSE). No LLM frontmatter, no embedding.
    if not rag_enabled:
        edge_count = await asyncio.to_thread(
            index_links, doc_id, title, abs_file, body, new_hash, vault_id
        )
        logger.info(
            "ingest_document: link-only index for %s (%d edges)", doc_id, edge_count
        )
        return {"status": "links_only", "doc_id": doc_id, "edges": edge_count}

    # RAG path: ensure document row exists, then run the embedding pipeline.
    await asyncio.to_thread(_upsert_document_row, doc_id, title, abs_file, vault_id)

    # Check rag_frontmatter flag (default: generate frontmatter). The caller can
    # also force-skip generation (bulk reindex = rebuild RAG DB only), and system
    # vaults ALWAYS skip it (the LLM must never mutate blessed files).
    rag_frontmatter = frontmatter.get("rag_frontmatter", "true")
    skip_frontmatter = (skip_frontmatter_gen or system_vault
                        or rag_frontmatter.lower() in ("false", "no", "0"))

    if skip_frontmatter:
        # Skip LLM generation, go straight to embedding
        reason = ("system vault (frontmatter is human-only)" if system_vault
                  else "reindex (frontmatter decoupled)" if skip_frontmatter_gen
                  else "rag_frontmatter=false")
        logger.info("ingest_document: %s, skipping to embed for %s", reason, doc_id)
        from src.task_definitions import embed_document_task
        task_id = f"embed:{vault_id}:{doc_id}"
        await _register_and_enqueue(embed_document_task, task_id, file_path=file_path, vault_id=vault_id)
    else:
        from src.task_definitions import generate_frontmatter_task
        task_id = f"frontmatter:{vault_id}:{doc_id}"
        await _register_and_enqueue(generate_frontmatter_task, task_id, file_path=file_path, vault_id=vault_id)

    return {"status": "ok", "doc_id": doc_id, "content_hash": new_hash}


async def generate_frontmatter(file_path: str, vault_id: str = DEFAULT_VAULT) -> dict:
    """Generate LLM tags/summary via existing _generate_metadata_impl, then spawn embed."""
    doc_id = file_path
    logger.info("generate_frontmatter: %s", doc_id)

    # Blessed files are human-only: the LLM must never write tags/summary into a
    # system vault. Ingest already routes system vaults straight to embed; this
    # guard covers the manual "Generate metadata" bulk actions.
    if vault_registry.is_system_vault(vault_id):
        logger.info("generate_frontmatter: skipping %s (system vault %s)", doc_id, vault_id)
        return {"status": "skipped", "reason": "system vault (frontmatter is human-only)"}

    abs_file = _abs_path(file_path, vault_id)

    # Check rag_frontmatter flag
    try:
        content = await asyncio.to_thread(_read_file, abs_file)
    except Exception as e:
        logger.error("generate_frontmatter: failed to read %s: %s", abs_file, e)
        return {"status": "failed", "error": str(e)}

    frontmatter = WikiDoc.parse_frontmatter(content)
    rag_frontmatter = frontmatter.get("rag_frontmatter", "true")
    if rag_frontmatter.lower() in ("false", "no", "0"):
        logger.info("generate_frontmatter: rag_frontmatter=false, skipping LLM for %s", doc_id)
    else:
        # Delegate to existing implementation
        from src.task_definitions import _generate_metadata_impl

        result = await _generate_metadata_impl(
            abs_file,
            file_path,  # normalized_url_path = rel_path for debounce key match
            OLLAMA_URL,
            OLLAMA_MODEL,
            OLLAMA_KEEP_ALIVE,
            vault_id,
        )
        logger.info("generate_frontmatter: metadata result for %s: %s", doc_id, result.get("status"))

    # Set frontmatter:processed dedup key (vault-scoped)
    r = get_async_redis()
    try:
        await r.set(f"frontmatter:processed:{vault_id}:{doc_id}", "1", ex=FRONTMATTER_PROCESSED_TTL)
    finally:
        await r.close()

    # Spawn embedding task (verify file still exists after potentially slow LLM generation)
    abs_check = _abs_path(file_path, vault_id)
    if not os.path.isfile(abs_check):
        logger.warning("generate_frontmatter: file vanished before embed spawn: %s", file_path)
        return {"status": "skipped", "reason": "file vanished before embed"}

    from src.task_definitions import embed_document_task
    task_id = f"embed:{vault_id}:{doc_id}"
    await _register_and_enqueue(embed_document_task, task_id, file_path=file_path, vault_id=vault_id)

    return {"status": "ok", "doc_id": doc_id}


async def embed_document(file_path: str, vault_id: str = DEFAULT_VAULT) -> dict:
    """Chunk, embed, and write everything to the database in one transaction."""
    doc_id = file_path
    logger.info("embed_document: %s", doc_id)

    abs_file = _abs_path(file_path, vault_id)

    # Re-read file (now with fresh frontmatter from generate_frontmatter)
    try:
        content = await asyncio.to_thread(_read_file, abs_file)
    except Exception as e:
        logger.error("embed_document: failed to read %s: %s", abs_file, e)
        return {"status": "failed", "error": str(e)}

    frontmatter = WikiDoc.parse_frontmatter(content)
    title = _extract_title(file_path, frontmatter)
    body = WikiDoc.strip_frontmatter(content)
    content_hash = _compute_content_hash(body)

    # Run chunker
    from src.chunker import chunk as run_chunker

    chunk_result = await asyncio.to_thread(run_chunker, content, title)
    chunks = chunk_result["chunks"]
    fm_tags = chunk_result.get("frontmatter_tags", [])

    # Drop chunks with no real body text. An empty/whitespace-only chunk (e.g. the
    # gap before a document's first heading) produces a degenerate embedding that
    # matches arbitrary queries as noise -- so a near-empty page like a short
    # bullet list can surface for unrelated searches. Filtering them here keeps such
    # pages from polluting retrieval (and makes same-named pages across vaults stop
    # looking like cross-vault contamination).
    chunks = [c for c in chunks if has_indexable_text(c.get("content"))]

    if not chunks:
        logger.info("embed_document: no non-empty chunks produced for %s", doc_id)
        return {"status": "skipped", "reason": "no chunks"}

    # Collect texts to embed (context_content for semantic search)
    chunk_texts = [c["context_content"] for c in chunks]

    # Get summary for doc-level embedding. Strip first: an LLM-generated
    # frontmatter summary can come back as whitespace only ("\n", "\n\n"),
    # which is truthy and would otherwise survive every gate below and land
    # a useless whitespace row (with a degenerate embedding) in doc_embeddings.
    summary = (frontmatter.get("summary") or "").strip()
    if not summary:
        # Use first chunk as fallback summary (chunks are already
        # whitespace-filtered above, so this is guaranteed non-empty).
        summary = chunks[0]["content"][:500].strip() if chunks else ""

    all_texts = chunk_texts + ([summary] if summary else [])

    # Batch embed via Ollama. Gate the (threaded) embed call through the shared
    # background-LLM semaphore so a bulk reindex doesn't fan out into many
    # concurrent embedding requests against the shared Ollama server.
    from src.llm_gate import get_llm_gate

    try:
        async with get_llm_gate("embed"):
            all_embeddings = await asyncio.to_thread(_batch_embed, all_texts)
    except Exception as e:
        logger.error("embed_document: embedding failed for %s: %s", doc_id, e)
        return {"status": "failed", "error": f"embedding failed: {e}"}

    chunk_embeddings = all_embeddings[: len(chunk_texts)]
    summary_embedding = all_embeddings[len(chunk_texts)] if summary else None

    # Collect edges, tags, asset_refs from chunks
    all_wikilinks = []  # (target_title, edge_type)
    all_doc_embeds = []
    all_asset_refs = []
    all_inline_tags = set()

    for c in chunks:
        for link in c.get("wikilinks", []):
            all_wikilinks.append((link, "wikilink"))
        for embed_target in c.get("doc_embeds", []):
            all_doc_embeds.append(embed_target)
        for asset in c.get("asset_refs", []):
            all_asset_refs.append(asset)
        for tag in c.get("tags", []):
            all_inline_tags.add(tag)

    # Deduplicate
    seen_links = set()
    deduped_wikilinks = []
    for link, etype in all_wikilinks:
        key = (link, etype)
        if key not in seen_links:
            seen_links.add(key)
            deduped_wikilinks.append((link, etype))
    all_wikilinks = deduped_wikilinks

    all_doc_embeds = list(dict.fromkeys(all_doc_embeds))
    all_asset_refs = list(dict.fromkeys(all_asset_refs))

    # Build tags list: !-prefixed = 'pinned', other frontmatter = 'auto', inline = 'inline'
    all_tags = []
    for tag in fm_tags:
        if tag.startswith("!"):
            all_tags.append((tag[1:], "pinned"))
        else:
            all_tags.append((tag, "auto"))
    seen_tags = {t[0] for t in all_tags}
    for tag in all_inline_tags:
        clean_tag = tag.lstrip("!")
        if clean_tag not in seen_tags:
            all_tags.append((clean_tag, "inline"))

    # Write everything to database
    try:
        await asyncio.to_thread(
            _write_to_db,
            doc_id,
            title,
            abs_file,
            content_hash,
            summary,
            summary_embedding,
            chunks,
            chunk_embeddings,
            all_wikilinks,
            all_tags,
            all_asset_refs,
            all_doc_embeds,
            vault_id,
        )
    except Exception as e:
        logger.error("embed_document: DB write failed for %s: %s", doc_id, e)
        return {"status": "failed", "error": f"DB write failed: {e}"}

    # Resolve ghost nodes: update any unresolved edges that point to this document
    resolved_count = await asyncio.to_thread(_resolve_ghost_edges, doc_id, title, vault_id)

    logger.info(
        "embed_document: indexed %s (%d chunks, %d edges, %d tags, %d ghost edges resolved)",
        doc_id, len(chunks), len(all_wikilinks) + len(all_doc_embeds), len(all_tags),
        resolved_count,
    )
    return {
        "status": "ok",
        "doc_id": doc_id,
        "chunks": len(chunks),
        "content_hash": content_hash,
        "ghost_edges_resolved": resolved_count,
    }


async def remove_document(file_path: str, vault_id: str = DEFAULT_VAULT) -> dict:
    """Remove a deleted document from the RAG index (within its vault).

    Deletes all child data (chunks, embeddings, tags, assets, outgoing edges).

    Then the row is handled one of two ways depending on whether any *other*
    document still links to it (an inbound edge with target_doc_id = this doc):
      - inbound links exist -> keep the row as a *connected* ghost node
        (doc_exists=FALSE) and leave those inbound edges resolved, so the graph
        still draws the missing-page node as a "fill me in" target.
      - no inbound links     -> hard-delete the row outright. A tombstone nothing
        references is just garbage; leaving it produced isolated ghosts that
        accumulated until the next full reindex.

    This keeps remove_document consistent with _sweep_unreferenced_ghosts, which
    also keeps exactly the ghosts still referenced by target_doc_id. Content-fetch
    paths all filter doc_exists=TRUE, so a resolved edge pointing at a kept ghost
    only surfaces the node on the graph, never as empty search content.
    """
    doc_id = file_path
    logger.info("remove_document: %s", doc_id)

    def _do_remove():
        conn = _get_pg_connection()
        try:
            cur = conn.cursor()
            # Decide ghost-vs-hard-delete from the *pre-delete* link graph: does any
            # other document point an edge at this doc? (Exclude self-links.)
            cur.execute(
                """
                SELECT EXISTS(
                    SELECT 1 FROM edges
                    WHERE vault_id = %s AND target_doc_id = %s AND source_doc_id <> %s
                )
                """,
                (vault_id, doc_id, doc_id),
            )
            row = cur.fetchone()
            has_inbound = bool(row and row[0])

            # Delete child data
            cur.execute("DELETE FROM chunks WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))
            cur.execute("DELETE FROM doc_embeddings WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))
            cur.execute("DELETE FROM asset_refs WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))
            cur.execute("DELETE FROM document_tags WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))
            # Delete outgoing edges
            cur.execute("DELETE FROM edges WHERE vault_id = %s AND source_doc_id = %s", (vault_id, doc_id))

            if has_inbound:
                # Keep as a connected ghost. Inbound edges stay resolved (still
                # pointing at this doc_id) so the missing-page node keeps drawing;
                # if the file is later recreated at this path, embed_document flips
                # doc_exists back TRUE and those edges are valid again untouched.
                cur.execute(
                    """
                    UPDATE documents
                    SET doc_exists = FALSE, content_hash = NULL, summary = NULL,
                        indexed_at = NULL, updated_at = NOW()
                    WHERE vault_id = %s AND doc_id = %s
                    """,
                    (vault_id, doc_id),
                )
                action = "ghosted"
            else:
                # Nothing references it -> no tombstone. Hard-delete the row so
                # isolated ghosts never accumulate.
                cur.execute("DELETE FROM documents WHERE vault_id = %s AND doc_id = %s", (vault_id, doc_id))
                action = "removed"

            conn.commit()
            return action
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    action = await asyncio.to_thread(_do_remove)
    logger.info("remove_document: %s %s", action, doc_id)
    return {"status": "ok", "doc_id": doc_id, "action": action}


async def prune_deleted_documents(existing_doc_ids: set[str], vault_id: str = DEFAULT_VAULT) -> dict:
    """Reconcile the index against the filesystem: remove documents whose files
    are gone from disk.

    Files deleted outside the app (while the watcher wasn't running) never trigger
    remove_document, so their rows orphan and keep showing on the graph / in search.
    This sweeps them out.

    For each orphan (a doc_id NOT in `existing_doc_ids`) we run remove_document --
    clearing its chunks/embeddings/tags/assets + outgoing edges, nulling incoming
    edges, and ghosting the row. Then we hard-delete every ghost row that no
    resolved edge still targets, so orphans don't accumulate as ghosts. A ghost a
    surviving document still links to is kept (its missing-page node stays valid).

    `existing_doc_ids` must be the set of doc_ids currently on disk (relative
    paths, e.g. "folder/Page.md") -- the same key used for documents.doc_id.

    Returns {"ghosted": <n orphans>, "hard_deleted": <n rows removed>}.
    """
    # Defense-in-depth: an empty set would mark every document an orphan and wipe
    # the index. The only legitimate empty case (truly empty vault) has nothing to
    # prune anyway, so refusing here is always safe. Callers should also guard.
    if not existing_doc_ids:
        logger.warning("prune_deleted_documents: empty existing set; skipping prune")
        return {"ghosted": 0, "hard_deleted": 0}

    def _find_orphans() -> list[str]:
        conn = _get_pg_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT doc_id FROM documents WHERE vault_id = %s AND doc_exists = TRUE",
                (vault_id,),
            )
            return [r[0] for r in cur.fetchall() if r[0] not in existing_doc_ids]
        finally:
            conn.close()

    orphans = await asyncio.to_thread(_find_orphans)
    for doc_id in orphans:
        logger.info("prune_deleted_documents: orphan on disk-miss: %s", doc_id)
        await remove_document(doc_id, vault_id)

    def _sweep_unreferenced_ghosts() -> int:
        conn = _get_pg_connection()
        try:
            cur = conn.cursor()
            # Hard-delete ghost rows (in this vault) that no resolved edge points at.
            # Cascade (via the source_doc_id FK) cleans any residual children/outgoing
            # edges. Incoming edges were already nulled by remove_document.
            cur.execute(
                """
                DELETE FROM documents
                WHERE vault_id = %s AND doc_exists = FALSE
                  AND doc_id NOT IN (
                      SELECT target_doc_id FROM edges
                      WHERE vault_id = %s AND target_doc_id IS NOT NULL
                  )
                """,
                (vault_id, vault_id),
            )
            deleted = cur.rowcount
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    hard_deleted = await asyncio.to_thread(_sweep_unreferenced_ghosts)
    logger.info(
        "prune_deleted_documents: ghosted %d orphan(s), hard-deleted %d unreferenced ghost(s)",
        len(orphans), hard_deleted,
    )
    return {"ghosted": len(orphans), "hard_deleted": hard_deleted}


async def reconcile_rename(
    old_id: str, new_id: str, new_title: str, new_file_path: str,
    vault_id: str = DEFAULT_VAULT,
) -> dict:
    """Re-key a document in place when it is moved/renamed (content unchanged).

    A move/rename never alters content, so the doc's chunks, embeddings, tags and
    outgoing edges are all still valid -- only the primary key changes. Thanks to
    ON UPDATE CASCADE on the child FKs, a single UPDATE of documents.doc_id carries
    chunks/doc_embeddings/document_tags/asset_refs/edges.source_doc_id along with
    it, so we re-point without re-embedding. edges.target_doc_id has no FK (it may
    reference ghost nodes), so inbound links are re-pointed by hand.

    If new_id already exists as a *ghost* node (an unresolved wikilink target other
    docs link to -- e.g. you finally renamed a page everyone was linking to), the
    ghost row is dropped first; its inbound edges keep target_doc_id=new_id and so
    end up pointing at the now-real document.

    Returns {"status": "ok"|"missing_old"|"collision", ...}. "missing_old" lets the
    caller fall back to a fresh ingest; "collision" means a real document already
    occupies new_id and the rename was refused.
    """
    logger.info("reconcile_rename: %s -> %s", old_id, new_id)

    def _do_reconcile() -> dict:
        conn = _get_pg_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT doc_exists FROM documents WHERE vault_id = %s AND doc_id = %s",
                (vault_id, old_id),
            )
            if cur.fetchone() is None:
                return {"status": "missing_old"}

            if new_id != old_id:
                cur.execute(
                    "SELECT doc_exists FROM documents WHERE vault_id = %s AND doc_id = %s",
                    (vault_id, new_id),
                )
                row = cur.fetchone()
                if row is not None:
                    if row[0]:  # doc_exists=TRUE -> real document, refuse
                        return {"status": "collision"}
                    # Ghost node occupying the target name: drop it so the rename can
                    # take the key. Inbound edges (target_doc_id=new_id, no FK) survive
                    # and now resolve to the real document we're renaming in.
                    cur.execute(
                        "DELETE FROM documents WHERE vault_id = %s AND doc_id = %s",
                        (vault_id, new_id),
                    )

            # Re-key the document; children cascade via ON UPDATE CASCADE (vault_id is
            # stable, only doc_id changes).
            cur.execute(
                """
                UPDATE documents
                SET doc_id = %s, title = %s, file_path = %s,
                    doc_exists = TRUE, updated_at = NOW()
                WHERE vault_id = %s AND doc_id = %s
                """,
                (new_id, new_title, new_file_path, vault_id, old_id),
            )
            # Re-point inbound edges (no FK to cascade), within this vault.
            cur.execute(
                "UPDATE edges SET target_doc_id = %s WHERE vault_id = %s AND target_doc_id = %s",
                (new_id, vault_id, old_id),
            )
            conn.commit()
            return {"status": "ok"}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    result = await asyncio.to_thread(_do_reconcile)
    if result["status"] == "ok":
        # Resolve any still-unresolved edges whose text matches the new name.
        await asyncio.to_thread(_resolve_ghost_edges, new_id, new_title, vault_id)
        logger.info("reconcile_rename: re-keyed %s -> %s", old_id, new_id)
    return result


async def move_document(src_path: str, dest_path: str, vault_id: str = DEFAULT_VAULT) -> dict:
    """Handle a moved/renamed document by re-keying it in place (no re-embed).

    Used by the filesystem-watcher move path. Does NOT rewrite inbound wikilink
    text -- when an external tool (e.g. Obsidian) moves a file it rewrites those
    links itself, and they arrive as their own modified events. The in-wiki move
    path (content_ops.move_document_op) is what rewrites link text.
    """
    logger.info("move_document: %s -> %s", src_path, dest_path)

    # Only markdown lives in the documents table. A moved asset (image/pdf) or
    # canvas has no doc row, so re-keying is a no-op and a fresh ingest would try
    # to read a binary as prose and fail -- skip it. The asset's referrers get
    # their asset_refs rebuilt when they reindex after the embed rewrite.
    if not dest_path.endswith(".md"):
        logger.info("move_document: skipping non-markdown %s", dest_path)
        return {"status": "skipped", "action": "non_markdown",
                "src": src_path, "dest": dest_path}

    # Title follows frontmatter if present, else the new filename stem.
    abs_dest = _abs_path(dest_path, vault_id)
    try:
        content = await asyncio.to_thread(_read_file, abs_dest)
        new_title = _extract_title(dest_path, WikiDoc.parse_frontmatter(content))
    except FileNotFoundError:
        new_title = Path(dest_path).stem

    result = await reconcile_rename(src_path, dest_path, new_title, abs_dest, vault_id)

    if result["status"] == "missing_old":
        # Source was never indexed (e.g. a brand-new file moved before ingest);
        # nothing to re-key, so just ingest the destination fresh.
        logger.info("move_document: %s not indexed; ingesting dest", src_path)
        ingest = await ingest_document(dest_path, vault_id=vault_id)
        return {"status": "ok", "action": "ingested", "src": src_path,
                "dest": dest_path, "ingest": ingest}

    return {"status": result["status"], "action": "moved",
            "src": src_path, "dest": dest_path}
