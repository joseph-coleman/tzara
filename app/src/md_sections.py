# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Canonical markdown BODY-STRUCTURE primitives: headings, sections, blocks.

This module is THE home for "where does section X start and end, and how do I
splice it" - the same way WikiDoc's @staticmethod block (wikidoc.py) is THE home
for document I/O. Before it existed the answer was implemented three times, in
three modules, with three regexes, and one of them had silently forgotten to
skip code fences. Anything that needs to find or rewrite a heading section
belongs here; adding a fourth local `^#{1,6}` regex somewhere else is the bug
this module exists to prevent.

Everything here is PURE: str in, str/list out. No filesystem, no database, no
config import, no vault concept. That is deliberate - it is what lets chat.py,
agent_capabilities.py, edit_assist.py and markdown_extensions.py all depend on
it without dragging each other's machinery along.

Two addressing schemes, matching the two the wiki already exposes to users:
- SECTIONS - `parse_sections` returns character offsets, `lookup_section`
  resolves a heading name (or outline index) to one. Used by chat's section
  tools, the agent `propose_section_*` capabilities, and outline rendering.
- BLOCK REFS - `extract_block_ref` handles Obsidian's `^blockid` trailing
  marker. Used by `![[Page#^id]]` transclusion.

Code fences, YAML frontmatter and `$$`/`\\[` LaTeX blocks are all skipped when
locating headings, so a `## Heading` inside a ```python block is never mistaken
for document structure.
"""

import re

# The one fence detector. Shared with chunker/agent_registry/markdown_extensions
# rather than re-derived here - see the module docstring on why a local copy is
# exactly the failure mode this module prevents.
from src.chunker import _fence_info

_MD_HEADER_RE = re.compile(r'^(#{1,6})\s+(.+)')

# Aliases for the level-0 "content before the first heading" pseudo-section.
_TOP_ALIASES = {"(content before first heading)", "(top)"}


def fence_line_indices(lines: list[str]) -> set[int]:
    """Indices of lines belonging to a CONFIRMED fenced code block.

    Two rules, both chosen to match what this wiki's renderer actually does
    (python-markdown's `fenced_code`, verified empirically - not inherited from
    a spec, because reinterpreting markdown after the fact would change how
    already-written documents parse):

    1. A fence closes only on the SAME fence character at the SAME length.
       A longer closer does NOT close it. (CommonMark says a longer closer
       should close - python-markdown disagrees, and the renderer wins. This
       also keeps a ```` inside a ``` block from ending it early, which is the
       nesting case that matters here.)
    2. An opener with no matching closer is NOT A FENCE. Its line is ordinary
       text and scanning continues. python-markdown emits no code block at all
       for an unterminated fence, so the rest of the document keeps rendering
       as markdown - a single-pass state machine instead swallows everything
       after it, silently hiding every later heading.

    Rule 2 is why this is two passes: you cannot know an opener was real until
    you find its closer.
    """
    inside: set[int] = set()
    n = len(lines)
    i = 0
    while i < n:
        fc, fchar = _fence_info(lines[i])
        if fc < 3:
            i += 1
            continue
        # ``` foo ``` on one line is an inline code span, not a fence opener.
        remainder = lines[i][fc:]
        if fchar == '`' and ('`' * fc) in remainder:
            i += 1
            continue
        close = None
        for j in range(i + 1, n):
            fc2, fchar2 = _fence_info(lines[j])
            if fc2 >= 3 and fchar2 == fchar and fc2 == fc:
                close = j
                break
        if close is None:
            i += 1              # unterminated - treat the opener as plain text
        else:
            inside.update(range(i, close + 1))
            i = close + 1
    return inside


# A block-comment delimiter is `%%` alone on a line. Trailing `\r` counts as
# blank: the renderer only ever sees text already normalized by python-markdown's
# normalize_whitespace, but the chunker reads raw file bytes and a good number of
# vault documents are CRLF. Anchoring at column 0 also means an indented
# (4-space) code block's `%%` is never mistaken for a delimiter.
_COMMENT_MARKER_RE = re.compile(r'^%%[ \t\r]*$')


def comment_line_indices(lines: list[str]) -> set[int]:
    """Indices of lines inside an Obsidian `%%` BLOCK comment, markers included.

    Deliberately the same two-pass shape - and the same two rules - as
    `fence_line_indices`, because `%%` and a code fence are the same parsing
    problem: a paired whole-line delimiter you cannot resolve until you have
    looked ahead for the closer.

    1. A `%%` inside a confirmed fenced code block is EXAMPLE TEXT, not a
       delimiter, so a page documenting comment syntax still shows it.
    2. An opener with no matching closer is NOT A COMMENT. Its line is ordinary
       text and scanning continues.

    Rule 2 is the interesting one, and it is a judgement call rather than a spec:
    the alternative (swallow to end-of-document, as a single-pass toggle does) is
    defensible as a safety net for someone whose comment was meant to hide
    something. It loses on feedback. There is no syntax checking in markdown, so
    the rendered page IS the only signal an author gets - and if the renderer and
    the index agree, that page honestly reports what the whole system thinks the
    document says, which the author can then correct in one edit. Swallowing
    silently indexes nothing after a typo'd marker and shows nothing either, so
    the mistake has no symptom to notice. Same conclusion `fence_line_indices`
    reached above for an unterminated fence.

    The INLINE form (`text %%note%% text`) is not handled here - it never spans
    lines, so the renderer's inline pattern and agent_registry.strip_comments
    both cover it with a plain non-greedy regex.
    """
    fenced = fence_line_indices(lines)
    inside: set[int] = set()
    n = len(lines)
    i = 0
    while i < n:
        if i in fenced or not _COMMENT_MARKER_RE.match(lines[i]):
            i += 1
            continue
        close = None
        for j in range(i + 1, n):
            if j not in fenced and _COMMENT_MARKER_RE.match(lines[j]):
                close = j
                break
        if close is None:
            i += 1              # unterminated - treat the opener as plain text
        else:
            inside.update(range(i, close + 1))
            i = close + 1
    return inside


def strip_comment_blocks(body: str) -> str:
    """Remove whole-line `%%` ... `%%` comment blocks from `body`.

    Lines are DELETED rather than blanked, so a comment behaves as though it was
    never typed: blank lines the author put AROUND the block still separate the
    surrounding paragraphs, and a comment tucked between two list items does not
    split the list in half.

    Shared by the renderer (ObsidianCommentExtension's preprocessor) and the RAG
    chunker so the page and the index cannot disagree about what is a comment -
    see `comment_line_indices` on why they must not.
    """
    lines = body.split("\n")
    skip = comment_line_indices(lines)
    if not skip:
        return body
    return "\n".join(line for n, line in enumerate(lines) if n not in skip)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_sections(document: str) -> list[dict]:
    """Parse markdown into sections delimited by headings.

    Returns an ordered list of section dicts with keys:
        heading, heading_text, level, content_start, content_end, index

    `content_start` is the offset just AFTER the heading line; use
    `section_start` if you need the offset of the heading itself. A section
    ends at the next heading of equal-or-higher level (i.e. `## A` contains any
    `### B` beneath it), or at end of document.

    Content before the first heading - if any - is returned as a synthetic
    level-0 section named `(top)`, so every byte of the body is addressable.
    """
    sections: list[dict] = []
    lines = document.split('\n')

    in_latex_block = False

    # Skip YAML frontmatter
    content_start_offset = 0  # where content begins (after frontmatter)

    if document.startswith('---\n'):
        close = document.find('\n---', 4)
        if close != -1:
            # Frontmatter ends after the closing --- and its newline
            fm_end = close + 4  # len('\n---')
            if fm_end < len(document) and document[fm_end] == '\n':
                fm_end += 1
            content_start_offset = fm_end

    # Build line start offsets
    line_starts: list[int] = []
    offset = 0
    for line in lines:
        line_starts.append(offset)
        offset += len(line) + 1  # +1 for newline

    # Find all headers (respecting fences and LaTeX)
    headers: list[dict] = []  # {line_idx, offset, level, heading, heading_text}
    fenced = fence_line_indices(lines)

    for i, line in enumerate(lines):
        char_offset = line_starts[i]

        # Skip lines inside frontmatter
        if char_offset < content_start_offset:
            continue

        # Inside a confirmed fenced code block - see fence_line_indices.
        if i in fenced:
            continue

        # LaTeX block tracking
        stripped = line.rstrip()
        if re.match(r'^\$\$\s*$', line):
            in_latex_block = not in_latex_block
            continue
        if stripped == '\\[' and not in_latex_block:
            in_latex_block = True
            continue
        if stripped == '\\]' and in_latex_block:
            in_latex_block = False
            continue

        if in_latex_block:
            continue

        # Header detection
        m = _MD_HEADER_RE.match(line)
        if m:
            level = len(m.group(1))
            heading_text = m.group(2).strip()
            # Strip trailing # marks
            trailing = re.search(r'\s+#+\s*$', heading_text)
            if trailing:
                heading_text = heading_text[:trailing.start()].strip()

            headers.append({
                'line_idx': i,
                'offset': char_offset,
                'level': level,
                'heading': line.rstrip(),
                'heading_text': heading_text,
            })

    # Build sections from headers
    section_idx = 0

    # Content before first header (if any)
    first_header_offset = headers[0]['offset'] if headers else len(document)
    if first_header_offset > content_start_offset:
        sections.append({
            'heading': '(top)',
            'heading_text': '(top)',
            'level': 0,
            'content_start': content_start_offset,
            'content_end': first_header_offset,
            'index': section_idx,
        })
        section_idx += 1

    for hi, hdr in enumerate(headers):
        # Content starts after the heading line + newline
        heading_line_end = hdr['offset'] + len(hdr['heading']) + 1  # +1 newline
        if heading_line_end > len(document):
            heading_line_end = len(document)

        # Content ends at the next heading of equal or higher (lower number)
        # level, or EOF
        content_end = len(document)
        for nhi in range(hi + 1, len(headers)):
            if headers[nhi]['level'] <= hdr['level']:
                content_end = headers[nhi]['offset']
                break

        sections.append({
            'heading': hdr['heading'],
            'heading_text': hdr['heading_text'],
            'level': hdr['level'],
            'content_start': heading_line_end,
            'content_end': content_end,
            'index': section_idx,
        })
        section_idx += 1

    return sections


def lookup_section(sections: list[dict], heading: str,
                   index: int | None = None) -> dict | None:
    """Find a section by heading text or index. Returns a section dict or None.

    Resolution order: exact heading_text -> case-insensitive -> disambiguate
    duplicates by `index` -> bare `index` lookup. Leading/trailing `#` marks in
    `heading` are tolerated, as are the `(top)` aliases for the level-0 section.
    """
    if not sections:
        return None

    # Strip leading/trailing # marks from heading input
    stripped = re.sub(r'^#+\s*', '', heading or '').strip()
    stripped = re.sub(r'\s+#+\s*$', '', stripped).strip()

    # Match aliases for the level-0 "content before first heading" section
    if stripped.lower() in {a.lower() for a in _TOP_ALIASES}:
        for s in sections:
            if s['level'] == 0:
                return s

    # Try exact match on heading_text
    matches = [s for s in sections if s['heading_text'] == stripped]
    if len(matches) == 1:
        return matches[0]

    # Try case-insensitive match
    if not matches:
        lower = stripped.lower()
        matches = [s for s in sections if s['heading_text'].lower() == lower]
        if len(matches) == 1:
            return matches[0]

    # Disambiguate by index
    if len(matches) > 1 and index is not None:
        for s in matches:
            if s['index'] == index:
                return s

    # Direct index lookup as fallback
    if index is not None:
        for s in sections:
            if s['index'] == index:
                return s

    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def build_outline(sections: list[dict]) -> str:
    """Indented `[index] heading` outline - the form the chat/agent tools show
    a model so it can address a section by name or number."""
    if not sections:
        return ""
    lines = []
    for s in sections:
        indent = "  " * max(0, s['level'] - 1)
        if s['level'] == 0:
            lines.append(f"[{s['index']}] (content before first heading)")
        else:
            lines.append(f"[{s['index']}] {indent}{s['heading']}")
    return "\n".join(lines)


def describe_sections(sections: list[dict]) -> str:
    """One-line `[0] # Title, [1] ## Overview` summary, for the "section not
    found" message. A small model recovers from a listed alternative far more
    reliably than from a bare error, so every miss should return this."""
    return ", ".join(f"[{s['index']}] {s['heading']}" for s in sections)


# ---------------------------------------------------------------------------
# Splicing
#
# All of these take a section dict from parse_sections() against the SAME
# document string, and return a new document. Offsets are invalidated by any
# edit, so re-parse between successive splices.
#
# Every splice joins its fragments through `_join`, which normalizes the SEAM to
# exactly one blank line. Without it each operation left a scar: insert produced
# two blank lines before the new heading and none after it, delete left the next
# heading jammed against the previous paragraph, and replace dropped the blank
# line under the heading it preserved. Markdown still rendered, but the source
# degraded a little on every agent edit - which is the whole thing these tools
# exist not to do.
# ---------------------------------------------------------------------------

def _join(before: str, after: str) -> str:
    """Concatenate two document fragments with exactly one blank line between.

    An empty side is returned as-is, so joining at the very start or end of a
    document never introduces leading/trailing blank lines.
    """
    if not before.strip():
        return after.lstrip("\n")
    if not after.strip():
        return before
    return before.rstrip("\n") + "\n\n" + after.lstrip("\n")


def _end_document(text: str) -> str:
    """Documents end with exactly one newline (and an empty one stays empty)."""
    return text.rstrip("\n") + "\n" if text.strip() else ""

def section_start(document: str, section: dict) -> int:
    """Offset of the section's HEADING line (vs. `content_start`, which is the
    offset just after it). The level-0 `(top)` section has no heading, so its
    start is its content_start."""
    if section['level'] == 0:
        return section['content_start']
    # The heading line plus its newline precede content_start.
    return section['content_start'] - (len(section['heading']) + 1)


def section_body(document: str, section: dict) -> str:
    """The section's content, excluding its heading line."""
    return document[section['content_start']:section['content_end']]


def section_slice(document: str, section: dict) -> str:
    """The whole section INCLUDING its heading line."""
    return document[section_start(document, section):section['content_end']]


def replace_section(document: str, section: dict, new_body: str) -> str:
    """Replace a section's body, preserving its heading line.

    The body is re-seated with one blank line under the heading and one before
    whatever follows, so a caller may pass bare text without worrying about
    surrounding whitespace. An empty body leaves the heading in place.
    """
    head = document[:section['content_start']]     # ends with the heading line
    tail = document[section['content_end']:]
    return _end_document(_join(_join(head, new_body.strip()), tail))


def delete_section(document: str, section: dict) -> str:
    """Remove a section entirely - heading line and body.

    The gap closes to a single blank line, so the following heading keeps its
    separation from the preceding paragraph.
    """
    head = document[:section_start(document, section)]
    tail = document[section['content_end']:]
    return _end_document(_join(head, tail))


def insert_section(document: str, heading: str, body: str, *,
                   reference: dict | None = None,
                   position: str = "after") -> str:
    """Insert a new `heading` + `body` section.

    `reference` is a section dict from the same document; `position` is
    "before" or "after" it. With no reference the section is appended at EOF.
    A bare `heading` (no leading `#`) is promoted to `##`.

    The new section is separated from its neighbours by exactly one blank line
    on each side, wherever it lands.
    """
    if not heading.startswith('#'):
        heading = f"## {heading}"

    block = _join(heading, body.strip())

    if reference is None:
        insert_pos = len(document)
    elif position == "before":
        insert_pos = section_start(document, reference)
    else:
        insert_pos = reference['content_end']

    head, tail = document[:insert_pos], document[insert_pos:]
    return _end_document(_join(_join(head, block), tail))


# ---------------------------------------------------------------------------
# Block references (`^blockid`)
# ---------------------------------------------------------------------------

def iter_unfenced_lines(body: str):
    """Yield `(line_no, line)` for lines NOT inside a code fence.

    Line-oriented counterpart to parse_sections' fence tracking, for callers
    that walk raw lines rather than character offsets.
    """
    lines = body.split("\n")
    fenced = fence_line_indices(lines)
    for n, line in enumerate(lines):
        if n not in fenced:
            yield n, line


def extract_block_ref(body: str, block_id: str) -> str | None:
    """Slice out the block ending with a trailing `` ^block_id`` marker.

    A block is the run of contiguous non-blank lines ending at the marker; the
    marker itself is stripped from the returned text. A heading is its own
    block, so the walk-back never absorbs the heading above. Returns None if
    the marker is absent.
    """
    marker = re.compile(r"\s*\^" + re.escape(block_id) + r"\s*$")
    heading_re = re.compile(r"^#{1,6}\s")
    lines = body.split("\n")
    for n, line in iter_unfenced_lines(body):
        if marker.search(line):
            start = n
            while (
                start > 0
                and lines[start - 1].strip()
                and not heading_re.match(lines[start - 1])
            ):
                start -= 1
            block = lines[start:n + 1]
            block[-1] = marker.sub("", block[-1])
            return "\n".join(block).rstrip()
    return None
