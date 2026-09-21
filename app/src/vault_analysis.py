# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Read-only vault-graph analysis tools, granted to agents as capabilities.

These are the deterministic *retrieval* half of vault-health analysis: thin
SQL/graph queries over the existing RAG tables (documents / edges /
doc_embeddings / chunks), each returning structured candidates. The agent LLM
does the *judgment + presentation* half - deciding what matters and writing it
up. Adding a new kind of check = add one function + one tool def here.

All queries are vault-scoped (hard isolation), accept an optional
`path_prefix` folder scope, and skip agent-owned pages (anything under
AGENT_OUTPUT_DIR, which should have no DB rows anyway - belt-and-suspenders)
so an agent never flags its own output. Functions are synchronous psycopg2
(like rag_search); src.agent_capabilities.execute_capability dispatches them
off the event loop and enforces the schemas' parameter bounds.
"""

import psycopg2.extras

from config import AGENT_OUTPUT_DIR
from src import vault_index
from src.rag_search import _get_pg_connection

# SQL LIKE prefix matching every agent-owned page (e.g. "_dada/%").
_AGENT_OWNED_PREFIX = AGENT_OUTPUT_DIR + "/%"


def _like_prefix(path_prefix: str) -> str:
    """Escape a folder prefix for LIKE ... ESCAPE '\\' prefix matching.

    Load-bearing: `_` is a LIKE wildcard and appears in real folder names
    (e.g. `Game_Ideas/`), so it must be escaped or the scope silently widens.
    An empty prefix yields '%' (match everything).
    """
    p = (path_prefix or "").strip().lstrip("/")
    p = p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return p + "%"


# ---------------------------------------------------------------------------
# Analysis queries (sync, psycopg2)
# ---------------------------------------------------------------------------

def list_orphans(vault_id: str, conn=None, limit: int = 50,
                 path_prefix: str = "") -> list[dict]:
    """Real pages (doc_exists) with no resolved wikilink in OR out - disconnected
    from the link graph. Excludes ghost nodes."""
    own = conn is None
    if own:
        conn = _get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT d.doc_id, d.title
            FROM documents d
            WHERE d.vault_id = %s AND d.doc_exists = TRUE
              AND d.doc_id NOT LIKE %s
              AND d.doc_id LIKE %s ESCAPE '\\'
              AND NOT EXISTS (
                  SELECT 1 FROM edges e WHERE e.vault_id = d.vault_id
                    AND e.source_doc_id = d.doc_id
                    AND e.resolved = TRUE AND e.target_doc_id IS NOT NULL)
              AND NOT EXISTS (
                  SELECT 1 FROM edges e WHERE e.vault_id = d.vault_id
                    AND e.target_doc_id = d.doc_id)
            ORDER BY d.doc_id
            LIMIT %s
            """,
            (vault_id, _AGENT_OWNED_PREFIX, _like_prefix(path_prefix), limit),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        if own:
            conn.close()


def find_near_duplicates(vault_id: str, conn=None,
                         threshold: float = 0.88,
                         limit: int = 30,
                         path_prefix: str = "") -> list[dict]:
    """Pairs of documents with cosine similarity >= threshold that are NOT linked
    to each other - candidate duplicates/merges. O(n^2) self-join on doc_embeddings,
    fine for a personal-wiki corpus."""
    own = conn is None
    if own:
        conn = _get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT a.doc_id AS doc_a, da.title AS title_a,
                   b.doc_id AS doc_b, db.title AS title_b,
                   1.0 - (a.embedding <=> b.embedding) AS similarity
            FROM doc_embeddings a
            JOIN doc_embeddings b
              ON a.vault_id = b.vault_id AND a.doc_id < b.doc_id
            JOIN documents da ON da.vault_id = a.vault_id AND da.doc_id = a.doc_id
            JOIN documents db ON db.vault_id = b.vault_id AND db.doc_id = b.doc_id
            WHERE a.vault_id = %s
              AND a.doc_id NOT LIKE %s AND b.doc_id NOT LIKE %s
              AND a.doc_id LIKE %s ESCAPE '\\' AND b.doc_id LIKE %s ESCAPE '\\'
              AND (1.0 - (a.embedding <=> b.embedding)) >= %s
              AND NOT EXISTS (
                  SELECT 1 FROM edges e WHERE e.vault_id = a.vault_id AND (
                      (e.source_doc_id = a.doc_id AND e.target_doc_id = b.doc_id) OR
                      (e.source_doc_id = b.doc_id AND e.target_doc_id = a.doc_id)))
            ORDER BY similarity DESC
            LIMIT %s
            """,
            (vault_id, _AGENT_OWNED_PREFIX, _AGENT_OWNED_PREFIX,
             _like_prefix(path_prefix), _like_prefix(path_prefix), threshold, limit),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        if own:
            conn.close()


def find_missing_links(vault_id: str, conn=None,
                       low: float = 0.62,
                       high: float = 0.88,
                       limit: int = 40,
                       path_prefix: str = "") -> list[dict]:
    """Unlinked document pairs that are semantically adjacent but NOT near-duplicates
    (low <= similarity < high) - propose a wikilink rather than a merge."""
    own = conn is None
    if own:
        conn = _get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT a.doc_id AS doc_a, da.title AS title_a,
                   b.doc_id AS doc_b, db.title AS title_b,
                   1.0 - (a.embedding <=> b.embedding) AS similarity
            FROM doc_embeddings a
            JOIN doc_embeddings b
              ON a.vault_id = b.vault_id AND a.doc_id < b.doc_id
            JOIN documents da ON da.vault_id = a.vault_id AND da.doc_id = a.doc_id
            JOIN documents db ON db.vault_id = b.vault_id AND db.doc_id = b.doc_id
            WHERE a.vault_id = %s
              AND a.doc_id NOT LIKE %s AND b.doc_id NOT LIKE %s
              AND a.doc_id LIKE %s ESCAPE '\\' AND b.doc_id LIKE %s ESCAPE '\\'
              AND (1.0 - (a.embedding <=> b.embedding)) >= %s
              AND (1.0 - (a.embedding <=> b.embedding)) <  %s
              AND NOT EXISTS (
                  SELECT 1 FROM edges e WHERE e.vault_id = a.vault_id AND (
                      (e.source_doc_id = a.doc_id AND e.target_doc_id = b.doc_id) OR
                      (e.source_doc_id = b.doc_id AND e.target_doc_id = a.doc_id)))
            ORDER BY similarity DESC
            LIMIT %s
            """,
            (vault_id, _AGENT_OWNED_PREFIX, _AGENT_OWNED_PREFIX,
             _like_prefix(path_prefix), _like_prefix(path_prefix), low, high, limit),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        if own:
            conn.close()


def list_stale_stubs(vault_id: str, conn=None,
                     max_chars: int = 400,
                     stale_days: int = 180,
                     limit: int = 40,
                     path_prefix: str = "") -> list[dict]:
    """Indexed pages whose total chunk text is short AND whose file has not been
    modified recently - likely abandoned stubs worth fleshing out or removing.

    Staleness is the file's mtime on THIS machine (``file_modified`` in each row),
    not documents.updated_at, which the indexer resets on every (re)index. A sync
    or `git pull` usually stamps a copied file with the time of the copy, so the
    common error makes a page look fresher, not older - a stub is missed rather
    than a recently edited page reported as abandoned."""
    import os
    import time

    from src import timefmt
    from src.wikidoc import WikiDoc

    own = conn is None
    if own:
        conn = _get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            """
            SELECT d.doc_id, d.title,
                   COALESCE(SUM(LENGTH(c.content)), 0) AS content_len
            FROM documents d
            LEFT JOIN chunks c ON c.vault_id = d.vault_id AND c.doc_id = d.doc_id
            WHERE d.vault_id = %s AND d.doc_exists = TRUE
              AND d.rag_indexed = TRUE
              AND d.doc_id NOT LIKE %s
              AND d.doc_id LIKE %s ESCAPE '\\'
            GROUP BY d.doc_id, d.title
            HAVING COALESCE(SUM(LENGTH(c.content)), 0) < %s
            """,
            (vault_id, _AGENT_OWNED_PREFIX, _like_prefix(path_prefix), max_chars),
        )
        short = cur.fetchall()
    finally:
        if own:
            conn.close()

    cutoff = time.time() - stale_days * 86400
    stale = []
    for r in short:
        try:
            mtime = os.path.getmtime(WikiDoc._abs_checked(vault_id, r["doc_id"]))
        except (OSError, ValueError):
            continue  # file gone since indexing, or a path the resolver refuses
        if mtime < cutoff:
            d = dict(r)
            d["file_modified"] = timefmt.to_local(int(mtime)).isoformat(timespec="seconds")
            stale.append((mtime, d))
    stale.sort(key=lambda pair: pair[0])
    return [d for _, d in stale[:limit]]


def list_broken_links(vault_id: str, conn=None,
                      limit: int = 50,
                      path_prefix: str = "") -> list[dict]:
    """Links whose target does not exist - the read side of the link-write tools.

    Two kinds, reported together because the fix differs:
      * ``planned`` - the target was never created (an unresolved edge). Either
        write the page or drop the link.
      * ``deleted`` - the target existed and was deleted, leaving a tombstone
        documents row. Here the LINKING page is what needs fixing.

    An unresolved edge is only a CANDIDATE, not a finding. Canvas files and agent
    output are excluded from the RAG index, so they have no documents row and
    every link to them looks unresolved while rendering perfectly. Each candidate
    is therefore confirmed against the FILESYSTEM with the renderer's own resolver
    (``vault_index.resolve``, whose index covers every file type, not just .md)
    and dropped if the file is really there - which also settles separator and
    case variants like [[Scratch Canvas.canvas]] vs Scratch_Canvas.canvas.

    Not every row is a mistake: wikilink syntax quoted in prose is
    indistinguishable from a real link ([[Ceres]] vs [[target]]), so the caller
    has to read the source page before changing it.

    One row per broken link rather than one per missing target: fan-in is close to
    1 in practice, and the flat (source, target) pair is what the fix tools take.
    Ordering by target keeps repeated links to one missing page adjacent anyway.
    """
    own = conn is None
    if own:
        conn = _get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        # No LIMIT here: the disk check below removes rows, so a SQL cap would let
        # non-findings eat the caller's budget. Unresolved edges are inherently
        # rare (tens across a whole install), so the candidate set is small.
        cur.execute(
            """
            SELECT e.source_doc_id, e.target_title, e.edge_type,
                   'planned' AS kind
            FROM edges e
            WHERE e.vault_id = %(vault)s
              AND e.resolved = FALSE AND e.target_doc_id IS NULL
              AND e.source_doc_id NOT LIKE %(owned)s
              AND e.source_doc_id LIKE %(prefix)s ESCAPE '\\'
            UNION ALL
            SELECT e.source_doc_id, e.target_title, e.edge_type,
                   'deleted' AS kind
            FROM edges e
            JOIN documents t ON t.vault_id = e.vault_id AND t.doc_id = e.target_doc_id
            WHERE e.vault_id = %(vault)s
              AND t.doc_exists = FALSE
              AND e.source_doc_id NOT LIKE %(owned)s
              AND e.source_doc_id LIKE %(prefix)s ESCAPE '\\'
            """,
            {"vault": vault_id, "owned": _AGENT_OWNED_PREFIX,
             "prefix": _like_prefix(path_prefix)},
        )
        candidates = cur.fetchall()
    finally:
        if own:
            conn.close()

    broken = []
    for r in candidates:
        # The stored title is the raw link text. Drop an Obsidian |alias or
        # |dimension and any #heading fragment first: both address a part of the
        # target, so the page to look for is the base -- the same normalization
        # the indexer applies before resolving (rag_indexer._resolve_target_doc_id).
        target = (r["target_title"] or "").split("|", 1)[0].split("#", 1)[0].strip()
        if not target:
            continue
        source = r["source_doc_id"]
        source_dir = source.rsplit("/", 1)[0] if "/" in source else ""
        if vault_index.resolve(target, source_dir, vault_id):
            continue                                # the file exists; not broken
        broken.append({"source_doc_id": source, "target": target,
                       "kind": r["kind"], "link_type": r["edge_type"]})

    broken.sort(key=lambda d: (d["target"].lower(), d["source_doc_id"]))
    return broken[:limit]


# ---------------------------------------------------------------------------
# Tool schemas (same shape as chat.TOOL_DEFINITIONS) + name set
# ---------------------------------------------------------------------------

_PREFIX_PARAM = {"type": "string", "default": "",
                 "description": "Restrict the check to pages under this folder prefix (e.g. 'Physics/')."}

ANALYSIS_TOOL_DEFINITIONS = [
    {"type": "function", "function": {
        "name": "list_orphans",
        "description": "List pages with no wikilinks in or out (disconnected from the link graph).",
        "parameters": {"type": "object", "properties": {
            "path_prefix": _PREFIX_PARAM,
            "limit": {"type": "integer", "description": "Max rows.",
                      "default": 50, "minimum": 1, "maximum": 200},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "find_near_duplicates",
        "description": "List pairs of pages that are highly semantically similar but not linked - candidate duplicates/merges.",
        "parameters": {"type": "object", "properties": {
            "threshold": {"type": "number",
                          "description": "Cosine similarity at or above which a pair counts as a near-duplicate.",
                          "default": 0.88, "minimum": 0.5, "maximum": 1.0},
            "path_prefix": _PREFIX_PARAM,
            "limit": {"type": "integer", "description": "Max pairs.",
                      "default": 30, "minimum": 1, "maximum": 200},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "find_missing_links",
        "description": "List pairs of pages that are related (semantically adjacent) but not yet linked - candidates for a new wikilink.",
        "parameters": {"type": "object", "properties": {
            "low": {"type": "number",
                    "description": "Lower similarity bound for 'related'.",
                    "default": 0.62, "minimum": 0.3, "maximum": 1.0},
            "high": {"type": "number",
                     "description": "Upper similarity bound (pairs at/above this are duplicates, not link candidates).",
                     "default": 0.88, "minimum": 0.3, "maximum": 1.0},
            "path_prefix": _PREFIX_PARAM,
            "limit": {"type": "integer", "description": "Max pairs.",
                      "default": 40, "minimum": 1, "maximum": 200},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "list_stale_stubs",
        "description": "List short pages that have not been updated in a long time - likely abandoned stubs.",
        "parameters": {"type": "object", "properties": {
            "stale_days": {"type": "integer",
                           "description": "Only pages untouched for at least this many days.",
                           "default": 180, "minimum": 1, "maximum": 3650},
            "max_chars": {"type": "integer",
                          "description": "Only pages whose indexed text is shorter than this.",
                          "default": 400, "minimum": 50, "maximum": 10000},
            "path_prefix": _PREFIX_PARAM,
            "limit": {"type": "integer", "description": "Max rows.",
                      "default": 40, "minimum": 1, "maximum": 200},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "list_broken_links",
        "description": ("List links that point nowhere, with the kind of breakage: "
                        "'planned' (the target page was never created - write it or "
                        "drop the link) or 'deleted' (the target was removed - fix the "
                        "linking page). Links to files that exist but are not indexed, "
                        "such as canvases and agent output, are NOT reported. Wikilink "
                        "syntax quoted in prose looks identical to a real link, so read "
                        "the source page before changing it."),
        "parameters": {"type": "object", "properties": {
            "path_prefix": _PREFIX_PARAM,
            "limit": {"type": "integer", "description": "Max rows.",
                      "default": 50, "minimum": 1, "maximum": 200},
        }, "required": []},
    }},
]

ANALYSIS_TOOL_NAMES = {tc["function"]["name"] for tc in ANALYSIS_TOOL_DEFINITIONS}
