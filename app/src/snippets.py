# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Plain-text excerpt extraction for search results.

`make_snippet` turns a raw document (or RAG chunk) into a single line of readable
prose centered on the query. Two properties are load-bearing:

* The output is PLAIN TEXT - markdown syntax, comments and raw HTML are all
  removed. A vault note may legitimately contain `<div markdown="1" style=...>`
  two-column layouts; pasting a slice of one into the results page hands the
  document control of the page structure.
* Cleaning happens BEFORE truncation. Cutting first is what lets a slice end
  mid-tag or mid-autolink, leaving a dangling `<` for the browser to chew on.

Callers still escape at the output boundary; this returns text, not markup.

Every rule that already has an owner is IMPORTED, not re-derived: fences from
md_sections, link/heading/list syntax from chunker's constants block, comments
from agent_registry, frontmatter from WikiDoc. What is left below is display-only
flattening (rules, blockquotes, emphasis, tags) that nothing else needs.
"""

import re

from src.agent_registry import strip_comments
# _fence_info is private but imported rather than re-derived, the same way
# md_sections takes it - a local fence regex is the drift this avoids.
from src.chunker import (EMBED_RE, MD_HEADER, MD_LINK_LABEL_RE,
                         MD_LIST_MARKER_RE, WIKILINK_RE, _fence_info)
from src.md_sections import fence_line_indices
from src.wikidoc import WikiDoc

DEFAULT_SNIPPET_CHARS = 200

# How far a window edge may travel to land on a space rather than mid-word.
_SNAP_CHARS = 20

_HR_LINE = re.compile(r"^[ \t]*(?:-{3,}|\*{3,}|_{3,})[ \t]*$")
# Indent, then any run of blockquote/list markers - `> - item` is one prefix.
_LINE_PREFIX = re.compile(rf"^[ \t]*(?:>[ \t]?|{MD_LIST_MARKER_RE})+")
_HEADER = re.compile(MD_HEADER)
_EMBED = re.compile(EMBED_RE)
_WIKILINK = re.compile(WIKILINK_RE)
_MD_LINK = re.compile(MD_LINK_LABEL_RE)
_AUTOLINK = re.compile(r"<((?:https?://|mailto:)[^>\s]+)>")
_HTML_TAG = re.compile(r"<[^>]*>")
_EMPHASIS = re.compile(r"\*\*|__|~~|`+|\*")
_WHITESPACE = re.compile(r"\s+")


def _flatten_lines(body: str, keep_code: bool = False) -> str:
    """Drop rules and fenced code, strip leading heading/quote/list markers.

    Fence DELIMITERS are dropped either way; `keep_code` only decides whether the
    code between them counts as excerptable text. See `make_snippet` on why the
    default is no.
    """
    lines = body.split("\n")
    fenced = fence_line_indices(lines)
    kept = []
    for n, line in enumerate(lines):
        if n in fenced and (not keep_code or _fence_info(line)[0] >= 3):
            continue
        if _HR_LINE.match(line):
            continue
        header = _HEADER.match(line)
        kept.append(header.group(1) if header else _LINE_PREFIX.sub("", line))
    return "\n".join(kept)


def _flatten_inline(body: str) -> str:
    """Reduce inline markdown and HTML to the words a reader would see."""
    body = _EMBED.sub("", body)                  # before the wikilink rule
    body = _WIKILINK.sub(r"\1", body)            # target text; alias is dropped
    body = _MD_LINK.sub(r"\1", body)
    body = _AUTOLINK.sub(r"\1", body)            # before the tag rule
    body = _HTML_TAG.sub(" ", body)
    return _EMPHASIS.sub("", body)


def _window(text: str, query: str, max_chars: int) -> str:
    """Return up to `max_chars` of `text` centered on `query`, elided with `…`."""
    if len(text) <= max_chars:
        return text

    start = 0
    if query:
        m = re.search(re.escape(query), text, re.IGNORECASE)
        if m:
            center = (m.start() + m.end()) // 2
            start = max(0, center - max_chars // 2)
    end = min(len(text), start + max_chars)
    start = max(0, end - max_chars)

    if start > 0:
        space = text.find(" ", start, start + _SNAP_CHARS)
        if space != -1:
            start = space + 1
    if end < len(text):
        space = text.rfind(" ", end - _SNAP_CHARS, end)
        if space > start:
            end = space

    snippet = text[start:end].strip()
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _prepare(body: str, keep_code: bool) -> str:
    """Frontmatter-free document body -> one line of plain text."""
    body = _flatten_lines(body, keep_code)   # fences go first: strip_comments is
    body = strip_comments(body)              # not fence-aware, so `%%` in code
    body = _flatten_inline(body)             # must already be gone
    return _WHITESPACE.sub(" ", body).strip()


def make_snippet(text: str, query: str = "", max_chars: int = DEFAULT_SNIPPET_CHARS) -> str:
    """Build a one-line plain-text excerpt of `text` around `query`.

    `text` may be a whole file (frontmatter and all) or a RAG chunk; both search
    paths share this so they cannot drift into different truncation conventions.
    Falls back to the head of the document when `query` does not appear literally,
    which is the normal case for a semantic match.
    """
    if not text:
        return ""
    body = WikiDoc.strip_frontmatter(text)
    prose = _prepare(body, keep_code=False)
    # Code is noise next to prose, but a page that is ONLY a code block (help/code,
    # a notebook page) would otherwise get a blank snippet and look like a bad hit.
    return _window(prose or _prepare(body, keep_code=True), query, max_chars)
