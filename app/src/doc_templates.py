# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Starter content for a document that does not exist yet.

The /edit route serves a buffer for a file that isn't on disk; what that buffer
is PREFILLED with is decided here, in one place, so every creation path (a typed
URL, a ghost wikilink, a "Planned" node on the graph) agrees.

The default is the generic wiki stub (Title/Date/Tags + a heading). But some
locations are not free-form prose - they are inputs to a parser. A file in the
system vault's `agents/` or `editors/` folder is read back by agent_registry /
editor_registry as a typed definition, so a new file there starts as that
definition's skeleton instead: the required fields already satisfied, the
optional grammar alongside as comments. Location IS the type, the same way it
is for blessing (only humans can put a file in the system vault).

The skeletons themselves live next to the parsers that consume them
(``agent_registry.new_agent_template`` / ``editor_registry.new_editor_template``)
so a grammar change and its template are edited in the same file; this module
only decides WHICH one applies.
"""

from config import INSERT_DEFAULT_TAGS_IN_NEW_DOCUMENT, SYSTEM_VAULT
from src import timefmt


def _now() -> str:
    return timefmt.iso_local()


def starter_document(wikidoc, vault: str) -> str:
    """Prefill text for a new (nonexistent) document at `wikidoc` in `vault`."""
    folders = [p for p in (wikidoc.path_list() or []) if p]
    file_name = wikidoc.file_name()

    # A typed definition only counts when it sits exactly where its registry
    # scans: the system vault, one folder deep, in that folder. Both registries
    # are flat os.listdir scans, so `agents/archive/old.md` is NOT an agent and
    # must not be handed an agent skeleton.
    if (vault == SYSTEM_VAULT and len(folders) == 1
            and (wikidoc.extension() or "") in ("", "md")):
        # Imported lazily (the /edit route's common case is an ordinary page,
        # and these pull the capability map in behind them).
        from src.agent_registry import AGENTS_SUBDIR, new_agent_template
        from src.editor_registry import EDITORS_SUBDIR, new_editor_template

        slug = wikidoc.file_name_no_ext()
        if folders[0] == AGENTS_SUBDIR:
            return new_agent_template(slug, _now())
        if folders[0] == EDITORS_SUBDIR:
            return new_editor_template(slug, _now())

    body = f"""# Header \n Edit your document {file_name}"""
    if INSERT_DEFAULT_TAGS_IN_NEW_DOCUMENT:
        body = f"---\nTitle: {file_name}\nDate: {_now()}\nTags: \n---\n\n{body}"
    return body
