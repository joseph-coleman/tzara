---
type: editor
label: TL;DR
description: Put a two-sentence summary of the document at the very top.
scope: document
operation: prepend
Tags: prompt, summarization
Title: TL;DR
Summary: The prompt directs the writer to create a TL;DR blockquote of no more than two sentences that captures the document's purpose and main point, using its own terminology and avoiding self‑referential phrases. It also requires that the output consist solely of the blockquote with no additional preamble, commentary, or code fences.
---

# Prompt
Read the whole document and write a TL;DR of at most two sentences that says what the document is about and what its main point is. Write it as a blockquote beginning `> **TL;DR**`. Use the document's own terminology, and don't refer to "this document" or "the text".

Output ONLY the blockquote. No preamble, no commentary, no code fences.
