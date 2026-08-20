# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Message history compaction with structured logging.

APPEND-ONLY design: the live pipeline never rewrites the content of a message
it keeps. The only trimming operation is dropping the oldest *whole* atomic
groups off the front of the history when it exceeds the message-count cap or
the token budget. This preserves Ollama's KV prefix cache across turns (a
stable prefix is reused instead of reprocessed) and keeps the agent's edit
ledger byte-intact so it does not re-derive tool calls it already made.

Live pipeline:
  - Sliding window - drop oldest atomic groups to fit the hard token budget.

Retained but NOT wired into the live path (they mutate already-sent messages,
which loses information and invalidates the prefix cache). Kept for standalone
use and as building blocks for a future one-time checkpoint-summary strategy:
  - Tool result compaction - truncate verbose tool outputs to short summaries
  - Turn aging - reduce detail in old conversation turns
"""

import logging
from collections import Counter
from dataclasses import dataclass

from config import CHARS_PER_TOKEN, CHAT_DEBUG_STATS, MIN_MESSAGES

logger = logging.getLogger("compaction")

# ---------------------------------------------------------------------------
# Checkpoint-summary tuning
# ---------------------------------------------------------------------------

# Trigger a checkpoint once history exceeds this fraction of the history budget.
CHECKPOINT_TRIGGER_FRACTION = 0.7
# After folding, keep the most-recent groups that fit within this fraction.
CHECKPOINT_KEEP_FRACTION = 0.5
# Sentinel prefix marking the rolling-summary message in the history. The
# summary lives as an ordinary user-role message (the Ollama client validates
# message shape, so a custom key is unsafe); this prefix is how the prior
# summary is recognised and folded into the next one.
CHECKPOINT_PREFIX = "[Summary of earlier conversation]"


# ---------------------------------------------------------------------------
# Token estimation
# ---------------------------------------------------------------------------

def estimate_message_tokens(msg: dict) -> int:
    """Estimate token count for a single message dict."""
    content_chars = len(msg.get("content", ""))
    # Tool calls add overhead (function name + serialized arguments)
    for tc in msg.get("tool_calls", []):
        func = tc.get("function", {})
        content_chars += len(func.get("name", ""))
        content_chars += len(str(func.get("arguments", {})))
    return int(content_chars / CHARS_PER_TOKEN)


def estimate_tokens(messages: list[dict]) -> int:
    """Estimate total tokens across a message list."""
    return sum(estimate_message_tokens(m) for m in messages)


def message_char_count(msg: dict) -> int:
    """Character length of a single message, including tool-call overhead.

    Mirrors estimate_message_tokens so the char and token figures stay
    consistent: content plus serialized tool-call names/arguments.
    """
    chars = len(msg.get("content", ""))
    for tc in msg.get("tool_calls", []):
        func = tc.get("function", {})
        chars += len(func.get("name", ""))
        chars += len(str(func.get("arguments", {})))
    return chars


def history_char_count(messages: list[dict]) -> int:
    """Total character length across a message list."""
    return sum(message_char_count(m) for m in messages)


# ---------------------------------------------------------------------------
# Debug instrumentation
# ---------------------------------------------------------------------------

def log_history_stats(label: str, messages: list[dict], system_prompt: str = "") -> None:
    """Log running history size (chars + estimated tokens) for chat debugging.

    No-op unless CHAT_DEBUG_STATS is enabled, so call sites can stay
    unconditional. Reports message-only figures and, when a system prompt is
    supplied, the combined total that actually lands in the model's context
    window - the number that matters for budget pressure.

    `label` names the moment being measured (e.g. "user-added",
    "post-compaction") so consecutive lines read as a timeline.
    """
    if not CHAT_DEBUG_STATS:
        return

    msg_chars = history_char_count(messages)
    msg_tokens = estimate_tokens(messages)
    role_counts = Counter(m.get("role", "?") for m in messages)
    roles_desc = ", ".join(f"{role}={n}" for role, n in sorted(role_counts.items()))

    if system_prompt:
        sys_chars = len(system_prompt)
        sys_tokens = int(sys_chars / CHARS_PER_TOKEN)
        logger.info(
            "chat-stats [%s]: %d msgs (%s) | history %d chars / ~%d tok | "
            "system %d chars / ~%d tok | total %d chars / ~%d tok",
            label, len(messages), roles_desc,
            msg_chars, msg_tokens,
            sys_chars, sys_tokens,
            msg_chars + sys_chars, msg_tokens + sys_tokens,
        )
    else:
        logger.info(
            "chat-stats [%s]: %d msgs (%s) | history %d chars / ~%d tok",
            label, len(messages), roles_desc, msg_chars, msg_tokens,
        )


# ---------------------------------------------------------------------------
# Compaction result (for structured logging)
# ---------------------------------------------------------------------------

@dataclass
class CompactionResult:
    """Tracks what a compaction step did, for structured logging."""
    strategy: str
    messages_before: int
    messages_after: int
    tokens_before: int
    tokens_after: int
    discarded_summary: str

    def log(self):
        freed = self.tokens_before - self.tokens_after
        logger.info(
            "compaction [%s]: messages %d->%d, tokens ~%d->~%d (freed ~%d). %s",
            self.strategy,
            self.messages_before, self.messages_after,
            self.tokens_before, self.tokens_after,
            freed,
            self.discarded_summary,
        )


# ---------------------------------------------------------------------------
# Logging wrappers for existing compaction (Phase 1)
# ---------------------------------------------------------------------------

def log_trim(
    label: str,
    messages_before: list[dict],
    messages_after: list[dict],
):
    """Log before/after stats for an existing trim operation."""
    before_count = len(messages_before)
    after_count = len(messages_after)
    if before_count == after_count:
        return  # nothing was trimmed
    tokens_before = estimate_tokens(messages_before)
    tokens_after = estimate_tokens(messages_after)
    dropped_count = before_count - after_count
    # Summarize what was dropped (roles of the removed messages)
    dropped_roles = [m.get("role", "?") for m in messages_before[:dropped_count]]
    summary = f"Dropped {dropped_count} oldest: {', '.join(dropped_roles[:8])}"
    if dropped_count > 8:
        summary += f" ... (+{dropped_count - 8} more)"
    result = CompactionResult(
        strategy=label,
        messages_before=before_count,
        messages_after=after_count,
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        discarded_summary=summary,
    )
    result.log()


def log_collapse(
    messages_collapsed: list[dict],
    summary_content: str,
):
    """Log stats for the post-loop collapse operation."""
    if not messages_collapsed:
        return
    collapsed_count = len(messages_collapsed)
    tool_msgs = [m for m in messages_collapsed if m.get("role") == "tool"]
    assistant_msgs = [m for m in messages_collapsed if m.get("role") == "assistant"]
    tool_call_count = sum(
        len(m.get("tool_calls", [])) for m in assistant_msgs
    )
    tokens_collapsed = estimate_tokens(messages_collapsed)
    tokens_summary = int(len(summary_content) / CHARS_PER_TOKEN)
    # List tool names that were called
    tool_names = []
    for m in messages_collapsed:
        if m.get("role") == "tool":
            tool_names.append(m.get("tool_name", "?"))
    tools_desc = ", ".join(tool_names[:6])
    if len(tool_names) > 6:
        tools_desc += f" (+{len(tool_names) - 6} more)"

    logger.info(
        "compaction [post_loop_collapse]: %d msgs -> 1 summary "
        "(tool_calls=%d, tool_results=%d, ~%d tokens -> ~%d tokens). Tools: %s",
        collapsed_count, tool_call_count, len(tool_msgs),
        tokens_collapsed, tokens_summary,
        tools_desc or "(none)",
    )


def log_checkpoint_fold(fold_msgs: list[dict], summary_msg: dict):
    """Dump the folded messages and their replacement summary at INFO.

    Mirrors the verbose per-message logging used when a chat message is
    dropped from history (chat._trim_history), so a checkpoint - the one
    compaction step that *replaces* content rather than just trimming it -
    leaves an auditable record of exactly what left the context and what
    took its place. Each folded message is logged individually so multi-line
    tool output and assistant turns stay readable in the log.
    """
    logger.info(
        "compaction [checkpoint]: folding %d message(s) into 1 summary",
        len(fold_msgs),
    )
    for n, msg in enumerate(fold_msgs):
        logger.info("Compaction Removed Message %d/%d:", n + 1, len(fold_msgs))
        logger.info(msg)
    logger.info("Compaction Replacement Summary:")
    logger.info(summary_msg)
    logger.info("End of Compaction")


# ---------------------------------------------------------------------------
# Strategy 1: Tool Result Compaction (gentle)
# ---------------------------------------------------------------------------

def compact_tool_result(msg: dict) -> dict:
    """Compress a single tool result message to a short summary.

    Short results (≤200 chars) pass through unchanged.
    Long results become: [tool_name: 'preview...' (N chars)]
    """
    if msg.get("role") != "tool":
        return msg
    content = msg.get("content", "")
    if len(content) <= 200:
        return msg
    
    logger.info(
        "compaction debug [pre]: %s ", content
    )
    
    tool_name = msg.get("tool_name", "tool")
    preview = content[:100].replace('\n', ' ').strip()
    compacted = f"[{tool_name}: '{preview}...' ({len(content)} chars)]"
    
    logger.info(
        "compaction debug [post]: %s ", compacted
    )
    
    return {**msg, "content": compacted}


def apply_tool_result_compaction(
    messages: list[dict],
    protect_last_n_tool_results: int = 2,
) -> tuple[list[dict], CompactionResult | None]:
    """Compact tool results except the most recent N.

    Returns (new_messages, result_or_None_if_nothing_changed).
    """
    tokens_before = estimate_tokens(messages)

    # Find indices of tool-result messages
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    if len(tool_indices) <= protect_last_n_tool_results:
        return messages, None

    # Compact all but the last N tool results
    to_compact = set(tool_indices[:-protect_last_n_tool_results])
    new_messages = []
    compacted_names = []
    for i, msg in enumerate(messages):
        if i in to_compact:
            original_len = len(msg.get("content", ""))
            compacted = compact_tool_result(msg)
            if compacted is not msg:
                compacted_names.append(f"{msg.get('tool_name', '?')}({original_len}c)")
            new_messages.append(compacted)
        else:
            new_messages.append(msg)

    if not compacted_names:
        return messages, None

    tokens_after = estimate_tokens(new_messages)
    result = CompactionResult(
        strategy="tool_result_compaction",
        messages_before=len(messages),
        messages_after=len(new_messages),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        discarded_summary=f"Compacted {len(compacted_names)} tool results: {', '.join(compacted_names[:5])}",
    )
    return new_messages, result


# ---------------------------------------------------------------------------
# Strategy 2: Turn Aging (moderate)
# ---------------------------------------------------------------------------

def apply_turn_aging(
    messages: list[dict],
    recent_turn_pairs: int = 3,
) -> tuple[list[dict], CompactionResult | None]:
    """Reduce detail in conversation turns older than the most recent N.

    A "turn" starts at each user message and includes all following
    assistant/tool messages until the next user message.

    In old turns:
    - Tool results → compacted form (short summary)
    - Assistant messages with tool_calls → content truncated to 100 chars
    - User messages → preserved in full
    """
    # Identify turn boundaries
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_indices) <= recent_turn_pairs:
        return messages, None

    tokens_before = estimate_tokens(messages)

    # Messages before this index are "old"
    cutoff_user_idx = user_indices[-recent_turn_pairs]

    new_messages = []
    aged_count = 0
    for i, msg in enumerate(messages):
        if i < cutoff_user_idx:
            if msg.get("role") == "tool":
                compacted = compact_tool_result(msg)
                if compacted is not msg:
                    aged_count += 1
                new_messages.append(compacted)
            elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                content = msg.get("content", "")
                if len(content) > 100:
                    new_messages.append({**msg, "content": content[:100] + "..."})
                    aged_count += 1
                else:
                    new_messages.append(msg)
            else:
                new_messages.append(msg)
        else:
            new_messages.append(msg)

    if aged_count == 0:
        return messages, None

    tokens_after = estimate_tokens(new_messages)
    old_turn_count = len(user_indices) - recent_turn_pairs
    result = CompactionResult(
        strategy="turn_aging",
        messages_before=len(messages),
        messages_after=len(new_messages),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        discarded_summary=f"Aged {aged_count} messages in {old_turn_count} old turn(s)",
    )
    return new_messages, result


# ---------------------------------------------------------------------------
# Strategy 3: Sliding Window + Token Budget (aggressive)
# ---------------------------------------------------------------------------

def _identify_message_groups(messages: list[dict]) -> list[list[int]]:
    """Identify atomic message groups by index.

    Groups are:
    - A user message (standalone)
    - An assistant message without tool_calls (standalone)
    - An assistant message with tool_calls + all following tool messages
      (atomic group - must be kept or dropped together)

    Returns list of groups, each group is a list of message indices.
    """
    groups = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        role = msg.get("role", "")

        if role == "assistant" and msg.get("tool_calls"):
            # Start of an atomic tool-call group
            group = [i]
            i += 1
            # Collect all following tool result messages
            while i < len(messages) and messages[i].get("role") == "tool":
                group.append(i)
                i += 1
            groups.append(group)
        else:
            groups.append([i])
            i += 1

    return groups


def apply_sliding_window(
    messages: list[dict],
    max_messages: int,
    system_prompt_tokens: int,
    context_length: int,
) -> tuple[list[dict], CompactionResult | None]:
    """Last-resort trimming: drop oldest message groups to fit budget.

    Respects atomic groups (assistant+tool_calls+tool results are dropped
    together, never orphaned). Drops from the front of the message list.
    """
    tokens_before = estimate_tokens(messages)
    msgs_before = len(messages)

    # Calculate token budget for messages
    response_reserve = max(512, context_length // 8)
    budget_tokens = context_length - system_prompt_tokens - response_reserve

    groups = _identify_message_groups(messages)

    # Drop groups from the front until we're within both limits
    drop_up_to_group = 0  # exclusive: groups[:drop_up_to_group] will be dropped
    remaining_msgs = len(messages)
    remaining_tokens = tokens_before

    while drop_up_to_group < len(groups):
        # Check if we're within limits
        if remaining_msgs <= max_messages and remaining_tokens <= budget_tokens:
            break
        # Don't drop below MIN_MESSAGES
        group_size = len(groups[drop_up_to_group])
        if remaining_msgs - group_size < MIN_MESSAGES:
            break
        # Drop this group
        group_tokens = sum(estimate_message_tokens(messages[i]) for i in groups[drop_up_to_group])
        remaining_msgs -= group_size
        remaining_tokens -= group_tokens
        drop_up_to_group += 1

    if drop_up_to_group == 0:
        return messages, None

    # Build the indices to keep
    keep_indices = set()
    for group in groups[drop_up_to_group:]:
        keep_indices.update(group)
    new_messages = [messages[i] for i in sorted(keep_indices)]

    # Summarize what was dropped
    dropped_indices = set()
    for group in groups[:drop_up_to_group]:
        dropped_indices.update(group)
    dropped_roles = [messages[i].get("role", "?") for i in sorted(dropped_indices)]
    summary = f"Dropped {len(dropped_roles)} msgs ({drop_up_to_group} groups): {', '.join(dropped_roles[:8])}"
    if len(dropped_roles) > 8:
        summary += f" ... (+{len(dropped_roles) - 8} more)"

    tokens_after = estimate_tokens(new_messages)
    result = CompactionResult(
        strategy="sliding_window",
        messages_before=msgs_before,
        messages_after=len(new_messages),
        tokens_before=tokens_before,
        tokens_after=tokens_after,
        discarded_summary=summary,
    )
    return new_messages, result


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------

def run_compaction_pipeline(
    messages: list[dict],
    system_prompt_tokens: int,
    context_length: int,
    max_messages: int = 200,
) -> list[dict]:
    """Append-only history trimming to fit the model's context budget.

    APPEND-ONLY CONTRACT: every message that survives is returned byte-identical
    to the input - this function never rewrites the content of a kept message.
    The sole operation is dropping the oldest whole atomic groups off the front
    (an assistant+tool_calls message and its following tool results are dropped
    together, never orphaned) when the history exceeds max_messages or the
    token budget. Down to a floor of MIN_MESSAGES.

    Mutating already-sent messages (the old gentle/moderate strategies) is
    intentionally avoided: it loses information the agent still needs and breaks
    Ollama's KV prefix cache. See module docstring.

    Logs the trim if one happened. Returns the (possibly shorter) message list.
    """
    initial_message_count = len(messages)
    initial_token_estimate = estimate_tokens(messages)
    logger.info(
        "compaction [pipeline]: %d messages, ~%d tokens, context=%d, sys_prompt_tokens=%d",
        initial_message_count,
        initial_token_estimate,
        context_length,
        system_prompt_tokens,
    )

    # Append-only: drop oldest whole groups to fit the budget. No in-place edits.
    messages, result = apply_sliding_window(
        messages, max_messages, system_prompt_tokens, context_length,
    )
    if result:
        result.log()

    final_message_count = len(messages)

    if final_message_count == initial_message_count:
        logger.info("compaction [pipeline]: no change (append-only)")
    else:
        logger.info(
            "compaction [pipeline]: done, %d messages, ~%d tokens",
            final_message_count,
            estimate_tokens(messages),
        )

    return messages


# ---------------------------------------------------------------------------
# Checkpoint summary (append-only consolidation)
# ---------------------------------------------------------------------------

def render_messages_for_summary(messages: list[dict]) -> str:
    """Serialize a span of messages to plain text for a summarization prompt.

    Includes role, content, tool-call names, and tool-result tool names so the
    summarizer can see what was asked, answered, and done.
    """
    lines = []
    for m in messages:
        role = m.get("role", "?")
        content = (m.get("content") or "").strip()
        tool_calls = m.get("tool_calls") or []
        if tool_calls:
            names = []
            for tc in tool_calls:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                names.append(fn.get("name", "?"))
            line = f"assistant (called: {', '.join(names)})"
            if content:
                line += f": {content}"
        elif role == "tool":
            line = f"tool[{m.get('tool_name', 'tool')}]: {content}"
        else:
            line = f"{role}: {content}"
        lines.append(line)
    return "\n".join(lines)


async def summarize_conversation(messages: list[dict], instruction: str, llm_mgr,
                                 max_tokens: int | None = None) -> str:
    """Render `messages` and summarize them under a caller-supplied `instruction`.

    The single shared "consolidate a conversation into durable text" primitive:
    one tool-free `ollama.generate` over `instruction` + the rendered transcript.
    Two callers with different instructions - chat's checkpoint summarizer (compress
    an old span in place) and the background-agent reserved memory turn (write a
    cross-run handoff note). `instruction` should END where the transcript belongs;
    the rendered transcript is appended after it.

    Uses the active model (via `llm_mgr.generate`). Returns "" on failure so the
    caller can fall back safely (chat: skip the checkpoint; memory: preserve prior
    memory rather than clobber it with an empty write).

    `max_tokens` caps the RESPONSE. Unset means unbounded, which is how this ran
    until 2026-08-02 - when the background memory turn was measured at 169s/call
    averaged over 14 calls, a quarter of the whole LLM budget. Callers producing a
    bounded artifact (a memory note, a checkpoint summary) should say so.
    """
    transcript = render_messages_for_summary(messages)
    prompt = f"{instruction}\n\n{transcript}"
    logger.info("summarize_conversation: %d msgs, ~%d prompt tokens, cap=%s",
                len(messages), estimate_tokens(messages), max_tokens or "none")
    try:
        result = await llm_mgr.generate(prompt, max_tokens=max_tokens)
        return (result or "").strip()
    except Exception as e:
        logger.warning("summarize_conversation failed: %s", e)
        return ""


def select_checkpoint_span(
    messages: list[dict],
    history_budget_tokens: int,
) -> tuple[list[int], list[int]] | None:
    """Decide which messages to fold into a checkpoint summary.

    Returns (fold_indices, keep_indices) where fold_indices is a contiguous
    prefix to be replaced by one summary message and keep_indices is the
    most-recent suffix kept verbatim. Returns None when no checkpoint is
    warranted.

    Token-based (not turn-based) so it adapts across model context sizes:
      - No-op unless total tokens exceed CHECKPOINT_TRIGGER_FRACTION of budget.
      - Keep the most-recent whole atomic groups that fit within
        CHECKPOINT_KEEP_FRACTION of budget, but never fewer than MIN_MESSAGES.
      - The split is on atomic-group boundaries (an assistant+tool_calls group
        and its tool results stay together), so the kept suffix never starts
        with an orphaned tool message.
    """
    if history_budget_tokens <= 0:
        return None
    if estimate_tokens(messages) <= CHECKPOINT_TRIGGER_FRACTION * history_budget_tokens:
        return None

    groups = _identify_message_groups(messages)
    if len(groups) <= 1:
        return None  # nothing older to fold

    keep_budget = CHECKPOINT_KEEP_FRACTION * history_budget_tokens

    # Walk groups newest -> oldest, keeping recent ones within budget but always
    # at least MIN_MESSAGES messages.
    kept_group_indices: list[int] = []
    kept_tokens = 0
    kept_msg_count = 0
    for gi in range(len(groups) - 1, -1, -1):
        group_tokens = sum(estimate_message_tokens(messages[i]) for i in groups[gi])
        if kept_msg_count >= MIN_MESSAGES and kept_tokens + group_tokens > keep_budget:
            break
        kept_group_indices.append(gi)
        kept_tokens += group_tokens
        kept_msg_count += len(groups[gi])

    kept_group_indices.sort()
    if not kept_group_indices:
        return None
    first_kept = kept_group_indices[0]
    if first_kept == 0:
        return None  # everything kept; nothing to fold

    fold_indices: list[int] = []
    for gi in range(first_kept):
        fold_indices.extend(groups[gi])
    keep_indices: list[int] = []
    for gi in kept_group_indices:
        keep_indices.extend(groups[gi])

    if not fold_indices:
        return None
    return sorted(fold_indices), sorted(keep_indices)


def apply_checkpoint(
    messages: list[dict],
    keep_indices: list[int],
    summary_message: dict,
) -> list[dict]:
    """Replace the folded prefix with one summary message, keep the suffix verbatim.

    Append-only on the kept suffix: those messages are the same objects, byte
    for byte. Only the folded prefix is removed (and represented by the summary).
    """
    return [summary_message] + [messages[i] for i in sorted(keep_indices)]
