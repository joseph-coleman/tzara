# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Embedding configuration management.

Detects the current embedding model's output dimension, tracks the active
model in a DB singleton row, and migrates vector columns when the model changes.
"""

import logging

from config import (
    OLLAMA_EMBED_MODEL,
    OLLAMA_URL,
)

logger = logging.getLogger("embedding_config")


# ---------------------------------------------------------------------------
# DB connection (same pattern as rag_indexer)
# ---------------------------------------------------------------------------

def _get_pg_connection():
    from config import get_pg_connection
    return get_pg_connection()


# ---------------------------------------------------------------------------
# Dimension detection
# ---------------------------------------------------------------------------

def detect_embedding_dimension() -> tuple[str, int]:
    """Embed a test string to discover the model's output dimension.

    Returns (model_name, dimension).
    Raises on backend connection failure.
    """
    from src.llm_backend import embed_texts_sync

    dim = len(embed_texts_sync(["dimension probe"])[0])
    return (OLLAMA_EMBED_MODEL, dim)


# ---------------------------------------------------------------------------
# Config table helpers
# ---------------------------------------------------------------------------

def _ensure_config_table(conn):
    """Create the embedding_config table if it doesn't exist (handles pre-existing DBs)."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS embedding_config (
            id          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            model_name  TEXT NOT NULL,
            dimension   INTEGER NOT NULL,
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    conn.commit()


def get_stored_config(conn) -> dict | None:
    """Read the singleton config row. Returns {"model_name": str, "dimension": int} or None."""
    cur = conn.cursor()
    cur.execute("SELECT model_name, dimension FROM embedding_config WHERE id = 1")
    row = cur.fetchone()
    if row is None:
        return None
    return {"model_name": row[0], "dimension": row[1]}


def upsert_config(conn, model_name: str, dimension: int):
    """Insert or update the singleton config row."""
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO embedding_config (id, model_name, dimension, updated_at)
        VALUES (1, %s, %s, NOW())
        ON CONFLICT (id) DO UPDATE
            SET model_name = EXCLUDED.model_name,
                dimension = EXCLUDED.dimension,
                updated_at = NOW()
        """,
        (model_name, dimension),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Vector column migration
# ---------------------------------------------------------------------------

def migrate_vector_dimension(conn, new_dim: int):
    """ALTER vector columns to a new dimension in a single transaction.

    Order: NULL out existing embeddings, ALTER columns, recreate indexes.
    Must NULL before ALTER since pgvector rejects ALTER if existing vectors
    have the wrong dimension.
    """
    cur = conn.cursor()
    try:
        # Drop HNSW indexes
        cur.execute("DROP INDEX IF EXISTS idx_chunks_embedding")
        cur.execute("DROP INDEX IF EXISTS idx_doc_emb")

        # NULL out stale embeddings (preserves row data)
        cur.execute("UPDATE chunks SET embedding = NULL")
        cur.execute("UPDATE doc_embeddings SET embedding = NULL")

        # ALTER columns to new dimension
        cur.execute(f"ALTER TABLE chunks ALTER COLUMN embedding TYPE vector({new_dim})")
        cur.execute(f"ALTER TABLE doc_embeddings ALTER COLUMN embedding TYPE vector({new_dim})")

        # Recreate HNSW indexes
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_embedding "
            "ON chunks USING hnsw (embedding vector_cosine_ops)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_doc_emb "
            "ON doc_embeddings USING hnsw (embedding vector_cosine_ops)"
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# Documents needing re-embedding
# ---------------------------------------------------------------------------

def get_docs_needing_embedding(conn) -> list[tuple[str, str]]:
    """Return (vault_id, doc_id) of existing documents that have NULL-embedding chunks.

    The JOIN keys on BOTH vault_id and doc_id: doc_ids are only unique *within* a vault
    (e.g. `main.md`, `.gitattributes` exist in several vaults), so joining on doc_id
    alone cross-matches same-named docs across vaults and, worse, hands the caller a
    bare doc_id with no vault context -- which then defaults to the "main" vault and
    writes chunks under a (vault, doc_id) that has no documents row, tripping
    chunks_doc_fkey. Returning the vault keeps each re-embed routed to its real home.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT DISTINCT d.vault_id, d.doc_id
        FROM documents d
        JOIN chunks c ON c.vault_id = d.vault_id AND c.doc_id = d.doc_id
        WHERE d.doc_exists = TRUE
          AND c.embedding IS NULL
    """)
    return [(row[0], row[1]) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def check_and_migrate_embedding_config() -> dict:
    """Check embedding model config against DB and migrate if needed.

    Called from worker startup. Returns a status dict:
      - {"status": "deferred"}         - Ollama unavailable
      - {"status": "initialized"}      - first run, config saved
      - {"status": "ok"}               - model matches, all embeddings present
      - {"status": "partial_reindex", "doc_ids": [(vault_id, doc_id), ...]} - some NULL embeddings
      - {"status": "migrated", "doc_ids": [(vault_id, doc_id), ...]}        - model changed
    """
    import asyncio

    conn = _get_pg_connection()
    try:
        # 1. Ensure config table exists (idempotent)
        _ensure_config_table(conn)

        # 2. Probe Ollama for current model dimension
        try:
            model_name, dimension = await asyncio.to_thread(detect_embedding_dimension)
        except Exception as e:
            logger.warning("Ollama unavailable for dimension probe: %s", e)
            return {"status": "deferred"}

        # 3. Read stored config
        stored = get_stored_config(conn)

        if stored is None:
            # First run - save current model config
            upsert_config(conn, model_name, dimension)
            logger.info("Embedding config initialized: %s (%d dims)", model_name, dimension)
            return {"status": "initialized"}

        # 4. Compare stored vs current
        if stored["model_name"] == model_name and stored["dimension"] == dimension:
            # Model matches - check for NULL embeddings (interrupted re-embed)
            doc_ids = get_docs_needing_embedding(conn)
            if doc_ids:
                logger.info("Found %d documents needing re-embedding", len(doc_ids))
                return {"status": "partial_reindex", "doc_ids": doc_ids}
            return {"status": "ok"}

        # Model changed - migrate
        logger.info(
            "Embedding model changed: %s (%d dims) -> %s (%d dims)",
            stored["model_name"], stored["dimension"], model_name, dimension,
        )
        migrate_vector_dimension(conn, dimension)
        upsert_config(conn, model_name, dimension)

        doc_ids = get_docs_needing_embedding(conn)
        logger.info("Migration complete. %d documents need re-embedding", len(doc_ids))
        return {"status": "migrated", "doc_ids": doc_ids}

    finally:
        conn.close()
