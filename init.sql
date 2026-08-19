CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS embedding_config (
    id          INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),  -- singleton
    model_name  TEXT NOT NULL,
    dimension   INTEGER NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Vaults have NO registry table: existence is the filesystem (a slug is an immediate
-- subdirectory of VAULTS_DIR) and metadata is each vault's `.tzara/config.json`, both
-- filesystem-authoritative (see src/vault_registry.py). `documents.vault_id` is thus a
-- free-standing isolation key with no parent table to FK against -- which also let the
-- indexer create document rows without a registration step.
CREATE TABLE IF NOT EXISTS documents (
    vault_id    TEXT NOT NULL,      -- isolation key (vault slug)
    doc_id      TEXT NOT NULL,      -- file path relative to vault root
    title       TEXT NOT NULL,      -- filename without extension
    file_path   TEXT NOT NULL,      -- full path within container
    doc_exists  BOOLEAN DEFAULT TRUE, -- FALSE = ghost node (unresolved wikilink target)
    rag_indexed BOOLEAN DEFAULT FALSE, -- TRUE = embedded/chunked for RAG; FALSE = link-graph only (e.g. Index:False pages)
    content_hash TEXT,              -- hash of document body (excluding frontmatter)
    indexed_at  TIMESTAMPTZ,
    summary     TEXT,               -- auto-generated summary from frontmatter
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (vault_id, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_documents_vault ON documents(vault_id);


CREATE TABLE IF NOT EXISTS edges (
    id              SERIAL PRIMARY KEY,
    vault_id        TEXT NOT NULL,      -- isolation key; source and target share a vault
    source_doc_id   TEXT NOT NULL,
    target_title    TEXT NOT NULL,      -- raw wikilink text (e.g., "Gram-Schmidt Process")
    target_doc_id   TEXT,               -- NULL if unresolved (ghost node)
    edge_type       TEXT DEFAULT 'wikilink',  -- 'wikilink', 'embed'
    resolved        BOOLEAN DEFAULT FALSE,
    UNIQUE(vault_id, source_doc_id, target_title, edge_type),
    FOREIGN KEY (vault_id, source_doc_id) REFERENCES documents(vault_id, doc_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(vault_id, source_doc_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(vault_id, target_doc_id);
CREATE INDEX IF NOT EXISTS idx_edges_target_title ON edges(vault_id, target_title);


CREATE TABLE IF NOT EXISTS document_tags (
    vault_id TEXT NOT NULL,
    doc_id  TEXT NOT NULL,
    tag     TEXT NOT NULL,
    source  TEXT DEFAULT 'inline',  -- 'pinned', 'auto', or 'inline'
    PRIMARY KEY (vault_id, doc_id, tag),
    FOREIGN KEY (vault_id, doc_id) REFERENCES documents(vault_id, doc_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON document_tags(vault_id, tag);


CREATE TABLE IF NOT EXISTS chunks (
    chunk_id        SERIAL PRIMARY KEY,
    vault_id        TEXT NOT NULL,
    doc_id          TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,       -- position within document
    chunk_type      TEXT DEFAULT 'prose',    -- 'prose', 'code', 'latex', 'mixed'
    header_path     TEXT[],                  -- breadcrumb array: ['Main Topic', 'Subtopic', 'Sub-subtopic']
    content         TEXT NOT NULL,           -- raw chunk text
    context_content TEXT NOT NULL,           -- breadcrumb-prepended text (used for embedding)
    wikilinks       TEXT[],                  -- wikilinks found in this chunk
    embedding       vector(768),            -- dense vector (dimension depends on embedding model)
    search_vector   tsvector,               -- full-text search vector
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    FOREIGN KEY (vault_id, doc_id) REFERENCES documents(vault_id, doc_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(vault_id, doc_id);
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_search ON chunks USING gin (search_vector);


CREATE TABLE IF NOT EXISTS doc_embeddings (
    vault_id    TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    summary     TEXT NOT NULL,
    embedding   vector(768),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (vault_id, doc_id),
    FOREIGN KEY (vault_id, doc_id) REFERENCES documents(vault_id, doc_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_doc_emb ON doc_embeddings USING hnsw (embedding vector_cosine_ops);


CREATE TABLE IF NOT EXISTS asset_refs (
    id          SERIAL PRIMARY KEY,
    vault_id    TEXT NOT NULL,
    asset_name  TEXT NOT NULL,           -- filename of embedded asset
    doc_id      TEXT NOT NULL,
    UNIQUE(vault_id, asset_name, doc_id),
    FOREIGN KEY (vault_id, doc_id) REFERENCES documents(vault_id, doc_id) ON DELETE CASCADE ON UPDATE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_asset_name ON asset_refs(vault_id, asset_name);


-- Agent staging manifest: one row per (run, file) shadow proposal awaiting human
-- review. Shadow file BODIES live on disk under vault-history/.staging/{run_id}/
-- (shared server+worker mount, outside vaults/ so watcher+Dropbox never see them);
-- this table is the durable index the /agents inbox reads and the promotion path
-- updates. No FK to documents: a staged file may be brand new.
CREATE TABLE IF NOT EXISTS agent_staging (
    id          SERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL,
    agent_slug  TEXT NOT NULL,
    vault_id    TEXT NOT NULL,
    rel_path    TEXT NOT NULL,
    base_hash   TEXT NOT NULL,            -- sha256 of the file at stage time; '' = new file
    note        TEXT NOT NULL DEFAULT '', -- tool-provided rationale shown in the inbox
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|rejected|drift
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    decided_at  TIMESTAMPTZ,
    UNIQUE (run_id, vault_id, rel_path)
);
CREATE INDEX IF NOT EXISTS idx_agent_staging_status ON agent_staging(status);


-- Durable failure log: the record behind /manage/monitor and its nav badge.
-- Task results live in Redis with a 1h TTL, so before this table a failure older
-- than an hour was unrecoverable except from container logs -- which is how an
-- orphaned index task went unnoticed for 52 hours in July 2026.
--
-- COALESCING, not append-only: a retrying task or a file failing every 5 minutes
-- would otherwise become hundreds of rows. The partial unique index below is both
-- the ON CONFLICT arbiter (bump occurrences + last_seen_at) and the index that
-- answers the badge count, so the badge reads "distinct broken things" rather than
-- "events". A new failure AFTER an ack opens a fresh row and re-lights the badge.
--
-- `badge` enforces "hard failures only" in the SCHEMA rather than in a WHERE
-- clause: memory-turn degradations are worth recording but fire routinely, and a
-- badge that cries wolf gets ignored.
CREATE TABLE IF NOT EXISTS system_failures (
    id            SERIAL PRIMARY KEY,
    kind          TEXT NOT NULL,                   -- task | agent_run | memory_turn
    subject       TEXT NOT NULL DEFAULT '',        -- task id / agent slug
    vault_id      TEXT NOT NULL DEFAULT '',
    detail        TEXT NOT NULL DEFAULT '',        -- truncated to 200 chars
    badge         BOOLEAN NOT NULL DEFAULT TRUE,   -- counts toward the nav badge
    status        TEXT NOT NULL DEFAULT 'open',    -- open|acked|resolved
    occurrences   INT NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    acked_at      TIMESTAMPTZ,
    resolved_at   TIMESTAMPTZ                      -- reserved: auto-resolve on later success
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_system_failures_open
    ON system_failures (kind, subject, vault_id) WHERE status = 'open';
