# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Agent registry: agents defined as markdown FILES in the system vault.

An agent is a human-blessed markdown file at ``vaults/{SYSTEM_VAULT}/agents/
{slug}.md`` - frontmatter (capabilities, target vaults, output page) plus a
``# Prompt`` section (the directive) and an optional ``# Kickoff`` section.
Blessing is the file's LOCATION: only humans put files in the system vault
(the vault is hidden from agents' reach and refused at the write
gate), so existence there IS the trust grant.

The registry resolves a definition's ``capabilities:`` against CAPABILITIES -
the canonical name -> {tool schema, executor} map of internal tools - and
builds a ``BackgroundAgent`` the existing runner (src.background_agents)
drives unchanged. Fenced ```python blocks in agent files define human-authored
custom tools, executed in the isolated agent kernel (schemas derived statically
by src.agent_schema).

Scanning is on-demand: /manage/tasks, the worker scheduler tick, and
run_agent_task just call list_agents()/get_agent().
"""

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field

from config import AGENT_MEMORY_FILE, AGENT_OUTPUT_DIR, SYSTEM_VAULT, vault_root
from src.chunker import _fence_info
from src.wikidoc import WikiDoc

logger = logging.getLogger("agent_registry")


def agent_job_id(slug: str, vault_id: str | None = None) -> str:
    """Canonical run/job identity for an agent invocation.

    The SINGLE source of truth for the stable id shared by the enqueue path
    (taskiq ``with_task_id`` + dedup), the serializing run-lock, progress keys,
    and the cooperative cancel flag. Keep every producer/consumer routed
    through here so they can never drift apart (a mismatch would silently break
    dedup or leave a cancel signal addressed to a run that no one is listening
    for). ``vault_id=None`` is the fan-out-to-all-targets form."""
    return f"agent:{slug}:all" if not vault_id else f"agent:{slug}:vault:{vault_id}"


def agent_cancel_key(job_id: str) -> str:
    """Redis key for a run's cooperative-cancel flag. A per-job STRING key with
    a TTL (not a set member) so a cancel that's never observed - the run already
    ended, or the worker died before clearing it - self-heals instead of
    poisoning the NEXT run of the same agent. Derived from agent_job_id() so the
    signal is addressed with the same identity the loop checks."""
    return f"agentcancel:{job_id}"


# Subdirectory of the system vault that holds agent definitions (the rest of
# the vault is other wiki-owned content, e.g. help docs).
AGENTS_SUBDIR = "agents"

# The single definition of a valid agent slug - shared with the trigger
# grammar (src.agent_events imports it), so filename rules and `on:`-clause
# rules can never drift apart.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_SLUG_RE = SLUG_RE  # internal alias, kept for existing references

DEFAULT_KICKOFF = "Carry out your directive now."

# Autonomy modes: "act" is accepted spelling sugar - there is no un-checkpointed
# act, so both spellings canonicalize to the explicit form.
_MODE_ALIASES = {
    "propose": "propose",
    "act": "act-with-checkpoint",
    "act-with-checkpoint": "act-with-checkpoint",
}


# ---------------------------------------------------------------------------
# Capability registry: internal tools an agent file may be granted by name
# ---------------------------------------------------------------------------

def _build_capabilities() -> dict:
    """name -> {"def": <ollama tool schema>, "execute": <async dispatcher>}.

    The menu itself lives in src.agent_capabilities (analysis queries +
    generic retrieval/read/proposal tools); the executor contract matches
    background_agents.ExecuteTool:
    execute(name, args, vault_id, status_callback) -> result string.
    """
    from src.agent_capabilities import build_capability_map
    return build_capability_map()



_capabilities_cache: dict | None = None


def capabilities() -> dict:
    global _capabilities_cache
    if _capabilities_cache is None:
        _capabilities_cache = _build_capabilities()
    return _capabilities_cache


# ---------------------------------------------------------------------------
# Agent definition (parsed file)
# ---------------------------------------------------------------------------

@dataclass
class AgentDef:
    slug: str
    description: str = ""
    vaults: list[str] = field(default_factory=lambda: ["*"])
    capabilities: list[str] = field(default_factory=list)
    output: str = "Output.md"        # page under _dada/{slug}/ in the TARGET vault
    max_iterations: int | None = None
    schedule: str = ""               # human-readable rule (agent_schedule grammar); "" = manual
    on_raw: str = ""                 # human-readable event rules (agent_events grammar)
    triggers: list = field(default_factory=list)  # parsed agent_events.Trigger list
    mode: str = "propose"            # autonomy ceiling: propose | act-with-checkpoint
    index_output: bool = False       # opt-in RAG indexing of the OUTPUT page (not logs)
    log: bool = False                # opt-in per-run log page under _dada/{slug}/logs/
    memory: bool = False             # opt-in cross-run memory (reserved memory turn + injection)
    memory_prompt: str = ""          # `# Memory Prompt` body; "" = the shared default
    prompt: str = ""
    kickoff: str = DEFAULT_KICKOFF
    py_source: str = ""                  # fenced python: human-authored custom tools
    custom_tools: list[dict] = field(default_factory=list)  # AST-derived schemas
    errors: list[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# File-format parsing (frontmatter + fence-aware # sections)
# ---------------------------------------------------------------------------

def _walk_fenced(lines):
    """Yield (line, in_fence, fence_lang) walking fence state like chunker.chunk.

    fence_lang is the info string of the fence being OPENED on that line (else
    None). A fence closes on a line of >= the opening run of the same char.
    """
    fence_count, fence_char = 0, None
    for line in lines:
        opened_lang = None
        count, char = _fence_info(line)
        if fence_count == 0 and count:
            fence_count, fence_char = count, char
            opened_lang = line.strip().lstrip(char).strip().lower() or None
            yield line, True, opened_lang
            continue
        if fence_count and char == fence_char and count >= fence_count:
            yield line, True, None
            fence_count, fence_char = 0, None
            continue
        yield line, fence_count > 0, None


def _split_sections(body: str) -> dict[str, str]:
    """Split on top-level `# Heading` lines (outside code fences).

    Returns {lowercased heading: content}. Text before the first heading is
    ignored (agent files may open with prose/notes).
    """
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line, in_fence, _ in _walk_fenced(body.split("\n")):
        if not in_fence and re.match(r"^#\s+\S", line):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[1:].strip().lower()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return sections


# The two comment syntaxes this wiki already hides from a READER: Obsidian's
# `%%` (ObsidianCommentExtension, and skipped wholesale by the RAG chunker) and
# raw HTML comments. Both spellings are matched non-greedily and DOTALL, so the
# inline (`%% note %%`) and block (`%%` on its own line) forms are one rule.
_COMMENT_RE = re.compile(r"%%.*?%%|<!--.*?-->", re.S)


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
    """
    out = _COMMENT_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _extract_python_source(body: str) -> str:
    """Concatenated contents of all fenced ```python blocks (fence lines
    excluded) - the agent's human-authored custom tool code."""
    out: list[str] = []
    collecting = False
    for line, in_fence, lang in _walk_fenced(body.split("\n")):
        opened_or_closed_fence = line.strip() and _fence_info(line)[0] >= 3
        if lang in ("python", "py"):
            collecting = True
            continue
        if collecting and (not in_fence or opened_or_closed_fence):
            collecting = False
            out.append("")  # blank line between blocks
            continue
        if collecting:
            out.append(line)
    return "\n".join(out).strip()


def parse_agent_file(slug: str, content: str) -> AgentDef:
    """Parse one agent markdown file into an AgentDef; problems go to .errors."""
    fm = WikiDoc.parse_frontmatter(content)
    body = WikiDoc.strip_frontmatter(content)
    sections = _split_sections(body)
    d = AgentDef(slug=slug)

    if not _SLUG_RE.match(slug):
        d.errors.append(f"invalid agent slug {slug!r} (lowercase alphanumeric/-/_)")
    if fm.get("type", "") != "agent":
        d.errors.append("frontmatter must declare `type: agent`")

    d.description = fm.get("description", "")
    d.schedule = fm.get("schedule", "").strip()
    if d.schedule:
        from src.agent_schedule import ScheduleError, parse_schedule
        try:
            parse_schedule(d.schedule)
        except ScheduleError as e:
            d.errors.append(f"bad schedule: {e}")
    # Event triggers (`on:`): schedule and triggers compose as OR - either (or
    # both) makes the agent auto-firing; neither = manual only.
    d.on_raw = fm.get("on", "").strip()
    if d.on_raw:
        from src.agent_events import TriggerError, parse_triggers
        try:
            d.triggers = parse_triggers(d.on_raw)
        except TriggerError as e:
            d.errors.append(f"bad trigger: {e}")
    d.index_output = fm.get("index_output", "").lower() in ("1", "true", "yes")
    d.log = fm.get("log", "").lower() in ("1", "true", "yes")
    d.memory = fm.get("memory", "").lower() in ("1", "true", "yes")
    raw_mode = fm.get("mode", "propose").strip().lower() or "propose"
    canon = _MODE_ALIASES.get(raw_mode)
    if canon is None:
        d.errors.append(
            f"mode {raw_mode!r} is not valid - use 'propose' (writes staged for "
            "review in the /agents inbox) or 'act' / 'act-with-checkpoint' "
            "(writes applied immediately with a pre-image checkpoint commit)")
    else:
        d.mode = canon
    d.output = fm.get("output") or d.output
    if "/" in d.output or d.output.startswith("."):
        d.errors.append(f"output {d.output!r} must be a plain filename")

    vaults_raw = fm.get("vaults", "*").strip()
    d.vaults = (["*"] if vaults_raw in ("*", "") else
                [v.strip() for v in vaults_raw.split(",") if v.strip()])

    d.capabilities = [c.strip() for c in fm.get("capabilities", "").split(",") if c.strip()]
    unknown = [c for c in d.capabilities if c not in capabilities()]
    if unknown:
        d.errors.append(f"unknown capabilities: {', '.join(unknown)}")

    if fm.get("max_iterations"):
        try:
            d.max_iterations = int(fm["max_iterations"])
        except ValueError:
            d.errors.append(f"max_iterations {fm['max_iterations']!r} is not an integer")

    # Comments are stripped BEFORE the emptiness check, so a `# Prompt` section
    # that is nothing but authoring notes reads as missing (which it is).
    d.prompt = strip_comments(sections.get("prompt", ""))
    if not d.prompt:
        d.errors.append("missing `# Prompt` section")
    d.kickoff = strip_comments(sections.get("kickoff", "")) or DEFAULT_KICKOFF
    # Empty (or comments-only) means "use the shared default", which is what the
    # template ships commented out - so an unedited agent tracks improvements to
    # the default instead of freezing today's copy of it.
    d.memory_prompt = strip_comments(sections.get("memory prompt", ""))

    # Custom tools: fenced python defs, schemas derived STATICALLY (agent_schema
    # - ast.parse, zero exec; the agent kernel is the only place this code runs).
    d.py_source = _extract_python_source(body)
    if d.py_source:
        from src import agent_schema
        syntax_errors = agent_schema.validate_source(d.py_source)
        if syntax_errors:
            d.errors.extend(syntax_errors)
        else:
            d.custom_tools = agent_schema.functions_from_source(d.py_source)
            collisions = [t["name"] for t in d.custom_tools if t["name"] in capabilities()]
            if collisions:
                d.errors.append(
                    f"custom tool name(s) collide with internal capabilities: "
                    f"{', '.join(collisions)}")

    if not d.capabilities and not d.custom_tools:
        d.errors.append("agent grants no tools (frontmatter `capabilities:` "
                        "and/or fenced ```python custom tools)")
    return d


# ---------------------------------------------------------------------------
# Starter template for a NEW agent file
# ---------------------------------------------------------------------------

def titleize(slug: str) -> str:
    """`daily-digest` -> `Daily Digest`: a human display name from a slug."""
    return re.sub(r"[-_]+", " ", slug).strip().title() or slug


def slugify(name: str) -> str:
    """`Daily Digest` -> `daily-digest`; "" when nothing usable survives.

    The rough inverse of titleize(), for the "New agent"/"New editor" boxes: a
    definition's FILENAME is its identity - the slug the registry, the
    scheduler, the run-lock and the `on:` grammar all address it by - so a typed
    name has to land on one canonical, SLUG_RE-legal filename rather than being
    rejected for a capital letter or a space.
    """
    s = re.sub(r"[^a-z0-9_-]+", "-", name.strip().lower())
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s if SLUG_RE.match(s) else ""


def new_agent_template(slug: str, date: str = "") -> str:
    """Starter markdown for a NEW file in the system vault's `agents/` folder.

    The generic Title/Date/Tags stub is the wrong starting point here: an agent
    file is a contract with parse_agent_file() above, so the skeleton ships the
    REQUIRED half already satisfied (`type: agent`, one granted tool, a
    non-empty `# Prompt`) - saving it unedited yields a valid, manual-only,
    propose-mode agent rather than a red "invalid" row on /agents - and carries
    the OPTIONAL half beside it as inert YAML comments (the frontmatter parser
    skips lines it can't split on `:` and reads only keys it knows, so a
    commented field costs nothing until it is uncommented).

    Every comment is a WHOLE line: WikiDoc.parse_frontmatter takes everything
    after the first `:` verbatim, so a trailing `# ...` on a value line would
    become part of the value - and would still be there, silently breaking the
    field, on the day the author uncomments it. The frontmatter therefore stays
    values-plus-commented-fields and the PROSE lives in the `%%` note below it:
    the markdown editor renders every `#` line as a heading (it has no idea it
    is looking at YAML), so explaining each field in place turns the top of the
    file into a wall of bold text louder than the settings it describes.

    Keep this in step with parse_agent_file when the grammar changes; that is
    why it lives here rather than beside the route that serves it.
    """
    from src.memory_prompts import AGENT_MEMORY_PROMPT as default_memory_prompt
    name = titleize(slug)
    return f"""---
type: agent
description:
vaults: *
capabilities: search_wiki
output: {name}.md
mode: propose
max_iterations: 6
# Optional - uncomment to enable; the note below says what each one does.
# schedule: daily @ 7 am
# on: uploads in inbox/
# log: true
# memory: true
# index_output: true
Title: {name}
Date: {date}
Tags: agent
---

%%
New agent, and inert as saved: it runs only when you press Run on /agents, and
its writes wait for you in the inbox - a good way to leave it while you iterate
on the prompt below. Living in this vault is what makes it trusted, so nothing
here was written by an agent and nothing here can be.
The fields above: `vaults: *` is every content vault, each as its OWN run rather
than one run over the union; `capabilities` is the granted-tool list, whose full
menu is in the help page; `output` is the page this agent's final message
becomes, under _dada/{slug}/ in each target vault; `mode: propose` stages writes
for your review, while `act-with-checkpoint` applies them immediately behind a
pre-image commit.
Commented out above: `schedule` runs it on a clock (hourly / daily @ 7 am /
weekly on tuesday @ 9 am / 2nd saturday @ 7 am); `on` runs it on events (uploads
in inbox/ , agent other-agent completed, any agent failed) and composes with a
schedule as OR; `log` keeps a page per run under _dada/{slug}/logs/ ; `memory`
carries notes from one run to the next, shaped by the `# Memory Prompt` section
below; `index_output` lets RAG index the output page.
A note fenced like this one never reaches the rendered page, the search index or
the model, so keep it or delete it as you please. It may run as long as you like,
blank lines and all.
Field-by-field reference: [[authoring_agents]]
%%

# Prompt

Describe the standing directive: what this agent looks at, how it decides, and
what its final message must contain - that final message becomes `{name}.md`.

Output ONLY the finished markdown page: no preamble, no commentary, no fences.

# Tools

%%
Optional. Every fenced python block in this file becomes a custom tool: one
function per tool, its docstring is the description the model sees and its type
hints become the parameters. The code runs in the isolated agent kernel, never
in the wiki process. Delete this section if the granted capabilities are enough.
%%

# Kickoff

Carry out your directive now.

# Memory Prompt

%%
Optional, and only used with `memory: true`. Left fenced like this the agent uses
the shared default below, so it picks up any later improvement to it; unfence and
edit to take ownership of the wording instead. Available placeholders:
{{agent_name}}, {{tool_names}}. Do NOT write the "CURRENT memory" or "TRANSCRIPT"
sections - those are appended for you, and a hand-written one would arrive twice.

{default_memory_prompt}
%%
"""


# ---------------------------------------------------------------------------
# Loading from the system vault (on-demand scan)
# ---------------------------------------------------------------------------

def _agents_dir() -> str:
    return os.path.join(vault_root(SYSTEM_VAULT), AGENTS_SUBDIR)


def list_agents() -> list[AgentDef]:
    """All agent definitions in the system vault, parse errors included."""
    root = _agents_dir()
    if not os.path.isdir(root):
        return []
    out = []
    for name in sorted(os.listdir(root)):
        if not name.endswith(".md"):
            continue
        slug = name[:-3]
        try:
            out.append(parse_agent_file(
                slug, WikiDoc.read_text_at(os.path.join(root, name))))
        except Exception as e:
            out.append(AgentDef(slug=slug, errors=[f"unreadable: {e}"]))
    # Static trigger-cycle check (guard #7): needs the whole set, so it lives
    # here rather than in per-file parsing. A cycle invalidates the agents on
    # it (surfaced like any parse error; also blocks their schedule - decided).
    if any(a.triggers for a in out):
        from src.agent_events import validate_trigger_graph
        cyc = validate_trigger_graph([(a.slug, a.triggers) for a in out if a.triggers])
        for a in out:
            if a.slug in cyc:
                a.errors.append(cyc[a.slug])
    return out


def get_agent(slug: str) -> AgentDef | None:
    path = os.path.join(_agents_dir(), f"{slug}.md")
    if not _SLUG_RE.match(slug) or not os.path.isfile(path):
        return None
    return parse_agent_file(slug, WikiDoc.read_text_at(path))


def get_agent_checked(slug: str) -> AgentDef | None:
    """get_agent PLUS the cross-agent checks only a full scan can run (the
    trigger-cycle check appended in list_agents). FIRE paths must use this -
    plain get_agent reports a cycle-flagged agent as valid, letting a manual
    run bypass the 'cycle invalidates the agent' contract that the scheduler
    and event dispatcher (which scan via list_agents) already enforce."""
    for a in list_agents():
        if a.slug == slug:
            return a
    return None


def get_def_git_hash(slug: str) -> str:
    """Short hash of the commit holding this agent's CURRENT definition -
    recorded in run logs so "which version ran" stays answerable.

    Commit-if-dirty at load time: app-side edits commit themselves via the
    save path, but external edits (filesystem, Dropbox sync, other machines)
    have no auto-commit in the system vault - the watcher deliberately skips it.
    Checkpointing here, at the moment a definition is
    built to RUN, guarantees the logged hash always points at exactly the
    bytes that ran."""
    root = vault_root(SYSTEM_VAULT)
    abs_path = os.path.join(root, AGENTS_SUBDIR, f"{slug}.md")
    try:
        from config import USE_GIT_VERSIONING
        if USE_GIT_VERSIONING and os.path.isfile(abs_path):
            from src import vault_registry
            from src.docversioning import MarkdownGitVersioning
            vault_registry.init_vault_repo(SYSTEM_VAULT)
            MarkdownGitVersioning(root).save_version(
                abs_path, message=f"checkpoint agent definition: {slug}")
        # Pin --git-dir/--work-tree explicitly: the worktree's on-disk `.git` gitlink is
        # host-facing and unreadable from inside the container (see docversioning).
        from config import vault_git_dir, vault_abs_root
        res = subprocess.run(
            ["git", "-C", root,
             f"--git-dir={vault_git_dir(SYSTEM_VAULT)}",
             f"--work-tree={vault_abs_root(SYSTEM_VAULT)}",
             "log", "-1", "--format=%h", "--", f"{AGENTS_SUBDIR}/{slug}.md"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        return res.stdout.strip() or "(uncommitted)"
    except Exception:
        return "(unknown)"


# ---------------------------------------------------------------------------
# Instantiation
# ---------------------------------------------------------------------------

def resolve_target_vaults(agent: AgentDef) -> list[str]:
    """The content vaults this agent runs against. `*` fans out to every
    non-system vault (list_vaults' default filter IS the isolation boundary);
    an explicit list is validated against existing vaults."""
    from src import vault_registry
    existing = [v["vault_id"] for v in vault_registry.list_vaults()]
    if agent.vaults == ["*"]:
        return existing
    return [v for v in agent.vaults if v in existing]


def build_background_agent(agent: AgentDef):
    """AgentDef -> the BackgroundAgent the existing runner drives. Raises
    ValueError on an invalid definition - callers surface .errors instead."""
    from src.agent_capabilities import uses_ledgers
    from src.background_agents import BackgroundAgent, _build_tools_text, write_agent_output

    if not agent.valid:
        raise ValueError(f"agent {agent.slug!r} is invalid: {'; '.join(agent.errors)}")

    caps = capabilities()
    granted = list(agent.capabilities)
    # `recall` is not a vault capability - it reads the agent's OWN ledgers, the
    # same argument that lets the reserved ledger turn write without a grant. It
    # comes free wherever ledgers are in play, because the injected view may have
    # had to elide rows and an agent told to call a tool it was never granted is
    # a dead end. Granted HERE rather than at run time so _build_tools_text below
    # sees it: the human-readable tool list and the native schemas must agree.
    if "recall" in caps and "recall" not in granted and uses_ledgers(
            agent.memory, agent.capabilities):
        granted.append("recall")
    tool_defs = [caps[c]["def"] for c in granted]
    tool_names = set(granted)

    # Custom tools ride alongside internal capabilities in the model's tool
    # list; the RUNNER routes their calls to the agent kernel (the executor
    # below only handles internal names).
    custom_tool_names = {t["name"] for t in agent.custom_tools}
    tool_defs += [t["schema"] for t in agent.custom_tools]
    tool_names |= custom_tool_names

    async def _execute(name, args, vault_id, status_cb):
        return await caps[name]["execute"](name, args, vault_id, status_cb)

    # Owned-area owner: "agents/{slug}" so agent output lands at _dada/agents/{slug}/,
    # symmetric with editors' _dada/editors/{slug}/ (no name collision with an agent
    # literally called "editors"). `name` stays the bare slug (identity).
    owner = f"agents/{agent.slug}"

    async def _sink(vault_id, body):
        return await write_agent_output(vault_id, owner, agent.output, body,
                                        title=os.path.splitext(agent.output)[0],
                                        index=agent.index_output)

    return BackgroundAgent(
        name=agent.slug,
        owner=owner,
        directive=agent.prompt,
        kickoff=agent.kickoff,
        tools_text=_build_tools_text(tool_defs),
        tool_defs=tool_defs,
        tool_names=tool_names,
        execute=_execute,
        sink=_sink,
        max_iterations=agent.max_iterations,
        def_hash=get_def_git_hash(agent.slug),
        py_source=agent.py_source,
        custom_tool_names=custom_tool_names,
        mode=agent.mode,
        log=agent.log,
        output_rel=f"{AGENT_OUTPUT_DIR}/{owner}/{agent.output}",
        memory=agent.memory,
        memory_prompt=agent.memory_prompt,
        memory_rel=f"{AGENT_OUTPUT_DIR}/{owner}/{AGENT_MEMORY_FILE}",
    )
