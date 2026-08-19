# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Generic agent capabilities: the declarative internal-tool registry.

An agent markdown file grants itself tools by NAME (frontmatter
`capabilities:`); this module is the canonical menu those names resolve
against. Each capability pairs an Ollama tool schema - typed, described,
defaulted parameters - with a plain SYNC function `fn(vault_id, **kwargs)`.
One async dispatcher (execute_capability) validates/coerces/clamps the
model-supplied args against the schema and runs the function off the event
loop, so adding a capability never means adding dispatcher code.

Sources of capabilities:
- vault analysis queries (src.vault_analysis) - orphans, near-duplicates,
  missing links, stale stubs;
- thin wrappers over existing retrieval (kernel_api search/related,
  a vault-scoped document listing);
- read/outline helpers over the run's staged overlay (write_gate.read_through);
- write PROPOSALS (propose_*, apply_wikilink/remove_wikilink) that go through
  the write gate: they stage shadow copies reviewed in the /agents inbox. The
  gate lives BELOW the agent, so an injected agent can only stage, never apply.
  (Agents running in act-with-checkpoint mode have their proposals applied
  immediately with a pre-image checkpoint commit - same chokepoint, per-agent
  trust.)

The write menu is deliberately SYMMETRIC at both granularities - whole page
(propose_create/edit/append), one section (propose_section_edit/insert/delete),
one link (apply_wikilink/remove_wikilink) - and each level can add, change and
remove. An agent given only add-shaped tools cannot maintain a structure, only
inflate it; that is why the remove halves exist. Sections are addressed and
spliced via src.md_sections, shared with chat's section tools; every write
still lands as one whole-file gated_write, so staging and drift detection are
unchanged.

Schema convention: every property carries a `description`; optional params
carry a `default` (documentation + prompt rendering) and numeric params may
carry `minimum`/`maximum` (enforced by clamping in _validate_args). These are
standard JSON-Schema keywords, passed through to the model harmlessly.
"""

import asyncio
import json
import logging
import os
import re

import psycopg2.extras

from config import AGENT_LEDGER_MAX_ITEMS, AGENT_LEDGER_RECALL_ROWS
from src.arg_coercion import arg_as_str, arg_as_int, arg_as_float, arg_as_list

logger = logging.getLogger("agent_capabilities")


# ---------------------------------------------------------------------------
# Argument validation (schema-driven; replaces per-tool dispatcher ladders)
# ---------------------------------------------------------------------------

def _validate_args(tool_def: dict, args: dict) -> tuple[dict, list[str]]:
    """Coerce + clamp model-supplied args against the tool schema.

    Unknown args are dropped; typed args are coerced (int/float/bool/str) and
    clamped to the property's minimum/maximum; missing required args error.
    Returns (kwargs, errors).
    """
    params = tool_def["function"].get("parameters") or {}
    props = params.get("properties") or {}
    required = params.get("required") or []
    args = args or {}

    errors = [f"missing required argument '{k}'"
              for k in required if str(args.get(k, "") or "").strip() == ""]
    kwargs: dict = {}
    for key, val in args.items():
        spec = props.get(key)
        if spec is None:
            continue  # drop args not in the schema
        expected = spec.get("type", "string")
        # Recover the common small-model shape errors (bracket-wrapped scalars,
        # one-element lists, nested dicts) instead of rejecting them.  Only
        # genuinely non-numeric input still errors out for numeric types.
        if expected == "integer":
            coerced = arg_as_int(val)
            if coerced is None:
                errors.append(f"argument '{key}' must be {expected} (got {val!r})")
                continue
            val = coerced
        elif expected == "number":
            coerced = arg_as_float(val)
            if coerced is None:
                errors.append(f"argument '{key}' must be {expected} (got {val!r})")
                continue
            val = coerced
        elif expected == "boolean":
            val = val if isinstance(val, bool) else str(val).strip().lower() in (
                "1", "true", "yes", "on")
        elif expected == "array":
            val = arg_as_list(val)
        else:
            val = arg_as_str(val)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            if "minimum" in spec:
                val = max(spec["minimum"], val)
            if "maximum" in spec:
                val = min(spec["maximum"], val)
        kwargs[key] = val
    return kwargs, errors


def _format_rows(name: str, rows: list) -> str:
    """Compact, model-friendly rendering of a result set."""
    if not rows:
        return f"{name}: no results."
    return f"{name}: {len(rows)} result(s)\n" + "\n".join(
        json.dumps(r, ensure_ascii=False) for r in rows
    )


def _doc_id(path: str) -> str:
    """Normalize a model-supplied page reference to a stored doc_id."""
    from src.kernel_api import _normalize_doc_id
    return _normalize_doc_id(path)


# ---------------------------------------------------------------------------
# Retrieval wrappers (sync; reuse existing vault-scoped code paths)
# ---------------------------------------------------------------------------

def search_wiki(vault_id: str, query: str, top_k: int = 8) -> list[dict]:
    """Hybrid + graph-expanded RAG search (kernel_api's uniform row shape)."""
    from src.kernel_api import _op_search
    return _op_search({"query": query, "top_k": top_k}, vault_id)


def find_related(vault_id: str, doc_id: str, top_k: int = 10) -> list[dict]:
    """Documents related to a page via links, tags, and embeddings."""
    from src.kernel_api import _op_related
    return _op_related({"path": doc_id, "top_k": top_k}, vault_id)


# list_documents is an ENUMERATION, not a ranked search: it answers "what pages
# are here?", so a silent row cap is a blind spot (the caller mistakes a prefix
# for the whole vault). These bounds are single-sourced into the schema below,
# and the result header always reports the TRUE total so truncation is visible.
# Default is high enough that a whole personal-scale vault comes back in ONE
# call - completeness matters more than payload size for an enumeration, and a
# small model can't be relied on to react to a TRUNCATED hint by paginating.
# The MAX is a real ceiling for the rare huge vault (the header still reports the
# true total there so truncation stays visible).
LIST_DOCS_DEFAULT_LIMIT = 500
LIST_DOCS_MAX_LIMIT = 2000


def list_documents(vault_id: str, path_prefix: str = "", tag: str = "",
                   limit: int = LIST_DOCS_DEFAULT_LIMIT, after: str = "") -> str:
    """Vault-scoped document listing, optionally filtered by folder prefix
    and/or tag. Agent-owned pages are excluded (location rule). Returns a header
    line ("N of TOTAL matching page(s)") followed by one JSON row per page.

    KEYSET-paged by doc_id: a whole personal-scale vault fits in one call, but a
    vault larger than `limit` is still fully reachable - the header hands back a
    `continue with after='<last doc_id>'` cursor. Unlike the kernel client this
    does NOT auto-paginate (the rows land in the model's context, which must stay
    bounded); the agent pages on demand when the header says more remain."""
    from config import AGENT_OUTPUT_DIR
    from src.rag_search import _get_pg_connection
    from src.vault_analysis import _like_prefix

    # Build the filter once; reuse for the COUNT (true total) and the page query.
    where = ["d.vault_id = %s AND d.doc_exists = TRUE", "d.doc_id NOT LIKE %s"]
    binds: list = [vault_id, AGENT_OUTPUT_DIR + "/%"]
    if path_prefix.strip():
        where.append("d.doc_id LIKE %s ESCAPE '\\'")
        binds.append(_like_prefix(path_prefix))
    if tag.strip():
        where.append("EXISTS (SELECT 1 FROM document_tags dt"
                     " WHERE dt.vault_id = d.vault_id"
                     "   AND dt.doc_id = d.doc_id AND dt.tag = %s)")
        binds.append(tag.strip())
    count_where = " AND ".join(where)          # total ignores the paging cursor
    page_where = count_where
    page_binds = list(binds)
    if after.strip():                          # keyset: resume strictly past `after`
        page_where += " AND d.doc_id > %s"
        page_binds.append(after.strip())

    conn = _get_pg_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(f"SELECT COUNT(*) AS n FROM documents d WHERE {count_where}", binds)
        count_row = cur.fetchone()
        total = count_row["n"] if count_row else 0
        cur.execute(
            f"SELECT d.doc_id, d.title, d.updated_at FROM documents d "
            f"WHERE {page_where} ORDER BY d.doc_id LIMIT %s", page_binds + [limit])
        rows = []
        for r in cur.fetchall():
            d = dict(r)
            if d.get("updated_at") is not None:
                d["updated_at"] = d["updated_at"].isoformat()
            rows.append(d)
    finally:
        conn.close()

    header = f"list_documents: {len(rows)} of {total} matching page(s)"
    if len(rows) == limit:      # a full page: more may remain - hand back the cursor
        header += (f" - MORE MAY REMAIN; continue with after='{rows[-1]['doc_id']}', "
                   f"or narrow with path_prefix/tag")
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    return header + ("\n" + body if body else "")


# read_document truncation-window bounds. Single-sourced here into BOTH the
# function default and the tool schema (default/minimum/maximum) below, so the
# model is told the same numbers the code enforces. The MAX is a deliberate
# context-cost ceiling for small models - an explicit "read the whole page"
# escape hatch, not a hard document limit; head+tail windowing means a page's
# footer survives below it regardless of size.
READ_DOC_DEFAULT_CHARS = 8000
READ_DOC_MIN_CHARS = 200
READ_DOC_MAX_CHARS = 20000


def read_document(vault_id: str, doc_id: str,
                  max_chars: int = READ_DOC_DEFAULT_CHARS) -> str:
    """Read a document's raw markdown (truncated). Reads THROUGH the run's
    staged overlay (write_gate) so the agent sees its own pending proposals.

    Truncation keeps BOTH ends (head + tail) and elides the middle: a log or
    report page carries its freshest, highest-signal state at the BOTTOM (an
    agent's "Next run" note, the latest appended section), which head-only
    truncation would silently drop. The elision marker is loud and states the
    total size so the caller can re-read with a larger max_chars or use
    get_outline."""
    from src import write_gate
    try:
        safe = _doc_id(doc_id)
        text = write_gate.read_through(vault_id, safe)
    except Exception as e:  # pragma: no cover - defensive
        return f"(failed to read '{doc_id}': {e})"
    if text is None:
        return f"(document '{doc_id}' not found on disk)"
    if len(text) <= max_chars:
        return text
    head = max_chars * 2 // 3          # bias to the top, but guarantee a tail slice
    tail = max_chars - head
    return (text[:head]
            + f"\n\n…[elided {len(text) - max_chars} of {len(text)} chars from the "
              f"MIDDLE; re-read with a larger max_chars (up to {READ_DOC_MAX_CHARS}) "
              f"or call get_outline for structure]…\n\n"
            + text[-tail:])


def get_outline(vault_id: str, doc_id: str) -> str:
    """Section outline (heading tree with indices) of a document."""
    from src import md_sections, write_gate
    try:
        safe = _doc_id(doc_id)
        text = write_gate.read_through(vault_id, safe)
    except Exception as e:  # pragma: no cover - defensive
        return f"(failed to read '{doc_id}': {e})"
    if text is None:
        return f"(document '{doc_id}' not found on disk)"
    outline = md_sections.build_outline(md_sections.parse_sections(text))
    return outline or "(document has no headings)"


# ---------------------------------------------------------------------------
# Write proposals (all funnel through the write gate - never direct writes)
# ---------------------------------------------------------------------------

def _stage(vault_id: str, rel: str, new_content: str, note: str) -> str:
    # gated_write: stages in propose mode, applies-with-checkpoint in act mode
    # (the mode rides the run context, set from the blessed agent file).
    from src import write_gate
    return write_gate.gated_write(vault_id, rel, new_content, note=note)


def propose_create(vault_id: str, doc_id: str, content: str, note: str = "") -> str:
    """Propose a brand-new page; errors if the page already exists."""
    from src import write_gate
    rel = _doc_id(doc_id)
    if write_gate.read_through(vault_id, rel) is not None:
        return (f"propose_create: '{rel}' already exists - use propose_edit or "
                "propose_append instead.")
    body = content.rstrip() + "\n"
    result = _stage(vault_id, rel, body, note or f"new page {rel}")
    return f"propose_create: {result}"


def propose_edit(vault_id: str, doc_id: str, new_content: str, note: str = "") -> str:
    """Propose replacing a page's full content; errors if the page is missing."""
    from src import write_gate
    rel = _doc_id(doc_id)
    if write_gate.read_through(vault_id, rel) is None:
        return f"propose_edit: '{rel}' not found - use propose_create for new pages."
    body = new_content.rstrip() + "\n"
    result = _stage(vault_id, rel, body, note or f"edit {rel}")
    return f"propose_edit: {result}"


def propose_append(vault_id: str, doc_id: str, content: str, note: str = "") -> str:
    """Propose appending a block to the end of an existing page."""
    from src import write_gate
    rel = _doc_id(doc_id)
    current = write_gate.read_through(vault_id, rel)
    if current is None:
        return f"propose_append: '{rel}' not found - use propose_create for new pages."
    body = current.rstrip() + "\n\n" + content.strip() + "\n"
    result = _stage(vault_id, rel, body, note or f"append to {rel}")
    return f"propose_append: {result}"


# --- section-scoped proposals -------------------------------------------------
#
# Whole-document propose_edit forces a small model to re-emit a page to change
# one part of it - expensive, and it drops frontmatter and unrelated prose it
# was never asked to touch. These address ONE section instead, so the staged
# diff a human reviews is the size of the actual change. Addressing and splicing
# are md_sections' (shared with chat's section tools); staging is unchanged.


def _sections_for(vault_id: str, rel: str):
    """(content, sections) for a page, or (None, None) if it isn't on disk."""
    from src import md_sections, write_gate
    content = write_gate.read_through(vault_id, rel)
    if content is None:
        return None, None
    return content, md_sections.parse_sections(content)


def _resolve_section(sections, heading, index):
    """(section, error_message). The error names every available section - a
    small model recovers from a listed alternative, not from a bare failure."""
    from src import md_sections
    section = md_sections.lookup_section(sections, heading, index)
    if section is None:
        return None, (f"section '{heading}' not found. Available sections: "
                      + (md_sections.describe_sections(sections) or "(none)"))
    return section, ""


def propose_section_edit(vault_id: str, doc_id: str, section_heading: str,
                         new_content: str, section_index: int | None = None,
                         note: str = "") -> str:
    """Propose replacing ONE section's body, leaving its heading and the rest of
    the page untouched."""
    from src import md_sections
    rel = _doc_id(doc_id)
    content, sections = _sections_for(vault_id, rel)
    if content is None:
        return f"propose_section_edit: '{rel}' not found - use propose_create for new pages."
    section, err = _resolve_section(sections, section_heading, section_index)
    if err:
        return f"propose_section_edit: {err}"
    body = md_sections.replace_section(content, section, new_content)
    result = _stage(vault_id, rel, body,
                    note or f"edit section '{section['heading']}' in {rel}")
    return f"propose_section_edit: {result} (section '{section['heading']}')"


def propose_section_insert(vault_id: str, doc_id: str, heading: str,
                           content: str, position: str = "after",
                           reference_section: str = "",
                           reference_section_index: int | None = None,
                           note: str = "") -> str:
    """Propose inserting a new section before/after an existing one (or at the
    end of the page when no reference section is given)."""
    from src import md_sections
    rel = _doc_id(doc_id)
    current, sections = _sections_for(vault_id, rel)
    if current is None:
        return f"propose_section_insert: '{rel}' not found - use propose_create for new pages."

    position = (position or "after").strip().lower()
    if position not in ("before", "after"):
        position = "after"

    reference = None
    if reference_section:
        reference, err = _resolve_section(sections, reference_section,
                                          reference_section_index)
        if err:
            return f"propose_section_insert: {err}"

    body = md_sections.insert_section(current, heading, content,
                                      reference=reference, position=position)
    where = f"{position} '{reference_section}'" if reference else "at end"
    result = _stage(vault_id, rel, body,
                    note or f"insert section '{heading}' in {rel}")
    return f"propose_section_insert: {result} ('{heading}' {where})"


def propose_section_delete(vault_id: str, doc_id: str, section_heading: str,
                           section_index: int | None = None,
                           note: str = "") -> str:
    """Propose removing a whole section - its heading and its body."""
    from src import md_sections
    rel = _doc_id(doc_id)
    content, sections = _sections_for(vault_id, rel)
    if content is None:
        return f"propose_section_delete: '{rel}' not found."
    section, err = _resolve_section(sections, section_heading, section_index)
    if err:
        return f"propose_section_delete: {err}"
    body = md_sections.delete_section(content, section)
    removed = len(content) - len(body)
    result = _stage(vault_id, rel, body,
                    note or f"delete section '{section['heading']}' from {rel}")
    return (f"propose_section_delete: {result} "
            f"(section '{section['heading']}', {removed} chars removed)")


# --- wikilink add / remove ----------------------------------------------------

def _related_section(content: str):
    """The page's `## Related` section dict, or None."""
    from src import md_sections
    for s in md_sections.parse_sections(content):
        if s["level"] > 0 and s["heading_text"].strip().lower() == "related":
            return s
    return None


# A markdown list marker: bullet (-, *, +) or ordered (1. / 1)). Matched against
# the line ALREADY stripped of indentation, so nested list items count too.
_LIST_MARKER_RE = re.compile(r"^(?:[-*+]|\d+[.)])[ \t]+")

# A task checkbox immediately after the marker. Task items are NOT link
# listings - `- [x] [[X]]` records that something was DONE and `- [ ] [[X]]` is
# outstanding work. Deleting either destroys information a link list never
# carried, so these are protected and reported, never pruned.
_TASK_BOX_RE = re.compile(r"^\[[ xX]\][ \t]*")

# What may remain beside the link on a removable bullet: emphasis markers and
# trailing punctuation, nothing that carries meaning.
_BULLET_DECORATION = " \t*_`~-–—:;,.()"


def _classify_link_lines(content: str, target_doc: str) -> tuple[list[int], dict[int, str]]:
    """Split the lines mentioning target_doc into (removable, {index: reason}).

    REMOVABLE: a plain list bullet whose only payload is the link - `-`, `*`,
    `+`, `1.`, `1)`, nested/indented, with emphasis or trailing punctuation
    allowed. That is the shape apply_wikilink writes, so a pruning agent can
    undo a filed link without touching anything a human composed.

    PROTECTED: everything else that mentions the target - a link inside a
    sentence, a bullet carrying prose or a second link, and TASK items
    (`- [ ]` / `- [x]`), which are records of work rather than link listings.
    The caller reports these instead of editing them.
    """
    from src.content_ops import iter_wikilinks, link_targets_doc
    removable: list[int] = []
    protected: dict[int, str] = {}
    for i, line in enumerate(content.split("\n")):
        stripped = line.strip()
        matched = [m for m, t, _a, _al, _e in iter_wikilinks(stripped)
                   if link_targets_doc(t, target_doc)]
        if not matched:
            continue

        marker = _LIST_MARKER_RE.match(stripped)
        if marker is None:                      # prose, heading, table cell...
            protected[i] = "prose"
            continue
        payload = stripped[marker.end():]
        if _TASK_BOX_RE.match(payload):         # a task, not a link listing
            protected[i] = "task"
            continue

        remainder = payload
        for m in matched:
            remainder = remainder.replace(m.group(0), "")
        if remainder.strip(_BULLET_DECORATION):  # carries something else
            protected[i] = "prose"
        else:
            removable.append(i)
    return removable, protected


def _link_bullet_lines(content: str, target_doc: str) -> list[int]:
    """Removable link-bullet line indices (see _classify_link_lines)."""
    return _classify_link_lines(content, target_doc)[0]


def apply_wikilink(vault_id: str, source_doc: str, target_doc: str,
                   reason: str = "") -> str:
    """Propose adding a wikilink to target_doc under a `## Related` section in
    source_doc. Nothing touches the real page here: the change goes through the
    write gate like every proposal.

    It never edits existing prose - only adds a Related link. Reads go
    through the run's staged overlay so multiple links to the same source
    accumulate into one reviewable shadow.
    """
    from src import md_sections, write_gate
    from src.content_ops import iter_wikilinks, link_targets_doc

    src_rel = source_doc.lstrip("/")
    content = write_gate.read_through(vault_id, src_rel)
    if content is None:
        return f"apply_wikilink: source '{source_doc}' not found on disk."

    target_stem = os.path.splitext(target_doc.lstrip("/"))[0]
    wikilink = f"[[/{target_stem}]]"

    # Idempotency: does any existing link already resolve to the target? Matched
    # per path-segment (link_targets_doc), NOT by substring - a page linking
    # [[Ceres Station]] must not suppress a genuine link to [[Ceres]].
    if any(link_targets_doc(t, target_doc) for _m, t, _a, _al, _e
           in iter_wikilinks(content)):
        return f"apply_wikilink: '{source_doc}' already links to '{target_doc}'; skipped."

    related_line = f"- {wikilink}"
    related = _related_section(content)
    if related is not None:
        # Add the bullet at the END OF THE RELATED SECTION - not end of file.
        # Related is often not the last section, and appending at EOF filed the
        # link under whatever section happened to come last.
        body = md_sections.section_body(content, related).strip()
        new_body = (body + "\n" + related_line) if body else related_line
        new_content = md_sections.replace_section(content, related, new_body)
    else:
        # Same shape the branch above produces, so a page's Related section
        # looks identical whether this call created it or extended it.
        new_content = md_sections.insert_section(content, "## Related", related_line)

    try:
        note = f"{source_doc} -> {target_doc}" + (f": {reason}" if reason else "")
        result = _stage(vault_id, src_rel, new_content, note)
    except Exception as e:  # pragma: no cover - defensive
        return f"apply_wikilink: staging failed for '{source_doc}': {e}"
    return f"apply_wikilink: {result} ({wikilink} proposed{' - ' + reason if reason else ''})"


def remove_wikilink(vault_id: str, source_doc: str, target_doc: str,
                    reason: str = "") -> str:
    """Propose REMOVING a wikilink bullet from source_doc - the counterpart to
    apply_wikilink, so an agent maintaining a link structure can prune it and
    not only grow it.

    Only removes list bullets whose sole content is the link. A link written
    into a sentence is left in place and reported, because deleting it would
    mean rewriting someone's prose.
    """
    from src import md_sections, write_gate
    from src.content_ops import iter_wikilinks, link_targets_doc

    src_rel = source_doc.lstrip("/")
    content = write_gate.read_through(vault_id, src_rel)
    if content is None:
        return f"remove_wikilink: source '{source_doc}' not found on disk."

    present = any(link_targets_doc(t, target_doc) for _m, t, _a, _al, _e
                  in iter_wikilinks(content))
    if not present:
        return f"remove_wikilink: '{source_doc}' does not link to '{target_doc}'; nothing to do."

    lines = content.split("\n")
    bullets, protected = _classify_link_lines(content, target_doc)
    if not bullets:
        # Say WHICH protection applied - an agent told "it's a task" can report
        # that accurately instead of retrying or trying to route around it.
        if "task" in protected.values():
            return (f"remove_wikilink: '{source_doc}' links to '{target_doc}' from a "
                    "TASK item (`- [ ]` / `- [x]`); left unchanged. A task records "
                    "outstanding or completed work, not a filed link - removing it "
                    "would delete something a link list never carried.")
        return (f"remove_wikilink: '{source_doc}' links to '{target_doc}' only inside "
                "prose, not as a plain list bullet; left unchanged. Use "
                "propose_section_edit if that sentence genuinely needs rewriting.")

    kept = [ln for i, ln in enumerate(lines) if i not in set(bullets)]
    new_content = "\n".join(kept)

    # If that emptied the Related section, take the now-pointless heading too.
    related = _related_section(new_content)
    if related is not None and not md_sections.section_body(new_content, related).strip():
        new_content = md_sections.delete_section(new_content, related)

    try:
        note = (f"unlink {source_doc} -> {target_doc}"
                + (f": {reason}" if reason else ""))
        result = _stage(vault_id, src_rel, new_content, note)
    except Exception as e:  # pragma: no cover - defensive
        return f"remove_wikilink: staging failed for '{source_doc}': {e}"
    return (f"remove_wikilink: {result} ({len(bullets)} link line(s) removed"
            f"{' - ' + reason if reason else ''})")


# ---------------------------------------------------------------------------
# Ledgers (append-only; the durable half of cross-run memory)
# ---------------------------------------------------------------------------

def _ledger_owner() -> str | None:
    """Owned-area path of the caller ("agents/{slug}" / "editors/{slug}").

    Taken from the run context rather than a tool argument, for the same reason
    write_gate does it: identity the model can type is identity the model can
    forge, and a ledger belongs to exactly one owner. Outside a run there is no
    owner, so the tools refuse rather than guessing.
    """
    from src import write_gate
    ctx = write_gate.current_run()
    return ctx[1] if ctx else None


async def remember(vault_id: str, ledger: str, items: list) -> str:
    """Append items to a named append-only ledger (created on first use)."""
    owner = _ledger_owner()
    if not owner:
        return "remember: no active run - ledgers are per-agent and need a run context."
    from src.background_agents import apply_ledger_ops
    notes = await apply_ledger_ops(
        vault_id, owner, [{"ledger": ledger, "items": items}])
    return "remember: " + ("; ".join(notes) if notes else "nothing to record.")


async def forget(vault_id: str, ledger: str) -> str:
    """Delete an entire ledger."""
    owner = _ledger_owner()
    if not owner:
        return "forget: no active run - ledgers are per-agent and need a run context."
    from src.background_agents import apply_ledger_ops
    notes = await apply_ledger_ops(
        vault_id, owner, [{"ledger": ledger, "forget": True}])
    return "forget: " + ("; ".join(notes) if notes else "nothing to forget.")


# Recall window bounds, single-sourced into BOTH the function default and the
# tool schema below (the READ_DOC_* pattern), so the model is told the numbers
# the code enforces. The ceiling is the storage cap - asking for more rows than
# a ledger can hold is meaningless.
RECALL_DEFAULT_ROWS = AGENT_LEDGER_RECALL_ROWS
RECALL_MIN_ROWS = 1
RECALL_MAX_ROWS = AGENT_LEDGER_MAX_ITEMS


def recall(vault_id: str, ledger: str = "",
           max_rows: int = RECALL_DEFAULT_ROWS) -> str:
    """Read one of the caller's own ledgers, or list which ones it has.

    Sync, unlike its write-side siblings: a read touches no file and needs no git
    commit. The escape hatch for an injected view that had to elide rows - see
    context_providers.LedgerProvider, which declares the gap this closes.
    """
    owner = _ledger_owner()
    if not owner:
        return "recall: no active run - ledgers are per-agent and need a run context."
    from src.background_agents import read_agent_ledgers
    from src.ledgers import recall_text
    return recall_text(read_agent_ledgers(vault_id, owner), ledger, max_rows)


# Tools whose presence means the owner keeps ledgers - and therefore that the
# ledgers page must be INJECTED for them, whether or not `memory:` is on. A
# ledger write needs only a run context, so an owner granted `remember` with
# memory off recorded rows every run and never saw one: add-only with no read is
# not a memory system. `recall` counts too - an owner granted only recall is a
# read-only consumer of rows an earlier run wrote.
#
# NOT the same set as editor_registry.EDITOR_LEDGER_CAPABILITIES, which lists the
# tools an editor must BROKER to the worker because they write. `recall` must
# never join that one: a read needs no git and runs in-process. Do not merge.
LEDGER_TOOL_NAMES = {"remember", "forget", "recall"}


def uses_ledgers(memory: bool, tool_names) -> bool:
    """Whether the ledgers page should be injected for this owner."""
    return bool(memory) or bool(LEDGER_TOOL_NAMES & set(tool_names or ()))


# ---------------------------------------------------------------------------
# Tool schemas for the generic capabilities
# ---------------------------------------------------------------------------

_PROPOSAL_NOTE = (
    "The change is staged for the human's review inbox (/agents); agents "
    "trusted with act-with-checkpoint mode have it applied immediately with "
    "a checkpoint commit."
)

GENERIC_TOOL_DEFINITIONS = [
    {"type": "function", "function": {
        "name": "search_wiki",
        "description": "Semantic + full-text search across this vault. Returns chunk and whole-document matches with scores.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "How many results.",
                      "default": 8, "minimum": 1, "maximum": 25},
        }, "required": ["query"]},
    }},
    {"type": "function", "function": {
        "name": "find_related",
        "description": "Documents related to a page via wikilinks, shared tags, and embedding similarity.",
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string", "description": "The page to find relatives of."},
            "top_k": {"type": "integer", "description": "How many results.",
                      "default": 10, "minimum": 1, "maximum": 25},
        }, "required": ["doc_id"]},
    }},
    {"type": "function", "function": {
        "name": "list_documents",
        "description": "List pages in this vault, optionally restricted to a folder and/or a tag.",
        "parameters": {"type": "object", "properties": {
            "path_prefix": {"type": "string", "default": "",
                            "description": "Only pages whose path starts with this folder prefix (e.g. 'Physics/')."},
            "tag": {"type": "string", "default": "",
                    "description": "Only pages carrying this tag."},
            "limit": {"type": "integer",
                      "description": "Max rows returned; the result header still reports the true total when capped.",
                      "default": LIST_DOCS_DEFAULT_LIMIT, "minimum": 1,
                      "maximum": LIST_DOCS_MAX_LIMIT},
            "after": {"type": "string", "default": "",
                      "description": "Pagination cursor: pass the 'after=' value from a previous call's header to fetch the next page of a vault larger than one call can return."},
        }, "required": []},
    }},
    {"type": "function", "function": {
        "name": "read_document",
        "description": "Read a document's markdown content (truncated) to inspect it before acting on it.",
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string", "description": "The document path (doc_id) to read."},
            "max_chars": {"type": "integer",
                          "description": "Size of the read window; larger pages keep head+tail and elide the middle.",
                          "default": READ_DOC_DEFAULT_CHARS,
                          "minimum": READ_DOC_MIN_CHARS, "maximum": READ_DOC_MAX_CHARS},
        }, "required": ["doc_id"]},
    }},
    {"type": "function", "function": {
        "name": "get_outline",
        "description": "Get a document's section outline (heading tree) without its full text.",
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string", "description": "The document path (doc_id) to outline."},
        }, "required": ["doc_id"]},
    }},
    {"type": "function", "function": {
        "name": "apply_wikilink",
        "description": ("Propose adding a wikilink from source_doc to target_doc under a "
                        "'## Related' section (idempotent). " + _PROPOSAL_NOTE +
                        " Use ONLY for the most confident missing-link suggestions; "
                        "never for duplicates or uncertain links."),
        "parameters": {"type": "object", "properties": {
            "source_doc": {"type": "string", "description": "doc_id of the page to add the link TO."},
            "target_doc": {"type": "string", "description": "doc_id of the page to link."},
            "reason": {"type": "string", "default": "", "description": "Short reason the link makes sense."},
        }, "required": ["source_doc", "target_doc"]},
    }},
    {"type": "function", "function": {
        "name": "propose_create",
        "description": "Propose a brand-new page with the given content. " + _PROPOSAL_NOTE,
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string", "description": "Path for the new page (e.g. 'Physics/Quantum Tunneling')."},
            "content": {"type": "string", "description": "Full markdown content of the new page."},
            "note": {"type": "string", "default": "", "description": "Short reviewer-facing reason for the proposal."},
        }, "required": ["doc_id", "content"]},
    }},
    {"type": "function", "function": {
        "name": "propose_edit",
        "description": "Propose replacing an existing page's full content. " + _PROPOSAL_NOTE,
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string", "description": "The page to edit."},
            "new_content": {"type": "string", "description": "The complete replacement markdown."},
            "note": {"type": "string", "default": "", "description": "Short reviewer-facing reason for the proposal."},
        }, "required": ["doc_id", "new_content"]},
    }},
    {"type": "function", "function": {
        "name": "propose_append",
        "description": "Propose appending a markdown block to the end of an existing page. " + _PROPOSAL_NOTE,
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string", "description": "The page to append to."},
            "content": {"type": "string", "description": "The markdown block to append."},
            "note": {"type": "string", "default": "", "description": "Short reviewer-facing reason for the proposal."},
        }, "required": ["doc_id", "content"]},
    }},
    {"type": "function", "function": {
        "name": "propose_section_edit",
        "description": ("Propose replacing ONE section's body, keeping its heading and the "
                        "rest of the page untouched. Prefer this over propose_edit whenever "
                        "you are changing part of a page: you send only the new section, and "
                        "the human reviews a small diff. Call get_outline first to see the "
                        "section names. " + _PROPOSAL_NOTE),
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string", "description": "The page to edit."},
            "section_heading": {"type": "string", "description": "Heading of the section to replace, e.g. 'Overview' or '## Overview'. Use '(top)' for the text above the first heading."},
            "new_content": {"type": "string", "description": "The replacement markdown for that section's body. Do NOT repeat the heading line."},
            "section_index": {"type": "integer", "description": "Outline index from get_outline, to disambiguate when several sections share a heading."},
            "note": {"type": "string", "default": "", "description": "Short reviewer-facing reason for the proposal."},
        }, "required": ["doc_id", "section_heading", "new_content"]},
    }},
    {"type": "function", "function": {
        "name": "propose_section_insert",
        "description": ("Propose adding a NEW section to an existing page, positioned relative "
                        "to a section that is already there (or at the end if you give no "
                        "reference_section). " + _PROPOSAL_NOTE),
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string", "description": "The page to add a section to."},
            "heading": {"type": "string", "description": "Heading for the new section; a bare title becomes a '##' heading."},
            "content": {"type": "string", "description": "Markdown body of the new section."},
            "position": {"type": "string", "default": "after", "description": "'before' or 'after' the reference_section."},
            "reference_section": {"type": "string", "default": "", "description": "Heading of the existing section to position against. Omit to add at the end of the page."},
            "reference_section_index": {"type": "integer", "description": "Outline index of the reference section, to disambiguate duplicate headings."},
            "note": {"type": "string", "default": "", "description": "Short reviewer-facing reason for the proposal."},
        }, "required": ["doc_id", "heading", "content"]},
    }},
    {"type": "function", "function": {
        "name": "propose_section_delete",
        "description": ("Propose removing a whole section - heading and body - from a page. "
                        "Any deeper sub-sections nested under it go too. " + _PROPOSAL_NOTE),
        "parameters": {"type": "object", "properties": {
            "doc_id": {"type": "string", "description": "The page to remove a section from."},
            "section_heading": {"type": "string", "description": "Heading of the section to delete."},
            "section_index": {"type": "integer", "description": "Outline index, to disambiguate when several sections share a heading."},
            "note": {"type": "string", "default": "", "description": "Short reviewer-facing reason for the proposal."},
        }, "required": ["doc_id", "section_heading"]},
    }},
    {"type": "function", "function": {
        "name": "remove_wikilink",
        "description": ("Propose removing a wikilink from source_doc - the counterpart to "
                        "apply_wikilink, for pruning a link structure rather than only growing "
                        "it. Only removes list bullets whose sole content is that link; a link "
                        "written into a sentence is reported and left alone. " + _PROPOSAL_NOTE),
        "parameters": {"type": "object", "properties": {
            "source_doc": {"type": "string", "description": "doc_id of the page to remove the link FROM."},
            "target_doc": {"type": "string", "description": "doc_id the link points at."},
            "reason": {"type": "string", "default": "", "description": "Short reason the link should go."},
        }, "required": ["source_doc", "target_doc"]},
    }},
    {"type": "function", "function": {
        "name": "remember",
        "description": (
            "Record items to a named append-only ledger that survives every future run. "
            "Use this for facts you must never lose - topics you have already published, "
            "pages you have already processed. Your memory note is rewritten each run and "
            "cannot be trusted to carry a growing list; a ledger can. Name the ledger "
            "yourself, in plain words describing what it tracks; reuse the same name to "
            "add to it. Rows are deduplicated for you, so re-recording is harmless. "
            "Call this AS SOON AS you decide something, not at the end of the run."),
        "parameters": {"type": "object", "properties": {
            "ledger": {"type": "string", "description":
                       "Ledger name, e.g. 'Topics covered'. Reuse the exact name to append."},
            "items": {"type": "array", "items": {"type": "string"},
                      "description": "One entry per item. Short, one line each."},
        }, "required": ["ledger", "items"]},
    }},
    {"type": "function", "function": {
        "name": "forget",
        "description": (
            "Delete an entire ledger that has served its purpose. Individual rows can "
            "never be removed - only the whole ledger - because deciding a single row no "
            "longer matters is exactly the judgement that goes wrong. Use this when the "
            "work the ledger tracked is finished."),
        "parameters": {"type": "object", "properties": {
            "ledger": {"type": "string", "description": "Name of the ledger to delete."},
        }, "required": ["ledger"]},
    }},
    {"type": "function", "function": {
        "name": "recall",
        "description": (
            "Read the rows of one of your own ledgers. Use this whenever a ledger in "
            "your prompt is marked as only partly shown - those rows exist and you "
            "cannot see them, so check here before concluding something is not "
            "recorded. Call it with no ledger name to list the ledgers you have."),
        "parameters": {"type": "object", "properties": {
            "ledger": {"type": "string", "default": "",
                       "description": "Name of the ledger to read; omit to list the ledgers you have."},
            "max_rows": {"type": "integer",
                         "description": "How many rows to return; a longer ledger keeps its newest rows and says how many it hid.",
                         "default": RECALL_DEFAULT_ROWS,
                         "minimum": RECALL_MIN_ROWS, "maximum": RECALL_MAX_ROWS},
        }, "required": []},
    }},
]

_GENERIC_FNS = {
    "search_wiki": search_wiki,
    "find_related": find_related,
    "list_documents": list_documents,
    "read_document": read_document,
    "get_outline": get_outline,
    "apply_wikilink": apply_wikilink,
    "remove_wikilink": remove_wikilink,
    "propose_create": propose_create,
    "propose_edit": propose_edit,
    "propose_append": propose_append,
    "propose_section_edit": propose_section_edit,
    "propose_section_insert": propose_section_insert,
    "propose_section_delete": propose_section_delete,
    "remember": remember,
    "forget": forget,
    "recall": recall,
}


# ---------------------------------------------------------------------------
# Registry + dispatcher
# ---------------------------------------------------------------------------

_registry_cache: dict | None = None


def _registry() -> dict:
    """name -> {"def": <ollama tool schema>, "fn": <sync callable(vault_id, **kw)>}."""
    global _registry_cache
    if _registry_cache is None:
        from src import vault_analysis as va
        reg = {}
        for td in va.ANALYSIS_TOOL_DEFINITIONS:
            reg[td["function"]["name"]] = {"def": td, "fn": getattr(va, td["function"]["name"])}
        for td in GENERIC_TOOL_DEFINITIONS:
            name = td["function"]["name"]
            reg[name] = {"def": td, "fn": _GENERIC_FNS[name]}
        _registry_cache = reg
    return _registry_cache


def build_capability_map() -> dict:
    """The agent_registry-facing map: name -> {"def", "execute"} where execute
    follows the background_agents.ExecuteTool contract."""
    return {name: {"def": spec["def"], "execute": execute_capability}
            for name, spec in _registry().items()}


async def execute_capability(name: str, args: dict, vault_id: str,
                             status_callback=None) -> str:
    """Schema-validated dispatch of one capability call (sync work off-loop)."""
    spec = _registry().get(name)
    if spec is None:
        return f"Unknown capability: {name}"
    if status_callback is not None:
        try:
            await status_callback(f"Running {name}…")
        except Exception:
            pass
    kwargs, errors = _validate_args(spec["def"], args or {})
    if errors:
        return f"{name}: " + "; ".join(errors)
    try:
        # Most capabilities are sync (DB / filesystem) and belong off the loop.
        # A few are natively async - the ledger writers await a git commit - and
        # must be awaited directly rather than handed to a worker thread.
        if asyncio.iscoroutinefunction(spec["fn"]):
            result = await spec["fn"](vault_id, **kwargs)
        else:
            result = await asyncio.to_thread(spec["fn"], vault_id, **kwargs)
    except Exception as e:
        logger.exception("capability %s failed", name)
        return f"{name}: error - {e}"
    return result if isinstance(result, str) else _format_rows(name, result)
