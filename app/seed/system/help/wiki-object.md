---
title: The wiki object
Summary: The `wiki` object is automatically injected into every Python kernel, with two variants: a read‑only page‑kernel client (`_WikiClient`) for inline Jupyter cells, and a richer agent‑kernel client (`_AgentWiki`) that can read page text, stage or apply writes, and edit specific sections, all confined to a single vault via server‑side isolation and HMAC tokens. Both expose the same read‑only query methods (e.g., `search`, `queryDocuments`), while the agent version adds text access, granular editing helpers, and argument‑coercion utilities for custom tools.
Tags: python, jupyter, wiki-api, agent, vault, hmac, markdown
---

# The `wiki` object

Every Python kernel Tzara spawns is seeded with an object named `wiki` that reaches back into the vault index. There are **two** implementations - same name, same read-only core, different execution context and different powers:

- The **page `wiki`** (`_WikiClient`, in `jupyter_client.py`) lives in a **markdown page kernel** and backs the inline ```` ```jupyter ```` cells you write in a document.
- The **agent `wiki`** (`_AgentWiki`, in `agent_kernel.py`) lives in the **isolated agent kernel** and backs the custom Python tools an agent file defines.

Both are injected automatically when the kernel starts - you never construct one. Both are pinned to a single vault at injection time, and vault isolation is enforced **server-side**: a kernel can only ever see its own vault.

*[HMAC]: hash-based message authentication code 

## Which one am I using?

| | Page `wiki` (`_WikiClient`) | Agent `wiki` (`_AgentWiki`) |
|---|---|---|
| Runs in | `jupyterserver` kernel (page cells) | `jupyterserver-agent` kernel (agent tools) |
| Reaches | Tzara server, `/api/kernel/{vault}/query` | worker agent-API (`/query`, `/read`, `/write`) |
| Network | `tzara-net` (full vault file access) | `agent-net` only (no vault mount, no DB/LLM route) |
| Authorization | none needed (trusted network) | per-run HMAC token - **confines** the caller to one vault + one kernel, it is not a login |
| Query the index | yes | yes |
| Read page **text** | no | yes - `read()` (sees your run's own staged edits) |
| Write pages | no - use the editor | yes - `write()` / `write_file()`, funneled through the write gate |
| Argument coercion helpers | no | yes - `as_int` / `as_float` / `as_str` |

If you are writing a ```` ```jupyter ```` cell in a page, you have the page `wiki`. If you are writing a fenced `python` tool inside an agent file, you have the agent `wiki`.

## Shared read-only methods (both objects)

Every method returns plain lists/dicts, so you can drop the result straight into `pandas.DataFrame(...)`.  The three whole-table reads (`queryDocuments` / `queryEdges` / `queryDocumentTags`) **take no arguments and always return the complete table** - there is no page number, cursor, or `limit` to pass. They are keyset-paginated to **completeness** internally (the client repeatedly asks the server for the next page past an opaque cursor and stitches every page together before returning), so vault size never silently caps the result. Want only a subset? Filter the returned list in Python - e.g. `[d for d in wiki.queryDocuments() if d['title'].startswith('Programming/')]`.

| Method | Returns |
|--------|---------|
| `wiki.search(query, top_k=10)` | relevant chunks: `doc_id, title, header_path, snippet, score` |
| `wiki.related(path, top_k=10)` | docs related by links/tags/embeddings |
| `wiki.tagged(tag)` | docs carrying a tag: `doc_id, title, summary` |
| `wiki.backlinks(path)` | docs that link to `path` |
| `wiki.frontmatter(path)` | one doc's `title, summary, tags, outbound_links, backlinks` |
| `wiki.queryDocuments()` | every document row in the vault |
| `wiki.queryEdges()` | every link edge in the vault |
| `wiki.queryDocumentTags()` | every document/tag pairing |
| `wiki.list_orphans(path_prefix="", limit=50)` | real pages with no resolved wikilink in or out |
| `wiki.find_near_duplicates(path_prefix="", threshold=0.88, limit=30)` | unlinked doc pairs that say nearly the same thing |
| `wiki.find_missing_links(path_prefix="", low=0.62, high=0.88, limit=40)` | related-but-unlinked pairs - link candidates |
| `wiki.list_stale_stubs(path_prefix="", max_chars=400, stale_days=180, limit=40)` | short pages not updated in a while |

`path` accepts a vault-relative page path with or without a leading `/` or the `.md` suffix (e.g. `Programming/Code/Pytorch`).

```jupyter
import pandas as pd

pd.DataFrame(wiki.tagged("help"))          # every note tagged "help"
pd.DataFrame(wiki.search("jupyter", top_k=10))  # hybrid + graph-expanded search
pd.DataFrame(wiki.related("help"))         # neighbors by link/tag/embedding
```

## Agent-only methods

The agent `wiki` adds page **text** access and **writing**. Writes never touch disk directly from the sandbox - they funnel through the write gate. Custom tools have no other way of accessing vault content, so the `wiki` object provides gated access.  The agent's `mode` decides whether writes are staged for human review or applied.

| Method | Effect |
|--------|--------|
| `wiki.read(path)` | Returns the page's markdown, reading **through** this run's staged overlay (you see your own not-yet-applied edits). |
| `wiki.write(path, content, note="")` | STAGES a write proposal for human review - unless `path` is inside the agent's own output folder, where it applies directly. |
| `wiki.write_file(path, data, note="")` | Saves a binary attachment (png/csv/...) inside the agent's own output folder only; link it from your pages. `data` may be `bytes` or `str`. |

### Targeted edits - change one part of a page

`wiki.write()` replaces a whole file. That is right when your tool composed the whole file, but usually a hand-written tool knows exactly which part it means: *rewrite the Status section*, *file this link*, *drop that one*. These methods say that directly and leave the rest of the page byte-identical - so nothing is lost to a regeneration that only partly understood the document, and the human reviews a diff the size of the actual change.

They go through the same write gate as `wiki.write()`: staged for review in `propose` mode, applied with a checkpoint commit in `act` mode.

| Method | What it does |
|--------|--------------|
| `wiki.outline(path)` | Heading tree with the `index` numbers the other methods accept. |
| `wiki.readSection(path, heading, index=None)` | That section's body text (reads through your staged overlay). |
| `wiki.editSection(path, heading, content, index=None, note="")` | Replaces one section's body; the heading and the rest of the page stay. |
| `wiki.insertSection(path, heading, content, position="after", reference=None, reference_index=None, note="")` | Adds a section before/after `reference` - or at the end of the page when `reference` is omitted. |
| `wiki.deleteSection(path, heading, index=None, note="")` | Removes a section: heading, body, and anything nested under it. |
| `wiki.addLink(path, target, reason="")` | Adds `- [[target]]` under `path`'s `## Related` section. Idempotent. |
| `wiki.removeLink(path, target, reason="")` | Removes a `## Related` bullet pointing at `target`. |

**Naming a section.** `heading` takes the heading text with or without its `#` marks - `"Overview"` and `"## Overview"` both work - and matching is case-insensitive. `"(top)"` addresses the text above the first heading. If a page repeats a heading, pass `index` (the number `outline()` shows in brackets) to say which one. A name that does not match raises with the list of sections that do exist.

**What `removeLink` will not touch.** Only plain list bullets whose sole content is the link - the shape `addLink` writes. A link inside a sentence, a bullet carrying other prose, or a task item (`- [ ]` / `- [x]`) is reported back and left alone: a task records outstanding or completed work, and prose is someone's writing. Use `editSection` if that text genuinely needs to change.

**Not for your own output folder.** These target reviewable vault pages. Your own `_dada/<agent>/` files you write whole with `wiki.write()`.

```python
# Refresh one section of a status page, leaving everything else alone.
rows = wiki.queryDocuments()
stale = [r for r in rows if (r.get("summary") or "") == ""]
wiki.editSection(
    "Vault/Status.md", "Missing summaries",
    "\n".join(f"- [[/{r['doc_id'][:-3]}]]" for r in stale[:20]) or "_none_",
    note="nightly refresh",
)
```

### Recovering malformed arguments

Small models sometimes mangle the *shape* of a tool argument - passing the string `'[3]'` or the list `[3]` where you asked for an `int`. The agent `wiki` carries three coercers so custom tools don't have to hand-roll parsing:

| Method | Recovers |
|--------|----------|
| `wiki.as_int(value, default=None)` | first integer in the value; `default` when nothing parses |
| `wiki.as_float(value, default=None)` | first number (with decimals); `default` when nothing parses |
| `wiki.as_str(value, default="")` | the useful string inside a dict/list/scalar; `default` for an absent (`None`) value |

The optional fallback works like `dict.get`, and it keys on **unparseable**, not falsiness - so a genuine `0` survives rather than being replaced:

```python
def add(first_number: int, second_number: int) -> str:
    """Add two numbers the model supplied."""
    a = wiki.as_int(first_number, 0)   # '[3]' -> 3, 'oops' -> 0, '0' -> 0
    b = wiki.as_int(second_number, 0)
    return str(a + b)
```

See [authoring agents](authoring_agents.md) for how custom tools, capabilities, and the write gate fit together.

## How they are wired

The two objects are pure-stdlib source (`urllib` + `json`) injected as text into a fresh kernel, so nothing from the project needs to be importable inside the sandbox. `jupyter_client.py` and `agent_kernel.py` hold the implementations; the agent's HMAC token is minted per run and marshals its calls through a taskiq worker that confines them to one vault and one kernel. See [jupyter technical details](jupyter/jupyter-technical-details.md) for the two-server rationale and the network/isolation diagram.

## Related

* [jupyter](jupyter.md)
    * [jupyter examples](jupyter/jupyter-examples.md)
    * [jupyter more examples](jupyter/jupyter-more-examples.md)
    * [jupyter technical details](jupyter/jupyter-technical-details.md)
* [authoring agents](authoring_agents.md)
* [agent security](agent-security.md)
