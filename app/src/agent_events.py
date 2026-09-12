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

Examples::

    on: agent stock-digest completed
    on: any agent failed
    on: agent vault-gardener staged changes
    on: staging rejected for vault-gardener
    on: uploads in inbox/, upload
    on: document created in inbox/, document modified in projects/ settled 30m

Full grammar:

    agent <slug> completed|failed|cancelled        -> agent.<verb>, subject=slug
    any agent completed|failed|cancelled           -> agent.<verb>, any subject
    agent <slug> staged [changes]                  -> staging.created, subject=slug
    any agent staged [changes]                     -> staging.created, any subject
    staging created|approved|rejected              -> staging.<verb>, any subject
    staging <verb> by|for [agent] <slug>           -> staging.<verb>, subject=slug
    upload[s] | file uploaded [in|to <prefix>]     -> upload, optional path prefix
    document created|modified|deleted|moved        -> document.<verb>, subject=page
        [in <prefix>] [by any actor] [settled <N>m]

Prefixes with spaces are double-quoted: ``uploads in "My Folder/"``.
Apostrophes are literal (``Joe's Notes/`` needs no quoting); commas stay
reserved as the clause separator even inside quotes. Prefix matching is
case-insensitive (casefold) - vault filesystems are case-insensitive here.

Document events match HUMAN changes unless ``by any actor`` widens them to
agent-authored ones (``system`` consequence writes never match). created and
modified wait for the page body to go quiet - ``settled <N>m``, defaulting to
the dispatcher's default settle - so an editing session fires once, after it.

The dispatch planner (plan_dispatch) is deliberately 100% pure: every piece of
Redis state (pool contents, active/cooling slugs, budget counters) is passed
in and the decision comes back as a DispatchPlan. src.events owns the
transport around it. Loop guards implemented here:

  - self-exclusion (trigger_matches): an agent never matches events about
    itself - by subject, by ``agent:<slug>`` actor, or by cause_run_id prefix
  - depth cap: events at depth >= max_depth match nobody
  - cooldown / budget / already-active / global run lock held: eligible events
    are RETAINED in the pool (deferred, never dropped) until the agent may fire
    again
  - settle: document events are retained until their page has been quiet for
    the trigger's settle time (a hold, not an alert)
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

DOCUMENT_TYPES = frozenset({
    "document.created", "document.modified", "document.deleted", "document.moved",
})
# Document events whose page is still being written; these settle by default.
_SETTLING_TYPES = frozenset({"document.created", "document.modified"})

AVAILABLE_TYPES = frozenset({
    "agent.completed", "agent.failed", "agent.cancelled",
    "staging.created", "staging.approved", "staging.rejected",
    "upload",
}) | DOCUMENT_TYPES

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
    settle_minutes: int | None = None  # quiet time before firing (document.*)
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
    if not 0 <= minutes <= 1440:
        raise TriggerError(f"'settled' takes 0..1440 minutes (got {minutes})")
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

    # document <verb> [in <prefix>] [by any actor] [settled <N>m]
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

    raise TriggerError(f"cannot parse trigger: {raw!r}")


def parse_triggers(text: str) -> list[Trigger]:
    """Parse an ``on:`` value into Triggers; TriggerError on any bad clause."""
    out: list[Trigger] = []
    for clause in text.split(","):
        clause = clause.strip()
        if not clause:
            continue
        out.append(_parse_clause(clause))
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
    case-insensitive, so ``Inbox/`` must match ``inbox/report.pdf``. A move
    matches a prefix at EITHER end: moving a page out of ``inbox/`` is as much
    an inbox event as moving one in.

    Document events carry the writer as actor. Only ``human`` matches unless
    the trigger says ``by any actor``; ``system`` (link rewrites after a move,
    vault seeding) never matches - those writes are consequences, not intent.
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
    if etype in DOCUMENT_TYPES:
        actor = event.get("actor") or ""
        if actor == "system" or (actor != "human" and trig.actor != "any"):
            return False
    if trig.prefix is not None:
        paths = [event.get("subject") or ""]
        if etype == "document.moved":
            paths.append(str((event.get("payload") or {}).get("from") or ""))
        pre = trig.prefix.casefold()
        if not any(p.casefold().startswith(pre) for p in paths):
            return False
    return True


# ---------------------------------------------------------------------------
# Document events: change classification, settle, and rename following
# ---------------------------------------------------------------------------

def doc_key(vault: str, rel: str) -> str:
    """Key for one page's change record / settle lookup. Casefolded: the vault
    mount is case-insensitive, so an agent's ``physics/foo.md`` and the
    watcher's ``Physics/Foo.md`` are the same page."""
    return f"{vault}\x1f{(rel or '').casefold()}"


def classify_change(kind: str, prev_hash: str | None, new_hash: str | None,
                    marker: dict | None) -> dict:
    """What one watcher-observed change means for events. Pure.

    kind: created | modified | deleted | moved. Hashes are of the page BODY
    (frontmatter stripped), so ``Updated`` stamps and LLM AutoTags/Summary
    writes - the busiest writers - never read as a modification. marker is the
    writer marker the write path left ({actor, cause_run_id, depth}); none
    means a human (editor, Obsidian, the move/delete APIs).

    Returns {"emit": bool, "touch": bool, "actor", "cause_run_id", "depth"}.
    ``touch`` = this change restarts the page's settle clock.
    """
    marker = marker or {}
    actor = marker.get("actor") or "human"
    out = {"emit": False, "touch": False, "actor": actor,
           "cause_run_id": marker.get("cause_run_id") or "",
           "depth": int(marker.get("depth") or 0)}
    intent = actor != "system"
    if kind == "modified":
        changed = prev_hash is None or prev_hash != new_hash
        out["emit"] = out["touch"] = changed and intent
    elif kind == "created":
        out["emit"] = out["touch"] = intent
    elif kind in ("deleted", "moved"):
        out["emit"] = intent
    return out


def effective_settle(trig: Trigger, default_m: int) -> int:
    """Minutes a matching event waits for its page to go quiet."""
    if trig.settle_minutes is not None:
        return trig.settle_minutes
    return default_m if trig.type in _SETTLING_TYPES else 0


def follow_moves(pool: list[dict]) -> tuple[dict, list]:
    """Re-point pooled page events at where the page went. Pure.

    A new Obsidian note is born ``Untitled.md``, renamed, then typed into. By
    the time it settles, a ``created`` event naming ``Untitled.md`` points at
    nothing - so a pooled created/modified event whose page later MOVED takes
    the new path (payload ``was`` keeps the old one), and one whose page was
    later DELETED is dropped: there is nothing left to react to. Chains are
    followed in stream order (pool is oldest-first).

    Returns (rewritten {id: entry}, drop_ids). The caller persists both - the
    moved event itself may leave the pool this tick, taking the evidence.
    """
    rewritten: dict[str, dict] = {}
    drops: list[str] = []
    live: dict[str, list[dict]] = {}        # doc_key -> pooled entries naming it
    for ev in pool:
        etype = ev.get("type", "")
        if etype not in DOCUMENT_TYPES:
            continue
        vault = ev.get("vault", "")
        subject = ev.get("subject") or ""
        if etype in _SETTLING_TYPES:
            live.setdefault(doc_key(vault, subject), []).append(ev)
        elif etype == "document.moved":
            src = str((ev.get("payload") or {}).get("from") or "")
            moved = live.pop(doc_key(vault, src), [])
            for old in moved:
                new = dict(old)
                payload = dict(new.get("payload") or {})
                payload.setdefault("was", new.get("subject"))
                new["payload"] = payload
                new["subject"] = subject
                rewritten[new["id"]] = new
            live.setdefault(doc_key(vault, subject), []).extend(
                rewritten[o["id"]] for o in moved)
        elif etype == "document.deleted":
            for old in live.pop(doc_key(vault, subject), []):
                drops.append(old["id"])
                rewritten.pop(old["id"], None)
    return rewritten, drops


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
    settling: dict = field(default_factory=dict)         # slug -> events held for quiet
    matching: dict = field(default_factory=dict)         # event id -> full matching slug list


def _quiet_seconds(ev: dict, now: datetime.datetime, last_touch: dict) -> float:
    """How long the event's page has gone without a body change: since the
    later of its recorded last change and the event itself."""
    stamps = [ev.get("ts", ""),
              last_touch.get(doc_key(ev.get("vault", ""), ev.get("subject") or ""), "")]
    latest = None
    for s in stamps:
        try:
            t = datetime.datetime.fromisoformat(s)
        except (ValueError, TypeError):
            continue
        latest = t if latest is None or t > latest else latest
    return float("inf") if latest is None else (now - latest).total_seconds()


def plan_dispatch(agents: list[tuple[str, list, list]], pool: list[dict],
                  now: datetime.datetime, active: set, cooling: set,
                  budget_used: dict, *, max_depth: int, budget_per_hour: int,
                  max_age_s: int,
                  unavailable: set | frozenset = frozenset(),
                  run_lock_held: bool = False,
                  last_touch: dict | None = None,
                  default_settle_m: int = 0) -> DispatchPlan:
    """Decide fires/deletes/retentions for one tick. Pure - all Redis state
    comes in as arguments (agents: (slug, triggers, target_vaults)).

    Deferral semantics: events matching an active/cooling/over-budget agent
    are simply NOT delivered this tick - they stay in the pool (the caller
    only deletes plan.delete_ids and events whose full matching set has been
    delivered).

    Settling is the same retention, per subscriber: an event is held for a
    slug until its page (last_touch: doc_key -> last body change) has been
    quiet for that slug's settle time - the smallest effective_settle among
    the slug's matching triggers. Held events are counted in plan.settling,
    not plan.deferred: waiting for quiet is the design working, not an alert.
    """
    last_touch = last_touch or {}
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
        settle_for: dict[str, int] = {}
        for slug, trigs, targets in agents:
            if ev.get("vault") not in targets:
                continue
            hits = [t for t in trigs if trigger_matches(t, ev, slug)]
            if hits:
                matching.append(slug)
                settle_for[slug] = min(effective_settle(t, default_settle_m)
                                       for t in hits)
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
        quiet_s = None
        for s in undelivered:
            if settle_for.get(s):
                if quiet_s is None:
                    quiet_s = _quiet_seconds(ev, now, last_touch)
                if quiet_s < settle_for[s] * 60:
                    plan.settling[s] = plan.settling.get(s, 0) + 1
                    continue
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
        if run_lock_held:                                  # guard 7: defer
            # LAST, so a more specific reason always wins: an agent that is also
            # cooling would not run even with the lock free. Guard 6 asks whether
            # THIS agent is busy; this asks whether ANY agent is - the condition
            # run_agent_task actually enforces. Without it the planner fires into a
            # lock it never consulted and the run is dropped, because delivery is
            # recorded at enqueue.
            plan.deferred[slug] = {"reason": "another agent run holds the global "
                                             "run lock",
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
