---
title: Editoric Tools
description: What editor tools are and how they add custom commands to the edit-mode "/" menu.
Tags: editor-tools, llm, markdown, prompts, custom-python, note-taking
Summary: Editor tools are saved commands invoked from the “/” menu that use an LLM to transform selected text or the caret position, performing operations such as replace, prepend, append, insert, or note, and can incorporate read‑only searches, custom Python functions, and memory across runs. Each tool is defined by a minimal markdown file containing a `type: editor` frontmatter block with a label and operation plus a prompt, and the system provides example tools and an [/editors] page to list and validate them. Users can create their own tools following the authoring documentation and wiki object reference.
---

# What are editor tools?

An **editor tool** is a saved command you run from the **"/" menu while editing a page**. You select some text - or select nothing and just leave the caret where you want new text - then type `/` (or press `Ctrl+Shift+/`, which works anywhere, including mid-word), pick your tool, and an LLM does something useful: rewrites it, reformats it, writes the missing paragraph, or files it away as a note.

If an [agent](agents.md) is an LLM that talks to itself in the background to tend your whole vault, an editor tool is the opposite: it's an LLM you reach for **in the moment**, pointed at exactly the text in front of you, with the result handed straight back to you to accept or reject.

*[LLM]: Large Language Model

# What they can do

Every editor tool has a **prompt** (what to do with the text), a **scope** (what text it looks at - the selection, the whole document, or the area around your caret), and an **operation** (what to do with the result):

- **replace** what the tool looked at - "rewrite this in plain English", "fix the grammar", "turn this into a table". With nothing selected, this replaces the paragraph your caret is sitting in.
- **prepend** or **append** - put the result before or after it. "Write a lede for this section", "add a TL;DR at the top", "extract the key points and list them at the end".
- **insert** at the caret exactly - "continue this sentence", "write the paragraph that bridges these two". A caret-scoped tool sees the document on both sides of the caret, so it can write something that fits *between* what comes before and what comes after, not just something that follows on.
- **note** - leave the document untouched and instead append the result to a growing external digest - "add this passage to my reading journal", "collect these characters into a glossary".

Tools can also be given a couple of **read-only search tools**, or **custom Python functions you write**, and can keep **memory** across invocations so a note-taking tool assimilates what it has seen over many runs and many documents.

# The building blocks

An editor tool is a single markdown file. At minimum it needs:

1. A `type: editor` frontmatter block with a `label` and an `operation`.
2. A `# Prompt` describing the transform.

That's it - a two-line frontmatter and a prompt is a complete, working tool. Everything else (search tools, Python tools, notes, memory) is optional.

# Examples

The system vault ships a few example editors under `editors/`. Open any of them to read its definition:

- **British Spelling** / **Secretary** - pure-prompt transforms (no tools).
- **Decoder Ring** - a custom Python tool (ROT13/Atbash/reverse) run in the isolated kernel.
- **Add to Glossary** / **Research Note** - `operation: note` tools that keep a growing, memory-assimilated digest.

# Seeing what's installed

The **[/editors](/editors)** page lists every editor tool with its settings and whether it's valid - including *why* an invalid one was rejected (a frontmatter mistake or a Python syntax error). It's the editor-tool counterpart to the `/agents` view.

# Making your own

* [authoring editors](authoring_editors.md) - reference details for every field, the `editor` and `wiki` objects, memory, and validation.
* [the wiki object](wiki-object.md) - the corpus-access object your custom Python tools use.

## Related
- [agents](agents.md) - the background counterpart to editor tools
- [Main](../Main.md)
