# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
RAG search module -- standalone, testable search functions.

Public functions:
  - hybrid_search(query, ...)      -- chunk-level RRF search (vector + FTS)
  - vector_search(query, ...)      -- chunk-level pure vector search
  - fts_search(query, ...)         -- chunk-level pure full-text search
  - document_search(query, ...)    -- document-level search via summary embeddings
  - graph_expand(results, ...)     -- expand results with graph-linked neighbors
  - find_related(doc_id, ...)      -- find related documents via links, tags, embeddings
  - search(query, ...)             -- combined entry point (hybrid + graph + document)
"""

import logging

import psycopg2
import psycopg2.extras

from config import (
    DEFAULT_VAULT,
    GRAPH_EXPANSION_ENABLED,
    GRAPH_EXPANSION_MAX_NEIGHBOR_CHUNKS,
    GRAPH_EXPANSION_SEED_DOCS,
    truncate_for_embedding,
)
from src.rag_indexer import _build_name_index, _resolve_target_doc_id

logger = logging.getLogger("rag_search")


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get_pg_connection():
    from config import get_pg_connection
    return get_pg_connection()



def _embed_query(text: str) -> list[float]:
    """Embed a single query string via the configured LLM backend (follows
    LLM_PROVIDER: native /api/embed or /v1/embeddings)."""
    from src.llm_backend import embed_texts_sync
    return embed_texts_sync([truncate_for_embedding(text)])[0]


# ---------------------------------------------------------------------------
# Public search functions
# ---------------------------------------------------------------------------

def vector_search(
    query: str,
    top_k: int = 10,
    query_vec: list[float] | None = None,
    conn=None,
    exclude_doc_id: str | None = None,
    vault_id: str | None = None,
) -> list[dict]:
    """Pure vector (cosine similarity) search over chunk embeddings.

    When `exclude_doc_id` is set, chunks belonging to that document are
    omitted from the results - useful for "find related but not me" calls
    (e.g. retrieval-grounded continuation in the editor).

    When `vault_id` is set, results are restricted to that vault (hard isolation).
    """
    if query_vec is None:
        query_vec = _embed_query(query)
    vec_str = str(query_vec)

    own_conn = conn is None
    if own_conn:
        conn = _get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # `IS DISTINCT FROM NULL` is TRUE for any non-NULL value, so when
        # exclude_doc_id is None the predicate is a no-op. Likewise the
        # `vault_id IS NULL OR ...` clause is a no-op when vault_id is None.
        # NOTE: vault_id=None ("global") searches EVERY vault including the system
        # vault -- system vaults are ingested like any other (they are searchable
        # within themselves; see config.SYSTEM_VAULT), so their rows are right there
        # for a NULL-vault query to return. No app path passes None today; keep the
        # agent/tool boundary requiring an explicit vault so it stays that way.
        cur.execute(
            """
            SELECT c.chunk_id, c.doc_id, c.chunk_index, c.chunk_type,
                   c.header_path, c.content, c.context_content,
                   d.title AS doc_title,
                   c.embedding <=> %(vec)s::vector AS distance
            FROM chunks c
            JOIN documents d ON d.vault_id = c.vault_id AND d.doc_id = c.doc_id
            WHERE c.doc_id IS DISTINCT FROM %(exclude_doc_id)s
              AND (%(vault_id)s IS NULL OR c.vault_id = %(vault_id)s)
            ORDER BY c.embedding <=> %(vec)s::vector
            LIMIT %(top_k)s
            """,
            {"vec": vec_str, "exclude_doc_id": exclude_doc_id,
             "vault_id": vault_id, "top_k": top_k},
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        if own_conn:
            conn.close()


def fts_search(
    query: str,
    top_k: int = 10,
    conn=None,
    exclude_doc_id: str | None = None,
    vault_id: str | None = None,
) -> list[dict]:
    """Pure PostgreSQL full-text search over chunk content.

    See `vector_search` for `exclude_doc_id` / `vault_id` semantics.
    """
    own_conn = conn is None
    if own_conn:
        conn = _get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT c.chunk_id, c.doc_id, c.chunk_index, c.chunk_type,
                   c.header_path, c.content, c.context_content,
                   d.title AS doc_title,
                   ts_rank(c.search_vector, websearch_to_tsquery('english', %(query)s)) AS fts_score
            FROM chunks c
            JOIN documents d ON d.vault_id = c.vault_id AND d.doc_id = c.doc_id
            WHERE c.search_vector @@ websearch_to_tsquery('english', %(query)s)
              AND c.doc_id IS DISTINCT FROM %(exclude_doc_id)s
              AND (%(vault_id)s IS NULL OR c.vault_id = %(vault_id)s)
            ORDER BY fts_score DESC
            LIMIT %(top_k)s
            """,
            {"query": query, "exclude_doc_id": exclude_doc_id,
             "vault_id": vault_id, "top_k": top_k},
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        if own_conn:
            conn.close()


def hybrid_search(
    query: str,
    top_k: int = 10,
    candidate_pool: int = 20,
    vector_weight: float = 1.0,
    fts_weight: float = 1.0,
    query_vec: list[float] | None = None,
    conn=None,
    exclude_doc_id: str | None = None,
    vault_id: str | None = None,
) -> list[dict]:
    """Hybrid search using Reciprocal Rank Fusion (RRF) of vector + FTS results.

    Args:
        query: search query string
        top_k: number of final results to return
        candidate_pool: number of candidates to retrieve from each search mode
        vector_weight: weight multiplier for vector search in RRF scoring
        fts_weight: weight multiplier for FTS in RRF scoring
        query_vec: pre-computed query embedding (avoids redundant Ollama call)
        exclude_doc_id: when set, chunks from this document are filtered out
            of both the vector and FTS candidate pools.
    """
    if query_vec is None:
        query_vec = _embed_query(query)
    vec_str = str(query_vec)

    own_conn = conn is None
    if own_conn:
        conn = _get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            WITH vector_hits AS (
                SELECT c.chunk_id, c.doc_id, c.chunk_index, c.chunk_type,
                       c.header_path, c.content, c.context_content,
                       c.embedding <=> %(vec)s::vector AS vector_distance,
                       ROW_NUMBER() OVER (ORDER BY c.embedding <=> %(vec)s::vector) AS v_rank
                FROM chunks c
                WHERE c.doc_id IS DISTINCT FROM %(exclude_doc_id)s
                  AND (%(vault_id)s IS NULL OR c.vault_id = %(vault_id)s)
                ORDER BY c.embedding <=> %(vec)s::vector
                LIMIT %(pool)s
            ),
            fts_hits AS (
                SELECT c.chunk_id, c.doc_id, c.chunk_index, c.chunk_type,
                       c.header_path, c.content, c.context_content,
                       ts_rank(c.search_vector, websearch_to_tsquery('english', %(query)s)) AS fts_score,
                       ROW_NUMBER() OVER (
                           ORDER BY ts_rank(c.search_vector, websearch_to_tsquery('english', %(query)s)) DESC
                       ) AS f_rank
                FROM chunks c
                WHERE c.search_vector @@ websearch_to_tsquery('english', %(query)s)
                  AND c.doc_id IS DISTINCT FROM %(exclude_doc_id)s
                  AND (%(vault_id)s IS NULL OR c.vault_id = %(vault_id)s)
                LIMIT %(pool)s
            )
            SELECT
                COALESCE(v.chunk_id, f.chunk_id) AS chunk_id,
                COALESCE(v.doc_id, f.doc_id) AS doc_id,
                COALESCE(v.chunk_index, f.chunk_index) AS chunk_index,
                COALESCE(v.chunk_type, f.chunk_type) AS chunk_type,
                COALESCE(v.header_path, f.header_path) AS header_path,
                COALESCE(v.content, f.content) AS content,
                COALESCE(v.context_content, f.context_content) AS context_content,
                v.vector_distance,
                v.v_rank AS vector_rank,
                f.fts_score,
                f.f_rank AS fts_rank,
                COALESCE(%(vw)s * 1.0/(60 + v.v_rank), 0)
                    + COALESCE(%(fw)s * 1.0/(60 + f.f_rank), 0) AS rrf_score
            FROM vector_hits v
            FULL OUTER JOIN fts_hits f ON v.chunk_id = f.chunk_id
            ORDER BY rrf_score DESC
            LIMIT %(top_k)s
            """,
            {
                "vec": vec_str,
                "query": query,
                "pool": candidate_pool,
                "vw": vector_weight,
                "fw": fts_weight,
                "top_k": top_k,
                "exclude_doc_id": exclude_doc_id,
                "vault_id": vault_id,
            },
        )
        rows = cur.fetchall()

        # Join doc titles (scoped to the vault when set, so doc_id collisions across
        # vaults can't pull a foreign title).
        doc_ids = {row["doc_id"] for row in rows}
        titles = {}
        if doc_ids:
            cur.execute(
                """SELECT doc_id, title FROM documents
                   WHERE doc_id = ANY(%(ids)s)
                     AND (%(vault_id)s IS NULL OR vault_id = %(vault_id)s)""",
                {"ids": list(doc_ids), "vault_id": vault_id},
            )
            titles = {r["doc_id"]: r["title"] for r in cur.fetchall()}

        results = []
        for row in rows:
            d = dict(row)
            d["doc_title"] = titles.get(d["doc_id"], "")
            results.append(d)

        return results
    finally:
        if own_conn:
            conn.close()


def document_search(
    query: str,
    top_k: int = 5,
    query_vec: list[float] | None = None,
    conn=None,
    vault_id: str | None = None,
) -> list[dict]:
    """Search document-level summary embeddings (scoped to vault when set)."""
    if query_vec is None:
        query_vec = _embed_query(query)
    vec_str = str(query_vec)

    own_conn = conn is None
    if own_conn:
        conn = _get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT de.doc_id, de.summary, d.title, d.file_path,
                   de.embedding <=> %(vec)s::vector AS distance
            FROM doc_embeddings de
            JOIN documents d ON d.vault_id = de.vault_id AND d.doc_id = de.doc_id
            WHERE d.doc_exists = TRUE
              AND (%(vault_id)s IS NULL OR d.vault_id = %(vault_id)s)
            ORDER BY de.embedding <=> %(vec)s::vector
            LIMIT %(top_k)s
            """,
            {"vec": vec_str, "vault_id": vault_id, "top_k": top_k},
        )
        return [dict(row) for row in cur.fetchall()]
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Graph-aware retrieval expansion
# ---------------------------------------------------------------------------

_GRAPH_WEIGHTS = {
    ("wikilink", "outbound"): 0.7,
    ("embed", "outbound"):    0.7,
    ("wikilink", "inbound"):  0.6,
    ("embed", "inbound"):     0.6,
    # ("tag", "shared_tag"):    0.4,  # unused: tag expansion uses document_tags JOIN, not edges
}


def _get_graph_neighbors(
    seed_doc_ids: list[str],
    conn,
    overrides: dict[str, dict] | None = None,
    vault_id: str | None = None,
) -> dict[str, float]:
    """Find first-degree graph neighbors of seed documents.

    `overrides` lets a caller substitute live wikilinks/tags for one or more
    seed docs whose persisted edges/tags are stale (e.g. the doc being edited
    in /edit/). Shape: ``{doc_id: {"wikilinks": [str, ...], "tags": [str, ...]}}``.
    For an override seed, outbound edges and shared-tag membership come from
    the live values; inbound edges still come from the DB (other docs are
    not being edited, so their saved edges to this doc are authoritative).

    Returns dict mapping neighbor_doc_id → best_weight.
    """
    if not seed_doc_ids:
        return {}

    overrides = overrides or {}
    override_ids = set(overrides.keys())
    db_seeds = [s for s in seed_doc_ids if s not in override_ids]

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    neighbors: dict[str, float] = {}

    # 1. Outbound + inbound edges from the DB for non-override seeds
    if db_seeds:
        cur.execute(
            """
            SELECT neighbor_doc_id, edge_type, direction
            FROM (
                SELECT e.target_doc_id AS neighbor_doc_id,
                       e.edge_type,
                       'outbound' AS direction
                FROM edges e
                WHERE e.source_doc_id = ANY(%(seeds)s)
                  AND e.resolved = TRUE
                  AND e.target_doc_id IS NOT NULL
                  AND e.target_doc_id != ALL(%(all_seeds)s)
                  AND (%(vault_id)s IS NULL OR e.vault_id = %(vault_id)s)

                UNION ALL

                SELECT e.source_doc_id AS neighbor_doc_id,
                       e.edge_type,
                       'inbound' AS direction
                FROM edges e
                WHERE e.target_doc_id = ANY(%(seeds)s)
                  AND e.source_doc_id != ALL(%(all_seeds)s)
                  AND (%(vault_id)s IS NULL OR e.vault_id = %(vault_id)s)
            ) AS edge_neighbors
            """,
            {"seeds": db_seeds, "all_seeds": seed_doc_ids, "vault_id": vault_id},
        )
        for row in cur.fetchall():
            doc_id = row["neighbor_doc_id"]
            weight = _GRAPH_WEIGHTS.get((row["edge_type"], row["direction"]), 0.3)
            neighbors[doc_id] = max(neighbors.get(doc_id, 0), weight)

        cur.execute(
            """
            SELECT DISTINCT dt2.doc_id AS neighbor_doc_id
            FROM document_tags dt1
            JOIN document_tags dt2 ON dt1.tag = dt2.tag
                                   AND dt1.doc_id != dt2.doc_id
                                   AND dt1.vault_id = dt2.vault_id
            WHERE dt1.doc_id = ANY(%(seeds)s)
              AND dt2.doc_id != ALL(%(all_seeds)s)
              AND (%(vault_id)s IS NULL OR dt1.vault_id = %(vault_id)s)
            """,
            {"seeds": db_seeds, "all_seeds": seed_doc_ids, "vault_id": vault_id},
        )
        for row in cur.fetchall():
            doc_id = row["neighbor_doc_id"]
            neighbors[doc_id] = max(neighbors.get(doc_id, 0), 0.4)

    # 2. Inbound DB edges TO override seeds (other docs' saved state - still authoritative)
    if override_ids:
        cur.execute(
            """
            SELECT e.source_doc_id AS neighbor_doc_id, e.edge_type
            FROM edges e
            WHERE e.target_doc_id = ANY(%(override_ids)s)
              AND e.source_doc_id != ALL(%(all_seeds)s)
              AND (%(vault_id)s IS NULL OR e.vault_id = %(vault_id)s)
            """,
            {"override_ids": list(override_ids), "all_seeds": seed_doc_ids,
             "vault_id": vault_id},
        )
        for row in cur.fetchall():
            doc_id = row["neighbor_doc_id"]
            weight = _GRAPH_WEIGHTS.get((row["edge_type"], "inbound"), 0.3)
            neighbors[doc_id] = max(neighbors.get(doc_id, 0), weight)

    # 3. Outbound from override seeds - synthesized from live wikilinks.
    # Resolution shares rag_indexer's canonical name index so search and
    # indexing agree on which file [[Game Ideas]] points to.
    if any(ov.get("wikilinks") for ov in overrides.values()):
        name_index = _build_name_index(conn.cursor())
        for src_doc_id, ov in overrides.items():
            for target_title in ov.get("wikilinks") or []:
                target_doc_id = _resolve_target_doc_id(name_index, target_title, src_doc_id)
                if target_doc_id and target_doc_id not in seed_doc_ids:
                    neighbors[target_doc_id] = max(
                        neighbors.get(target_doc_id, 0),
                        _GRAPH_WEIGHTS.get(("wikilink", "outbound"), 0.7),
                    )

    # 4. Shared tags from override seeds - match live tags to other docs
    if any(ov.get("tags") for ov in overrides.values()):
        live_tags = sorted({t for ov in overrides.values() for t in (ov.get("tags") or [])})
        if live_tags:
            cur.execute(
                """
                SELECT DISTINCT doc_id FROM document_tags
                WHERE tag = ANY(%(tags)s) AND doc_id != ALL(%(all_seeds)s)
                  AND (%(vault_id)s IS NULL OR vault_id = %(vault_id)s)
                """,
                {"tags": live_tags, "all_seeds": seed_doc_ids, "vault_id": vault_id},
            )
            for row in cur.fetchall():
                doc_id = row["doc_id"]
                neighbors[doc_id] = max(neighbors.get(doc_id, 0), 0.4)

    return neighbors


def _fetch_neighbor_chunks(
    neighbor_doc_ids: list[str],
    query_vec: list[float],
    max_chunks: int,
    conn,
    exclude_doc_id: str | None = None,
    vault_id: str | None = None,
) -> list[dict]:
    """Fetch the most query-relevant chunks from neighbor documents (vault-scoped)."""
    if not neighbor_doc_ids:
        return []

    vec_str = str(query_vec)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(
        """
        SELECT c.chunk_id, c.doc_id, c.chunk_index, c.chunk_type,
               c.header_path, c.content, c.context_content,
               d.title AS doc_title,
               c.embedding <=> %(vec)s::vector AS distance
        FROM chunks c
        JOIN documents d ON d.vault_id = c.vault_id AND d.doc_id = c.doc_id
        WHERE c.doc_id = ANY(%(neighbor_ids)s)
          AND c.doc_id IS DISTINCT FROM %(exclude_doc_id)s
          AND c.embedding IS NOT NULL
          AND (%(vault_id)s IS NULL OR c.vault_id = %(vault_id)s)
        ORDER BY c.embedding <=> %(vec)s::vector
        LIMIT %(limit)s
        """,
        {
            "vec": vec_str,
            "neighbor_ids": neighbor_doc_ids,
            "limit": max_chunks,
            "exclude_doc_id": exclude_doc_id,
            "vault_id": vault_id,
        },
    )
    return [dict(row) for row in cur.fetchall()]


def graph_expand(
    chunk_results: list[dict],
    query_vec: list[float],
    max_neighbor_chunks: int = GRAPH_EXPANSION_MAX_NEIGHBOR_CHUNKS,
    seed_doc_count: int = GRAPH_EXPANSION_SEED_DOCS,
    conn=None,
    exclude_doc_id: str | None = None,
    current_doc_overrides: dict | None = None,
    vault_id: str | None = None,
) -> list[dict]:
    """Expand search results with chunks from graph-linked neighbor documents.

    Takes the top search results, finds their first-degree graph neighbors
    (via wikilinks, backlinks, shared tags), and fetches the most
    query-relevant chunks from those neighbors at a reduced score.

    `current_doc_overrides` lets the caller pin a doc whose live editor
    state should drive graph traversal instead of stale persisted edges.
    Shape: ``{"doc_id": str, "wikilinks": [str, ...], "tags": [str, ...]}``.
    The override doc is added as an additional graph seed (even if its
    chunks were excluded from `chunk_results` via `exclude_doc_id`), so
    the editor's current wikilinks/tags affect which neighbor docs surface.
    """
    if not chunk_results:
        return chunk_results

    own_conn = conn is None
    if own_conn:
        conn = _get_pg_connection()
    try:
        # Extract seed doc_ids (ordered by best RRF score per doc)
        seen_docs: dict[str, float] = {}
        for r in chunk_results:
            doc_id = r["doc_id"]
            score = r.get("rrf_score") or r.get("fts_score") or 0
            if doc_id not in seen_docs or score > seen_docs[doc_id]:
                seen_docs[doc_id] = score
        seed_doc_ids = sorted(seen_docs, key=seen_docs.get, reverse=True)[:seed_doc_count]

        overrides_map: dict[str, dict] = {}
        if current_doc_overrides and current_doc_overrides.get("doc_id"):
            cdoc_id = current_doc_overrides["doc_id"]
            overrides_map[cdoc_id] = current_doc_overrides
            if cdoc_id not in seed_doc_ids:
                seed_doc_ids = [cdoc_id, *seed_doc_ids]

        # Find graph neighbors (scoped to the vault so doc_id collisions across
        # vaults can't pull in a foreign neighbor).
        neighbors = _get_graph_neighbors(
            seed_doc_ids, conn, overrides=overrides_map or None, vault_id=vault_id
        )
        if not neighbors:
            logger.debug("graph_expand: no neighbors found for seeds %s", seed_doc_ids)
            return chunk_results

        logger.debug("graph_expand: found %d neighbors for seeds %s", len(neighbors), seed_doc_ids)

        # Fetch query-relevant chunks from neighbors
        neighbor_chunks = _fetch_neighbor_chunks(
            list(neighbors.keys()), query_vec, max_neighbor_chunks * 2, conn,
            exclude_doc_id=exclude_doc_id, vault_id=vault_id,
        )

        # Deduplicate against existing results
        existing_chunk_ids = {r["chunk_id"] for r in chunk_results}
        neighbor_chunks = [c for c in neighbor_chunks if c["chunk_id"] not in existing_chunk_ids]

        if not neighbor_chunks:
            return chunk_results

        # Score: neighbor_weight * min_rrf * (1 - cosine_distance)
        min_rrf = float(min(
            (r.get("rrf_score") or r.get("fts_score") or 0.001 for r in chunk_results),
            default=0.001,
        ))
        for chunk in neighbor_chunks:
            neighbor_weight = neighbors.get(chunk["doc_id"], 0.3)
            similarity = max(0, 1.0 - (chunk.get("distance") or 1.0))
            chunk["graph_score"] = neighbor_weight * min_rrf * similarity
            chunk["source"] = "graph"

        # Sort by graph_score, take top N
        neighbor_chunks.sort(key=lambda c: c["graph_score"], reverse=True)
        expanded = neighbor_chunks[:max_neighbor_chunks]

        # Tag original results
        for r in chunk_results:
            r.setdefault("source", "search")

        return chunk_results + expanded
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Related documents (document-level graph query)
# ---------------------------------------------------------------------------

def find_related(
    doc_id: str,
    top_k: int = 10,
    conn=None,
    vault_id: str = DEFAULT_VAULT,
) -> list[dict]:
    """Find documents related to a given document via links, tags, and embeddings,
    scoped to ``vault_id`` (hard isolation -- related docs never cross a vault).

    Combines four signals:
    - Outbound wikilinks (weight 1.0)
    - Backlinks (weight 0.85)
    - Shared tags (0.3 per shared tag, capped at 1.0)
    - Semantic similarity via doc_embeddings (weight 0.5)
    """
    own_conn = conn is None
    if own_conn:
        conn = _get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        scores: dict[str, dict] = {}  # doc_id → {score, relationships}

        def _add(did, score, rel_type):
            if did == doc_id or did is None:
                return
            if did not in scores:
                scores[did] = {"score": 0, "relationships": []}
            scores[did]["score"] += score
            scores[did]["relationships"].append(rel_type)

        # Outbound wikilinks
        cur.execute(
            """SELECT target_doc_id FROM edges
               WHERE vault_id = %s AND source_doc_id = %s
                 AND resolved = TRUE AND target_doc_id IS NOT NULL""",
            (vault_id, doc_id),
        )
        for row in cur.fetchall():
            _add(row["target_doc_id"], 1.0, "outbound_link")

        # Backlinks
        cur.execute(
            """SELECT source_doc_id FROM edges
               WHERE vault_id = %s AND target_doc_id = %s""",
            (vault_id, doc_id),
        )
        for row in cur.fetchall():
            _add(row["source_doc_id"], 0.85, "backlink")

        # Shared tags
        cur.execute(
            """SELECT dt2.doc_id, COUNT(*) AS shared
               FROM document_tags dt1
               JOIN document_tags dt2 ON dt1.tag = dt2.tag AND dt1.doc_id != dt2.doc_id
                                      AND dt1.vault_id = dt2.vault_id
               WHERE dt1.vault_id = %s AND dt1.doc_id = %s
               GROUP BY dt2.doc_id""",
            (vault_id, doc_id),
        )
        for row in cur.fetchall():
            tag_score = min(row["shared"] * 0.3, 1.0)
            _add(row["doc_id"], tag_score, "shared_tags")

        # Semantic similarity via doc_embeddings
        cur.execute(
            "SELECT embedding FROM doc_embeddings WHERE vault_id = %s AND doc_id = %s",
            (vault_id, doc_id),
        )
        emb_row = cur.fetchone()
        if emb_row and emb_row["embedding"]:
            cur.execute(
                """SELECT de.doc_id,
                          1.0 - (de.embedding <=> %s::vector) AS similarity
                   FROM doc_embeddings de
                   WHERE de.vault_id = %s AND de.doc_id != %s
                   ORDER BY de.embedding <=> %s::vector
                   LIMIT 20""",
                (str(emb_row["embedding"]), vault_id, doc_id, str(emb_row["embedding"])),
            )
            for row in cur.fetchall():
                sim_score = max(0, row["similarity"]) * 0.5
                if sim_score > 0.05:
                    _add(row["doc_id"], sim_score, "semantic")

        if not scores:
            return []

        # Join with document titles and sort
        result_doc_ids = list(scores.keys())
        cur.execute(
            """SELECT doc_id, title FROM documents
               WHERE vault_id = %s AND doc_id = ANY(%s) AND doc_exists = TRUE""",
            (vault_id, result_doc_ids),
        )
        titles = {r["doc_id"]: r["title"] for r in cur.fetchall()}

        results = []
        for did, info in scores.items():
            if did in titles:
                results.append({
                    "doc_id": did,
                    "title": titles[did],
                    "score": round(info["score"], 4),
                    "relationships": info["relationships"],
                })
        results.sort(key=lambda r: r["score"], reverse=True)
        return results[:top_k]
    finally:
        if own_conn:
            conn.close()


# ---------------------------------------------------------------------------
# Combined entry point
# ---------------------------------------------------------------------------

def search(
    query: str,
    top_k: int = 10,
    include_document_search: bool = True,
    include_graph_expansion: bool = True,
    vector_weight: float = 1.0,
    fts_weight: float = 1.0,
    exclude_doc_id: str | None = None,
    current_doc_overrides: dict | None = None,
    vault_id: str | None = None,
) -> dict:
    """Combined search entry point returning chunk-level and document-level results.

    `vault_id`, when set, scopes every step (chunk search, graph expansion, document
    search) to that vault for hard isolation. None searches the whole corpus.

    `exclude_doc_id` filters the given doc out of chunk-level results (and
    graph-expanded neighbors). Document-level results are not filtered -
    callers using exclusion typically only consume `chunk_results`.

    `current_doc_overrides` (see `graph_expand`) lets the caller substitute
    live wikilinks/tags for the excluded doc when seeding graph expansion,
    so retrieval reflects the editor's in-progress state rather than the
    on-disk index.
    """
    conn = _get_pg_connection()
    try:
        # Compute embedding once, share across all search steps
        query_vec = _embed_query(query)

        chunk_results = hybrid_search(
            query,
            top_k=top_k,
            vector_weight=vector_weight,
            fts_weight=fts_weight,
            query_vec=query_vec,
            conn=conn,
            exclude_doc_id=exclude_doc_id,
            vault_id=vault_id,
        )

        graph_expanded = False
        if include_graph_expansion and GRAPH_EXPANSION_ENABLED and chunk_results:
            chunk_results = graph_expand(
                chunk_results, query_vec, conn=conn,
                exclude_doc_id=exclude_doc_id,
                current_doc_overrides=current_doc_overrides,
                vault_id=vault_id,
            )
            graph_expanded = True

        document_results = []
        if include_document_search:
            document_results = document_search(
                query, top_k=5, query_vec=query_vec, conn=conn, vault_id=vault_id
            )

        return {
            "query": query,
            "chunk_results": chunk_results,
            "document_results": document_results,
            "graph_expanded": graph_expanded,
        }
    finally:
        conn.close()
