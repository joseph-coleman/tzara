# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Human-readable schedule grammar for agent files - richer than keywords,
far simpler than cron. Pure stdlib; naive local datetimes throughout.

Accepted forms (case-insensitive; `on`/`the`/`every`/`of the month` are filler;
time introduced by `@` or `at`, as `3:30 pm`, `15:30`, or a bare hour `4`):

    hourly
    every 15 minutes         every minute / every 30 min   (sub-hour interval)
    3 times an hour          4 times per hour              (interval = 60/N min)
    daily                    daily @ 3:30 pm
    weekly                   weekly on tuesday @ 9 am
    saturday                 saturday @ 7 am          (bare weekday = weekly)
    1st saturday             2nd tuesday @ 6:15 pm    (that weekday of each month)
    last friday
    first of the month       last of the month @ 3:30 pm

Anything the grammar can't say is written as a standard 5-field CRON expression
instead - the escape hatch, recognized by shape (five cron-ish fields) or by an
explicit `cron` prefix, plus the usual `@daily`-style nicknames:

    0 */4 * * *              cron 0 */4 * * *         (every 4 hours)
    30 9 * * mon-fri         0 9 1,15 * *
    @daily                   @weekly

A rule without a time runs at AGENT_DEFAULT_RUN_HOUR (config, default 4am) plus
a stable per-agent offset within AGENT_SCHEDULE_JITTER_MINUTES - otherwise every
untimed agent fires on the same minute, and the herd grows with each one added.
Pass `jitter_key=<slug>` to `next_due` to get that spread; a rule that NAMES a
time is never moved. Times are naive LOCAL time, so the container's TZ is the
schedule's timezone (see docker-compose: TZ defaults to UTC).
`hourly` runs at the top of each hour, and `interval` schedules snap to
wall-clock boundaries anchored at midnight (so "every 15 minutes" fires at
:00/:15/:30/:45). `next_due(schedule, after)` returns the next occurrence
strictly AFTER `after` - the scheduler stores a last-run stamp and fires when
next_due(last_run) has passed, so restarts neither double-fire nor skip.

NB: an interval finer than the scheduler tick (AGENT_SCHEDULER_TICK_S) cannot
be honored - the tick is the resolution floor. `parse_schedule` accepts any
interval >= 1 minute regardless; keep the tick <= the smallest interval you use.
The same floor applies to a cron expression whose minute field fires more than
once per tick.
"""

import calendar
import datetime
import hashlib
import re
from dataclasses import dataclass

from config import AGENT_DEFAULT_RUN_HOUR, AGENT_SCHEDULE_JITTER_MINUTES

_WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}  # monday=0
_ORDINALS = {"1st": 1, "first": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3,
             "4th": 4, "fourth": 4, "last": -1}


@dataclass
class Schedule:
    kind: str                 # hourly | interval | daily | weekly | ordinal_weekday | month_edge | cron
    weekday: int | None = None    # 0=monday (weekly / ordinal_weekday)
    ordinal: int | None = None    # 1-4, -1=last (ordinal_weekday / month_edge day-of-month)
    hour: int | None = None       # None -> AGENT_DEFAULT_RUN_HOUR
    minute: int = 0
    interval_minutes: int | None = None   # interval: gap between runs, in minutes
    cron: "CronSpec | None" = None        # cron: the expanded 5-field expression
    raw: str = ""


class ScheduleError(ValueError):
    pass


_TIME_RE = re.compile(
    r"(?:@|\bat\b)\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.IGNORECASE)


def _extract_time(text: str) -> tuple[str, int | None, int]:
    """Split a trailing time clause off the rule. Returns (rest, hour, minute)."""
    m = _TIME_RE.search(text)
    if not m:
        return text.strip(), None, 0
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleError(f"invalid time in schedule: {text!r}")
    return text[: m.start()].strip(), hour, minute


_MINUTE_WORDS = {"minute", "minutes", "min", "mins", "m"}
_HOUR_WORDS = {"hour", "hours", "hr", "hrs", "h"}
_INTERVAL_RE = re.compile(r"(\d+)\s*(?:m|mins?|minutes?)$")
_INTERVAL_HOURS_RE = re.compile(r"(\d+)\s*(?:h|hrs?|hours?)$")


def _parse_interval(words: list[str]) -> int | None:
    """Recognize the interval spellings, returning a gap in minutes (or None if
    `words` is not an interval rule). Raises ScheduleError only when the spelling
    is clearly an interval but the number is out of range.

        every 15 minutes / every 30 min / every 15m  -> 15, 30, 15
        every minute                                 -> 1
        every 4 hours / every 6h                     -> 240, 360
        3 times an hour / 4 times per hour           -> 20, 15  (60 // N)
    """
    joined = " ".join(words)

    # "<N> times a|an|per hour"  (filler 'every'/'the' already stripped upstream)
    if (len(words) == 4 and words[0].isdigit() and words[1] in ("time", "times")
            and words[2] in ("a", "an", "per") and words[3] == "hour"):
        n = int(words[0])
        if not (1 <= n <= 60) or 60 % n != 0:
            raise ScheduleError(
                f"'{n} times an hour' must divide 60 evenly (1,2,3,4,5,6,...)")
        return 60 // n

    # "every minute" / "every hour" (bare)
    if joined in ("minute", "min"):
        return 1
    if joined in ("hour", "hr"):
        return 60

    # "every <N> minutes" and glued "<N>m"
    m = _INTERVAL_RE.fullmatch(joined)
    if m:
        n = int(m.group(1))
        if not (1 <= n <= 1440):
            raise ScheduleError(f"interval must be 1..1440 minutes, got {n}")
        return n

    # "every <N> hours" - the same midnight-anchored interval, in hour units, so
    # "every 4 hours" lands on 00:00/04:00/08:00/... rather than drifting.
    m = _INTERVAL_HOURS_RE.fullmatch(joined)
    if m:
        n = int(m.group(1))
        if not (1 <= n <= 24):
            raise ScheduleError(f"interval must be 1..24 hours, got {n}")
        return n * 60

    # A trailing minute/hour word with junk in front is a malformed interval, not
    # a fall-through to weekday/etc. parsing - flag it so the user gets a clear error.
    if words and (words[-1] in _MINUTE_WORDS or words[-1] in _HOUR_WORDS):
        raise ScheduleError(f"cannot parse interval schedule from {joined!r}")

    return None


# ---------------------------------------------------------------------------
# Cron escape hatch
# ---------------------------------------------------------------------------
# The grammar above is deliberately small, so anything it cannot say ("every 4
# hours on weekdays", "0 9 1,15 * *") is written as a standard 5-field cron
# expression instead. Supported syntax is Vixie-cron's portable core: `*`,
# lists, ranges, `/` steps, and 3-letter month/weekday names. Not supported (and
# rejected with a message rather than silently misread): `L`/`W`/`#` extensions,
# `?`, seconds/year fields, and wrap-around ranges like `22-2`.

_CRON_RANGES = {"minute": (0, 59), "hour": (0, 23), "day-of-month": (1, 31),
                "month": (1, 12), "day-of-week": (0, 7)}  # cron dow: 0 AND 7 = sunday
_CRON_NAMES = {
    "month": {n.lower(): i for i, n in enumerate(calendar.month_abbr) if i},
    "day-of-week": {"sun": 0, "mon": 1, "tue": 2, "wed": 3,
                    "thu": 4, "fri": 5, "sat": 6},
}
_CRON_NICKNAMES = {
    "@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *",
}
_CRON_PREFIX_RE = re.compile(r"^cron\b[:\s]*")
# A field is cron-shaped when it is cron atoms (a number, `*`, or a month/weekday
# NAME) joined by cron punctuation. Deliberately strict about the name list: a
# looser `[a-z]{3,}` would also match "every"/"minutes"/"at", and a five-word
# English rule would then be reported with a baffling cron error.
_CRON_ATOM = (r"(?:\d+|\*|"
              + "|".join(sorted(set(_CRON_NAMES["month"])
                                | set(_CRON_NAMES["day-of-week"]))) + r")")
_CRON_TOKEN_RE = re.compile(_CRON_ATOM + r"(?:[-/,]" + _CRON_ATOM + r")*")
_CRON_HORIZON_DAYS = 366 * 5      # next_due search bound (a Feb 29 rule needs >4y)
_DAYS_IN_MONTH = (31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)  # leap-generous


@dataclass(frozen=True)
class CronSpec:
    """A parsed cron expression, each field expanded to the values it matches.

    `weekdays` is in PYTHON convention (monday=0) like the rest of this module,
    converted from cron's sunday=0 at parse time. The `_restricted` flags carry
    the classic Vixie day rule: when BOTH day-of-month and day-of-week are
    restricted a day matches on EITHER (union, not intersection), which is why
    the expanded sets alone are not enough to decide a match.
    """
    minutes: tuple[int, ...]      # sorted, for the next_due walk
    hours: tuple[int, ...]        # sorted
    days: frozenset[int]          # day-of-month
    months: frozenset[int]
    weekdays: frozenset[int]      # monday=0
    dom_restricted: bool
    dow_restricted: bool


def _cron_value(token: str, field: str) -> int:
    names = _CRON_NAMES.get(field, {})
    lo, hi = _CRON_RANGES[field]
    if token in names:
        value = names[token]
    elif token.isdigit():
        value = int(token)
    else:
        raise ScheduleError(f"bad {field} value {token!r} in cron expression")
    if not (lo <= value <= hi):
        raise ScheduleError(f"cron {field} must be {lo}-{hi}, got {token!r}")
    return value


def _cron_field(text: str, field: str) -> set[int]:
    """Expand one field (`*`, lists, ranges, steps) to the set of values it matches."""
    lo, hi = _CRON_RANGES[field]
    out: set[int] = set()
    for part in text.split(","):
        if not part:
            raise ScheduleError(f"empty item in cron {field} field {text!r}")
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise ScheduleError(
                    f"bad step {step_text!r} in cron {field} field {text!r}")
            step = int(step_text)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            bits = part.split("-")
            if len(bits) != 2:
                raise ScheduleError(f"bad range {part!r} in cron {field} field")
            start, end = _cron_value(bits[0], field), _cron_value(bits[1], field)
            if start > end:
                raise ScheduleError(
                    f"cron {field} range {part!r} must ascend - wrap-around is not "
                    f"supported, write it as two comma-separated ranges")
        else:
            start = _cron_value(part, field)
            # Vixie's `N/step` shorthand: start at N, run to the top of the field.
            end = hi if step > 1 else start
        out.update(range(start, end + 1, step))
    if not out:
        raise ScheduleError(f"cron {field} field {text!r} matches nothing")
    return out


def _parse_cron(text: str) -> CronSpec:
    fields = text.split()
    if len(fields) != 5:
        raise ScheduleError(
            f"a cron expression needs 5 fields (minute hour day-of-month month "
            f"day-of-week), got {len(fields)}: {text!r}")
    minute_f, hour_f, dom_f, month_f, dow_f = fields
    months = _cron_field(month_f, "month")
    days = _cron_field(dom_f, "day-of-month")
    spec = CronSpec(
        minutes=tuple(sorted(_cron_field(minute_f, "minute"))),
        hours=tuple(sorted(_cron_field(hour_f, "hour"))),
        days=frozenset(days),
        months=frozenset(months),
        # cron sunday=0 -> python monday=0
        weekdays=frozenset((v + 6) % 7 for v in _cron_field(dow_f, "day-of-week")),
        dom_restricted=dom_f != "*",
        dow_restricted=dow_f != "*",
    )
    # Reject day/month pairs that can never occur ("0 0 30 2 *") at parse time
    # rather than letting next_due walk its whole horizon for a day that is never
    # coming. Only decidable when day-of-week cannot rescue the match.
    if not spec.dow_restricted and not any(
            d <= _DAYS_IN_MONTH[m - 1] for m in months for d in days):
        raise ScheduleError(f"cron expression never occurs: {text!r}")
    return spec


def _cron_text(raw: str) -> str | None:
    """The cron expression `raw` denotes, or None if `raw` is not cron at all.

    Three ways in: an `@hourly`-style nickname, an explicit `cron ...` / `cron: ...`
    prefix (which FORCES cron parsing, so a typo reports a cron error instead of
    falling through to a generic "cannot parse schedule"), or a bare expression
    recognized by shape.
    """
    low = raw.strip().lower()
    if low in _CRON_NICKNAMES:
        return _CRON_NICKNAMES[low]
    m = _CRON_PREFIX_RE.match(low)
    if m:
        return low[m.end():].strip()
    fields = low.split()
    # 4-of-5 rather than all-5, so a rule that is plainly cron but uses an
    # unsupported token ("0 0 L * *") is still routed here and gets a cron error
    # naming the bad field, instead of a generic "cannot parse schedule".
    if len(fields) == 5 and sum(
            bool(_CRON_TOKEN_RE.fullmatch(f)) for f in fields) >= 4:
        return low
    return None


def _cron_day_matches(spec: CronSpec, day: datetime.date) -> bool:
    if day.month not in spec.months:
        return False
    dom_ok = day.day in spec.days
    dow_ok = day.weekday() in spec.weekdays
    if spec.dom_restricted and spec.dow_restricted:
        return dom_ok or dow_ok          # Vixie union rule
    return dom_ok and dow_ok             # an unrestricted field is always True


# ---------------------------------------------------------------------------


def parse_schedule(text: str) -> Schedule:
    """Parse a schedule rule; raises ScheduleError on anything unrecognized."""
    raw = text.strip()
    if not raw:
        raise ScheduleError("empty schedule")

    # Cron is tested FIRST: `@daily` and `*` tokens would otherwise be chewed up
    # by the time-clause regex and the filler-word stripper below.
    cron_text = _cron_text(raw)
    if cron_text is not None:
        return Schedule(kind="cron", cron=_parse_cron(cron_text), raw=raw)

    rest, hour, minute = _extract_time(raw.lower())
    # Strip filler words so the grammar reads naturally in agent files.
    words = [w for w in re.split(r"[\s,]+", rest)
             if w and w not in ("on", "the", "every", "each")]

    if words == ["hourly"]:
        if hour is not None:
            raise ScheduleError("hourly takes no time clause")
        return Schedule(kind="hourly", raw=raw)

    # Sub-hour intervals. Both spellings reduce to a gap in minutes and reject a
    # trailing time clause (a boundary-snapped interval has no single time).
    interval = _parse_interval(words)
    if interval is not None:
        if hour is not None:
            raise ScheduleError("interval schedule takes no time clause")
        return Schedule(kind="interval", interval_minutes=interval, raw=raw)

    if words == ["daily"]:
        return Schedule(kind="daily", hour=hour, minute=minute, raw=raw)

    # weekly [<weekday>]
    if words and words[0] == "weekly":
        wd = _WEEKDAYS.get(words[1]) if len(words) > 1 else 0
        if len(words) > 2 or (len(words) == 2 and wd is None):
            raise ScheduleError(f"cannot parse schedule: {raw!r}")
        return Schedule(kind="weekly", weekday=wd, hour=hour, minute=minute, raw=raw)

    # bare weekday = weekly
    if len(words) == 1 and words[0] in _WEEKDAYS:
        return Schedule(kind="weekly", weekday=_WEEKDAYS[words[0]],
                        hour=hour, minute=minute, raw=raw)

    # first|last of [the] month  (filler already stripped -> ["first","of","month"])
    if (len(words) == 3 and words[0] in ("first", "last")
            and words[1] == "of" and words[2] == "month"):
        return Schedule(kind="month_edge", ordinal=_ORDINALS[words[0]],
                        hour=hour, minute=minute, raw=raw)

    # <ordinal> <weekday>  (e.g. "2nd saturday", "last friday")
    if len(words) == 2 and words[0] in _ORDINALS and words[1] in _WEEKDAYS:
        return Schedule(kind="ordinal_weekday", ordinal=_ORDINALS[words[0]],
                        weekday=_WEEKDAYS[words[1]], hour=hour, minute=minute, raw=raw)

    raise ScheduleError(f"cannot parse schedule: {raw!r}")


# ---------------------------------------------------------------------------
# Next occurrence
# ---------------------------------------------------------------------------

def _at_time(day: datetime.date, sched: Schedule, offset_min: int = 0) -> datetime.datetime:
    hour = sched.hour if sched.hour is not None else AGENT_DEFAULT_RUN_HOUR
    base = datetime.datetime(day.year, day.month, day.day, hour, sched.minute)
    return base + datetime.timedelta(minutes=offset_min)


def jitter_minutes(key: str | None, sched: Schedule) -> int:
    """Stable per-agent offset for a schedule that named NO time.

    `daily` and `weekly` both resolve to AGENT_DEFAULT_RUN_HOUR:00, so every agent
    written that way fires on the same minute - measured 2026-08-03, three agents
    shared 04:00:37 exactly, and the herd grows with every agent added.

    Only untimed schedules are moved: an author who wrote "@ 4:30 pm" meant it,
    and shifting that would be a bug, not a courtesy.

    sha256-derived rather than `hash()`, whose per-process randomization would
    give an agent a different run time after every restart - the opposite of the
    predictability this is for.
    """
    if not key or sched.hour is not None or AGENT_SCHEDULE_JITTER_MINUTES <= 0:
        return 0
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % AGENT_SCHEDULE_JITTER_MINUTES


def _ordinal_weekday_of_month(year: int, month: int, weekday: int,
                              ordinal: int) -> datetime.date | None:
    days = [d for d in range(1, calendar.monthrange(year, month)[1] + 1)
            if datetime.date(year, month, d).weekday() == weekday]
    try:
        return datetime.date(year, month, days[ordinal - 1 if ordinal > 0 else -1])
    except IndexError:
        return None  # e.g. a month with no 5th friday can't happen for 1-4/-1


def _month_edge_day(year: int, month: int, ordinal: int) -> datetime.date:
    if ordinal == 1:
        return datetime.date(year, month, 1)
    return datetime.date(year, month, calendar.monthrange(year, month)[1])


def next_due(sched: Schedule, after: datetime.datetime,
             jitter_key: str | None = None) -> datetime.datetime:
    """The next occurrence strictly after `after` (naive local time).

    `jitter_key` (an agent slug) spreads schedules that named no time across
    AGENT_SCHEDULE_JITTER_MINUTES - see jitter_minutes. Omit it and behaviour is
    exactly as before, which keeps validation/preview call sites honest.

    Only the named-day kinds take the offset: `hourly`/`interval` are anchored to
    wall-clock boundaries by design, and `cron` is explicit by definition.
    """
    offset = jitter_minutes(jitter_key, sched)
    if sched.kind == "hourly":
        return (after.replace(minute=0, second=0, microsecond=0)
                + datetime.timedelta(hours=1))

    if sched.kind == "interval":
        n = sched.interval_minutes or 1
        # Anchor at midnight so boundaries are wall-clock-stable (:00/:15/... for
        # divisors of 60/1440), independent of when last_run happened to land.
        anchor = after.replace(hour=0, minute=0, second=0, microsecond=0)
        elapsed_min = (after - anchor).total_seconds() / 60
        steps = int(elapsed_min // n) + 1        # strictly-after: always advance
        return anchor + datetime.timedelta(minutes=steps * n)

    if sched.kind == "cron":
        if sched.cron is None:
            raise ScheduleError(f"cron schedule was never expanded: {sched.raw!r}")
        # Walk DAYS (cheap set lookups), descending into hours/minutes only on a
        # day that matches - a minute-by-minute walk would be millions of steps.
        start = after.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
        day = start.date()
        for _ in range(_CRON_HORIZON_DAYS):
            if _cron_day_matches(sched.cron, day):
                for h in sched.cron.hours:
                    for m in sched.cron.minutes:
                        candidate = datetime.datetime(day.year, day.month, day.day, h, m)
                        if candidate >= start:
                            return candidate
            day += datetime.timedelta(days=1)
        raise ScheduleError(
            f"cron rule has no occurrence in the next {_CRON_HORIZON_DAYS} days: "
            f"{sched.raw!r}")

    if sched.kind == "daily":
        candidate = _at_time(after.date(), sched, offset)
        if candidate <= after:
            candidate = _at_time(after.date() + datetime.timedelta(days=1), sched, offset)
        return candidate

    if sched.kind == "weekly":
        day = after.date()
        for _ in range(8):
            if day.weekday() == sched.weekday:
                candidate = _at_time(day, sched, offset)
                if candidate > after:
                    return candidate
            day += datetime.timedelta(days=1)
        raise AssertionError("unreachable")

    if sched.kind in ("ordinal_weekday", "month_edge"):
        year, month = after.year, after.month
        for _ in range(14):  # at most ~14 months to find the next hit
            if sched.kind == "ordinal_weekday":
                day = _ordinal_weekday_of_month(year, month, sched.weekday, sched.ordinal)
            else:
                day = _month_edge_day(year, month, sched.ordinal)
            if day is not None:
                candidate = _at_time(day, sched, offset)
                if candidate > after:
                    return candidate
            month += 1
            if month > 12:
                month, year = 1, year + 1
        raise AssertionError("unreachable")

    raise ScheduleError(f"unknown schedule kind {sched.kind!r}")


# ---------------------------------------------------------------------------
# The one answer to "is this agent due?"
# ---------------------------------------------------------------------------

@dataclass
class DueState:
    """Everything the three consumers need, computed once."""
    next_due: datetime.datetime | None = None
    is_due: bool = False
    overdue_by_s: float = 0.0
    error: str = ""          # unparseable schedule; next_due is None
    first_sight: bool = False  # no last-run stamp yet


def due_state(schedule_text: str, last_run: datetime.datetime | None,
              now: datetime.datetime, *, jitter_key: str | None = None) -> DueState:
    """Resolve a schedule against its last run. THE shared implementation.

    This was inlined twice - agent_scheduler._tick and the /agents table - and the
    two copies had already drifted: one handled a missing last-run stamp with a
    separate first-sight branch, the other folded it into ``last or now``; one
    caught bare Exception, the other only ScheduleError. A third copy on
    /manage/monitor would have made it worse. The comment at the /agents call site
    even warned that its ``jitter_key`` MUST match the scheduler's - a sign the
    duplication was load-bearing.

    ``last_run is None`` means FIRST SIGHT: the agent is not due (the scheduler
    stamps now and lets it land on the next natural occurrence, so a new agent
    can't stampede on deploy) but callers still get a next_due to display.
    """
    try:
        sched = parse_schedule(schedule_text)
    except Exception as e:
        return DueState(error=str(e) or "unparseable schedule")

    if last_run is None:
        return DueState(next_due=next_due(sched, now, jitter_key=jitter_key),
                        is_due=False, first_sight=True)

    nd = next_due(sched, last_run, jitter_key=jitter_key)
    overdue = (now - nd).total_seconds()
    return DueState(next_due=nd, is_due=nd <= now,
                    overdue_by_s=max(overdue, 0.0))
