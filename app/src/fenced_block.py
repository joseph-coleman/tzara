# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

# fenced_block.py
# Shared fence-aware scanner for preprocessors that rewrite specific code
# fences (jupyter cells, mermaid diagrams) BEFORE Python-Markdown's own
# fenced_code preprocessor runs.
#
# The naive `re.sub(r"```lang\n(.*?)\n```", ...)` approach those preprocessors
# used has no concept of an enclosing fence, so a ```python/```mermaid block
# nested inside a longer outer fence (e.g. `````markdown ... `````, used to SHOW
# a fenced example verbatim) was wrongly rewritten - mangling the outer block.
#
# This scanner mirrors fenced_code's nesting rule: a fence opened by a run of N
# backticks or tildes is closed only by a line consisting of that exact same run
# (plus optional trailing spaces), so a shorter inner fence is body text, never
# its own block.

import re

# Opening fence at column 0: a run of >=3 backticks or tildes, then an optional
# info string. fenced_code only opens fences at the start of a line, so we do too.
_OPEN_RE = re.compile(r"^(?P<fence>`{3,}|~{3,})[ ]*(?P<info>.*)$")


def _first_lang(info: str) -> str:
    """
    Extract the language token from a fence info string, lowercased.

    Handles the bare form (```python) and the attr-list/brace forms fenced_code
    also accepts (```{.python}, ```{.python .foo}). Returns "" when there is no
    language (a plain ``` fence).
    """
    info = info.strip()
    if not info:
        return ""
    # Brace / attr-list form: pull the first `.name` class if present.
    if info.startswith("{"):
        m = re.search(r"\.([\w#.+-]+)", info)
        return m.group(1).lower() if m else ""
    return info.split()[0].lower()


def replace_top_level_fences(text: str, replace) -> str:
    """
    Walk `text` line by line and hand each TOP-LEVEL fenced code block to
    `replace(lang, body, block)`.

    A top-level block is one not nested inside a longer outer fence (same rules
    as Python-Markdown's fenced_code preprocessor). `replace` receives:
      - lang:  the info-string language token, lowercased ("" for a bare fence)
      - body:  the lines between the fences, joined with "\n" (no trailing "\n")
      - block: the full original block text, fence lines included

    Return a replacement string to swap the whole block, or None to leave it
    verbatim. Interiors of declined blocks are never rescanned, so fences nested
    inside them (the ```python inside `````markdown case) are left untouched.
    """
    lines = text.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        m = _OPEN_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue

        fence = m.group("fence")
        # Closing fence: the exact same run of the same character on its own line.
        close_re = re.compile(r"^" + re.escape(fence) + r"[ ]*$")

        j = i + 1
        while j < n and not close_re.match(lines[j]):
            j += 1

        if j >= n:
            # Unterminated fence - not a real block. Emit the opening line as-is
            # and keep scanning after it so we don't swallow the rest of the doc.
            out.append(lines[i])
            i += 1
            continue

        body = "\n".join(lines[i + 1 : j])
        block = "\n".join(lines[i : j + 1])
        lang = _first_lang(m.group("info"))

        replacement = replace(lang, body, block)
        out.append(block if replacement is None else replacement)
        i = j + 1

    return "\n".join(out)
