---
title: Authoring Agents
description: Reference for every frontmatter field and section in an agent definition file.
Tags: agents, yaml-frontmatter, scheduling, capabilities, custom-tools, memory, ledger
Summary: An agent is a markdown file placed in the system vault whose frontmatter configures its type, description, target vaults, granted capabilities, output location, schedule, triggers, mode (propose or act), and optional features like logging, memory, and custom Python tools; it must include a required “# Prompt” section and may contain optional “# Kickoff”, “# Memory Prompt”, and custom tool definitions. The specification details syntax for schedules, event triggers, cross‑run memory handling, ledger tools, and validation rules ensuring agents are properly named, authorized, and equipped before execution.
---

# Overview

An **agent** is a single markdown file that you place in the system vault under `agents/{slug}.md`. Its *location is its blessing*: only a human can put a file in the system vault (the vault is hidden from agents and refused at the write gate), so the file existing here IS the trust grant that lets it run.

An agent file has two parts mininum:

1. **Frontmatter** - the YAML-style block between `---` fences that configures the agent (all fields documented below).
2. **`# Prompt`** section (required) - the standing directive the model follows.
3. Optional **`# Kickoff`** section and optional fenced ` ```python ` blocks that define custom tools.
4. Optional **`# Memory Prompt`** section, optionally used with `memory:true` setting. 

The filename (minus `.md`) is the agent's **slug**: it must be lowercase and contain only letters, digits, `-`, or `_` (e.g. `vault-gardener.md`, `stock_digest.md`). The slug names the agent everywhere - its output folder, its logs, and the `/agents` inbox.

---

## Frontmatter fields

### `type`

**required, must be `agent`**

```yaml
type: agent
```

The registry refuses to treat a file as an agent unless it declares `type: agent`. This is what distinguishes an agent from an ordinary help/notes page that also lives in the system vault.

### `description`

A one-line human summary of what the agent does. Shown in management UIs; has no effect on execution.

```yaml
description: Proposes wikilinks between related but unlinked pages.
```

### `vaults`

*default `*`*

Which **content vaults** the agent runs against.

- `*` (or omitted) - fan out to **every** non-system vault. The agent runs **once per vault, in isolation**; it does not see a union of all vaults.
- A comma-separated list - run only against those vaults (names are validated   against existing vaults; unknown names are silently dropped at run time).

```yaml
vaults: "*"            # every content vault, each in isolation
vaults: main, physics  # only these two
```

> [!Info] Note
> `vaults: *` means *isolated per-vault fan-out*, not cross-vault visibility. A single agent that reads several vaults at once is deliberately not expressible.

### `capabilities`

A comma-separated list of **internal tools** to grant the agent by name. Each name must exist in the capability menu (see **Capabilities menu** below) or the agent is marked invalid. These tools run server-side against the vault's database and staging layer - they are the *trusted* tool tier.

```yaml
capabilities: search_wiki, read_document, list_orphans, apply_wikilink
```

An agent must grant **at least one** tool - either a `capabilities:` entry or a fenced `python` custom tool (or both). An agent that grants nothing is invalid. Without any tools provided, the agent can not read anythin nor produce any output.

### `output`

*default `Output.md`*

The filename of the page the agent writes its final report to. Must be a **plain filename** - no `/` and no leading `.`. The file is written to the agent-owned area of the target vault: `{vault}/_dada/{slug}/{output}`.

```yaml
output: Vault Health.md
```

Agent-owned output lives under the special `_dada/` directory (configurable via `AGENT_OUTPUT_DIR`) and is excluded from RAG indexing by default - see `index_output` to opt back in. You "take ownership" of an agent's output by moving the file out of that directory.

### `max_iterations`

Integer cap on how many reasoning/tool-call steps the agent loop may take before it is forced to wrap up. Omit to use the system default. Lower this to keep cheap agents from looping; raise it for agents that must chain many tool calls.

```yaml
max_iterations: 6
```

### `schedule`

*default empty (manual only)*

A **human-readable** rule for when the worker auto-runs the agent. An empty value (the default) means the agent only runs when invoked manually. The presence of a `schedule:` is what flips an agent from manual to automatic.

The grammar (case-insensitive; the words `on`, `the`, `every`, `each` are optional filler; a time is introduced by `@` or `at`):

| Form | Example | Meaning |
|------|---------|---------|
| *minutes* | `every 15 minutes` , `3 times an hour`| interval of minutes, from 1 to 1440 |
| *hours* | `every 4 hours`, `every 6h` | interval of hours, from 1 to 24 |
| `hourly` | `hourly` | top of every hour (takes no time clause) |
| `daily` | `daily @ 3:30 pm` | every day |
| `weekly` | `weekly on tuesday @ 9 am` | every week (defaults to Monday if no weekday) |
| *bare weekday* | `saturday @ 7 am` | every week on that day |
| *ordinal weekday* | `2nd saturday @ 6:15 pm`, `last friday` | that weekday of each month |
| *month edge* | `first of the month`, `last of the month @ 3:30 pm` | first/last day of each month |
| *cron* | `0 */4 * * *`, `@daily` | any 5-field cron expression (see below) |

Times accept `3:30 pm`, `15:30`, or a bare hour `4`. Ordinals are `1st`/`first`, `2nd`/`second`, `3rd`/`third`, `4th`/`fourth`, and `last`. A rule **without** a time clause runs at the configured default hour (`AGENT_DEFAULT_RUN_HOUR`, default 4 am). Interval rules are anchored at midnight, so `every 4 hours` fires at 00:00/04:00/08:00/... rather than drifting from the last run. An unrecognized rule marks the agent invalid.

Schedules are wall-clock times in the timezone you set as `TZ`, and they hold that time across a daylight saving change: `daily @ 4:30 pm` fires at 4:30 pm on both sides of the switch, and a rule landing in the hour that repeats or goes missing still runs exactly once. Interval rules (`every 4 hours`, `hourly`) are the exception - because they are anchored to the wall clock, one slot is skipped or comes an hour early at each transition. If an occurrence must not be missed, name a time instead of an interval.

#### Cron expressions

The grammar above is small on purpose. When the schedule you want is not something it can say, write a standard **5-field cron expression** instead - it is recognized automatically:

```yaml
schedule: 0 */4 * * *          # every 4 hours, on the hour
schedule: 30 9 * * mon-fri     # 9:30 am on weekdays
schedule: 0 9 1,15 * *         # 9 am on the 1st and the 15th
schedule: cron 0 3 * * sun     # the `cron` prefix is optional but explicit
```

The fields are `minute hour day-of-month month day-of-week`, and each accepts `*`, a number, a list (`1,15`), a range (`9-17`), and a step (`*/15`, `9-17/2`). Months and weekdays also accept 3-letter names (`jan`, `mon`). Cron's weekday numbering is used, where **`0` and `7` are both Sunday**. The nicknames `@hourly`, `@daily`, `@midnight`, `@weekly`, `@monthly`, `@yearly` work too.

Two behaviors worth knowing:

- **Both day fields restricted = OR.** As in classic cron, if *neither* `day-of-month` nor `day-of-week` is `*`, a day matches when **either** does - `0 9 13 * fri` means "the 13th **or** any Friday", not "Friday the 13th".
- **No extensions.** `L`, `W`, `#`, `?`, seconds and year fields, and wrap-around ranges (`22-2`) are rejected with an error rather than silently misread. Write `22-23,0-2` for a wrap.

A cron expression whose minute field is finer than the scheduler tick (`AGENT_SCHEDULER_TICK_S`, default 60s) can only fire once per tick - the tick is the resolution floor for the interval forms too.

```yaml
schedule: 2nd saturday @ 7 am
```

### `on`

*default empty*

**Event triggers**: a human-readable rule for firing the agent off application events, dispatched on the same worker tick as schedules. `schedule:` and `on:` compose as OR - either (or both) makes the agent automatic. Event-triggered runs respect the agent's own `mode:`.

Clauses are comma-separated and case-insensitive (`when`, `a`, `an`, `the` are optional filler):

| Form | Example | Fires when |
|------|---------|------------|
| `agent <slug> completed\|failed\|cancelled` | `agent stock-digest completed` | that agent's run ends that way (per vault) |
| `any agent completed\|failed\|cancelled` | `any agent failed` | any OTHER agent's run ends that way |
| `agent <slug> staged changes` / `any agent staged changes` | `agent vault-gardener staged changes` | a run staged proposals for review |
| `staging created\|approved\|rejected [by\|for <slug>]` | `staging rejected for vault-gardener` | a human decides a staged batch |
| `upload[s] [in\|to <prefix>]` | `uploads in inbox/` | a file is uploaded (optionally under a folder) |

Folder prefixes with spaces are double-quoted: `uploads in "My Folder/"`. Prefix matching is case-insensitive. Document/chat events (`document modified in x/ settled 10m`, `chat with x/`) are planned but **not available yet** - they parse, then refuse at load time.

The triggering events are described to the agent in its kickoff message and recorded in the run log's *Triggered by* section.

**Loop guards** (see the *Recent events* panel on [/agents](/agents)): an agent never matches events about itself; chained triggers stop at `EVENT_MAX_DEPTH` (default 3); after an event fire the agent cools down (`EVENT_COOLDOWN_S`, default 10 min) and is budgeted per hour (`EVENT_BUDGET_PER_HOUR`, default 6) - deferred events wait in a pool rather than being dropped, and stale ones are discarded after `EVENT_MAX_AGE_S`. Two agents whose `on:` rules name each other's runs form a cycle and are refused at load time.

```yaml
on: any agent failed, uploads in inbox/
```

### `mode`

*default `propose`*

The agent's **autonomy ceiling** - what happens to the writes its tools make.

- `propose` (default) - every write is **staged** as a shadow copy for you to review and approve in the `/agents` inbox. Nothing touches real pages until you approve.
- `act-with-checkpoint` (accepts the alias `act`) - writes are **applied immediately**, each preceded by a checkpoint commit so any change is recoverable. Grant this only to agents you trust.

There is no un-checkpointed "act" mode; `act` and `act-with-checkpoint` mean the same thing.

```yaml
mode: propose
mode: act              # alias for act-with-checkpoint
```

### `index_output`

*default `false`*

Opt **in** to RAG-indexing the agent's **output page** so it becomes
searchable/retrievable like ordinary vault content. Agent output is excluded from the index by default (it lives under `_dada/`). Only the output page is affected - run logs are never indexed.

Accepts truthy strings: `1`, `true`, or `yes` (case-insensitive).

```yaml
index_output: true
```

### `log`

*default `false`*

Opt **in** to writing a per-run **log page** under `{vault}/_dada/{slug}/logs/`. Useful for auditing what an agent did on each run. Logs are RAG-excluded by location regardless of `index_output`.

Accepts truthy strings: `1`, `true`, or `yes`.

```yaml
log: true
```

### `memory`

*default `false`*

Opt **in** to **cross-run memory**: the agent keeps a small, self-curated handoff note between runs so each run can build on the last instead of starting cold. See **Cross-run memory** below for the full mechanism.

Accepts truthy strings: `1`, `true`, or `yes` (case-insensitive).

```yaml
memory: true
```

`memory` is independent of `log` - memory is the agent's private working note; `log` is a human-facing audit trail. Turning one on does not turn on the other.

To shape *what* the agent records, write a [`# Memory Prompt`](#memory-prompt) section.  When creating a new agent, a commented example is provided.  See [`# Cross-run memory`](#cross-run-memory) for further details on how this works.

---

## Body sections

Headings are matched at the top level only (a `#` heading outside any code fence). Any prose before the first heading is ignored, so you may open the file with notes.

### `# Prompt`

**required**

The standing directive - the agent's system-prompt-style instructions. This is where you describe the agent's job, its tools, and the exact shape of the output you want. A file with no `# Prompt` section is invalid.

> [!tip] Small-LLM guardrails
> Keep the load-bearing boilerplate - e.g. *"Output ONLY the report markdown. No preamble, no code fences, no commentary."* Local models need this to stop wrapping output in fences or chatter.

### `# Memory Prompt`

*optional, only used with `memory: true`*

The instruction the consolidation turn runs on. Omit it - the usual case - and the agent uses the shared default, which means it also picks up any later improvement to that default. Write one to take ownership of the wording instead.

New agent files ship the default in this section, fenced in `%%` so it reads as a comment and the shared default stays in force. Unfence and edit to make it yours.

Placeholders `{agent_name}` and `{tool_names}` are filled in for you; any other braces are left alone, so JSON or LaTeX in your prompt is safe.

> [!warning] Don't write the tail
> The "CURRENT memory" and "TRANSCRIPT" sections are appended for you. Writing your own would hand the model two copies.

### `# Kickoff`

*optional*

The concrete first user turn that starts the loop. If omitted, defaults to *"Carry out your directive now."* Use it to pass run-specific framing while keeping `# Prompt` general.

```markdown
# Kickoff

Assess the vault's health and propose links for up to 5 orphan pages, then write your report.
```

### Custom tools

*fenced ` ```python ` blocks*

Any fenced `python` (or `py`) block defines **human-authored custom tools** - first-class functions the agent may call by name, alongside its granted capabilities. Their schemas are derived **statically** (via AST parsing - the code is never executed to build the schema); the code only ever runs inside the isolated **agent kernel**, which has no vault mount and reaches data through the thin `wiki` proxy.

Guidelines:

- Each top-level `def` becomes one tool. Give it **type hints** and a **docstring** - both feed the schema the model sees. Default values become optional parameters.
- A custom tool's name must **not collide** with a granted internal capability.
- Inside the function, use the injected `wiki` object for data access, e.g. `wiki.queryDocuments()`, `wiki.queryEdges()`, `wiki.search(q, top_k=3)`, `wiki.read(doc_id)`, `wiki.write(doc_id, body, note=...)`, `wiki.list_orphans()`. Writes still funnel through the same write gate, so `mode` governs them exactly as it governs capability writes. See [the wiki object](wiki-object.md) for the full method reference of both `wiki` objects.
- Small models sometimes mangle argument *shape* - passing the string `'[3]'` or the list `[3]` where you asked for an `int`. To recover the intended value without writing your own parsing, wrap incoming args with `wiki.as_int(v)` / `wiki.as_float(v)` / `wiki.as_str(v)`. Each takes an optional `dict.get`-style fallback used when nothing parses, e.g. `n = wiki.as_int(first_number, 7)` (without one, `as_int`/`as_float` return `None`). The fallback keys on *unparseable*, not falsiness, so a genuine `0` is kept rather than replaced.

```python
def vault_health_report() -> str:
    """One-call vault health summary: page count, orphans, top hubs."""
    import pandas as pd
    docs = pd.DataFrame(wiki.queryDocuments())
    orphans = pd.DataFrame(wiki.list_orphans())
    return f"Pages: {len(docs)}  Orphans: {len(orphans)}"
```

---

## Cross-run memory

By default an agent is **stateless between runs** - every scheduled or event-triggered run starts cold, knowing nothing of what earlier runs did. Setting `memory: true` gives the agent a small, self-curated note that carries forward from one run to the next.

### How it works

- **Storage.** The note lives at `{vault}/_dada/{slug}/memory.md` (filename configurable via `AGENT_MEMORY_FILE`). Like agent output it sits in the RAG-excluded, agent-owned area and is git-committed each run. It is **separate from the output page**: output is the human-facing report; memory is the agent's private working state. A sibling `ledgers.md` holds the append-only half (see **Ledgers** below).
- **Injection.** At the start of each run the note is injected into the agent's system prompt (head-capped at `AGENT_MEMORY_INJECT_CHARS`, default 6000, keeping the highest-priority sections). The agent is told this is its own memory from previous runs and to start from it.
- **Consolidation (the reserved turns).** When the working loop ends, the agent spends **two model calls above `max_iterations`**: the first rewrites `memory.md`, the second records to ledgers. Because they are reserved, step-exhaustion can never starve them: memory advances on **every run that actually ran** - a natural finish, `max_iterations` exhaustion, or a timeout alike. Only a hard error or a cancel skips it. They are split because a model asked for prose *and* tool calls in one turn reliably returns one or the other, not both.
- **No-clobber safety.** If the consolidation call comes back empty (e.g. a very long transcript overflowed the summarizer), the previous memory is **preserved**, never overwritten with nothing. The two writes are independent: a run whose note failed to generate still records its ledger rows.

### What to put in memory 

**(and what NOT to)**

The single most important rule: **the vault is the primary memory.** Which pages exist, their links, and their contents are all durable in the vault and re-discoverable next run via the agent's own tools. So `memory.md` should hold only the **complement** - the things the tools *cannot* reconstruct:

- **Next run** - what to do next, most valuable first.
- **In progress** - any page left deliberately unfinished, and what remains.
- **Decisions & conventions** - choices future runs must honor (naming, page structure, canon calls) that aren't obvious from the pages themselves.
- **Avoid / tried** - dead ends and things deliberately not done, so they aren't redone.

The consolidation prompt enforces **differential retention**: *Next run* and *In progress* **churn** (completed items drop off, new ones appear), while *Decisions & conventions* and *Avoid / tried* are **sticky** (carried forward unchanged - only added to or corrected). This keeps the long-lived constraints stable while the volatile plan refreshes each run.

> [!tip] Don't record a page inventory
> Because the agent re-inspects the vault every run with its tools, a memory note that lists "the pages that exist" is wasted space that also decays over time. Record the *plan and the reasoning*, not the state the tools already surface.

Write a [`# Memory Prompt`](#memory-prompt) section when your agent needs a different shape - for example a running checklist rather than a plan/decision note.

### Ledger Memory

The notes in the memory file above are rewritten by the model on every run, which makes it a *lossy* memory.  For more robust, or lossless, recollection, there are ledgers. 

A **ledger** is a named list stored beside the memory file at `{vault}/_dada/agents/{slug}/ledgers.md`, where the filename can be configured with `AGENT_LEDGERS_FILE`. The sections in the ledgers file are managed by code, not LLM, so they're never rewritten.

Retention is deliberately split in two:

| what | who enforces it |
|---|---|
| Rows **within** a ledger are append-only, deduplicated, never lost | the system |
| The ledger **itself** may be created and deleted freely | the agent |

That division is the whole point. Never losing a row is something code can guarantee and a model cannot; knowing that a list has served its purpose is something only the agent can judge.

**Two ways rows get recorded:**

- **Automatically**, in the second consolidation turn - no `capabilities:` grant needed. Every `memory: true` agent gets this, because which agents turn out to need a ledger is not knowable in advance.
- **Deliberately**, if you grant [`remember`](#ledgers) and `forget`. Prefer this when the agent should record *at the moment it decides* rather than in hindsight - a run that fails half way then still keeps what it finished.

**Ledgers are injected whenever the agent keeps them** - `memory: true` *or* a granted `remember` / `forget` / `recall`. You do not need `memory: true` just to read your rows back. What `memory: true` still buys you is the automatic recording above; without it an agent records only through the tools you granted it.

Rows are deduplicated **lexically**, so `Convex hull` and `convex hull` are the same row while `Convex hull algorithms` is a different one. Telling near-misses apart is the prompt's job; never losing a row is the ledger's.

When a ledger fills up (`AGENT_LEDGER_MAX_ITEMS`, default 500), further writes are **refused** rather than silently dropping the oldest row - a quietly truncated avoid-list is the exact failure ledgers exist to prevent. The refusal is called out in the run log's **Ledger operations** section, saying which ledger and how many rows were lost. That section only exists on a log page, so an agent that keeps ledgers is worth running with `log: true` - otherwise a full ledger is silent.

**When the ledgers outgrow the prompt.** Only a slice of the model's context window is spent on ledgers (`AGENT_LEDGER_CONTEXT_FRACTION`), so a large book cannot always be shown whole. It **degrades rather than disappears**:

- Every ledger keeps its `##` heading, whatever the budget. A ledger the agent cannot name is one it cannot ask for, so the list of names is the one thing never cut.
- Ledgers touched most recently keep the most rows - the same oldest-first, newest-last ordering the rows inside them already have.
- Anything hidden is declared in place, as `_(328 of 340 rows not shown)_`. The agent is told those rows exist and is given [`recall`](#ledgers) to read them, so it can never mistake "not shown to me" for "not recorded".

`recall` is granted **automatically** to any agent that keeps ledgers - it reads the agent's own rows, so it is not a capability you hand out. When the injected view had to be trimmed, the run log's **Ledger operations** section names the ledgers shown in part and how many rows each really holds. That is your cue to prune or `forget` one.

### Correcting or resetting memory

`memory.md` and `ledgers.md` are ordinary wiki pages in the agent's owned folder. Open and edit them like any other page - the next run reads back exactly what you left. `/manage/tasks` links both per vault.

- **Fix a wrong convention:** edit the line out. Your directive also outranks the note - if you change `# Prompt` to contradict something the agent recorded, it is told to delete that entry rather than carry it forward.
- **Reset entirely:** blank the body. An empty note reads as "first run".
- **Drop a ledger:** delete its `##` section, or let the agent `forget` it.
- **Section order means recency.** The ledger at the bottom is the one written to most recently, matching the newest-last order of rows inside it. Adding rows moves a ledger down. This is not cosmetic: when the budget is tight, the ledgers nearest the bottom keep the most rows.

---

## Capabilities menu

These are the internal tool names you may list in `capabilities:`. All are vault-scoped and (for writes) routed through the review/checkpoint gate.

### Analysis

*(read-only vault health queries)*

| Name | What it returns | Key parameters |
|------|-----------------|----------------|
| `list_orphans` | Pages with no wikilinks in or out. | `path_prefix`, `limit` (1–200, def 50) |
| `find_near_duplicates` | Highly similar but **unlinked** page pairs (merge candidates). | `threshold` (0.5–1.0, def 0.88), `path_prefix`, `limit` (def 30) |
| `find_missing_links` | Related-but-unlinked page pairs (new-link candidates). | `low` (def 0.62), `high` (def 0.88), `path_prefix`, `limit` (def 40) |
| `list_stale_stubs` | Short, long-untouched pages (likely abandoned). | `stale_days` (def 180), `max_chars` (def 400), `path_prefix`, `limit` (def 40) |

### Retrieval & reading

*(read-only)*

| Name | What it does | Key parameters |
|------|--------------|----------------|
| `search_wiki` | Hybrid semantic + full-text search across the vault. | `query` (required), `top_k` (1–25, def 8) |
| `find_related` | Pages related to one page via links, tags, embeddings. | `doc_id` (required), `top_k` (def 10) |
| `list_documents` | List pages, optionally filtered by folder and/or tag. | `path_prefix`, `tag`, `limit` (1–2000, def 500), `after` |
| `read_document` | Read a page's markdown (reads *through* the run's staged overlay). | `doc_id` (required), `max_chars` (200–20000, def 8000) |
| `get_outline` | A page's heading tree without its full text. | `doc_id` (required) |

### Ledgers

*(append-only; the agent's own durable memory, scoped by vault)*

| Name | What it does | Key parameters |
|------|--------------|----------------|
| `remember` | Append items to a named ledger, creating it on first use. Rows are deduplicated. | `ledger`, `items` (required) |
| `forget` | Delete an entire ledger that has served its purpose. | `ledger` (required) |
| `recall` | Read one ledger's rows, or list the ledgers held. Granted automatically wherever ledgers are kept. | `ledger`, `max_rows` (both optional) |

The two writers touch only `_dada/agents/{slug}/ledgers.md`, so they need no `mode` and stage nothing. Grant them when the agent should record **as it works** - a run that dies half way then keeps what it finished. Granting either also gets the ledgers **injected**, with or without `memory: true`; `recall` needs no grant at all. See **Cross-run memory** above.

### Write proposals

*(governed by `mode`)*

Every write funnels through the write gate: in `propose` mode it stages a shadow copy for the `/agents` inbox; in `act-with-checkpoint` mode it applies immediately after a checkpoint commit.

**Whole page**

| Name | What it proposes | Key parameters |
|------|------------------|----------------|
| `propose_create` | A brand-new page (errors if it already exists). | `doc_id`, `content` (required), `note` |
| `propose_edit` | Replace an existing page's full content. | `doc_id`, `new_content` (required), `note` |
| `propose_append` | Append a block to the end of an existing page. | `doc_id`, `content` (required), `note` |

**One section** *(prefer these over `propose_edit` when changing part of a page)*

| Name | What it proposes | Key parameters |
|------|------------------|----------------|
| `propose_section_edit` | Replace one section's body, keeping its heading. | `doc_id`, `section_heading`, `new_content` (required), `section_index`, `note` |
| `propose_section_insert` | Add a new section before/after an existing one. | `doc_id`, `heading`, `content` (required), `position`, `reference_section`, `reference_section_index`, `note` |
| `propose_section_delete` | Remove a section - heading, body, and anything nested under it. | `doc_id`, `section_heading` (required), `section_index`, `note` |

**One link**

| Name | What it proposes | Key parameters |
|------|------------------|----------------|
| `apply_wikilink` | Add a `[[wikilink]]` under a `## Related` section (idempotent). | `source_doc`, `target_doc` (required), `reason` |
| `remove_wikilink` | Remove a `[[wikilink]]` bullet from a page. | `source_doc`, `target_doc` (required), `reason` |

#### Addressing a section

`section_heading` takes the heading text, with or without its `#` marks - `Overview` and `## Overview` both work, and matching is case-insensitive. Two special cases:

- `(top)` addresses the text above the first heading.
- If a page repeats a heading, add `section_index` - the number `get_outline` shows in brackets - to say which one you mean.

Call `get_outline` first. If a section name does not match, the tool replies with the list of sections that *do* exist, so a second attempt can correct itself.

#### Why the menu has removal tools

Each granularity can add, change **and** remove. This is deliberate: an agent given only add-shaped tools cannot maintain a structure, it can only inflate it. An agent asked to keep a vault's links tidy needs `remove_wikilink` for the same reason an agent asked to keep pages tidy needs `propose_section_delete` - otherwise every run can only make the vault bigger.

`remove_wikilink` is intentionally narrow. It removes list bullets whose only content is the link - the shape `apply_wikilink` writes. A link written into a sentence is reported and left alone, because deleting it would mean rewriting someone's prose; use `propose_section_edit` if that sentence genuinely needs to change.

---

## Complete example

`````markdown
---
type: agent
description: Assesses vault health and proposes links for orphan pages.
vaults: *
mode: propose
output: Vault Health.md
max_iterations: 6
schedule: 2nd saturday @ 7 am
log: true
memory: true
---

# Prompt

You maintain a tidy wiki. Assess its health, then write 
a short markdown report. Output ONLY the report markdown.
No preamble, no code fences, no commentary.

# Kickoff

Assess the vault and propose links for up to 5 orphan
pages, then write your report.

```python
def vault_health_report() -> str:
    """One-call vault health summary."""
    import pandas as pd
    docs = pd.DataFrame(wiki.queryDocuments())
    orphans = pd.DataFrame(wiki.list_orphans())
    return f"Pages: {len(docs)}  Orphans: {len(orphans)}"
```

`````

---

## Validation checklist

Before an agent will run, the registry checks that:

- the filename slug is lowercase alphanumeric / `-` / `_`;
- frontmatter declares `type: agent`;
- `output` is a plain filename (no `/`, no leading `.`);
- every `capabilities:` name exists in the menu;
- `max_iterations`, if present, is an integer;
- `schedule`, if present, parses under the grammar above;
- `mode` is `propose`, `act`, or `act-with-checkpoint`;
- there is a non-empty `# Prompt` section;
- the agent grants at least one tool (a capability or a custom `python` tool);
- no custom tool name collides with an internal capability, and the `python`   source has no syntax errors.

Any failure marks the agent invalid; invalid agents are listed but never executed.

## Related

- [Main](../Main.md)
- [authoring editors](authoring_editors.md)
- [editors](editors.md)
