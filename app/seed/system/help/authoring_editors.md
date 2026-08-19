---
title: Authoring Editor Tools
description: Reference for every frontmatter field and section in an editor-tool definition file.
Tags: editor-tools, markdown, llm, custom-python, memory, capabilities
Summary: Editor tools are markdown files stored in the system vault that add commands to the edit‑mode “/” menu, letting you run an LLM on a selection, the whole document, or the caret context and replace, prepend, append, insert, or note the result. They are defined by required frontmatter (type, label, description, scope, operation, etc.), a mandatory # Prompt, optional # Memory Prompt, and optional fenced Python blocks for custom functions, with limited capabilities, optional cross‑invocation memory, and logging.
---

# Overview

An **editor tool** is a single markdown file you place in the **system vault** under `editors/{slug}.md`. Like an agent, its *location is its blessing*: only a human can put a file in the system vault, so the file existing here IS the trust grant that lets it run.

An editor tool adds a custom command to the **edit-mode "/" menu**. While editing any page, type `/`, pick your tool, and it runs an LLM over your selection, the whole document, or the text around your caret, then either transforms the text in place or files an external note.

An editor file has up to three parts:

1. **Frontmatter** - the YAML-style block between `---` fences (all fields below).
2. **`# Prompt`** section (required) - the directive the model follows.
3. Optional fenced ` ```python ` blocks that define custom tools.

The filename (minus `.md`) is the tool's **slug**: lowercase letters, digits, `-`, or `_` (e.g. `british.md`, `decoder_ring.md`). The slug names the tool's owned output area and appears on the [/editors](/editors) management page.

*[LLM]: Large Language Model
*[AST]: Abstract Syntax Tree

## How editor tools differ from agents

Editors and agents share the same underlying loop, but their roles are opposite:

| | **Agent** | **Editor** |
|---|---|---|
| How it runs | Autonomously - scheduled, event-triggered, or manual from `/agents` | Interactively - you invoke it from the "/" menu while editing |
| What it works on | A whole vault, via its tools | The text you have selected (or the current buffer) |
| Where output goes | New/edited vault pages (staged or applied) | Back into the document you're editing, or an external note |
| Writes to your pages | Yes (through the write gate: staged / checkpointed) | **No** - an editor's only output is its accept/reject text |

Because an editor never writes to arbitrary pages, it has no `mode:` and a much smaller, read-only capability set.

---

## Frontmatter fields

### `type`

**required, must be `editor`**

```yaml
type: editor
```

A file in `editors/` is only treated as an editor tool if it declares `type: editor`. Any other file there (a note, a stray agent) is ignored by the menu - and shown on the [/editors](/editors) page as *"not an editor tool"* so you can spot a forgotten `type:`.

### `label`

*default: the slug*

The name shown in the "/" menu.

```yaml
label: "British Spelling"
```

### `description`

A one-line summary, shown as the menu item's hover tooltip.

```yaml
description: "Rewrite the selection in British spelling."
```

### `scope`

*default `selection`*

Which text the tool reads, and therefore where in the "/" menu it can be used. Each scope also defines the tool's **range** - the stretch of document `operation` acts on.

- `selection` - the highlighted text; the range is the selection. The menu item is unavailable until something is selected.
- `document` - the whole unsaved buffer, frontmatter excluded; the range is the body. Always available.
- `cursor` - **nothing is selected.** The tool reads the document *around the caret*, marked with `<<CURSOR>>`. The range is the block the caret sits in - see `operation` below. Always available.

```yaml
scope: selection
```

Use `cursor` for anything **generative** - a tool that writes text that isn't there yet. The alternative is `scope: selection`, which would force the user to select the very text they were trying to create.

### `operation`

*default `replace`*

Where the tool's result goes. Four of the five position themselves against the **range** that `scope` defined:

- `replace` - the range is swapped out for the result.
- `prepend` - the result goes immediately **before** the range; nothing is removed.
- `append` - the result goes immediately **after** the range; nothing is removed.
- `insert` - the result goes **at the caret**, whatever the scope. This is the only operation that ignores the range, and the only one that can land in the middle of a line - which is exactly what a "continue this sentence" tool needs.
- `note` - **the document isn't touched at all**; the result is appended to an external digest page (see `output`). Use this for "collect this passage into a running list" tools.

```yaml
operation: replace
```

### Which one do I want?

| I want to… | operation |
|---|---|
| change text that's already there | `replace` |
| add something above / below it | `prepend` / `append` |
| write at the exact point I'm standing, possibly mid-sentence | `insert` |
| collect something onto a separate page | `note` |

### All fifteen combinations

| `scope` \ `operation` | `replace` | `prepend` | `append` | `insert` | `note` |
|---|---|---|---|---|---|
| **`selection`** | replace the selection | before the selection | after the selection | at the caret | digest page |
| **`document`** | replace the body | top of the body | end of the body | at the caret | digest page |
| **`cursor`** | replace the block | before the block | after the block | at the caret | digest page |

Examples down the diagonal: *"rewrite this to be funny"* (`selection`/`replace`), *"write a lede for this section"* (`selection`/`prepend`), *"extract bullet points as a summary"* (`document`/`append`), *"add a TL;DR at the top"* (`document`/`prepend`), *"reformat this paragraph"* (`cursor`/`replace`), *"continue this sentence"* (`cursor`/`insert`), *"list the key dates"* (`document`/`note`).

Three details worth knowing:

- **With `scope: cursor`, the range is the block the caret sits in** - the paragraph, list, or whole fenced code block. So `replace` reformats that paragraph without you selecting it, and `prepend`/`append` put text before or after the *whole* paragraph. If the caret isn't in a block (a blank line, an empty page), there is no range and all three behave like `insert`.
- **`insert` follows the caret, and the caret follows your mouse.** Selecting text leaves the caret at one *end* of the selection - which end depends on which way you dragged. So `selection` + `insert` lands before or after the selection accordingly. If you want a fixed side, that's what `prepend` and `append` are for.
- **Blank lines are added for you.** When an added block lands at a block boundary, the separating blank line is inserted automatically - on whichever side needs it, including the leading side when you `append` to the very end of a document. Don't write prompt instructions about blank lines; models strip surrounding whitespace no matter what they're told, so this is handled in code instead.

Everything except `note` is reviewed before anything changes - the result appears as a proposal you accept or reject.

### `output`

*default `Notes.md`, only meaningful with `operation: note`*

The filename of the digest page an `operation: note` tool appends to. Must be a **plain filename** - no `/`, no leading `.`. It is written to the tool's owned area: `{vault}/_dada/editors/{slug}/{output}`, and grows across invocations (and across the different documents you run the tool from).

```yaml
operation: note
output: Reading-journal.md
```

### `capabilities`

*default: none*

A comma-separated list of internal tools to grant. Editors may grant only these four:

| Name | What it does |
|------|--------------|
| `search_wiki` | Hybrid semantic + full-text search across the vault. |
| `find_related` | Pages related to a given page via links, tags, embeddings. |
| `remember` | Append items to one of the tool's own append-only ledgers. |
| `forget` | Delete a ledger that has served its purpose. |
| `recall` | Read one ledger's rows, or list the ledgers held. Granted automatically wherever ledgers are kept. |

```yaml
capabilities: search_wiki, remember
```

`remember` and `forget` are not an exception to the read-only rule: they write **only** to the tool's own ledger under `_dada/editors/{slug}/`, never to your vault. `recall` only reads them, and needs no grant. See **Cross-invocation memory** below.

> [!Info] Why so few?
> An editor's job is to transform the text in front of you, not rewrite your vault. Write/propose tools and current-document readers (`read_document`, `get_outline`) are deliberately **excluded**: writes aren't an editor's remit, and the live buffer - not the on-disk copy - is what the tool should see (it arrives as the input text and on the `editor` object).

Unlike an agent, an editor tool **need not grant any tool at all** - a pure-prompt editor (no `capabilities`, no Python) is a perfectly valid saved prompt.

### `max_iterations`

*default 4*

Integer cap (must be positive) on how many tool-calling steps the loop may take. The default is deliberately low so a tool that keeps searching can't spin. Raise it for tools that chain several tool calls.

```yaml
max_iterations: 6
```

### `vaults`

*default `*`*

An **availability whitelist**: which content vaults this tool appears (and runs) in.

- `*` (or omitted) - every content vault.
- A comma-separated list - only those vaults.

```yaml
vaults: fiction, worldbuilding
```

The system vault itself is never a target. Unknown vault names aren't an error (you can author a tool before a vault exists); the tool simply won't appear there.

### `memory`

*default `false`*

Opt **in** to **cross-invocation memory**: the tool keeps a small, self-curated note that persists across runs and across documents, so each run can build on what it has accumulated. See **Cross-invocation memory** below.

Accepts truthy strings: `1`, `true`, `yes`.

```yaml
memory: true
```

To shape *what* the tool records, write a [`# Memory Prompt`](#-memory-prompt) section rather than setting a frontmatter field.

### `log`

*default `false`*

Opt **in** to a per-invocation **log page** under `{vault}/_dada/editors/{slug}/logs/`. Each log records the input span, tool activity, the result, and - for a memory tool - the memory **before and after**, which makes the (necessarily fuzzy) memory consolidation inspectable.

```yaml
log: true
```

---

## Body sections

### `# Prompt`

**required**

The directive - what the tool should do with the input text. A file with no `# Prompt` is invalid.

> [!tip] Small-LLM guardrails
> Keep the load-bearing boilerplate - e.g. *"Output ONLY the transformed text. No preamble, no code fences, no commentary."* Local models need this to stop wrapping output in fences or chatter. The tool's terminal text is what lands in your document (or note), so stray chatter lands there too.

### `# Memory Prompt`

*optional, only used with `memory: true`*

The instruction the consolidation turn runs on. Omit it - the usual case - and the tool uses the shared default, so it also picks up any later improvement to that default. Write one to take ownership of the wording instead.

New editor files ship the default in this section, fenced in `%%` so it reads as a comment and the shared default stays in force. Unfence and edit to make it yours. The placeholder `{label}` is filled in for you; any other braces are left alone.

> [!warning] Don't write the tail
> The "CURRENT memory" and "TRANSCRIPT" sections are appended for you. Writing your own would hand the model two copies.

### Custom tools

*fenced ` ```python ` blocks*

Any fenced `python` block defines **human-authored custom tools** - functions the model may call by name. Schemas are derived **statically** (AST parsing; the code is never executed to build the schema). The code runs only inside the **isolated editor kernel** - the same sandbox agents use: no vault mount, no direct database or filesystem access, reaching data through the injected `wiki` proxy.

Guidelines:

- Each top-level `def` becomes one tool. Give it **type hints** and a **docstring** - both feed the schema the model sees. Names starting with `_` are private helpers, not tools.
- A custom tool's name must **not collide** with a built-in capability.
- Inside the function you have two injected objects: **`editor`** (the document being edited) and **`wiki`** (corpus access), documented next.

```python
def rot13():
    """Return the selected text in ROT13 - a decoder ring for your notes."""
    import codecs
    return codecs.encode(editor.selection, "rot_13")
```

---

## The `editor` object

Custom tools receive an `editor` object: a read-only snapshot of the document being edited *this* invocation. It exists only for editor tools (nothing else exposes the live buffer).

| Attribute | Value |
|-----------|-------|
| `editor.selection` | the highlighted text (`""` when nothing is selected) |
| `editor.document` | the text your tool operates in: the whole unsaved buffer, minus the frontmatter block for a `scope: document` tool |
| `editor.frontmatter` | the buffer's parsed YAML frontmatter, as a `dict` |
| `editor.path` | the document's vault path (may be `""` for a brand-new doc) |

It also tells you **where the user is**, which is what a tool needs in order to write text that fits its destination. All offsets index into `editor.document`.

| Attribute | Value |
|-----------|-------|
| `editor.selection_start` | start of the **range** |
| `editor.selection_end` | end of the range |
| `editor.before` | everything before the **range** |
| `editor.after` | everything after the range |
| `editor.cursor` | the **caret** offset |
| `editor.before_cursor` | everything before the caret |
| `editor.after_cursor` | everything after the caret |

There are two pairs because there are **two positions**: the range your `operation` acts on, and the caret the user left behind. `editor.before + editor.selection + editor.after` always reconstructs `editor.document`; the `_cursor` pair always splits it at the caret.

**They are not always the same**, and which is which follows from `scope` and `operation`:

| | `.before` ends at | same as `.before_cursor`? |
|---|---|---|
| `selection` (any op) | the start of the selection | **only** if you dragged backwards, leaving the caret there |
| `document` (any op) | the start of the body - so `.before` is `""` | no - the caret is wherever you were standing |
| `cursor` + `insert` / `note` | the caret (the range is empty) | **yes**, always - and `editor.selection` is `""` |
| `cursor` + `replace` / `prepend` / `append` | the start of the **block** | no - the caret is somewhere inside that block |

That last row is the one that surprises people. With `scope: cursor` and a range operation, the range is the paragraph you're standing in, so `editor.selection` is that whole paragraph and `editor.before` stops at its first character - while `editor.before_cursor` runs all the way to where your caret actually is, part-way through it. Use `.before` / `.after` to reason about **what the tool will change**, and the `_cursor` pair to reason about **where the user was**.

```python
def word_count():
    """Report word and character counts for the current selection."""
    s = editor.selection
    return f"{len(s.split())} words, {len(s)} characters"


def neighbors(chars: int = 400):
    """Report the paragraphs immediately before and after the insertion point."""
    n = wiki.as_int(chars, 400)
    prev = editor.before.rstrip().rsplit("\n\n", 1)[-1][-n:]
    nxt = editor.after.lstrip().split("\n\n", 1)[0][:n]
    return f"PREVIOUS:\n{prev or '(start of document)'}\n\nNEXT:\n{nxt or '(end of document)'}"
```

## The `wiki` object

For corpus access (searching, reading, and - if you write a note yourself - writing owned pages), custom tools use the same `wiki` proxy agents use. See **[the wiki object](wiki-object.md)** for the full method reference; the arg-coercion helpers (`wiki.as_int`, `wiki.as_float`, `wiki.as_str`) are handy for small models that mangle argument shape.

```python
def related_pages(query: str):
    """Search the wiki and list the top related page titles for QUERY."""
    hits = wiki.search(query, top_k=5) or []
    return "\n".join("- " + (h.get("title") or h.get("path") or "?") for h in hits) \
        or "(no related pages found)"
```

---

## Cross-invocation memory

By default an editor tool is **stateless** - each run starts cold. Setting `memory: true` gives it a small, self-curated note that carries forward from one invocation to the next, *including across different documents*.

- **Storage.** The note lives at `{vault}/_dada/editors/{slug}/memory.md` - RAG-excluded, git-committed, separate from any `operation: note` digest.
- **Injection.** At the start of each run the note is injected into the tool's prompt, so the run builds on what it has accumulated.
- **Consolidation.** After the working loop, two extra model calls run: the first rewrites `memory.md`, *assimilating* this run's contribution - merging related points and keeping it concise, rather than blindly appending. If that call comes back empty, the previous memory is preserved (never clobbered). A garbled (malformed) run is skipped so it can't corrupt good memory; and because `memory.md` is git-committed every write, any bad overwrite is recoverable from history.

`memory` is orthogonal to `operation`: a `note` tool with `memory: true` keeps *both* a raw append digest (the `output` page) **and** a consolidated `memory.md`. Write a [`# Memory Prompt`](#-memory-prompt) to shape what the note records.

> [!Info] Memory is fuzzy by design
> The note is built by summarizing "what just happened," which is lossy. Turn on `log:` to see the before/after of each consolidation and judge whether it's keeping what you care about.

### Ledgers: the part memory can't hold

The note is **rewritten by the model on every invocation**. That is right for a digest and wrong for a list that must only ever grow: an entry surviving fifty runs would have to survive fifty verbatim re-copies, and it does not - rows quietly go missing while the note still looks healthy.

So anything that must never be lost goes in a **ledger** instead: a named list at `{vault}/_dada/editors/{slug}/ledgers.md`, one `##` section per ledger, merged by code rather than rewritten by the model. Rows within a ledger are append-only and deduplicated; the ledger itself the tool may create and `forget` freely.

Rows get recorded two ways - automatically in the second consolidation call (no grant needed), and deliberately if you grant [`remember`](#capabilities). This is the same mechanism agents use, and behaves identically.

Ledgers are **injected whenever the tool keeps them** - `memory: true` *or* a granted `remember` / `forget` / `recall`. You do not need `memory: true` just to read your rows back.

A ledger that fills up (`AGENT_LEDGER_MAX_ITEMS`, default 500) **refuses** further rows rather than dropping the oldest, and the refusal is called out in the invocation log's **Ledger operations** section. That section only exists on a log page, so a tool that keeps ledgers is worth running with `log: true` - otherwise a full ledger is silent.

Ledgers too large for the prompt **degrade rather than disappear**: every ledger keeps its `##` heading and true row count however tight the budget, the most recently written keep the most rows, and anything hidden is declared as `_(328 of 340 rows not shown)_` so the tool can fetch it with [`recall`](#capabilities). Section order on the page means recency - the ledger at the bottom was written to most recently, and keeps the most rows when space is short.

### Correcting or resetting memory

`memory.md` and `ledgers.md` are ordinary wiki pages. Edit them like any other page - the next invocation reads back exactly what you left. Blank the note to reset it; delete a `##` section to drop a ledger. Your `# Prompt` also outranks the note: if you change the directive to contradict something the tool recorded, it is told to delete that entry rather than carry it forward.

---

## Where a tool's files live

Everything an editor tool owns sits under the vault's RAG-excluded `_dada/editors/{slug}/` folder:

- `output` digest (for `operation: note`),
- `memory.md` and `ledgers.md` (for `memory: true`),
- `logs/` (for `log: true`).

These are hidden from search and normal navigation, but you can browse them in the **[Index](/index/{{vault}})** file manager (collapse the `_dada` folder to keep it out of the way). An `operation: note` run also gives you a clickable link to the digest right in the save confirmation.

---

## Complete examples

**Pure-prompt transform** (no tools):

`````markdown
---
type: editor
label: "British Spelling"
description: "Rewrite the selection in British spelling."
scope: selection
operation: replace
---

# Prompt

Rewrite the selected text in British spelling (colour, organise, …).
Output ONLY the rewritten text. No preamble, no code fences.
`````

**Generative insert at the caret** (nothing selected):

`````markdown
---
type: editor
label: "Filler Paragraph"
description: "Write a paragraph that bridges the text before and after the caret."
scope: cursor
operation: insert
---

# Prompt

Write ONE short paragraph for the caret position. It must read as a natural
bridge between the paragraph that precedes the caret and the paragraph that
follows it: pick up where the previous paragraph left off and lead into the
subject of the next one. Match the surrounding voice, tense, and level of
formality. Do not restate either neighboring paragraph, and do not add a
heading.
`````

The prompt can talk about "the paragraph before" and "the paragraph after" because `scope: cursor` hands the model the document with the caret marked as `<<CURSOR>>` - it can see both sides. The same tool written as `scope: selection` could not even be *started* without selecting something first.

**Custom Python tool** (runs in the isolated kernel):

`````markdown
---
type: editor
label: "Decoder Ring"
description: "Encode the selection with a classic cipher."
scope: selection
operation: replace
---

# Prompt

Call the cipher tool the user names (ROT13 by default) and output ONLY
its exact return value. No preamble, no code fences.

```python
def rot13():
    """ROT13-encode the selected text."""
    import codecs
    return codecs.encode(editor.selection, "rot_13")
```
`````

**Note + memory digest** (grows across documents):

`````markdown
---
type: editor
label: "Add to Glossary"
description: "Extract terms from the selection into a consolidated glossary."
scope: selection
operation: note
output: Glossary-entries.md
memory: true
capabilities: remember
log: true
---

# Prompt

From the selected passage, extract notable terms, characters, or places,
each with a one-line definition. Output ONLY those lines, no preamble.
`````

---

## Validation checklist

An editor tool must pass these to appear in the menu (the [/editors](/editors) page shows the reason for any that don't):

- the filename slug is lowercase alphanumeric / `-` / `_`;
- frontmatter declares `type: editor`;
- `scope` is `selection` or `document`;
- `operation` is `replace`, `insert`, or `note`;
- `output` is a plain filename (no `/`, no leading `.`);
- every `capabilities:` name is one of `search_wiki`, `find_related`;
- `max_iterations`, if present, is a positive integer;
- there is a non-empty `# Prompt` section;
- any `python` source has no syntax errors and no tool name collides with a built-in capability.

A tool may grant **no** tools at all (a pure prompt is valid). Any failure marks the tool invalid; invalid tools are listed on `/editors` but never offered in the menu.

## Related
- [editors](editors.md) - what editor tools are, conceptually
- [the wiki object](wiki-object.md) - the `wiki` proxy method reference
- [agent security](agent-security.md) - the isolation model custom tools run under (shared with agents)
- [Main](../Main.md)
