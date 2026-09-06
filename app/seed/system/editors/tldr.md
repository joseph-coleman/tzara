---
type: editor
label: TL;DR
description: Put a two-sentence summary of the document at the very top.
scope: document
operation: prepend
Tags: prompt, summarization
Title: TL;DR
GenerateMetadata: false
Summary: Too long, didn't read.
---

# Prompt
Read the whole document and write a TL;DR of at most two sentences that says what the document is about and what its main point is. Write it as a blockquote beginning `> **TL;DR**`. Use the document's own terminology, and don't refer to "this document" or "the text".

Output ONLY the blockquote. No preamble, no commentary, no code fences.
