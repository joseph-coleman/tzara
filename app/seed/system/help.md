# Welcome

Welcome to the Tzara help pages. These live in the **system vault** and are excluded
from search indexing, so they never pollute your own notes' retrieval.

These pages are seeded once, on first run. After that they're yours - edit them,
extend them, or delete the ones you don't need.

## Contents

1. [configurations](help/configurations.md) - How (and where) to configure things.
1. [basics](help/basics.md) - The basics of using Tzara. 
1. [markdown-syntax](help/markdown-syntax.md) - The markdown you'll use every day, shown by example.
1. [jupyter](help/jupyter.md) - Jupyter integration details and examples.
1. [agents](help/agents.md) - Creating agents and how they work
1. [editors](help/editors.md) - Custom "/" menu commands that transform text as you edit

> [!caution]
> Agent subsystem (**OFF by default** - opt in once you understand it) 
> Tzara can run background agents (which defined as markdown files in the system vault). They are powerful (scheduled/event-triggered runs, staged writes, sandboxed custom Python tools). Leave these disabled until you've read the authoring guide; flip to true to enable. See app/config.py for the full semantics. Can also specify in `.env`.
>
> **`AGENT_SCHEDULER_ENABLED=false`**
> 
> **`EVENT_TRIGGERS_ENABLED=false`**
> 
> `run_python` lets the CHAT agent execute LLM-authored code in the page kernel (approval-gated per block). Off by default because note text read via search could carry prompt-injection that steers that code. Enable only if you trust your vaults' content.
> 
> **`CHAT_ENABLE_RUN_PYTHON=false`**

## Example agents

The system vault also ships a couple of small example agents under the `agents/` directory.  Click "Help" and then [Index](/index/{{vault}}/) to see everything. 

- [agents/vault-health](agents/vault-health.md) - Demonstrates custom tools to generate a manual report of vault health, such as well well things link together, and any orphaned pages that need your attention.
- [agents/nasa-apod](agents/nasa-apod.md) - Fetches NASA's Astronomy Picture of the Day. Runs daily.
- [agents/math_of_the_day](agents/math_of_the_day.md) - Creates a daily math article explaining some topic.
- [agents/physics-librarian](agents/physics-librarian.md) - Uses a bunch of built in tools to look at the `/physics` section of a wiki. A specialized version of the vault-health agent above.
- [agents/physics-citation-finder](agents/physics-citation-finder.md) - Companion with the Physics Librarian, but this proposes changes to pages for better external references to Wikipedia.
- [agents/expanse-worldbuilder](agents/expanse-worldbuilder.md) - This is an example of an autonomous agent that builds out pages on a regular schedule in a specifically named vault, so it is contained.  The theme of this is a role playing game based on the the book series The Expanse.
- [agents/expanse-continuity-linker](agents/expanse-continuity-linker.md) - This agent demonstrates event driven behavior. This agent run when the Expanse Worldbuilder agent finishes its work.  

Open an agent's page to read its definition, then run it from the **/agents** view.
They ship **manual-run** (no schedule), so nothing fires until you ask it to.

## Example Editors

The system vault also ships a couple of small example editors in the `editors/` folder here.  

- [editors/cipher](editors/cipher.md) - This is a decoder ring of sorts. Converts selected text using a simple cypher.  Select the text and run the tool again to decrypt. 
- [editors/research_notes](editors/research_notes.md) - Given some selected text, tries to find related information in your vault of documents, and then saves the snippet of references in an external note file.
- [editors/british](editors/british.md) - Convert the spelling of the selected text from American to British for a splash of colour.
- [editors/tldr](editors/tldr.md) - Reads the whole document and then creates a "Too long; didn't read" block at the top of a page.
- [editors/equation-to-latex](editors/equation-to-latex.md) - Given a selected text equation, tries to convert it into LaTeX output for elegant typesetting.

## Architecture

![[architecture.canvas]]

