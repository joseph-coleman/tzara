# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Event-trigger grammar + the pure dispatch planner.

No Redis and no I/O at runtime - every function here is decision logic over
plain values, which is what makes the guard behavior unit-testable. (The one
non-stdlib import is agent_registry's shared SLUG_RE pattern.)

Agents subscribe to application events through an ``on:`` frontmatter field -
a small human-readable grammar, sibling to ``schedule:`` (src.agent_schedule).
Clauses are comma-separated; case-insensitive; filler words dropped.

Pass-1 (available) forms::

    on: agent stock-digest completed
    on: any agent failed
    on: agent vault-gardener staged changes
    on: staging rejected for vault-gardener
    on: uploads in inbox/, upload

Full grammar:

    agent <slug> completed|failed|cancelled        -> agent.<verb>, subject=slug
    any agent completed|failed|cancelled           -> agent.<verb>, any subject
    agent <slug> staged [changes]                  -> staging.created, subject=slug
    any agent staged [changes]                     -> staging.created, any subject
    staging created|approved|rejected              -> staging.<verb>, any subject
    staging <verb> by|for [agent] <slug>           -> staging.<verb>, subject=slug
    upload[s] | file uploaded [in|to <prefix>]     -> upload, optional path prefix

Prefixes with spaces are double-quoted: ``uploads in "My Folder/"``.
Apostrophes are literal (``Joe's Notes/`` needs no quoting); commas stay
reserved as the clause separator even inside quotes. Prefix matching is
case-insensitive (casefold) - vault filesystems are case-insensitive here.

Future forms (document/chat events) parse to their full shape but are refused
at load time with "not available yet" - the grammar is the user-facing
contract and must not change when those event types ship::

    document created|modified|deleted|moved [in <prefix>] [by any actor]
                                            [settled <N>m]
    chat with <prefix>

The dispatch planner (plan_dispatch) is deliberately 100% pure: every piece of
Redis state (pool contents, active/cooling slugs, budget counters) is passed
in and the decision comes back as a DispatchPlan. src.events owns the
transport around it. Loop guards implemented here:

  - self-exclusion (trigger_matches): an agent never matches events about
    itself - by subject, by ``agent:<slug>`` actor, or by cause_run_id prefix
  - depth cap: events at depth >= max_depth match nobody
  - cooldown / budget / already-active: eligible events are RETAINED in the
    pool (deferred, never dropped) until the agent may fire again
  - max-age: stale pool events are discarded
  - static cycle check (validate_trigger_graph): NAMED subscriptions to
    run-emitted events form a dependency graph; cycles are a load-time error
"""

import datetime
import re
import shlex
from dataclasses import dataclass, field

# The single definition of a valid agent slug lives in the registry; the
# import is safe (the registry imports THIS module only lazily, inside
# parse_agent_file/list_agents - no cycle).
from src.agent_registry import SLUG_RE  # noqa: E402

_FILLER = {"when", "a", "an", "the"}

# Event types emittable/subscribable in pass 1.
AVAILABLE_TYPES = frozenset({
    "agent.completed", "agent.failed", "agent.cancelled",
    "staging.created", "staging.approved", "staging.rejected",
    "upload",
})
# Parseable but refused at load time ("not available yet").
FUTURE_TYPES = frozenset({
    "document.created", "document.modified", "document.deleted",
    "document.moved", "chat",
})

# Events an agent RUN emits (edges for the static cycle check). Human-gated
# staging.approved/rejected deliberately create no edge - a human click breaks
# any loop through them.
_RUN_EMITTED = frozenset({
    "agent.completed", "agent.failed", "agent.cancelled", "staging.created",
})

_AGENT_VERBS = {"completed": "completed", "failed": "failed",
                "cancelled": "cancelled", "canceled": "cancelled"}
_STAGING_VERBS = frozenset({"created", "approved", "rejected"})
_DOC_VERBS = frozenset({"created", "modified", "deleted", "moved"})


class TriggerError(ValueError):
    """A trigger clause that cannot be parsed (or names an unshipped type)."""


@dataclass
class Trigger:
    type: str                          # canonical event type ("agent.completed", ...)
    subject: str | None = None         # agent-slug scope; None = any
    prefix: str | None = None          # path-prefix scope (upload / document.* / chat)
    actor: str | None = None           # None = default policy; "any" = include agents
    settle_minutes: int | None = None  # future pool-hold policy (document.*)
    raw: str = ""


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _tokenize(raw: str) -> list[str]:
    """Whitespace tokens honoring double-quoted segments, so folder names
    with spaces are expressible: ``uploads in "My Folder/"``. Only the double
    quote is special - apostrophes (Joe's Notes/), backslashes and ``#`` are
    literal characters."""
    lex = shlex.shlex(raw, posix=True)
    lex.whitespace_split = True
    lex.quotes = '"'
    lex.escape = ""
    lex.escapedquotes = ""
    lex.commenters = ""
    try:
        return list(lex)
    except ValueError as e:
        raise TriggerError(f"cannot parse trigger {raw!r}: {e}")


def _slug_of(token: str, raw: str) -> str:
    slug = token.lower()
    if not SLUG_RE.match(slug):
        raise TriggerError(f"bad agent slug {token!r} in trigger {raw!r}")
    return slug


def _agent_verb(tokens_l: list[str]) -> str | None:
    """agent.<verb> for a ``completed|failed|cancelled`` tail, staging.created
    for a ``staged [changes]`` tail, else None."""
    if len(tokens_l) == 1 and tokens_l[0] in _AGENT_VERBS:
        return f"agent.{_AGENT_VERBS[tokens_l[0]]}"
    if tokens_l in (["staged"], ["staged", "changes"]):
        return "staging.created"
    return None


def _parse_settle(tokens: list[str], i: int, raw: str) -> tuple[int, int]:
    """Parse the value of a ``settled`` modifier starting at tokens[i].
    Returns (minutes, next_index)."""
    if i >= len(tokens):
        raise TriggerError(f"'settled' needs a duration in trigger {raw!r}")
    tok = tokens[i].lower()
    m = re.match(r"^(\d+)(m|min|mins|minute|minutes)?$", tok)
    if not m:
        raise TriggerError(f"cannot parse settled duration {tokens[i]!r} in {raw!r}")
    minutes = int(m.group(1))
    nxt = i + 1
    if not m.group(2) and nxt < len(tokens) and \
            tokens[nxt].lower() in ("m", "min", "mins", "minute", "minutes"):
        nxt += 1
    if not 1 <= minutes <= 1440:
        raise TriggerError(f"'settled' takes 1..1440 minutes (got {minutes})")
    return minutes, nxt


def _parse_clause(raw: str) -> Trigger:
    tokens = [t for t in _tokenize(raw.strip()) if t]
    tokens = [t for t in tokens if t.lower() not in _FILLER]
    if not tokens:
        raise TriggerError("empty trigger")
    lt = [t.lower() for t in tokens]

    # upload | uploads | file uploaded  [in|to <prefix>]
    rest = None
    if lt[0] in ("upload", "uploads"):
        rest = tokens[1:]
    elif lt[:2] == ["file", "uploaded"]:
        rest = tokens[2:]
    if rest is not None:
        if not rest:
            return Trigger(type="upload", raw=raw)
        if len(rest) == 2 and rest[0].lower() in ("in", "to"):
            return Trigger(type="upload", prefix=rest[1].lstrip("/"), raw=raw)
        raise TriggerError(f"cannot parse trigger: {raw!r}")

    # agent <slug> <verb> | agent <slug> staged [changes]
    if lt[0] == "agent" and len(tokens) >= 3:
        slug = _slug_of(tokens[1], raw)
        etype = _agent_verb(lt[2:])
        if etype:
            return Trigger(type=etype, subject=slug, raw=raw)
        raise TriggerError(f"cannot parse trigger: {raw!r}")

    # any agent <verb> | any agent staged [changes]
    if lt[:2] == ["any", "agent"] and len(tokens) >= 3:
        etype = _agent_verb(lt[2:])
        if etype:
            return Trigger(type=etype, subject=None, raw=raw)
        raise TriggerError(f"cannot parse trigger: {raw!r}")

    # staging <verb> [by|for [agent] <slug>]
    if lt[0] == "staging" and len(tokens) >= 2:
        if lt[1] not in _STAGING_VERBS:
            raise TriggerError(f"cannot parse trigger: {raw!r}")
        etype = f"staging.{lt[1]}"
        rest, rest_l = tokens[2:], lt[2:]
        if not rest:
            return Trigger(type=etype, subject=None, raw=raw)
        if rest_l[0] in ("by", "for"):
            idx = 2 if len(rest_l) > 1 and rest_l[1] == "agent" else 1
            if len(rest) == idx + 1:
                return Trigger(type=etype, subject=_slug_of(rest[idx], raw), raw=raw)
        raise TriggerError(f"cannot parse trigger: {raw!r}")

    # document <verb> [in <prefix>] [by any actor] [settled <N>m]   (FUTURE)
    if lt[0] in ("document", "documents") and len(tokens) >= 2:
        if lt[1] not in _DOC_VERBS:
            raise TriggerError(f"cannot parse trigger: {raw!r}")
        trig = Trigger(type=f"document.{lt[1]}", raw=raw)
        i = 2
        while i < len(tokens):
            word = lt[i]
            if word == "in" and i + 1 < len(tokens):
                trig.prefix = tokens[i + 1].lstrip("/")
                i += 2
            elif word == "by" and lt[i + 1:i + 3] == ["any", "actor"]:
                trig.actor = "any"
                i += 3
            elif word == "settled":
                trig.settle_minutes, i = _parse_settle(tokens, i + 1, raw)
            else:
                raise TriggerError(
                    f"cannot parse trigger modifier {tokens[i]!r} in {raw!r}")
        return trig

    # chat with <prefix>   (FUTURE)
    if lt[0] == "chat" and len(tokens) == 3 and lt[1] == "with":
        return Trigger(type="chat", prefix=tokens[2].lstrip("/"), raw=raw)

    raise TriggerError(f"cannot parse trigger: {raw!r}")


def parse_triggers(text: str, *, allow_future: bool = False) -> list[Trigger]:
    """Parse an ``on:`` value into Triggers; TriggerError on any bad clause.

    Future event types (document.*, chat) parse to their full shape but raise
    "not available yet" unless allow_future - the grammar stays stable while
    the event types ship incrementally.
    """
    out: list[Trigger] = []
    for clause in text.split(","):
        clause = clause.strip()
        if not clause:
            continue
        trig = _parse_clause(clause)
        if trig.type in FUTURE_TYPES and not allow_future:
            raise TriggerError(f"'{trig.type}' triggers are not available yet")
        out.append(trig)
    if not out:
        raise TriggerError("empty trigger")
    return out


# ---------------------------------------------------------------------------
# Matching (self-exclusion is guard #1 and lives here)
# ---------------------------------------------------------------------------

def trigger_matches(trig: Trigger, event: dict, subscriber_slug: str) -> bool:
    """Does one Trigger match one event envelope, for this subscriber?

    Self-exclusion first - an agent NEVER matches events about itself: by
    subject (agent.*/staging.* events name the agent), by actor
    (``agent:<slug>``), or by cause_run_id. Run ids are minted as
    ``{slug}-{vault}-{YYYYMMDD-HHMMSS}`` and the event's vault IS the run's
    vault, so the cause check can be EXACT (fullmatch) - no hyphenated-slug
    false positives (``stock`` vs ``stock-digest``). If the run-id format
    ever changes this check quietly stops matching, but the subject/actor
    exclusions above cover every event the system emits today on their own.

    Prefix scoping is casefolded - the vault filesystems here are
    case-insensitive, so ``Inbox/`` must match ``inbox/report.pdf``.
    """
    etype = event.get("type", "")
    if (etype.startswith("agent.") or etype.startswith("staging.")) \
            and event.get("subject") == subscriber_slug:
        return False
    if event.get("actor") == f"agent:{subscriber_slug}":
        return False
    cause = event.get("cause_run_id") or ""
    if cause and re.fullmatch(
            re.escape(subscriber_slug) + "-"
            + re.escape(event.get("vault", "")) + r"-\d{8}-\d{6}", cause):
        return False

    if trig.type != etype:
        return False
    if trig.subject is not None and event.get("subject") != trig.subject:
        return False
    if trig.prefix is not None \
            and not (event.get("subject") or "").casefold().startswith(
                trig.prefix.casefold()):
        return False
    return True


# ---------------------------------------------------------------------------
# Static cycle check (guard #7, runs at registry load)
# ---------------------------------------------------------------------------

def validate_trigger_graph(subs: list[tuple[str, list[Trigger]]]) -> dict[str, str]:
    """slug -> error message for every agent on a NAMED trigger cycle.

    Edges: A depends on B iff A has a NAMED (subject) trigger of a run-emitted
    type on B. Wildcards create no edges (self-exclusion bounds them); a named
    self-subscription is flagged too (it can never fire). Content-mediated
    cycles cannot be seen statically - the dynamic guards own those.
    """
    errors: dict[str, str] = {}
    slugs = {s for s, _ in subs}
    edges: dict[str, set[str]] = {}
    for slug, trigs in subs:
        deps: set[str] = set()
        for t in trigs:
            if t.type in _RUN_EMITTED and t.subject:
                if t.subject == slug:
                    errors[slug] = (
                        f"trigger '{t.raw}' can never fire - an agent's "
                        "subscriptions never match its own events")
                elif t.subject in slugs:
                    deps.add(t.subject)
        edges[slug] = deps

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {s: WHITE for s in edges}
    stack: list[str] = []

    def dfs(node: str) -> None:
        color[node] = GRAY
        stack.append(node)
        for dep in sorted(edges[node]):
            if color[dep] == GRAY:
                cycle = stack[stack.index(dep):] + [dep]
                path = " -> ".join(cycle)
                for s in cycle[:-1]:
                    errors.setdefault(s, f"trigger cycle: {path}")
            elif color[dep] == WHITE:
                dfs(dep)
        stack.pop()
        color[node] = BLACK

    for s in sorted(edges):
        if color[s] == WHITE:
            dfs(s)
    return errors


# ---------------------------------------------------------------------------
# Pure dispatch planning (guards #2-#6 decision logic)
# ---------------------------------------------------------------------------

@dataclass
class Fire:
    """One event-triggered run to enqueue: an agent, one vault, its events."""
    slug: str
    vault_id: str
    events: list = field(default_factory=list)
    depth: int = 1


@dataclass
class DispatchPlan:
    fires: list = field(default_factory=list)            # list[Fire]
    delete_ids: list = field(default_factory=list)       # unconditional pool deletes
    dropped_expired: int = 0                             # deleted for age (counted)
    dropped_depth: int = 0                               # had subscribers but hit the
                                                         # depth cap (counted)
    deferred: dict = field(default_factory=dict)         # slug -> {"reason", "events"}
    matching: dict = field(default_factory=dict)         # event id -> full matching slug list


def plan_dispatch(agents: list[tuple[str, list, list]], pool: list[dict],
                  now: datetime.datetime, active: set, cooling: set,
                  budget_used: dict, *, max_depth: int, budget_per_hour: int,
                  max_age_s: int,
                  unavailable: set | frozenset = frozenset()) -> DispatchPlan:
    """Decide fires/deletes/retentions for one tick. Pure - all Redis state
    comes in as arguments (agents: (slug, triggers, target_vaults)).

    Deferral semantics: events matching an active/cooling/over-budget agent
    are simply NOT delivered this tick - they stay in the pool (the caller
    only deletes plan.delete_ids and events whose full matching set has been
    delivered). ``settled Xm`` later becomes one more retain-branch here.
    """
    plan = DispatchPlan()
    per_slug: dict[str, list[dict]] = {}

    for ev in pool:
        eid = ev.get("id", "")
        try:
            born = datetime.datetime.fromisoformat(ev.get("ts", ""))
            age_s = (now - born).total_seconds()
        except (ValueError, TypeError):
            age_s = None  # unparseable birth time -> treat as expired
        if age_s is None or age_s > max_age_s:
            plan.delete_ids.append(eid)
            plan.dropped_expired += 1
            continue

        matching: list[str] = []
        for slug, trigs, targets in agents:
            if ev.get("vault") not in targets:
                continue
            if any(trigger_matches(t, ev, slug) for t in trigs):
                matching.append(slug)
        if not matching:
            plan.delete_ids.append(eid)                    # pool hygiene
            continue
        if int(ev.get("depth", 0)) >= max_depth:           # guard 2: depth cap -
            plan.delete_ids.append(eid)                    # dropped VISIBLY: this
            plan.dropped_depth += 1                        # event had subscribers
            continue

        delivered = set(ev.get("delivered") or [])
        undelivered = [s for s in matching if s not in delivered]
        if not undelivered:
            plan.delete_ids.append(eid)                    # everyone already fired
            continue
        plan.matching[eid] = matching
        for s in undelivered:
            per_slug.setdefault(s, []).append(ev)

    for slug in sorted(per_slug):
        # Deferrals are RECORDED, not silent - the dispatcher publishes them
        # for the /agents surface (a budget breach = possible trigger storm).
        if slug in unavailable:                            # invalid definition
            plan.deferred[slug] = {"reason": "definition currently invalid - "
                                             "deferring until it parses again",
                                   "events": len(per_slug[slug])}
            continue
        if slug in active:                                 # guard 6: defer
            plan.deferred[slug] = {"reason": "agent busy (run active)",
                                   "events": len(per_slug[slug])}
            continue
        if slug in cooling:                                # guard 3: defer
            plan.deferred[slug] = {"reason": "cooling down between event fires",
                                   "events": len(per_slug[slug])}
            continue
        if budget_used.get(slug, 0) >= budget_per_hour:    # guard 4: defer
            plan.deferred[slug] = {"reason": "over hourly event budget",
                                   "events": len(per_slug[slug])}
            continue
        by_vault: dict[str, list[dict]] = {}
        for ev in per_slug[slug]:
            by_vault.setdefault(ev.get("vault", ""), []).append(ev)
        for vault in sorted(by_vault):
            vevts = by_vault[vault]
            depth = max(int(e.get("depth", 0)) for e in vevts) + 1
            plan.fires.append(Fire(slug=slug, vault_id=vault,
                                   events=vevts, depth=depth))
    return plan
