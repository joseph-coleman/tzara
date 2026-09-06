# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

import re

class CaseInsensitiveDict(dict):
    """dict with case-insensitive lookup, preserving the authored spelling.

    Frontmatter keys are case-insensitive everywhere else in Tzara because
    WikiDoc.parse_frontmatter folds them on parse. The browser parser in
    edit_assist.js cannot (its output is also handed to custom Python tools as
    `editor.frontmatter`, where renaming keys would break them), so the folding
    happens on LOOKUP here instead: iteration still yields keys as written.

    Read-only by contract. Frontmatter is edited as markdown text, never through
    this dict, so writes are left as plain dict behavior: a key added via
    __setitem__/update keeps its case and is still found by a folded lookup, but
    setdefault/pop use dict's own exact-match hashing and do not fold.

    Injected verbatim into the editor kernel via inspect.getsource, so it must
    stay dependency-free.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @staticmethod
    def _fold(key):
        return key.lower() if isinstance(key, str) else key

    def _resolve(self, key):
        """The stored key matching `key` case-insensitively, or None."""
        if dict.__contains__(self, key):
            return key                      # exact hit, no scan
        folded = self._fold(key)
        for k in self:
            if self._fold(k) == folded:
                return k
        return None

    def __setitem__(self, key, value):
        # Replace a differently-cased existing key rather than sitting beside it:
        # a duplicate would shadow the new value behind the scan's first match.
        real = self._resolve(key)
        if real is not None and real != key:
            dict.__delitem__(self, real)
        dict.__setitem__(self, key, value)

    def __getitem__(self, key):
        real = self._resolve(key)
        if real is None:
            raise KeyError(key)
        return dict.__getitem__(self, real)

    def __contains__(self, key):
        return self._resolve(key) is not None

    def get(self, key, default=None):
        real = self._resolve(key)
        return dict.__getitem__(self, real) if real is not None else default



# `WikiDoc.parse_frontmatter` takes everything after the first `:` verbatim, which is
# right for prose values but leaves the quotes on `label: "Decoder Ring"`. Callers that
# render a value as text strip them here rather than in the parser: unquoting globally
# would also rewrite values where the quotes are content (a prompt, a regex, a path).
def unquote(value) -> str:
    """Strip one matching pair of surrounding quotes from a frontmatter value."""
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1].strip()
    return text


# Tag ownership. `Tags` is yours and is never rewritten; `AutoTags` belongs to the
# metadata task and is replaced on every run. Everything downstream - RAG, search,
# the graph overlay, the sidebar - works off the UNION, so splitting ownership does
# not split what the tags actually do.
TAGS_KEY = "tags"
AUTOTAGS_KEY = "autotags"


def split_tag_field(value) -> list[str]:
    """A frontmatter tag field -> a clean, de-duplicated list.

    Accepts either shape the parsers produce: the comma-joined string from
    WikiDoc.parse_frontmatter, or a real list from the renderer's
    FrontmatterPreprocessor. A leading `!` is the legacy pin marker - it is
    invalid YAML and no longer written, but existing vaults carry it, so it is
    accepted and stripped on read.
    """
    if not value:
        return []
    parts = value.split(",") if isinstance(value, str) else list(value)
    out = []
    for part in parts:
        tag = str(part).strip().lstrip("!").strip()
        if tag and tag not in out:
            out.append(tag)
    return out


def manual_tags(frontmatter: dict) -> list[str]:
    """Tags you wrote. Never touched by the LLM."""
    return split_tag_field(frontmatter.get(TAGS_KEY))


def auto_tags(frontmatter: dict) -> list[str]:
    """Tags the metadata task owns and rewrites wholesale."""
    return split_tag_field(frontmatter.get(AUTOTAGS_KEY))


def merged_tags(frontmatter: dict) -> list[str]:
    """Every frontmatter tag, yours first. THE set for search, RAG and display."""
    out = manual_tags(frontmatter)
    for tag in auto_tags(frontmatter):
        if tag not in out:
            out.append(tag)
    return out


# Per-document gate on LLM-written metadata. `false`/`no`/`0` (any case) stops the
# metadata task writing AutoTags/Summary into the page; the page stays fully indexed
# and searchable either way - that is `Index:`, a separate gate. `rag_frontmatter` is
# the pre-rename spelling, still read so existing vaults keep working.
GENERATE_METADATA_KEY = "generatemetadata"      # parse_frontmatter folds keys to lowercase
_LEGACY_GENERATE_METADATA_KEY = "rag_frontmatter"
FALSEY = ("false", "no", "0")


def truthy(value, default: bool = True) -> bool:
    """Coerce a config/frontmatter value to a bool on the FALSEY convention.

    Shared by the frontmatter gates and the per-vault settings resolver, so
    `false`/`no`/`0` mean the same thing in a page, in .tzara/config.json and in a
    settings form. A missing value (None) takes `default`.
    """
    if value is None:
        return default
    return str(value).strip().lower() not in FALSEY


def metadata_generation_enabled(frontmatter: dict) -> bool:
    """Whether the LLM may write AutoTags/Summary into this page. Defaults to True."""
    value = frontmatter.get(GENERATE_METADATA_KEY)
    if value is None:
        value = frontmatter.get(_LEGACY_GENERATE_METADATA_KEY)
    return truthy(value, default=True)


# Timestamps. `Created` is stamped once, when the starter template builds a new page;
# `Updated` is rewritten by every content save Tzara itself performs. They exist so a
# vault stays meaningful away from its git history - see config.FRONTMATTER_TIMESTAMPS.
#
# `Date` is what the starter template wrote before these keys existed. It is still read
# as `Created` so existing vaults keep working; nothing writes it any more.
#
# `MetadataUpdated` belongs to the metadata task, the same way AutoTags/Summary do, and
# is deliberately a SEPARATE key: a bulk regenerate touches every page, and folding it
# into `Updated` would restamp the whole vault to one instant and destroy the ordering
# the field exists to carry.
#
# `modified` is NOT read. It means the same thing as `updated`, and Obsidian has no
# official spelling for either (both come from community plugins), so Tzara picks one.
CREATED_KEY = "created"
_LEGACY_CREATED_KEY = "date"
UPDATED_KEY = "updated"
METADATA_UPDATED_KEY = "metadataupdated"

# Authored spellings, for the writers. Keys are matched case-insensitively on read
# (parse_frontmatter folds them), so these only decide how a NEW line looks.
CREATED_LABEL = "Created"
UPDATED_LABEL = "Updated"
METADATA_UPDATED_LABEL = "MetadataUpdated"

# Where a newly inserted `Updated:` goes: directly below the creation key, so the pair
# reads together instead of the timestamp landing at the bottom of the block.
UPDATED_ANCHORS = (CREATED_KEY, _LEGACY_CREATED_KEY)


def created_at(frontmatter: dict) -> str | None:
    """The page's creation stamp, honoring the legacy `Date:` spelling. None if neither."""
    for key in (CREATED_KEY, _LEGACY_CREATED_KEY):
        value = frontmatter.get(key)
        if value:
            return str(value).strip()
    return None


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
