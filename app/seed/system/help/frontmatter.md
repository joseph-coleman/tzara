---
title: Frontmatter
summary: Every metadata key Tzara reads from the top of a page, and what each one changes.
GenerateMetadata: false
---

# Frontmatter

*[YAML]: Yet Another Markup Language or YAML Ain't Markup Language

A page can open with a block of metadata fenced by `---`. Obsidian calls this frontmatter, so we do too.  The frontmatter section is YAML.  Or YAML-ish.  If you're using your markdown files with other tools, such as [Jekyll](https://jekyllrb.com/), they often have their own vocabulary of metatdata they recongize and interpret.  This document highlights Tzara specific fields and interpretations. The format is roughly a `key` and `value` pair with a colon, `:`, seperating them.  

```markdown
---
Title: Ganymede Station
Tags: expanse, worldbuilding
---

# Ganymede Station

Notes on the dome...
```

The entire frontmatter section is optional.  A page with no frontmatter fine, and most pages probably don't even need a frontmatter. However, there are a handful of these items that change behavior, e.g. whether a page is searchable, who or what creates summaries and tag, what voice the editor tools write in, and more. 

[TOC]

## Rules and Caveats

There are a few items to be aware of, some specific to Tzara.

* A frontmatter starts the file with an opening `---` on the first line, and first line only, so no blank (or otherwise) lines can appear before the opening. 
* Keys are not case sensitive, so `Title`, `title`, and `TITLE` are the same thing.
* Tzara specific, list can be written as a bracketed comma seperated list, `Tags: [a, b]`, or as an indented `- a` form. Tzara tries to accept both, however, they both get stored as a naked comma seperated value, such as `Tags: a, b`.
* Tzara currently does not recognize end of line comments, so  don't put a `#` comment at the end of a value line. Everything after the first `:` is taken verbatim, so `Tags: alpha # todo` gives you a tag literally named `alpha # todo`. Put comments on their own line instead.
* Tzara only writes tags and a summary into a page that already has a frontmatter block. If you don't want the frontmatter metadata, simply delete the whole thing.  It will **not** be recreated, even if auto generate summary and tags is turned on.

## Keys Tzara reads

This is a partial list.  Both [editors](editors.md) and [agents](agents.md) have additional parameters that affect their behavior.

| Key | What it does |
|-----|--------------|
| `Title` | The title shown at the top of the page, and the name Tzara uses everywhere else it refers to the document - search results, graph nodes, backlinks, and the header bar on an `![[embed]]`. The **file name still decides the URL**, so changing the title never breaks a link. Defaults to the file name. |
| `Tags` | Your tags. Comma-separated, or a YAML list. **Never rewritten** - see [who owns your tags](#who-owns-your-tags-and-summary). |
| `AutoTags` | The LLM's tags, replaced wholesale on every metadata run. Yours to read, not to curate - anything you put here will be overwritten. |
| `Summary` | One line, shown in the sidebar. It's also the text that gets embedded as the document's overall vector, which means a good summary measurably improves how often the page turns up in a semantic search. Without one, the opening of the page is used instead. |
| `Index` | Set to `false` to keep the page out of search. See [keeping a page out of search](#keeping-a-page-out-of-search). Defaults to `INDEX_DOCUMENT_FRONTMATTER_DEFAULT` from [configurations](configurations.md), which ships as `True`. |
| `GenerateMetadata` | Set to `false` to stop the LLM writing `AutoTags:`, `Summary:` and `MetadataUpdated:` into this page, while leaving it fully searchable. |
| `Created` | When the page was made. Written once, by the starter template, and never touched again. |
| `Updated` | When the page last changed. Rewritten by every save Tzara performs. See [timestamps](#timestamps). |
| `MetadataUpdated` | When the LLM last changed this page's `AutoTags`/`Summary`. Deliberately separate from `Updated`. |
| `audience`, `voice`, `tone`, and/or `style` | Free text, handed to the Editor whenever you use the `/` menu on that page. See [steering what the editor writes](#steering-what-the-editor-writes). |

Anything else you write is left alone. It's parsed, it just isn't used - see [keys Tzara ignores](#keys-tzara-ignores).

## Who owns your tags and summary

Tags live in two fields, and the split is about who is allowed to write them:

```markdown
---
Tags: canon, ganymede
AutoTags: station, dome, agriculture
---
```

`Tags` is **yours**. Nothing in Tzara ever rewrites it. Put whatever you want there and it stays exactly as you left it, forever.

`AutoTags` belongs to the background metadata task, which replaces the whole line after each save when `AUTO_GENERATE_TAGS` is on (it ships on). Editing it is pointless - your changes last until the next save.

**Everything that uses tags uses both.** The sidebar chips, search filtering, `wiki.tagged()` in a code cell, the graph's tag overlay, and RAG retrieval all work off the union, yours listed first. Splitting who *writes* the tags does not split what the tags *do*, so you never have to think about which field a tag came from when you go looking for it.

A tag in both fields counts once.

`Summary` works the same way as `AutoTags` - the task owns it and rewrites it. To keep a summary you wrote, use `GenerateMetadata: false` below.

**Turning generation off entirely.** `GenerateMetadata: false` leaves the page fully indexed and searchable but stops the LLM touching its metadata at all - no `AutoTags`, no `Summary`. Use it on pages where the metadata is the point: an index page, a glossary, anything hand-curated. It holds against the bulk **Generate Metadata** actions on the Tasks page too, including *force regenerate* - a page that opts out is skipped there as well.

```markdown
---
Title: Reading List
Tags: books, reference
GenerateMetadata: false
---
```

**Delete the block.** No frontmatter block, no writes. Nothing is ever added to a page that does not already have one.

> [!note]
> `AutoTags` is always rewritten as a flat `AutoTags: a, b` line. If you wrote an
> indented YAML list there, expect it collapsed onto one line next time you look.
> Your `Tags` field is untouched either way.

## Keeping a page out of search

```markdown
---
Index: false
---
```

`false`, `no`, and `0` all work, in any capitalization.

This is a **downgrade, not a deletion**, and the distinction matters. The page keeps its entry in the link graph, so it still appears at [Graph](/graph/{{vault}}) - colored as "Not indexed" in the legend - and links to and from it still resolve and still show up in backlinks. What it loses is everything retrieval-related: it isn't chunked, isn't embedded, won't appear in search results, won't be found by full text search, and won't be handed to the LLM as context in a chat.

It also stops getting LLM tags and summaries, since there's nothing to generate them for.

Reach for this on scratch pages, meeting noise, long pasted transcripts, and anything else you want to keep and link to but never want surfacing in a search.

> [!note]
> **On a page that's already been indexed, change something in the body too.**
> Tzara decides whether a page is worth re-examining by comparing its body, and
> the frontmatter isn't part of that comparison - so a save that *only* adds or
> removes `Index: false` looks like nothing changed and gets skipped. Edit a line
> of the page in the same save and it takes effect immediately. On a page you're
> creating for the first time there's nothing to compare against, so it just works.
> To fix up pages after the fact, reindex the vault from the Tasks page.

## Timestamps

```markdown
---
Title: Ganymede Station
Created: 2026-08-01T09:14:02-05:00
Updated: 2026-09-01T14:23:05-05:00
---
```

`Created` is written once, when the page is first made. `Updated` is rewritten every time
Tzara saves the page.

The reason these exist at all, when the History view already knows when a page changed, is
**portability**. Git history lives outside the vault, and saving without versioning is a
choice the edit form gives you - so a vault copied to a machine with no Tzara can arrive
carrying no dates whatsoever. A line in the file survives the trip.

Four things worth knowing:

* **Only saves Tzara performs are stamped.** If you edit a page in Obsidian, or in a text
  editor straight on disk, nothing is written - Tzara sees the change and reindexes it,
  but writing into a file another editor has open is how you get a conflict, not a
  timestamp. Obsidian has its own plugins for this if you want them on that side.
* **A page with no frontmatter block never gets one.** Same rule as `AutoTags` and
  `Summary`, so deleting the block remains the way to opt one page out of everything.
* **Re-saving a page you didn't change leaves the date alone.** The stamp tracks edits,
  not visits.
* **The LLM does not touch `Updated`.** Regenerating tags and summaries writes
  `MetadataUpdated` instead. If they shared a field, one bulk **Generate Metadata** run
  would set every page in the vault to the same instant and you would lose the ordering
  the field exists to give you.

**Turning it off.** Timestamps ship on. `FRONTMATTER_TIMESTAMPS` in your `.env` turns them
off everywhere; the **Timestamps** control on the [vaults](/vaults) page overrides that for
one vault, which is what you want for a vault whose dates something else already manages.
See [configurations](configurations.md). Turning them off freezes the existing values
rather than stripping them.

`Created` is the key Tzara writes, but a page that came from somewhere else and carries the
older `Date:` spelling is read the same way, so nothing needs converting.

## Steering what the editor writes

Four keys get handed to the LLM as background whenever you run anything from the `/` menu on that page:

```markdown
---
audience: new players, no book spoilers
voice: in-world, present tense
tone: dry
style: short paragraphs, no bullet lists
---
```

Whatever you put here is passed along as "document metadata to honor." It's a hint, not a contract - a small model will drift - but it costs one line and it saves repeating yourself in every prompt. This is the difference between a page whose "Continue writing" sounds like your notes and one that sounds like a chatbot.

They apply to everything in the `/` menu - the built-in commands like Continue Writing, Tighten, and Wrap as admonition, as well as any editor tools you've written yourself. Chat and agents don't read them.

## Keys Tzara ignores

If your vault came from Obsidian it probably has some of these already. They're harmless - they parse, they sit there, nothing happens.  

| Key | Why nothing happens |
|-----|---------------------|
| `aliases` | Not implemented. Wikilinks here resolve by file name across the whole vault, which covers a lot of what aliases are for. |
| `cssclasses` | Obsidian-only. To style a single page, use an [attribute list](complete-markdown-reference.md) on the element you want to change. |
| `publish`, `draft` | Tzara is local-only. There's nothing to publish to. |
| `modified` | Not read. It means the same thing as `updated`, and since Obsidian has no official spelling for either, Tzara picks one - see [timestamps](#timestamps). |
| `Date` | The old spelling of `Created`, still read as one so an older vault needs no conversion. Nothing writes it any more. |
| `origin`, `generated` | Written onto pages an agent produced, as a breadcrumb for you. Nothing reads them - what actually marks a page as agent-owned is living under the agent's output folder. |
| `description` | Not a page key. It means something only inside an agent or editor definition - see [below](#agents-and-editor-tools-have-their-own-fields). On an ordinary page, use `Summary`. |

Per-vault settings are a different mechanism entirely. A vault's theme, start page, and colors are **not** frontmatter - they live in `.tzara/config.json` inside the vault. See [configurations](configurations.md).

## Agents and editor tools have their own fields

Two kinds of file in the system vault are read as definitions rather than as prose, and both are configured almost entirely through frontmatter. Those supported fields are much larger than anything on this page and have their own references:

* **[authoring agents](authoring_agents.md)** - `type: agent`, plus `schedule`, `on`, `mode`, `vaults`, `capabilities`, `output`, `memory`, and the rest.
* **[authoring editors](authoring_editors.md)** - `type: editor`, plus `label`, `scope`, `operation`, and the rest.

Both agent and editor frontmatter grammars include a `description`, which is different from `summary`, even though they seem like the same thing:

| | `description` | `Summary` |
|---|---|---|
| Who writes it | you | the LLM |
| How long it lasts | permanent | rewritten on every metadata run |
| What reads it | the definition itself - it's grammar, like `scope` or `capabilities` | the sidebar, and the vector this page is retrieved by |
| Where it means anything | agent and editor files only | any page |

So an agent or editor file wants a `description`: one line, written by you, saying what the thing does. It does not want a generated `Summary` sitting next to it saying the same thing in more words - which is why the shipped agents and editors all carry  `GenerateMetadata: false`, and why the starter templates write that line for you.

A file only counts as a tool if it sits directly in `agents/` or `editors/` in the system vault. Writing `type: agent` at the top of an ordinary page in your own vault does nothing at all - which is the point, since it means nothing an agent writes can ever turn itself into an agent.

Those files can also carry the ordinary keys from this page - `Title`, `Tags`, and so on - alongside their configuration. The two sets don't collide.

## Related

* [markdown syntax](markdown-syntax.md) - the syntax you'll use every day
* [complete markdown reference](complete-markdown-reference.md) - everything else Tzara renders
* [configurations](configurations.md) - `.env`, `config.py`, and per-vault settings
* [authoring agents](authoring_agents.md) - the agent frontmatter grammar
* [authoring editors](authoring_editors.md) - the editor-tool frontmatter grammar
