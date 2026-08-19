# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Internal vault-query API for the `wiki` object injected into Jupyter kernels.

A kernel runs in the separate `jupyterserver` container and has neither the app
source nor the Postgres/Ollama credentials, so it cannot import `rag_search`
directly. Instead it POSTs `{"op": ..., "args": {...}}` to
`/api/kernel/{vault}/query` (see `main.py`), which calls into this module.

Everything here is **synchronous** (psycopg2 + the Ollama embed path inside
`rag_search`); the route wraps each call in `run_in_threadpool` so it never
blocks the event loop. The vault is always taken from the URL by the route and
passed in here as `vault_id` -- it is never trusted from the request body, which
preserves hard vault isolation.

Return values are plain JSON-serializable structures (lists/dicts of scalars) so
results drop straight into `pandas.DataFrame(...)` inside a notebook cell.
"""

import logging

import psycopg2
import psycopg2.extras

from src import rag_search

logger = logging.getLogger("kernel_api")


class KernelApiError(Exception):
    """Raised for bad ops / missing args; the route maps this to HTTP 400."""


def _normalize_doc_id(path: "str | None") -> str:
    """Coerce a user-supplied page reference into a stored `doc_id`.

    doc_ids are vault-relative paths WITH the `.md` suffix (e.g.
    `folder/Note.md`). Kernel users naturally pass things like `/folder/Note`,
    `folder/Note`, or `Note.md`; normalize all of them to the stored form.
    """
    if not path or not isinstance(path, str):
        raise KernelApiError("a document path is required")
    doc_id = path.strip().strip("/")
    if not doc_id:
        raise KernelApiError("a document path is required")
    if not doc_id.lower().endswith(".md"):
        doc_id += ".md"
    return doc_id


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------

def _op_search(args: dict, vault_id: str) -> list[dict]:
    """Hybrid + graph-expanded chunk search plus document-level matches.

    Returns a single flat list of rows with a uniform schema so it drops
    straight into a DataFrame. The `result_type` column distinguishes the
    two granularities:

    - ``"chunk"``: a sub-document passage from hybrid (vector+FTS/RRF) search,
      possibly pulled in via graph expansion (see `source`). `header_path` is
      the chunk's heading trail; `snippet` is the chunk text.
    - ``"document"``: a whole-document match against summary embeddings.
      `header_path` is empty; `snippet` is the document summary.

    Scores are not comparable *across* types (chunks use RRF/FTS fusion;
    documents use cosine similarity = ``1 - distance``), so sort/filter
    within a single `result_type` rather than across the whole frame.
    """
    query = (args.get("query") or "").strip()
    if not query:
        raise KernelApiError("search requires a 'query'")
    top_k = _as_int(args.get("top_k"), 10)
    result = rag_search.search(query, top_k=top_k, vault_id=vault_id)
    rows = []
    for r in result.get("chunk_results", []):
        # Graph-expanded chunks carry a `graph_score` instead of RRF/FTS scores.
        score = r.get("rrf_score") or r.get("fts_score") or r.get("graph_score") or 0
        rows.append({
            "result_type": "chunk",
            "source": r.get("source", "search"),
            "doc_id": r.get("doc_id"),
            "title": r.get("doc_title"),
            "header_path": r.get("header_path"),
            "snippet": (r.get("content") or "")[:500],
            "score": round(float(score), 5),
        })
    for r in result.get("document_results", []):
        # document_search returns a cosine distance (lower = closer); convert to
        # a similarity so higher = better, consistent with the chunk `score`.
        similarity = 1.0 - float(r.get("distance") or 1.0)
        rows.append({
            "result_type": "document",
            "source": "document",
            "doc_id": r.get("doc_id"),
            "title": r.get("title"),
            "header_path": None,
            "snippet": (r.get("summary") or "")[:500],
            "score": round(similarity, 5),
        })
    return rows


def _op_related(args: dict, vault_id: str) -> list[dict]:
    """Documents related to a page via links, tags, and embeddings."""
    doc_id = _normalize_doc_id(args.get("path"))
    top_k = _as_int(args.get("top_k"), 10)
    related = rag_search.find_related(doc_id, top_k=top_k, vault_id=vault_id)
    # find_related already returns JSON-friendly dicts.
    return related


def _op_tagged(args: dict, vault_id: str) -> list[dict]:
    """All documents carrying a given tag (vault-scoped)."""
    tag = (args.get("tag") or "").strip()
    if not tag:
        raise KernelApiError("tagged requires a 'tag'")
    conn = rag_search._get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT d.doc_id, d.title, d.summary
               FROM documents d
               JOIN document_tags dt
                 ON dt.vault_id = d.vault_id AND dt.doc_id = d.doc_id
               WHERE dt.vault_id = %(vault)s AND dt.tag = %(tag)s
                 AND d.doc_exists = TRUE
               ORDER BY d.title
               LIMIT 500""",
            {"vault": vault_id, "tag": tag},
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _op_backlinks(args: dict, vault_id: str) -> list[dict]:
    """Documents that link TO the given page (resolved edges only)."""
    doc_id = _normalize_doc_id(args.get("path"))
    conn = rag_search._get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT DISTINCT d.doc_id, d.title
               FROM edges e
               JOIN documents d
                 ON d.vault_id = e.vault_id AND d.doc_id = e.source_doc_id
               WHERE e.vault_id = %(vault)s AND e.target_doc_id = %(doc_id)s
                 AND e.resolved = TRUE AND d.doc_exists = TRUE
               ORDER BY d.title""",
            {"vault": vault_id, "doc_id": doc_id},
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def _op_frontmatter(args: dict, vault_id: str) -> dict:
    """One document's metadata: title, summary, tags, and link counts."""
    doc_id = _normalize_doc_id(args.get("path"))
    conn = rag_search._get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """SELECT doc_id, title, summary FROM documents
               WHERE vault_id = %(vault)s AND doc_id = %(doc_id)s
                 AND doc_exists = TRUE""",
            {"vault": vault_id, "doc_id": doc_id},
        )
        doc = cur.fetchone()
        if doc is None:
            raise KernelApiError(f"document not found in vault '{vault_id}': {doc_id}")
        cur.execute(
            """SELECT tag FROM document_tags
               WHERE vault_id = %(vault)s AND doc_id = %(doc_id)s ORDER BY tag""",
            {"vault": vault_id, "doc_id": doc_id},
        )
        tags = [row["tag"] for row in cur.fetchall()]
        cur.execute(
            """SELECT
                 COUNT(*) FILTER (
                   WHERE source_doc_id = %(doc_id)s AND resolved = TRUE
                 ) AS outbound_links,
                 COUNT(*) FILTER (WHERE target_doc_id = %(doc_id)s) AS backlinks
               FROM edges WHERE vault_id = %(vault)s""",
            {"vault": vault_id, "doc_id": doc_id},
        )
        counts = cur.fetchone() or {}
        return {
            "doc_id": doc["doc_id"],
            "title": doc["title"],
            "summary": doc["summary"],
            "tags": tags,
            "outbound_links": counts.get("outbound_links", 0),
            "backlinks": counts.get("backlinks", 0),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Whole-table reads (structured metadata -> pandas). These exist so a human-
# authored agent tool (or an interactive notebook) can COMPOSE analytics in
# Python instead of relying on the LLM to chain finer tools in the right order.
#
# Fixed column projections -- never `SELECT *`: keeps payloads bounded and
# avoids shipping columns like `documents.file_path` (a container-internal path)
# or a future body/tsvector column. The big vector/text tables (`chunks`,
# `doc_embeddings`) are deliberately NOT exposed; vector work goes through
# `search`/`related`, which do the similarity math server-side and return small
# ranked rows.
# ---------------------------------------------------------------------------

# Per-request PAGE SIZE (not a hard cap): the kernel-side client keyset-paginates
# on the server cursor to assemble the complete table, so vault size never caps
# the result - these only bound one round-trip's payload.
_DOCUMENTS_LIMIT = 5000
_EDGES_LIMIT = 20000
_TAGS_LIMIT = 20000


def _query_table(vault_id: str, table: str, select_sql: str, order_cols: list,
                 page_size: int, after=None, iso_cols: tuple = ()) -> dict:
    """One KEYSET-paginated page of a whole-table enumeration.

    These answer "give me the WHOLE table" (unlike ranked search), so a silent
    row cap is a blind spot. Rather than cap-and-hope, we page: order by a UNIQUE
    key (`order_cols`, which must be selected) and, when `after` is supplied,
    resume strictly past it via a row-value comparison `(cols) > (after)`. Keyset
    (not OFFSET) so each page is O(page_size) regardless of depth and stable under
    concurrent writes - the two things that bite a large vault.

    Returns {"rows", "has_more", "next_cursor", "page_size"}. `next_cursor` is an
    OPAQUE token (the last row's order-key values); the kernel-side client loops
    on it to assemble the complete table, so no cap leaks into user code."""
    where = ["vault_id = %s"]
    binds: list = [vault_id]
    if after:
        cols = ", ".join(order_cols)
        placeholders = ", ".join(["%s"] * len(order_cols))
        where.append(f"({cols}) > ({placeholders})")
        binds.extend(after if isinstance(after, (list, tuple)) else [after])
    order = ", ".join(order_cols)
    conn = rag_search._get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            f"{select_sql} FROM {table} WHERE {' AND '.join(where)} "
            f"ORDER BY {order} LIMIT %s", binds + [page_size])
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            for key in iso_cols:
                if d.get(key) is not None:
                    d[key] = d[key].isoformat()
            rows.append(d)
    finally:
        conn.close()
    has_more = len(rows) == page_size
    next_cursor = [rows[-1][c] for c in order_cols] if (rows and has_more) else None
    return {"rows": rows, "has_more": has_more, "next_cursor": next_cursor,
            "page_size": page_size}


def _op_query_documents(args: dict, vault_id: str) -> dict:
    """One page of every page in the vault (no body/vector columns)."""
    return _query_table(
        vault_id, "documents",
        "SELECT doc_id, title, doc_exists, rag_indexed, summary, created_at, updated_at",
        ["doc_id"], _DOCUMENTS_LIMIT, after=args.get("after"),
        iso_cols=("created_at", "updated_at"))


def _op_query_edges(args: dict, vault_id: str) -> dict:
    """One page of the raw wikilink edge list (`id` is the paging key)."""
    return _query_table(
        vault_id, "edges",
        "SELECT id, source_doc_id, target_title, target_doc_id, edge_type, resolved",
        ["id"], _EDGES_LIMIT, after=args.get("after"))


def _op_query_document_tags(args: dict, vault_id: str) -> dict:
    """One page of the raw (doc_id, tag, source) tag assignments."""
    return _query_table(
        vault_id, "document_tags",
        "SELECT doc_id, tag, source",
        ["doc_id", "tag"], _TAGS_LIMIT, after=args.get("after"))


# ---------------------------------------------------------------------------
# Analysis mirror: the SAME `src.vault_analysis` functions that back the agents'
# LLM-callable capabilities, exposed here as raw structured rows. One source,
# two surfaces -- the capability wraps them into text for the model; these ops
# return dicts a human tool can filter/join/aggregate in pandas.
# ---------------------------------------------------------------------------

def _op_analyze_orphans(args: dict, vault_id: str) -> list[dict]:
    from src import vault_analysis
    return vault_analysis.list_orphans(
        vault_id, limit=_as_int(args.get("limit"), 50),
        path_prefix=str(args.get("path_prefix") or ""))


def _op_analyze_near_duplicates(args: dict, vault_id: str) -> list[dict]:
    from src import vault_analysis
    return vault_analysis.find_near_duplicates(
        vault_id, threshold=_as_float(args.get("threshold"), 0.88),
        limit=_as_int(args.get("limit"), 30),
        path_prefix=str(args.get("path_prefix") or ""))


def _op_analyze_missing_links(args: dict, vault_id: str) -> list[dict]:
    from src import vault_analysis
    return vault_analysis.find_missing_links(
        vault_id, low=_as_float(args.get("low"), 0.62),
        high=_as_float(args.get("high"), 0.88),
        limit=_as_int(args.get("limit"), 40),
        path_prefix=str(args.get("path_prefix") or ""))


def _op_analyze_stale_stubs(args: dict, vault_id: str) -> list[dict]:
    from src import vault_analysis
    return vault_analysis.list_stale_stubs(
        vault_id, max_chars=_as_int(args.get("max_chars"), 400),
        stale_days=_as_int(args.get("stale_days"), 180),
        limit=_as_int(args.get("limit"), 40),
        path_prefix=str(args.get("path_prefix") or ""))


_OPS = {
    "search": _op_search,
    "related": _op_related,
    "tagged": _op_tagged,
    "backlinks": _op_backlinks,
    "frontmatter": _op_frontmatter,
    # whole-table reads (metadata only; wrap in pandas.DataFrame to compose)
    "queryDocuments": _op_query_documents,
    "queryEdges": _op_query_edges,
    "queryDocumentTags": _op_query_document_tags,
    # analysis mirror (same fns as the LLM capabilities; raw rows for composition)
    "list_orphans": _op_analyze_orphans,
    "find_near_duplicates": _op_analyze_near_duplicates,
    "find_missing_links": _op_analyze_missing_links,
    "list_stale_stubs": _op_analyze_stale_stubs,
}


def run_query(op: str, args: dict, vault_id: str):
    """Dispatch a kernel query op. Synchronous -- call via run_in_threadpool.

    `vault_id` is supplied by the route from the URL, never from the body.
    """
    handler = _OPS.get(op)
    if handler is None:
        raise KernelApiError(
            f"unknown op '{op}'; valid ops: {', '.join(sorted(_OPS))}"
        )
    if not isinstance(args, dict):
        raise KernelApiError("'args' must be an object")
    return handler(args, vault_id)
