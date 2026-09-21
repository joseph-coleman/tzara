# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Canonical raw-markdown SYNTAX: what a wikilink, heading or formula LOOKS like.

This module is THE home for the grammar itself. It exists because the patterns
below had accumulated in `chunker.py` -- a module named for something else
entirely -- and three others were reaching into its privates (`_fence_info`) for
want of a better address. A local `^#{1,6}` or `\\[\\[..\\]\\]` regex anywhere else
is the drift this module prevents: two copies of a wikilink pattern don't fail,
they quietly disagree on edge cases.

The ownership split across the markdown modules:

- **md_syntax** (here) -- GRAMMAR. What the markup looks like, plus the helpers
  that operate on a pattern (compile it code-span-aware, strip a region).
- **md_sections** -- body STRUCTURE. Where a section starts and ends, how to
  splice it.
- **chunker** -- CHUNKING, extraction and wikilink RESOLUTION. Turning grammar
  matches into edges, tags and embeddable pieces.
- **markdown_extensions** -- RENDERING. Turning grammar matches into HTML.

Everything here is PURE: str in, str/pattern out. Imports are `re` and the
`fenced_block` scanner, nothing more -- no config, no filesystem, no vault
concept. That is deliberate, and it is what lets every one of the modules above
depend on this one without dragging each other's machinery along.

Patterns are exported as raw STRINGS, not compiled, because callers compose them
(`rf"^[ \\t]*(?:>[ \\t]?|{MD_LIST_MARKER_RE})+"`) and need different flags.
"""

import re

from src.fenced_block import replace_top_level_fences


# --------------------------------------------------------------------------
# Inline / link grammar
# --------------------------------------------------------------------------

# Wikilink, capturing the TARGET only: `[[Page|alias]]` yields "Page".
WIKILINK_RE = r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]"

# Same syntax, capturing the WHOLE inner text including any `|alias`. The
# renderer needs this because it splits the alias itself downstream (it has to
# emit the alias as the link text), while everything else wants the target.
# Two patterns on purpose -- keeping them distinct is what makes the difference
# visible instead of an accident waiting to be "fixed".
WIKILINK_INNER_RE = r"\[\[([^\]]+)\]\]"

# Obsidian embed: `![[Target]]`.
EMBED_RE = r"!\[\[([^\]]+)\]\]"

# An embed ALONE on its line, which is what the block-level processors
# (transclusion, canvas, attachment preview) claim. Compile with re.MULTILINE.
STANDALONE_EMBED_RE = r"^[ \t]*!\[\[([^\]\n]+?)\]\][ \t]*$"

# Standard markdown link target: [label](target) and image ![alt](target). Captures
# the target up to whitespace or the closing paren, tolerating an optional <...> wrap.
MD_LINK_TARGET_RE = r"!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?[^)]*\)"

# Same syntax, opposite capture: the visible LABEL rather than the destination, for
# callers flattening markdown to the words a reader sees. Not image-tolerant on
# purpose -- an image's alt text is not prose (see EMBED_RE for the wiki spelling).
MD_LINK_LABEL_RE = r"\[([^\]]*)\]\([^)]*\)"

# Inline #tag. Must start with a letter so `#1` and a bare `#` are not tags.
TAG_RE = r"(?:^|\s)#([a-zA-Z][a-zA-Z0-9_/-]*)"


# --------------------------------------------------------------------------
# Block grammar
# --------------------------------------------------------------------------

# ATX heading. Groups are NAMED, not positional, and that is load-bearing: this
# pattern replaced two spellings whose group(1) meant OPPOSITE things (chunker's
# was the heading text, md_sections' was the hash run). A positional unification
# would have silently swapped one caller's text for "###" with no error anywhere.
MD_HEADER_RE = r"^(?P<hashes>#{1,6})\s+(?P<text>.+)"

# The looser "is this line a heading at all" test: no capture, and it does not
# require text after the hashes. Structural walks want this; anything that needs
# the level or the title wants MD_HEADER_RE.
MD_HEADER_LINE_RE = r"^#{1,6}\s"

# Setext underlines, converted to ATX form before structural parsing.
SETEXT_HEADER1 = r"^=+\s*$"
SETEXT_HEADER2 = r"^-+\s*$"

# A list marker and the space after it: bullet (-, *, +) or ordered (1.). The
# paren form `1)` is deliberately absent -- verified against the renderer
# (python-markdown + sane_lists), which emits `<p>1) Item</p>`, not a list.
# Probably should update sane_lists to fix this gap.
# Unanchored so callers compose it -- match against an already-dedented line, or
# prefix `^[ \t]*` to take the indent too.
MD_LIST_MARKER_RE = r"(?:[-*+]|\d+\.)[ \t]+"

# The task checkbox that may follow a list marker: `- [ ]`, `- [x]`, `1. [X]`.
# Separate from the marker because it is not consumed with it -- no task-list
# extension is enabled, so the renderer emits `[x]` as LITERAL text inside the
# <li>, and anything showing list content to a human should show it too.
# Currently unreferenced: the live copies are edit_assist._CHECKBOX_PREFIX_RE and
# agent_capabilities._TASK_BOX_RE, left alone because consolidating them is a
# behavior change (agent_capabilities' list marker accepts `1)`), not a move.
MD_TASK_BOX_RE = r"\[[ xX]\][ \t]*"


# --------------------------------------------------------------------------
# Code regions
# --------------------------------------------------------------------------

# Inline code span: a run of N backticks closed by exactly N, not crossing a
# blank line (markdown's inline pass never pairs backticks across paragraphs).
# Shared with the renderer, which needs the same notion to keep `$` in `` `$` ``
# from opening a formula.
CODE_SPAN_RE = r"(?<!\\)(?P<fence>`+)(?:(?!\n[ \t]*\n).)+?(?<!`)(?P=fence)(?!`)"

# Groups the code-span alternative adds ahead of a caller's pattern: its own
# (?P<code>...) wrapper plus every group inside CODE_SPAN_RE. Derived rather than
# written as 2, so the caller's group numbers survive a change to that pattern.
CODE_ALT_GROUPS = re.compile(CODE_SPAN_RE).groups + 1


# --------------------------------------------------------------------------
# Math regions
# --------------------------------------------------------------------------

# LaTeX delimiters, each capturing the formula as `tex`. The renderer's math
# preprocessor compiles these (UnifiedMathPreprocessor) and link extraction strips
# them, so the page and the graph agree on where a formula starts and ends.
MATH_BLOCK_DOLLAR_RE = r"^\$\$\s*\n(?P<tex>.*?)\n\s*\$\$"
MATH_BLOCK_BRACKET_RE = r"^\s*\\\[\s*\n(?P<tex>.*?)\n\s*\\\]\s*$"
MATH_INLINE_DOUBLEDOLLAR_RE = (
    r"(?<!\\)(?<!\$)\$\$(?!\$)(?P<tex>.+?)(?<!\\)(?<!\$)\$\$(?!\$)"
)
# The (?!\d) guard keeps "$5 and $7" from opening a formula; see the inline-math
# digit rule the help docs state for authors.
MATH_INLINE_DOLLAR_RE = (
    r"(?<!\\)(?<!\$)\$(?!\$)(?!\d)(?P<tex>.+?)(?<!\\)(?<!\$)\$(?!\$)"
)
MATH_INLINE_PAREN_RE = r"(?<!\\)\\\((?P<tex>.+?)\\\)"
MATH_INLINE_BRACKET_RE = r"(?<!\\)\\\[(?P<tex>.+?)\\\]"

# (pattern, flags) in the order the renderer applies them. ORDER IS LOAD-BEARING:
# `$$` must be claimed before a single `$` can pair with one of its delimiters,
# and block forms before their inline spellings.
MATH_SPAN_PATTERNS = (
    (MATH_BLOCK_DOLLAR_RE, re.MULTILINE),
    (MATH_BLOCK_BRACKET_RE, re.MULTILINE),
    (MATH_INLINE_DOUBLEDOLLAR_RE, 0),
    (MATH_INLINE_DOLLAR_RE, 0),
    (MATH_INLINE_PAREN_RE, 0),
    (MATH_INLINE_BRACKET_RE, 0),
)

# A `$$` delimiter ALONE on its line. The line-scanners (chunker's chunk split,
# md_sections' structure walk) toggle on this rather than matching a whole block,
# so they need the line form, not MATH_BLOCK_DOLLAR_RE.
MATH_BLOCK_DOLLAR_LINE_RE = r"^\$\$\s*$"


# --------------------------------------------------------------------------
# Comments
# --------------------------------------------------------------------------

# The two comment syntaxes this wiki already hides from a READER: Obsidian's
# `%%` (ObsidianCommentExtension, and skipped wholesale by the RAG chunker) and
# raw HTML comments. Both spellings are matched non-greedily and DOTALL, so the
# inline (`%% note %%`) and block (`%%` on its own line) forms are one rule.
INLINE_COMMENT_RE = re.compile(r"%%.*?%%|<!--.*?-->", re.S)


def strip_comments(text: str) -> str:
    """Drop `%% ... %%` and `<!-- ... -->` from text that becomes an LLM prompt.

    Comments are this file format's authoring-notes channel: invisible on the
    rendered page, visible while editing. That is only a safe channel if they
    are invisible to the MODEL too - otherwise the starter template's own
    scaffolding is read as part of the directive by every author who didn't
    delete it, and any note left in a `# Prompt` section becomes silent prompt
    contamination that nothing in the UI would show. A note that vanishes from
    the page, from RAG, and from the prompt is one consistent rule an author can
    hold in their head.

    Applied to the directive/kickoff text ONLY. Fenced python (py_source),
    frontmatter, and anything an agent writes are untouched - the cost of the
    rule is that a prompt cannot ask for a literal `%%`/`<!-- -->` sequence,
    which is a fair trade for "notes in this file never reach the model".

    NOT fence-aware: a `%%` shown inside a code fence is stripped like any other.
    Drop fenced lines first if that matters. The fence-aware BLOCK-form stripper
    is md_sections.strip_comment_blocks, which is a different rule for a
    different job (it honors "an unterminated opener is not a delimiter").
    """
    out = INLINE_COMMENT_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


# --------------------------------------------------------------------------
# Pattern-level helpers
# --------------------------------------------------------------------------

def _backtick_count(line):
    m = re.match(r"^`+", line)
    return len(m.group()) if m else 0


def _tilde_count(line):
    m = re.match(r"^~+", line)
    return len(m.group()) if m else 0


def fence_info(line):
    """Check if a line starts a code fence (``` or ~~~).
    Returns (count, char) or (0, None).

    THE fence detector. Imported by every line-scanner in the repo rather than
    re-derived -- a local fence regex is exactly the drift this module prevents.
    """
    bc = _backtick_count(line)
    if bc >= 3:
        return bc, "`"
    tc = _tilde_count(line)
    if tc >= 3:
        return tc, "~"
    return 0, None


def math_span_re(pattern, flags=0):
    """Compile a math pattern with an inline-code-span alternative ahead of it.

    Math is resolved before markdown's inline pass has claimed code spans, so `$`
    in `` `$` `` would otherwise open a formula. As one regex the leftmost of code
    span or formula wins, matching a real inline parser. A match is math only when
    the `code` group is empty.
    """
    return re.compile(rf"(?P<code>{CODE_SPAN_RE})|{pattern}", flags | re.DOTALL)


_MATH_SPAN_RES = tuple(math_span_re(p, f) for p, f in MATH_SPAN_PATTERNS)


def without_fenced_code(text):
    """`text` with top-level fenced code blocks dropped.

    Link extraction has to agree with the renderer, which never linkifies inside a
    fence. replace_top_level_fences is the shared scanner, so a fence nested in a
    longer outer fence (the ```python inside `````markdown case) is treated as body
    text here exactly as it is at render time. Extraction needs no character
    offsets, so dropping the block outright is safe.
    """
    return replace_top_level_fences(text, lambda lang, body, block: "")


def without_math(text):
    """`text` with LaTeX formulas replaced by a space.

    Math is not text: `\\left[f(t)\\right](s)` is TeX sizing commands, but it also
    matches MD_LINK_TARGET_RE exactly, so without this the graph grows planned
    pages named `s` and `t`. The renderer stashes each formula as opaque HTML
    before any link processor runs and must not linkify inside one either.

    A space, not "", because removal happens mid-line: `[label]$x$(target)` is not
    a link on the page and must not become one by having its separator deleted.
    """
    for rx in _MATH_SPAN_RES:
        text = rx.sub(lambda m: m.group(0) if m.group("code") else " ", text)
    return text


def without_code_or_math(text):
    """What the link/embed extractors see. Fences go first: a `$$` displayed inside
    a ```markdown example is code, not math, and is already gone by then."""
    return without_math(without_fenced_code(text))


def finditer_outside_code(pattern, text):
    """Yield `pattern`'s matches in `text`, skipping anything inside an inline
    code span. Callers drop fenced blocks and math first (without_code_or_math).

    The code span is an ALTERNATIVE ahead of `pattern` in one regex, so the
    leftmost of the two wins -- the way markdown resolves it, its backticks
    processor claiming a span before the wikilink processor ever sees it. A match
    inside a span is consumed by the code branch and skipped.
    """
    rx = re.compile(rf"(?P<code>{CODE_SPAN_RE})|{pattern}", re.DOTALL)
    for m in rx.finditer(text):
        if m.group("code") is None:
            yield m


def findall_outside_code(pattern, text):
    """re.findall for a single-capture `pattern`, minus matches inside code."""
    return [m.group(1 + CODE_ALT_GROUPS)
            for m in finditer_outside_code(pattern, text)]
