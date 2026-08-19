# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

# jupyter_extension.py
# Python-Markdown extension to convert ```jupyter``` code fences into interactive cells


import html
from hashlib import sha1

from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor

from config import EXECUTABLE_CODE_LANGUAGES, EXECUTABLE_HIGHLIGHT_ALIASES
from src.fenced_block import replace_top_level_fences

# Which fence languages become executable cells is configurable (default "jupyter").
_EXECUTABLE = {lang.lower() for lang in EXECUTABLE_CODE_LANGUAGES}


class JupyterCellPreprocessor(Preprocessor):
    """
    A block of code starting with !!!jupyter and ending with !!!
    gets processed into container div that holds some buttons,
    a textarea for editing code in the web page,
    a div for displaying pretty markdown syntax highlighted code
        which is done in a different step, this puts the code
        in a markdown block ```python ...code... ``` to be processed
        later
    and another div for holding code output.
    """

    def run(self, lines):
        text = "\n".join(lines)

        def repl(lang, body, block):
            # Only top-level fences whose language is executable become cells;
            # everything else (including the same fence nested inside a longer
            # outer fence) is returned unchanged.
            if lang not in _EXECUTABLE:
                return None

            code = body.rstrip("\n")

            # "jupyter" has no real lexer; alias it (and any future pseudo-language)
            # to a concrete Pygments lexer purely for the read-only highlighted display.
            hl = EXECUTABLE_HIGHLIGHT_ALIASES.get(lang, lang)
            md_code = f"<div class='jupyter-formatted'>\n```{hl}\n{code}\n```\n</div>"

            # Unique stable id for the cell (hash of content) - helps with ordering & persistence
            # Not really using cell hash,
            cell_hash = sha1(code.encode("utf-8")).hexdigest()[:12]
            safe_code = html.escape(code)

            return (
                f"""<div class="jupyter-cell" data-cell-hash="{cell_hash}" data-cell-lang="{lang}">"""
                + self.md.htmlStash.store(
                    f"""<div class="jupyter-button-wrapper">
                    <button title="Run" class="jupyter-run" onclick="runJupyterCode(this)">▶️</button>
                    <button title="Clear Output" class="jupyter-clear" onclick="runJupyterClear(this)">🗑️</button>
                    <button title="Edit" class="jupyter-edit" onclick="runJupyterEdit(this)" aria-pressed="false">✏️</button>
                    </div>
                    <div class="jupyter-output" style="display:none;"></div>
                    <textarea  style="display:none;" class="jupyter-code" spellcheck="false" autocomplete="off" autocorrect="off" autocaptialize="off">{safe_code}</textarea>
                """
                )
                + md_code
                + "</div>"
            )

        new = replace_top_level_fences(text, repl)

        # Only flag (and so load codemirror + jupyter.js) when an executable cell
        # was actually produced. The scanner does not rescan replacement text, so
        # the ```{hl} re-wrap above is never re-matched even when "python" is executable.
        if new != text:
            self.md.tzara_has_jupyter = True
        return new.split("\n")


class JupyterCellExtension(Extension):
    def extendMarkdown(self, md):
        md.tzara_has_jupyter = False
        md.registerExtension(self)  # this do anything?
        md.preprocessors.register(JupyterCellPreprocessor(md), "jupyter_cell", 27)
