# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Canonical timestamp formatting: one timezone, applied everywhere.

Tzara is a single-user local wiki, so the container's TZ - set once in .env and
identical across every service - IS the user's timezone. Every timestamp shown
in the UI renders in it. There is no browser detection and no per-user
preference, which is what lets a displayed time carry no zone label and still
be unambiguous.

This module is THE home for "turn an instant into a string", the same way
md_sections.py owns heading parsing and WikiDoc owns document I/O. It exists
because the alternative already happened: `"%Y-%m-%d %H:%M:%S"` was copy-pasted
across main.py, docversioning.py and background_agents.py, and the copies
disagreed about their timezone - /history rendered a commit as UTC in the
Timestamp column and as local in the message beside it.

to_local() does the real work. Naive datetimes are assumed local, which is what
datetime.now() returns; aware ones are converted - psycopg hands back a
TIMESTAMPTZ as UTC-aware, and git commit times arrive as absolute epochs.
astimezone() already distinguishes the two cases correctly, so every formatter
here funnels through it and a UTC value cannot reach a page by accident.

NOT in scope: schedule arithmetic. agent_schedule.py works in naive local wall
clock so that "daily @ 4:30 pm" stays 4:30 pm across a DST change, and must
keep doing so.
"""

from __future__ import annotations

import datetime

_DISPLAY = "%Y-%m-%d %H:%M:%S"
_DISPLAY_MIN = "%Y-%m-%d %H:%M"
_FILE = "%Y%m%d-%H%M%S"


def to_local(when=None) -> datetime.datetime:
    """Coerce epoch seconds, a datetime, or None into an aware LOCAL datetime.

    astimezone() carries the whole naive/aware distinction: on a naive value it
    assumes local and attaches the current offset, on an aware value it
    converts. That is exactly the behavior both callers need, so neither case
    is special-cased here.
    """
    if when is None:
        return datetime.datetime.now().astimezone()
    if isinstance(when, (int, float)):
        return datetime.datetime.fromtimestamp(when).astimezone()
    return when.astimezone()


def now_local() -> datetime.datetime:
    """Aware local now. Prefer this over datetime.now() for anything stored."""
    return datetime.datetime.now().astimezone()


def stamp(when=None) -> str:
    """Display form with seconds: "2026-08-06 20:32:03"."""
    return to_local(when).strftime(_DISPLAY)


def stamp_min(when=None) -> str:
    """Display form to the minute: "2026-08-06 20:32"."""
    return to_local(when).strftime(_DISPLAY_MIN)


def iso_local(when=None) -> str:
    """Offset-carrying ISO 8601: "2026-08-06T20:32:03-05:00".

    For values that PERSIST (Redis stamps, `generated:` frontmatter). The offset
    is the point: a zone-less string silently changes meaning if TZ is edited,
    and this round-trips through datetime.fromisoformat().
    """
    return to_local(when).replace(microsecond=0).isoformat()


def file_stamp(when=None, *, ms: bool = False) -> str:
    """Filename-safe form: "20260806-203203", or "20260806-203203-417" with ms.

    Millisecond precision is for log names written in bursts, where two entries
    can land in the same second and would otherwise collide.
    """
    local = to_local(when)
    if ms:
        return local.strftime(_FILE + "-%f")[:-3]
    return local.strftime(_FILE)


def ago(seconds) -> str:
    """Compact age. Seconds arrive from SQL (EXTRACT EPOCH) or Redis, never from
    subtracting a tz-aware TIMESTAMPTZ from a naive datetime."""
    if seconds is None:
        return "?"
    s = float(seconds)
    if s < 90:
        return f"{s:.0f}s ago"
    if s < 5400:
        return f"{s/60:.0f}m ago"
    if s < 172800:
        return f"{s/3600:.1f}h ago"
    return f"{s/86400:.0f}d ago"
