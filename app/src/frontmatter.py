# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

import re


def parse_llm_tags(raw_response: str) -> list[str]:
    """Clean LLM output into a list of tags. Handle common quirks
    (numbering, bullets, markdown formatting, extra text).
    Strip to lowercase alphanumeric + hyphens. Cap at 8 tags."""
    text = raw_response.strip()

    # Remove thinking blocks (e.g. <think>...</think> from qwen models)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()

    # Remove markdown code fences
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = text.replace("`", "")

    # Try to find a comma-separated list on any line
    # Pick the longest comma-containing line as the likely tag list
    best_line = text
    for line in text.split("\n"):
        line = line.strip()
        if "," in line and len(line) > len(best_line.split("\n")[0].strip()):
            best_line = line

    # Split by commas first, fall back to newlines
    if "," in best_line:
        raw_tags = best_line.split(",")
    else:
        raw_tags = text.split("\n")

    tags = []
    for tag in raw_tags:
        tag = tag.strip()
        # Remove numbering like "1.", "1)", "- ", "* "
        tag = re.sub(r"^[\d]+[.)]\s*", "", tag)
        tag = re.sub(r"^[-*]\s*", "", tag)
        # Remove surrounding quotes
        tag = tag.strip("\"'")
        # Lowercase and keep only alphanumeric, hyphens, spaces
        tag = tag.lower()
        tag = re.sub(r"[^a-z0-9\- ]", "", tag)
        # Convert spaces to hyphens, collapse multiples
        tag = re.sub(r"\s+", "-", tag).strip("-")
        tag = re.sub(r"-+", "-", tag)
        if tag and len(tag) <= 40:
            tags.append(tag)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            unique.append(t)

    return unique[:8]
