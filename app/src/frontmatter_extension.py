# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

# frontmatter_extension.py
# Custom Python-Markdown extension that properly parses YAML frontmatter.
# Replaces the built-in "meta" extension which doesn't handle YAML lists correctly.

import re

from markdown.extensions import Extension
from markdown.preprocessors import Preprocessor

RE_BLOCK_ITEM = re.compile(r"^\s+-\s+(.*)")


class FrontmatterPreprocessor(Preprocessor):
    def run(self, lines):
        meta = {}

        if not lines or lines[0].strip() != "---":
            self.md.Meta = meta
            return lines

        # find closing ---
        close = None
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                close = i
                break

        if close is None:
            self.md.Meta = meta
            return lines

        # parse frontmatter lines
        fm_lines = lines[1:close]
        idx = 0
        while idx < len(fm_lines):
            line = fm_lines[idx]
            if ":" not in line:
                idx += 1
                continue

            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()

            # YAML inline list: [a, b, c]
            if value.startswith("[") and value.endswith("]"):
                inner = value[1:-1].strip()
                meta[key] = [item.strip() for item in inner.split(",") if item.strip()]

            # possible YAML block list: key with empty value followed by "  - item" lines
            elif value == "":
                list_items = []
                while idx + 1 < len(fm_lines):
                    m = RE_BLOCK_ITEM.match(fm_lines[idx + 1])
                    if m:
                        list_items.append(m.group(1).strip())
                        idx += 1
                    else:
                        break
                if list_items:
                    meta[key] = list_items
                else:
                    meta[key] = ""

            # plain scalar value (including CSV strings)
            else:
                meta[key] = value

            idx += 1

        self.md.Meta = meta
        return lines[close + 1 :]


class FrontmatterExtension(Extension):
    def extendMarkdown(self, md):
        md.Meta = {}
        md.registerExtension(self)
        md.preprocessors.register(FrontmatterPreprocessor(md), "frontmatter", 50)
