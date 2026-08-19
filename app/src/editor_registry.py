# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Editor tools: text-transform tools defined as markdown FILES in the system vault.

An editor tool is a human-authored markdown file at
``vaults/{SYSTEM_VAULT}/editors/{slug}.md`` - frontmatter (`type: editor`, label,
scope, operation, capabilities) plus a ``# Prompt`` section (the directive).

Unlike agents (background, whole-vault, file-output via taskiq), an editor tool is
a SYNCHRONOUS text-in / text-out transform invoked from the edit-mode "/" menu: it
receives the current selection (or the whole unsaved buffer) as input, runs an LLM
tool-calling loop over a curated set of read-only built-in capabilities (in-process;
NO kernel, NO redis/taskiq), and returns a single text payload the editor presents
behind the existing accept/reject confirmation. It is a *saved* custom prompt.

Two orthogonal filters, mirroring the agent-blessing motif:
  - `type:` frontmatter is authoritative for WHAT a file is. A foreign `type:`
    dropped in `editors/` is skipped (never errors), just as list_agents skips
    non-agents.
  - The `editors/` folder LOCATION is authoritative for menu VISIBILITY: move a
    file in/out of the folder to toggle it in the "/" menu.

Parsing reuses the agent_registry primitives (``_split_sections``,
``_extract_python_source``) rather than reinventing frontmatter/section scanning.

Tier-2 (human-authored ```python custom tools, run in the isolated jupyter-agent
kernel) is designed-for - the file format already tolerates fenced python blocks,
captured in ``py_source`` - but is NOT wired in this tier; such blocks are parsed
and ignored for now.
"""

import logging
import os
from dataclasses import dataclass, field

from config import SYSTEM_VAULT, vault_root
from src.agent_registry import (
    SLUG_RE,
    _extract_python_source,
    _split_sections,
    strip_comments,
    titleize,
)
from src.wikidoc import WikiDoc

logger = logging.getLogger("editor_registry")


# Subdirectory of the system vault that holds editor-tool definitions (beside
# `agents/`). Only files here populate the edit-mode "/" menu.
EDITORS_SUBDIR = "editors"

# The curated set of built-in Tzara capabilities an editor-tool author may grant.
# DELIBERATELY read-only and corpus-level (about OTHER documents): an editor's
# only output is its accept/reject payload, so no writes are needed or allowed.
# EXCLUDED on purpose:
#   - read_document / get_outline: for the CURRENT doc these read stale on-disk
#     state while the editor operates on the unsaved buffer (the buffer is handed
#     in as the tool input instead); offering them invites disk-vs-buffer drift.
#   - propose_create / propose_edit / propose_append / apply_wikilink: writes /
#     staged mutations of other documents - out of an editor's remit entirely.
# Granted-tool menu for editors. Read-only corpus tools, plus the two ledger
# writers - which are NOT an exception to "editors don't get write tools": they
# write only to the tool's OWN append-only ledger under _dada/editors/{slug}/,
# never to the vault, and their write is brokered to the worker like every other
# owned-area write.
EDITOR_CAPABILITIES = {"search_wiki", "find_related", "remember", "forget", "recall"}
# The subset whose WRITES must be brokered rather than run in-process. `recall`
# is deliberately absent: it only reads, so it needs no git and no worker. Do not
# conflate this with agent_capabilities.LEDGER_TOOL_NAMES, which asks a different
# question - who should have ledgers injected at all.
EDITOR_LEDGER_CAPABILITIES = {"remember", "forget"}

_VALID_SCOPES = ("selection", "document", "cursor")
# Where the result goes. Four of the five are RANGE-RELATIVE - they position
# themselves against the range `scope` defines - and apply to the CURRENT doc via
# the accept/reject overlay:
#   replace  - swap the range out
#   prepend  - immediately before the range
#   append   - immediately after the range
#   insert   - CARET-ABSOLUTE: the caret point itself, in every scope. The only
#              operation that can land mid-line, which is what a continuation
#              needs; also the only one whose position doesn't come from `scope`.
# "note" is the odd one out: it routes the result to an external owned digest page
# that grows across calls, and the doc being edited is left untouched.
_VALID_OPERATIONS = ("replace", "prepend", "append", "insert", "note")


@dataclass
class EditorToolDef:
    """One parsed editor-tool file. Problems accumulate in ``errors``; an invalid
    tool is dropped from the menu (like an invalid agent is refused a run)."""
    slug: str
    label: str = ""
    description: str = ""            # richer menu tooltip (distinct from the short `label`)
    # The range the tool operates on. A caret IS a zero-width selection, so all
    # three scopes share one geometry (see AssistContext / the injected `editor`
    # object): `before` is the text preceding the range, `after` follows it, and
    # `selection` is what lies between (empty for `cursor`).
    #   "selection" - the highlighted text (menu item needs a non-empty selection)
    #   "document"  - the whole unsaved buffer, frontmatter excluded
    #   "cursor"    - nothing is selected; the caret neighborhood is the context
    scope: str = "selection"
    # What happens to the result: swap the range out, add before/after it, drop it
    # at the caret, or file it away. See _VALID_OPERATIONS.
    operation: str = "replace"
    output: str = "Notes.md"        # op:note target filename under _dada/editors/{slug}/
    capabilities: list = field(default_factory=list)  # granted built-in tool names (subset of EDITOR_CAPABILITIES)
    max_iterations: int = None      # tool-loop ceiling; None -> engine default (_EDITOR_MAX_ITERATIONS)
    vaults: list = field(default_factory=lambda: ["*"])  # availability whitelist: which vaults this tool shows/runs in; ["*"] = all
    memory: bool = False            # opt-in cross-invocation memory (reserved consolidation turn)
    memory_prompt: str = ""         # `# Memory Prompt` body; "" = the shared default
    log: bool = False               # opt-in per-invocation log page under _dada/editors/{slug}/logs/
    prompt: str = ""                # the directive (the `# Prompt` section body)
    py_source: str = ""             # human-authored fenced ```python custom-tool source
    custom_tools: list = field(default_factory=list)  # AST-derived tool schemas from py_source
    is_editor: bool = True          # False = a foreign file in editors/ (no `type: editor`)
    errors: list = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors

    def available_in(self, vault) -> bool:
        """Whether this tool is offered/runnable while editing a file in `vault`.

        Mirrors agent vault scoping (see agent_registry.resolve_target_vaults):
        - The SYSTEM_VAULT is NEVER a target - editor tools don't operate on
          wiki-owned content, even for a `["*"]` (all-vaults) tool.
        - `["*"]` (the default when no `vaults:` is given) = every content vault.
        - When the caller's vault is unknown (None), only all-vaults tools
          qualify; an explicitly-scoped tool can't be confirmed in-scope, so it
          is withheld rather than shown everywhere.
        """
        if vault == SYSTEM_VAULT:
            return False
        if self.vaults == ["*"]:
            return True
        if vault is None:
            return False
        return vault in self.vaults


def parse_editor_file(slug: str, content: str) -> EditorToolDef:
    """Parse one editor-tool markdown file into an EditorToolDef."""
    fm = WikiDoc.parse_frontmatter(content)
    body = WikiDoc.strip_frontmatter(content)
    sections = _split_sections(body)
    d = EditorToolDef(slug=slug)

    if not SLUG_RE.match(slug):
        d.errors.append(f"invalid editor slug {slug!r} (lowercase alphanumeric/-/_)")
    if fm.get("type", "") != "editor":
        d.errors.append("frontmatter must declare `type: editor`")

    d.label = fm.get("label", "").strip() or slug
    d.description = fm.get("description", "").strip()

    max_iter_raw = fm.get("max_iterations", "").strip()
    if max_iter_raw:
        try:
            max_iter = int(max_iter_raw)
        except ValueError:
            d.errors.append("max_iterations must be an integer")
        else:
            if max_iter <= 0:
                d.errors.append("max_iterations must be a positive integer")
            else:
                d.max_iterations = max_iter

    scope = fm.get("scope", "selection").strip().lower() or "selection"
    if scope not in _VALID_SCOPES:
        d.errors.append(f"scope {scope!r} must be one of {', '.join(_VALID_SCOPES)}")
    else:
        d.scope = scope

    operation = fm.get("operation", "replace").strip().lower() or "replace"
    if operation not in _VALID_OPERATIONS:
        d.errors.append(f"operation {operation!r} must be one of {', '.join(_VALID_OPERATIONS)}")
    else:
        d.operation = operation

    # op:note target page (under the tool's owned _dada/editors/{slug}/ area).
    # Plain filename only - no path separators, no dotfiles (mirrors agent output).
    output = fm.get("output", "Notes.md").strip() or "Notes.md"
    if "/" in output or output.startswith("."):
        d.errors.append(f"output {output!r} must be a plain filename (no '/', no leading '.')")
    else:
        d.output = output

    # Availability whitelist (mirrors agent_registry vaults semantics, but here it
    # gates menu visibility / invocation, not fan-out). `*` or empty = all vaults.
    # Kept lenient like agents: unknown vault ids are not an error (a tool can be
    # authored ahead of a vault existing); it simply won't appear there yet.
    vaults_raw = fm.get("vaults", "*").strip()
    d.vaults = (["*"] if vaults_raw in ("*", "") else
                [v.strip() for v in vaults_raw.split(",") if v.strip()])

    # Opt-in cross-invocation memory (mirrors agent_registry): a reserved
    # consolidation turn rewrites _dada/editors/{slug}/memory.md, re-injected next run.
    d.memory = fm.get("memory", "").lower() in ("1", "true", "yes")
    d.log = fm.get("log", "").lower() in ("1", "true", "yes")

    d.capabilities = [c.strip() for c in fm.get("capabilities", "").split(",") if c.strip()]
    disallowed = [c for c in d.capabilities if c not in EDITOR_CAPABILITIES]
    if disallowed:
        d.errors.append(
            f"unknown or disallowed editor capabilities: {', '.join(disallowed)} "
            f"(allowed: {', '.join(sorted(EDITOR_CAPABILITIES))})")

    # Authoring notes (`%%` / HTML comments) never reach the model - see
    # agent_registry.strip_comments. Stripped before the emptiness check, so a
    # section holding only notes reads as the missing prompt it is.
    d.prompt = strip_comments(sections.get("prompt", ""))
    if not d.prompt:
        d.errors.append("missing `# Prompt` section")
    # Mirrors agent_registry: empty means "use the shared default", so an unedited
    # tool tracks later improvements to it rather than freezing today's copy.
    d.memory_prompt = strip_comments(sections.get("memory prompt", ""))

    # Custom tools: human-authored fenced ```python defs, schemas derived STATICALLY
    # (agent_schema - ast.parse, zero exec; the isolated editor kernel is the only
    # place this code ever runs, brokered through the worker). Shares the exact
    # deriver agents use. Unlike agents, editors need NOT grant any tool - a Tier-1
    # editor with no python + no capabilities is a valid saved prompt.
    d.py_source = _extract_python_source(body)
    if d.py_source:
        from src import agent_schema
        from src.agent_capabilities import build_capability_map
        syntax_errors = agent_schema.validate_source(d.py_source)
        if syntax_errors:
            d.errors.extend(syntax_errors)
        else:
            d.custom_tools = agent_schema.functions_from_source(d.py_source)
            builtin_names = set(build_capability_map()) | EDITOR_CAPABILITIES
            collisions = [t["name"] for t in d.custom_tools if t["name"] in builtin_names]
            if collisions:
                d.errors.append(
                    f"custom tool name(s) collide with built-in capabilities: "
                    f"{', '.join(collisions)}")
    return d


# ---------------------------------------------------------------------------
# Starter template for a NEW editor-tool file
# ---------------------------------------------------------------------------

def new_editor_template(slug: str, date: str = "") -> str:
    """Starter markdown for a NEW file in the system vault's `editors/` folder.

    Same bargain as new_agent_template: the fields parse_editor_file() REQUIRES
    (`type: editor` plus a non-empty `# Prompt`) are filled in, so the skeleton
    is a working - if generic - "/" menu entry the moment it is saved, and the
    two decisions that actually shape a tool (`scope:` = what text it receives,
    `operation:` = what happens to its answer) are spelled out with their legal
    values spelled out above them. Optional fields ride along as inert YAML
    comments, always on their OWN line - parse_frontmatter takes everything
    after the first `:` verbatim, so a trailing comment would end up inside the
    value (and would still be there when the author uncomments the line).
    """
    from src.memory_prompts import EDITOR_MEMORY_PROMPT as default_memory_prompt
    name = titleize(slug)
    return f"""---
type: editor
label: {name}
description:
# selection | document | cursor
scope: selection
# replace | prepend | append | insert | note
operation: replace
# Optional - uncomment to enable; the note below says what each one does.
# capabilities: search_wiki, find_related
# vaults: main
# max_iterations: 4
# output: Notes.md
# memory: true
# log: true
Title: {name}
Date: {date}
Tags: editor
---

%%
New editor tool - it appears in the edit-mode "/" menu as soon 
as this file parses, in whatever vault you are editing (never in
this one). `scope` and `operation` above are the two decisions 
that shape it; everything else is the prompt. The result is always
shown behind accept/reject, so a tool can never write over your 
text on its own.
`scope` is what the tool RECEIVES: `selection` the highlighted
text (the menu entry then needs a selection), `document` the whole
unsaved buffer with frontmatter excluded, `cursor` nothing
selected and the caret neighborhood as context. `operation` is
what happens to its ANSWER: `replace` swaps the range out,
`prepend` and `append` go just before or after it, `insert` lands
at the caret and is the only one that can land mid-line, and
`note` files the answer to a page of its own, leaving the
document untouched.
Commented out above: `capabilities` grants read-only corpus tools
(search_wiki, find_related); `vaults` limits which vaults offer
it; `max_iterations` caps the tool loop; `output` names the
`note` target under _dada/editors/{slug}/ ; `memory` remembers
house style across invocations, shaped by the `# Memory Prompt`
section below; `log` keeps a page per invocation.
A note fenced like this one is hidden three ways over - from the
rendered page, from the search index, and from the model - so
keep them or delete them as you like. They may run as long as
you like, blank lines and all.
Field-by-field reference: [[authoring_editors]]
%%

# Prompt

Describe the transform: what to do with the text handed in, and what to hand
back. Be specific about tone, length, and what must be preserved verbatim.

Output ONLY the text to insert: no preamble, no explanation, no code fences.

# Memory Prompt

%%
Optional, and only used with `memory: true`. Left fenced like this the tool uses
the shared default below, so it picks up any later improvement to it; unfence and
edit to take ownership of the wording instead. Available placeholder: {{label}}.
Do NOT write the "CURRENT memory" or "TRANSCRIPT" sections - those are appended
for you, and a hand-written one would arrive twice.

{default_memory_prompt}
%%
"""


def _editors_dir() -> str:
    return os.path.join(vault_root(SYSTEM_VAULT), EDITORS_SUBDIR)


def list_editor_tools(include_foreign: bool = False) -> list[EditorToolDef]:
    """All `type: editor` definitions in the system vault's `editors/` folder.

    Type-aware and NON-fatal: a file whose frontmatter isn't `type: editor` is
    skipped silently (so dropping an agent or note here breaks nothing). Parse
    errors on genuine editor files are kept on the returned def for surfacing.

    `include_foreign=True` (for the /editors management view) instead RETURNS
    those non-editor files as stub defs with `is_editor=False`, so an author who
    forgot `type: editor` can see why their file isn't showing up in the menu."""
    root = _editors_dir()
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".md"):
            continue
        slug = name[:-3]
        try:
            content = WikiDoc.read_text_at(os.path.join(root, name))
        except Exception as e:
            out.append(EditorToolDef(slug=slug, errors=[f"unreadable: {e}"]))
            continue
        if WikiDoc.parse_frontmatter(content).get("type", "") != "editor":
            # foreign type in the folder: not an editor tool
            if include_foreign:
                out.append(EditorToolDef(slug=slug, is_editor=False))
            continue
        out.append(parse_editor_file(slug, content))
    return out


def get_editor_tool(slug: str) -> EditorToolDef | None:
    """Load a single editor tool by slug, or None if missing / not `type: editor`."""
    if not SLUG_RE.match(slug):
        return None
    path = os.path.join(_editors_dir(), f"{slug}.md")
    if not os.path.isfile(path):
        return None
    content = WikiDoc.read_text_at(path)
    if WikiDoc.parse_frontmatter(content).get("type", "") != "editor":
        return None
    return parse_editor_file(slug, content)
