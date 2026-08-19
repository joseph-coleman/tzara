"""Default consolidation prompts for cross-run memory, plus instruction assembly.

Leaf module - imports nothing from the agent/editor stack - so the three places
that need the defaults can share one copy: agent_registry / editor_registry
(which prefill them into new-file templates), background_agents (agents) and
edit_assist (editors).

An author overrides a default by writing a `# Memory Prompt` section in the agent
or editor file. The BODY is theirs. The TAIL - current memory plus the transcript
header - is always appended here, never authored: summarize_conversation renders
the transcript immediately after the instruction, so the instruction must end on
that header or the transcript arrives unlabeled.

Framing of the agent default = MAINTAIN A LIVING HANDOFF DOC, not summarize the
run. Store only the vault-COMPLEMENT - what the agent's own tools cannot re-derive
next run - because the vault itself is the primary memory. Differential retention
(churning vs sticky sections) confines erosion to the volatile parts. Tool names
are interpolated from the AGENT'S ACTUAL grant, never hardcoded (that would lie
for agents without those tools).
"""

# Placeholders an author may use in a custom `# Memory Prompt`. Anything else is
# left alone by fill().
AGENT_PLACEHOLDERS = ("agent_name", "tool_names")
EDITOR_PLACEHOLDERS = ("label",)


def fill(text: str, **values: str) -> str:
    """Substitute `{name}` placeholders, leaving every other brace untouched.

    str.format would raise on any brace the caller did not supply, and authored
    prompts routinely contain them - JSON examples, set notation, LaTeX. Only the
    documented placeholders are substituted.
    """
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


AGENT_MEMORY_PROMPT = """You are the {agent_name} agent. You just finished one autonomous work session in a wiki vault, and you are about to lose all working state - the note you write now is your ONLY memory into your next session. Below is your CURRENT memory and a TRANSCRIPT of the session you just finished. Produce your UPDATED memory.

CRITICAL - record only what you could NOT rediscover next session. Next session you will again have your tools ({tool_names}) and can inspect the current state of the vault, so do NOT record facts those tools can retrieve for you - e.g. what pages exist or what they contain. Record only what the tools cannot:
- Next run: what KIND of work to do next, most valuable first. Do NOT name a specific pre-chosen item unless it is genuinely half-finished - choosing is next session's job, and a name recorded here reads as an instruction to repeat it.
- In progress: ONLY work you deliberately left UNFINISHED and intend to resume. If you finished everything you started, write exactly "nothing pending". NEVER list work you completed, and never park ideas or candidates here - a finished page recorded as in-progress is an instruction to redo it.
- Decisions & conventions: choices future sessions must honor to stay consistent (naming, page structure, canon calls) - only ones not obvious from the pages themselves.
- Avoid / tried: things you decided NOT to do, or dead ends, so you don't redo them.

Retention: Next run and In progress CHURN - DELETE items you completed this session, do not restate them in the past tense, and add what is now most valuable. Decisions & conventions and Avoid / tried are STICKY - carry them all forward unchanged; only add, or correct one this session overturned; never drop them just because this session did not touch them. Keep the whole note short - a working handoff, not a report.

Your standing directive OUTRANKS this note. If a remembered decision or convention contradicts the directive you were given, DELETE it now - do not carry it forward. A convention you recorded is not binding on the person who rewrote your directive to say otherwise.

If you keep append-only ledgers, they are maintained for you: do NOT copy their rows into this note, and do not keep a parallel list of your own. Refer to a ledger by name if you must mention it.

Output ONLY the updated memory in exactly this format - no preamble, no explanation, no code fences:

## Next run
- ...

## In progress
- ... (or "nothing pending")

## Decisions & conventions
- ...

## Avoid / tried
- ..."""


EDITOR_MEMORY_PROMPT = """You are "{label}", an editor tool a person runs on passages while they write. You keep a single running note ("memory") that PERSISTS across invocations and across different documents - it is how you accumulate and organize what matters over many runs.

Below is your CURRENT memory and a TRANSCRIPT of the session you just finished (the passage you were given and what you did with it). Produce your UPDATED memory.

Integrate anything worth keeping from this session into the existing note: merge related points, remove redundancy, and keep it organized and concise - a living, consolidated digest, NOT an ever-growing log. Carry forward everything still relevant; only drop what is now obsolete. If this session added nothing worth keeping, return the current memory unchanged.

Your standing directive OUTRANKS this note. If something you remembered contradicts the directive you were given, DELETE it now rather than carrying it forward. If you keep append-only ledgers, they are maintained for you - do not copy their rows into this note.
Output ONLY the updated memory as Markdown. No preamble, no commentary, no code fences."""


# System-owned tail. Ends on the transcript header - summarize_conversation
# appends the rendered transcript directly after it.
_TAIL = """

CURRENT memory:
{prior_memory}

TRANSCRIPT of the session just finished:"""

_NO_MEMORY = "(none yet - this is your first session)"


def agent_instruction(memory_prompt: str, agent_name: str, tool_names: str,
                      prior_memory: str) -> str:
    """Assemble the agent memory turn's instruction.

    `memory_prompt` is the agent's `# Memory Prompt` body, or "" for the default.
    """
    body = fill((memory_prompt or "").strip() or AGENT_MEMORY_PROMPT,
                agent_name=agent_name, tool_names=tool_names)
    return body + fill(_TAIL, prior_memory=prior_memory or _NO_MEMORY)


def editor_instruction(memory_prompt: str, label: str, prior_memory: str) -> str:
    """Assemble the editor memory turn's instruction. Mirrors agent_instruction."""
    body = fill((memory_prompt or "").strip() or EDITOR_MEMORY_PROMPT, label=label)
    return body + fill(_TAIL, prior_memory=prior_memory or _NO_MEMORY)


# The SECOND memory call: a tool-only turn whose whole job is `remember`.
#
# Split from the note-writing call because they cannot share one. Measured on
# gpt-oss-120b (.test/probe_memory_turn_tools.py): asked for prose AND tool calls
# together the model returned tool calls and NO prose 5/5, which would have
# written a perfect ledger and wiped memory.md every run. A single fat tool
# carrying both (probe_memory_turn_single_tool.py) failed the other way - clean
# note, garbage ledger fields, 5/5. Given ONE job each it does both correctly
# (probe_memory_two_call.py, 3/3), and the second call is small enough to be
# cheap: ~3.6s against a ~10s first call.
#
# System-owned rather than authorable: this is a mechanical instruction about a
# tool contract, not the editorial voice a `# Memory Prompt` shapes.
LEDGER_TURN_PROMPT = """You are recording durable facts to append-only ledgers that outlive every future run.

Below is the memory note you just wrote and the session it came from. Call `remember` for anything this session ESTABLISHED that must never be lost - work you completed, a page you processed, a topic you published. Reuse an existing ledger name to add to it; invent a new name only when nothing existing fits.

Record FACTS about what is done, never plans about what to do next - plans belong in the note, not the ledger. If this session established nothing durable, call nothing at all.

Judge what the session DID from the TRANSCRIPT, not from how the note characterizes it. A session that produced the work has done it, even if the note files it as unfinished or plans to redo it - the note is a plan for next time and can be wrong about this one. Recording it here is what stops the next run repeating it.

Match the grain of the rows already in a ledger. If it lists bare topic names, add a bare topic name - not a sentence about having added one. Rows are compared to each other to spot repeats, so a row phrased differently from its neighbours is a row that will be missed.

{ledgers}

The memory note you just wrote:
{note}

TRANSCRIPT of the session just finished:
{transcript}"""

_NO_LEDGERS = "You have no ledgers yet. Create one only if this session earned it."


def ledger_turn_prompt(ledgers_text: str, note: str, transcript: str) -> str:
    """Assemble the tool-only ledger turn. Deliberately small - the note plus a
    transcript tail, not the whole session again, which is what makes this
    cheaper than spending a second full loop iteration.

    `ledgers_text` is the WHOLE book, never a capped view: "do NOT re-record rows
    already here" is not a rule a model can follow against rows it was not shown,
    and unlike the injected view this prompt is built once per run.
    """
    block = (f"Your existing ledgers (do NOT re-record rows already here):\n{ledgers_text}"
             if (ledgers_text or "").strip() else _NO_LEDGERS)
    return fill(LEDGER_TURN_PROMPT, ledgers=block, note=note or "(none)",
                transcript=transcript or "(empty)")
