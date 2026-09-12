---
title: Agents
Summary: Agents in Tzara, what they are, how they function, and how to build them
GenerateMetadata: false
---

* [help](../help.md)
    * [configurations](configurations.md) - How (and where) to configure things.
    * [basics](basics.md) - The basics of using Tzara.
    * [markdown-syntax](markdown-syntax.md) - The markdown you’ll use every day, shown by example.
    * [frontmatter](frontmatter.md) - The metadata block at the top of a page, and what Tzara does with it.
    * [jupyter](jupyter.md) - Jupyter integration details and examples.
    * **agents** - Creating agents and how they work
    * [editors](editors.md) - Custom “/” menu commands that transform text as you edit


# What are agents?

An agent is just a clever way of having an agent talk to itself with the capability of calling some external tool that can do something. 

There is a lot to unpack there, so let's dive right in. 

*[LLM]: Large Language Model

A large language model, or LLM, essentially just spews out a string of text until it spits out a full stop command.  It predicts words and one of those words is a special one that makes it stop.  Then a human typically generates words (we call that typing) and then it (the human) will generate a stop word (pressing Enter or Return on their keyboard). 

And then they loop. LLM says something then stops.  Human says something then stops. LLM says something then stops.  Human says something then stops.  

An "agent" takes the human out of the loop and just talks to itself.  The human is replaced with a "tool."  All the tool does is is run some computer code or run or interact with some other program, and then take the response from that code or program and feed that back into the loop.  

There really is no magic invovled in this design.  The magic is what tools you provide the LLM and what the LLM is initially instructed to do.  

# Tzara's Agents

There are 2 or 3 things an agent needs.  A **prompt** describing a problem or task to be completed, a **tool**, in this case a tool or function built into Tzara or custom python functions you write and provide, and optionally a **kickoff** message, which amounts to something like "You have a **tool**, go use it to solve the problem."

And then it loops until it decides it's done needing to use tools.  

# Examples

There some example agents, all in manual mode.  You'll have to set a schedule or edit them as necessary if you don't want to have to manually trigger them.  Manual is preferable until you get a feel for what it does.   Behavior and success rate will depend on which LLM  you're using. 

In all of the examples, you'll see they have the 3 basic components.  
1. Prompt
2. Tools
3. Kickoff

# Security

See [agent security](agent-security.md) for details. 

# Making your own

This is the droid you were looking for. 

* [authoring agents](authoring_agents.md) - Reference details on writing your own Tzara agent.