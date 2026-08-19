"""Append-only ledgers: the durable half of cross-run memory.

A ledger is a NAMED list its owner may add to but whose rows are never silently
lost. The consolidation turn rewrites memory.md wholesale every run, so anything
that must not be forgotten cannot live there - a fact surviving N runs would have
to survive N verbatim LLM re-copies, and measurably does not. Ledger rows are
merged by CODE instead: the model says what to append, this module decides what
the file ends up containing.

Storage is one page per owner (`_dada/{ns}/{slug}/{AGENT_LEDGERS_FILE}`), one
`##` section per ledger, one row per line. Plain enough to hand-edit, which is
the human override path.

Two-level retention, deliberately split:
  - WITHIN a ledger, rows are append-only and deduped - enforced here.
  - The ledger ITSELF is disposable; its owner may forget() it once the list has
    served its purpose. Only the owner can know that.

Ordering means the same thing at both levels: oldest first, newest last. Rows
arrive that way by construction, and _touch() keeps sections in step by moving a
ledger to the bottom when rows are added to it. That ordering is what
render_capped() spends a tight budget on when the whole book will not fit.

Pure functions - no I/O, no config, no agent imports - so the agent and editor
paths share one implementation and the tests need nothing running.
"""
import re
import unicodedata

# Leading list markers a model or a human may put on a row. The model does emit
# them: a probe returned ["- Cantor set"], which stored raw would reappear as a
# distinct row next run.
_MARKER_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
# Trailing punctuation is cosmetic drift, not a distinction ("Cantor set" vs
# "Cantor set."). Stripped for the dedup KEY only; the stored row keeps its text.
_TRAILING_PUNCT = " \t.,;:!?—-"


def clean_item(raw: str) -> str:
    """The text actually stored for a row: marker removed, whitespace tidied.

    Internal runs collapse too, so two spellings that differ only in spacing are
    stored identically rather than merely comparing equal - the page is meant to
    be read by a human.
    """
    return re.sub(r"\s+", " ", _MARKER_RE.sub("", (raw or ""))).strip()


def item_key(item: str) -> str:
    """Dedup key. Lexical, NOT semantic: "convex hull" and "convex hull algorithms"
    stay distinct rows, which is correct - telling them apart is the prompt's job,
    while never losing a row is this module's."""
    s = unicodedata.normalize("NFC", clean_item(item)).casefold()
    s = re.sub(r"\s+", " ", s)
    return s.strip(_TRAILING_PUNCT)


def name_key(name: str) -> str:
    """Ledger-name key. Case- and space-insensitive so "Topics Covered" reaches
    the ledger a previous run created as "Topics covered"."""
    return re.sub(r"\s+", " ", (name or "").strip()).casefold()


def parse(text: str) -> dict[str, list[str]]:
    """Parse a ledgers page body (frontmatter already stripped) into
    {ledger name: [rows]}, preserving both ledger order and row order.

    Every non-empty line under a heading counts as a row, not just `- ` bullets:
    the page is meant to be hand-editable, and a human writing a bare list should
    not have their edits silently ignored.
    """
    out: dict[str, list[str]] = {}
    current: str | None = None
    for line in (text or "").split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            current = m.group(1)
            out.setdefault(current, [])
            continue
        if current is None:
            continue
        item = clean_item(line)
        if item:
            out[current].append(item)
    return out


def _block(name: str, rows: list[str], shown: int) -> str:
    """One `## heading` section showing the LAST `shown` of `rows`.

    The single renderer for a ledger section, so the full and capped views can
    never drift. `shown >= len(rows)` is the complete section; anything less
    carries a count of what is missing.
    """
    head = f"## {name}"
    if shown >= len(rows):
        return f"{head}\n" + "\n".join(f"- {r}" for r in rows).rstrip()
    lines = [f"- {r}" for r in rows[len(rows) - shown:]] if shown else []
    # Terse by design: the marker states the FACT, and the consumer's intro
    # states what to do about it once (see context_providers.LedgerProvider).
    # Repeating "call recall(...)" per ledger would spend the budget this exists
    # to conserve, and would couple this module to a tool name it must not know.
    lines.append(f"_({len(rows) - shown} of {len(rows)} rows not shown)_")
    return head + "\n" + "\n".join(lines)


def render(ledgers: dict[str, list[str]]) -> str:
    """Render back to markdown. Empty ledgers keep their heading - an emptied
    ledger is a deliberate state, distinct from one that was forgotten."""
    return "\n\n".join(_block(n, items, len(items)) for n, items in ledgers.items())


def render_capped(ledgers: dict[str, list[str]],
                  char_cap: int) -> tuple[str, list[str]]:
    """Render within `char_cap`. Returns (markdown, names shown incompletely).

    OMISSION READS AS NON-EXISTENCE. A reader cannot tell "not in the ledger"
    from "not shown to me", so nothing here disappears quietly: every ledger
    keeps its heading and every elision states how many rows it hid. What the
    caller does with that - offering a way to fetch the rest - is the caller's
    job; being honest about the gap is this function's.

    RECENCY IS THE ONLY PRIORITY SIGNAL. Ledgers are walked newest-touched
    first (see _touch) and each takes what it can from the remaining budget,
    exactly as rows within a ledger keep their tail. No size heuristic: packing
    the largest NUMBER of intact ledgers optimizes a metric nobody asked for,
    and it would let a ledger flicker in and out of full view as it grows.

    Allocation is continuous - a ledger may show 20 rows, or 2, or 0 - with no
    threshold below which a partial view is suppressed. Two rows still carry
    the SHAPE of that ledger's rows, which is what tells a reader whether
    fetching the rest is worth it, and a floor would only re-create in
    miniature the cliff this whole mechanism exists to remove.
    """
    text = render(ledgers)
    if char_cap <= 0 or len(text) <= char_cap:
        return text, []

    names = list(ledgers)
    shown = dict.fromkeys(names, 0)
    # The skeleton - every heading, every count - is the floor. It is emitted
    # whole even when it alone busts the cap: a named ledger can still be asked
    # for, an unnamed one cannot.
    bases = {n: len(_block(n, ledgers[n], 0)) for n in names}
    budget = char_cap - len(_render_blocks(ledgers, shown))

    for name in reversed(names):
        if budget <= 0:
            break
        rows = ledgers[name]
        if not rows:
            continue
        full = len(_block(name, rows, len(rows))) - bases[name]
        if full <= budget:
            shown[name] = len(rows)
            budget -= full
            continue
        # Largest partial view that fits. Monotonic on [0, total-1] because each
        # row costs more than the marker shrinks; the step to `total` is NOT (it
        # drops the marker entirely), which is why full is tested separately.
        lo, hi = 0, len(rows) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if len(_block(name, rows, mid)) - bases[name] <= budget:
                lo = mid
            else:
                hi = mid - 1
        shown[name] = lo
        budget -= len(_block(name, rows, lo)) - bases[name]

    return (_render_blocks(ledgers, shown),
            [n for n in names if shown[n] < len(ledgers[n])])


def _render_blocks(ledgers: dict[str, list[str]], shown: dict[str, int]) -> str:
    """Blocks in FILE order (oldest touched first) whatever order they were
    allocated in - the injected view must read like the page on disk."""
    return "\n\n".join(_block(n, rows, shown[n]) for n, rows in ledgers.items())


def index_text(ledgers: dict[str, list[str]]) -> str:
    """Just the names and row counts - the answer to "what do I have?"."""
    if not ledgers:
        return "(no ledgers)"
    return "\n".join(f"- {n} ({len(v)} rows)" for n, v in ledgers.items())


def recall_text(ledgers: dict[str, list[str]], name: str = "",
                max_rows: int = 0) -> str:
    """One ledger in full (tail-capped), or the index when `name` is empty.

    A miss lists what DOES exist rather than just saying no: the caller reached
    for a ledger by a name it believed in, and the useful correction is the real
    spelling, not a refusal.
    """
    if not ledgers:
        return "(no ledgers recorded yet)"
    if not (name or "").strip():
        return "Ledgers you have:\n" + index_text(ledgers)
    key = find(ledgers, name)
    if key is None:
        return (f"(no ledger named '{name}')\nLedgers you have:\n"
                + index_text(ledgers))
    rows = ledgers[key]
    if not rows:
        return f"## {key}\n(empty - the ledger exists but has no rows)"
    if max_rows and len(rows) > max_rows:
        body = "\n".join(f"- {r}" for r in rows[-max_rows:])
        return (f"## {key} ({len(rows)} rows, showing the last {max_rows})\n"
                f"{body}\n_({len(rows) - max_rows} of {len(rows)} rows not "
                f"shown; they are the oldest)_")
    return f"## {key} ({len(rows)} rows)\n" + "\n".join(f"- {r}" for r in rows)


def find(ledgers: dict[str, list[str]], name: str) -> str | None:
    """Existing ledger name matching `name` (case-insensitively), or None."""
    want = name_key(name)
    for existing in ledgers:
        if name_key(existing) == want:
            return existing
    return None


def _touch(ledgers: dict[str, list[str]], key: str) -> None:
    """Move `key` last - the most recently modified ledger sorts to the bottom.

    Section order then carries the same meaning as row order: oldest at the top,
    newest at the bottom, at both levels. Order IS the recency record, so a file
    format whose whole premise is that a human can hand-edit it needs no
    timestamps bolted on.

    Called ONLY when rows were actually added. A duplicates-only call must leave
    the order alone: apply_ledger_ops persists only when something changed, so
    reordering a no-op would desync this dict from the page on disk.
    """
    ledgers[key] = ledgers.pop(key)


def append(ledgers: dict[str, list[str]], name: str, items: list[str],
           max_items: int, max_count: int) -> tuple[list[str], list[str], str]:
    """Append `items` to ledger `name`, in place. Returns (added, duplicates, refusal).

    `refusal` is "" on success or a human-readable reason. Refusing is deliberate
    over trimming: dropping the oldest row is not even well defined without
    per-row timestamps, and for an avoid-list the oldest rows are exactly the ones
    still worth avoiding. A silently capped ledger is the failure this whole
    mechanism exists to prevent, so overflow is loud and lossless instead.
    """
    key = find(ledgers, name)
    if key is None:
        if len(ledgers) >= max_count:
            return [], [], (
                f"ledger limit reached ({max_count}); '{name}' not created. "
                f"Forget a ledger you no longer need first.")
        key = (name or "").strip()
        if not key:
            return [], [], "ledger name is required"
        ledgers[key] = []

    rows = ledgers[key]
    seen = {item_key(r) for r in rows}
    added: list[str] = []
    dupes: list[str] = []
    for idx, raw in enumerate(items or []):
        item = clean_item(raw)
        if not item:
            continue
        k = item_key(item)
        if k in seen:
            dupes.append(item)
            continue
        if len(rows) >= max_items:
            # Count only what is actually left to record. Blank entries the loop
            # skips are not losses, and this number is the loud part of a refusal.
            dropped = sum(1 for r in items[idx:] if clean_item(r))
            if added:
                _touch(ledgers, key)
            return added, dupes, (
                f"ledger '{key}' is full ({max_items} rows); "
                f"{dropped} item(s) not recorded.")
        seen.add(k)
        rows.append(item)
        added.append(item)
    if added:
        _touch(ledgers, key)
    return added, dupes, ""


def forget(ledgers: dict[str, list[str]], name: str) -> bool:
    """Drop a whole ledger. Returns whether it existed."""
    key = find(ledgers, name)
    if key is None:
        return False
    del ledgers[key]
    return True
