# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

# mermaid_extension.py
# Python-Markdown extension to convert ```mermaid``` code fences into rendered diagrams

import html

from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor

from src.fenced_block import replace_top_level_fences


class MermaidPreprocessor(Preprocessor):
    def run(self, lines):
        text = "\n".join(lines)

        def repl(lang, body, block):
            # Only a top-level ```mermaid fence becomes a diagram; a mermaid
            # fence nested inside a longer outer fence stays verbatim.
            if lang != "mermaid":
                return None
            code = body.rstrip("\n")
            safe_code = html.escape(code)
            return self.md.htmlStash.store(
                f'<pre class="mermaid">{safe_code}</pre>'
            )

        new = replace_top_level_fences(text, repl)

        if new != text:
            self.md.tzara_has_mermaid = True
        return new.split("\n")


class MermaidExtension(Extension):
    def extendMarkdown(self, md):
        md.tzara_has_mermaid = False
        md.registerExtension(self)
        md.preprocessors.register(MermaidPreprocessor(md), "mermaid_diagram", 27)
