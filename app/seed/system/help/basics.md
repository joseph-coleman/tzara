# Basic workflow

In Tzara, documents are just plain text files with some simple, extra punctuation to impart a sense of structure and emphasis.  This is called markdown.

Your basic workflow is to edit and create documents.  Your web of thoughts grow as you edit a document and put a link to some other documents.  Links are just double square brackets around a word or phrase.  For example, if you create a document and type `[[help]]` it will appear as [[help]]. You can put square brackets around any word or phrase, and that creates a link to a new, as of yet unwritten document.  You save your current document, click on the new links, and that takes you right into editing mode.  The more you do this, the more interlinked your notes become.  

And if you're not new to this, perhaps from using other wiki software or if you've used the software Obsidian, there are a few things here that might be new to you.


## Editing

On any given page you can either click the "Edit" link in the menu at the top or bottom of the page, or click the title in the center of the page. You'll land on a text box and you can start writing your novel or screenplay immediatly.  

You can click Edit on any page to see what the "markdown" or puncutation looks like in practice to achieve whatever visual effect you see in your browser. The headings use a hash mark at the beginning of line, as you can see from  clicking "Edit" on this page.

If you've configured Tzara with an LLM, there are a few extra fun things you can do when editing a document.  You can type a forward slash, `/`, and a menu will pop up with two options. They are:

* Continue Writing
* Continue (grounded in notes)

The first option will use a local LLM to try to finish your sentence based on the current document. The second option will search your vault for related documents and try to use those as sources for what it writes. 

For all the other options in the forward slash menu, select a passage of text first, and while selected, type `/` and you can then select from the other options. 

The `/` only opens the menu at the start of a line or just after a space, so it keeps out of your way when you are typing a path or using a slash as ordinary punctuation. To open the menu anywhere else - at the end of a sentence, or in the middle of a word - press `Ctrl+Shift+/` instead. It works with or without a selection, and nothing is typed into your document, so `Esc` leaves your text exactly as it was.

> [!note]
> One of my favorites is converting prose to a mermaid diagram.  However, to create this note, I used the option "wrap as admonition."  I can never remember the syntax, so that's why this exists. 

If you inject anything into your document you'll get prompted with a confirmation dialog, `Tab` to accept and `Esc` to reject.  You can click the buttons with your mouse, but you don't really need to take your hands off the keyboard either.  

You can always undo with Ctrl+Z.  The editor is using [Codemirror 6](https://codemirror.net/) which is really nice. 

## Document conversation

When you view any document, you can use the chat interface at the bottom of the page to ask questions about it, about other things in your vault, even create new sections or edit existing sections in the current document.  You'll get a confirmation dialog to either accept or reject any suggested changes if they're proposed.  

You are always in control. 

## Vault conversation

A different approach is the vault wide conversations that you can reach from the "Chat" link at the top of the page.  This is a broader scope conversation.  You can ask about things in the vault and it will try to find them.  With this interface you can ask the LLM to create multiple pages, and it will attempt to do that. 

> [!info]- The success...
> The success of any of this is going to be how big of a model you can run locally.  Some of the small ones can be fun to chat with even if they can't make any tool calls.  On vault level models without tool calling are essentially useless, but at a document level, really small models can still see the document you're viewing at the time, and so can be focused on that.

## Canvas

You can create a canvas and embed that in a page.  A canvas is collection of notes and links and images that you can draw connections between.  

You can also embed pages in a vault into a canvas.  They can fit into each other wither way.

## Index

To view all the files in a vault, the Index page is the place to be.  It is linked at the top and bottom of every page. Here you can move files around and organize things.  

If you move a file, any links you have in pages may be edited to try to follow the new location.  And linking is also a bit forgiving.  This is an attempt to mimic Obsidian's lax linking rules.  

## Graph

As pages link to one another, they start to form a mesh or network or web of interconnections.  Here you can view all your documents and how they are linked together.  Obsidian's graph view is so much better than this.  

## History

When you edit a document, and if you have `USE_GIT_VERSIONING` set to `True`, the changes for the document are staged and commited into that vault's associated git repo.  This page allows you to view past snapshots of the file.   You can click and view a past version, edit a past version, or look at the differences beteen a past version and the current version.  

From a git perspective, there is no branching. Everything is a string of single file commits.

## Kernels

If there are any pages with jupyter code that's been run, an entry will appear here for every page with live instance of code.  This page allows you to destroy any running kernels.  Sometimes we all write bad code that breaks something, and this page is our reset mechanism. 

## Models

Depending on what LLM server you're using, this page offers a little bit of management.  With a server that has an Ollama-style management API - Ollama itself, or Lemonade - we can easily inspect downloaded models, actually download more of them, and switch between them.  A plain OpenAI-compatible server has no notion of managing models, so those controls quietly do nothing there.  

The models for chat and for embedding are two different types of models.  These are configured in your `.env` file, and you shoud pay special attention to those settings.  

This page allows you to temporarily switch your chat model to something else.  As long as you don't have to restart the server, that model should stay selected.  If your computer reboots and Tzara starts again, it will start with the configured model in the `.env` file.  

Personally, I'm running Lemonade on a secondary machine on my local network, and I really like the `gpt-oss-120b` model for chat, and `embeddinggemma-300m` for embeddings.  The embedding models have a **strong** influence on how well Tzara can retrieve documents based on similar meanings. 

> [!warning]
> Switching embedding models is not so simple.  It requires a config change and a restart of the Tzara server and workers.  When an embedding model change is detected, all the documents in all the vaults need to be reindexed, and that can take some time.  If it doesn't happen, or something interrupts, then you can go to the Tasks page. 

## Tasks

The Task Queue page has a lot going on.  The web server uses Redis and Taskiq workers to do stuff in the background, such as indexing files, running agents, and automatically generating tags and summaries for documents.  These are potentially time consuming tasks, so they're run in the background so they don't get in the way of what you're doing. 

*[RAG]: retreival augmented generation

For the RAG capablities, used in search and behind the scenes in various places, documents need to be analyzed in small chunks, and then an embedding model has to parse all those chunks.  The computational results of that are stored in the PostgreSQL + pgvector database.  Sometimes stuff happens, or you want to change your embedding model, and you need to reindex things.  Here you can do that on a vault by vault basis.  

You can also schedule manual runs of any agents you may have created.  Spoiler alert, agents are just markdown files. Agents can be written for specific vaults of your choosing, or you can let them loose on everything.  If an agent is scheduled to run on a specific schedule or in response to a specific event, you always have the ability to manually run the agent. 

You can also see jobs in progress, jobs in a queue, and recent results.  The "bulk task progress" section keeps track of agents that run multiple times because they're configured to run on many vaults.  For example, say you have an agent to check for spelling errors on recent documents and have set it to run on 5 different vaults.  Here you'll see progress at it makes its way through the vaults one at a time.  Each agent run is scoped to a single vault, so to run for 5 vaults means it runs consecutively 5 times, once for each vault. 

## Agent Activity

Here you can see any document edit proposals an agent is asking to make.  When an agent wants to make changes to a document it works on a shadow copy.  That shadow copy is staged for you to either accept or reject changes.  You can accept or reject all changes, or go through them individually. 

You can set an agent to work without staging files, but is not the default behavior. It is something where you have to explitictly opt-in to have that autonomy.

This page also shows the schedule of each of your agents, how they act, what triggers them, last run, and next run times, and which vaults they're associated with. 

You can also see current agents running with an option to cancel those runs.  Recent runs and recent events are also displayed, along with their queue depth, which increments if an agent was triggered by another agent or event.  The depth limit is set to 3 by default, and of course, can be configured.

## Related
- [jupyter technical details](jupyter/jupyter-technical-details.md)
