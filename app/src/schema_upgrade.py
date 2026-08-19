# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Bring a database created by an EARLIER release in line with the current schema.

Postgres runs /docker-entrypoint-initdb.d exactly once, on an empty data
directory. A fresh install therefore gets the current schema and an upgraded
install keeps whatever its volume was created with, forever - so without this,
the only way to pick up a schema change is to delete the volume and reindex.

Two tiers, because they cover different things:

  1. BASELINE - init.sql is re-executed on every startup. Every statement in it
     is `CREATE ... IF NOT EXISTS`, so this is a no-op on a current database and
     creates anything added since the volume was made. Covers new TABLES, new
     INDEXES and new EXTENSIONS for free.

  2. STEPS - what `CREATE TABLE IF NOT EXISTS` structurally cannot do. Adding a
     column to an existing table is the common case: the CREATE is skipped
     wholesale, so the column never appears no matter how often init.sql runs.
     Column adds, type changes, backfills and drops go here.

This is a RECONCILIATION, not a migration sequence: every step is idempotent and
re-checks the live catalog, so there is no version table, no ordering to get
wrong, and running the whole thing on every boot is free. Failures are logged
and swallowed - a wiki that cannot upgrade its schema should still serve pages,
and the next startup tries again.

ADDING A SCHEMA CHANGE
  New table or index -> edit init.sql. Nothing else to do.
  New column         -> edit init.sql (so fresh installs get it) AND append a
                        step below (so existing installs do). Both, always.
  Type change/backfill -> append a step; make it detect its own completion.

Steps must be safe to run on a database that is already correct. Prefer
Postgres's idempotent DDL (`ADD COLUMN IF NOT EXISTS`, `DROP ... IF EXISTS`);
where none exists, inspect information_schema first - see the timestamptz step,
where re-running the ALTER would silently shift data that was already right.
"""

import logging
from pathlib import Path

from config import get_pg_connection

logger = logging.getLogger("schema_upgrade")

# Arbitrary but fixed: serializes concurrent upgraders (compose starts the worker
# before the server, so both can arrive here at once).
_ADVISORY_LOCK_KEY = 0x747a617261  # "tzara schema"

# /app/init.sql - this file is /app/app/src/schema_upgrade.py
_INIT_SQL = Path(__file__).resolve().parent.parent.parent / "init.sql"


# ---------------------------------------------------------------------------
# Tier 1: the idempotent baseline
# ---------------------------------------------------------------------------

def _apply_baseline(cur) -> None:
    """Re-run init.sql. Every statement is IF NOT EXISTS, so this only creates
    what is missing and is silent on a database that is already current."""
    if not _INIT_SQL.is_file():
        logger.warning("schema baseline: %s not found; skipping", _INIT_SQL)
        return
    cur.execute(_INIT_SQL.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tier 2: steps init.sql cannot express
# ---------------------------------------------------------------------------

def _step_naive_timestamps_to_timestamptz(cur) -> list[str]:
    """Convert naive TIMESTAMP columns to TIMESTAMPTZ, reading them as UTC.

    The UTC premise is what makes this safe: before Tzara gave pgserver a TZ,
    every naive NOW() written to these columns was UTC.

    It must run exactly once per column. `timestamptz AT TIME ZONE 'UTC'`
    converts the OTHER direction, so re-running it on an already-correct column
    shifts the data - hence the catalog read rather than a blind ALTER, and the
    advisory lock in reconcile_schema() so a concurrent upgrader cannot make the
    read stale.
    """
    cur.execute("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND data_type = 'timestamp without time zone'
        ORDER BY table_name, column_name
    """)
    targets = [(r[0], r[1]) for r in cur.fetchall()]
    for table, column in targets:
        cur.execute(
            f'ALTER TABLE {table} ALTER COLUMN {column} '
            f"TYPE TIMESTAMPTZ USING {column} AT TIME ZONE 'UTC'"
        )
    return [f"{t}.{c} -> timestamptz" for t, c in targets]


# Append-only. Each entry is (name, fn) where fn(cur) -> list of descriptions of
# what it changed, empty when there was nothing to do. Order is irrelevant by
# construction (every step is self-checking), but keep it chronological so the
# list reads as a history.
STEPS = [
    ("naive timestamps -> timestamptz", _step_naive_timestamps_to_timestamptz),
]


# ---------------------------------------------------------------------------

def reconcile_schema() -> list[str]:
    """Bring an older database in line with init.sql. Returns what changed."""
    changed: list[str] = []
    try:
        conn = get_pg_connection()
    except Exception:
        logger.exception("schema reconcile: could not connect; skipping")
        return changed
    try:
        with conn:
            cur = conn.cursor()
            # Lock FIRST, then read: in READ COMMITTED each statement takes a
            # fresh snapshot, so whatever a concurrent upgrader committed while
            # we waited is visible and we skip the work it already did.
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (_ADVISORY_LOCK_KEY,))

            # Savepointed like the steps: a baseline that cannot run (missing
            # init.sql, no CREATE EXTENSION privilege) must not take the steps
            # down with it - those are the ones that fix an existing install.
            cur.execute("SAVEPOINT tzara_baseline")
            try:
                _apply_baseline(cur)
            except Exception:
                cur.execute("ROLLBACK TO SAVEPOINT tzara_baseline")
                logger.exception("schema baseline failed; continuing to steps")
            else:
                cur.execute("RELEASE SAVEPOINT tzara_baseline")

            for i, (name, step) in enumerate(STEPS):
                # One savepoint per step: without it a single failing statement
                # aborts the whole transaction and every LATER step dies with
                # "current transaction is aborted" rather than being tried.
                sp = f"tzara_step_{i}"
                cur.execute(f"SAVEPOINT {sp}")
                try:
                    done = step(cur)
                except Exception:
                    cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                    logger.exception("schema step %r failed; continuing", name)
                    continue
                cur.execute(f"RELEASE SAVEPOINT {sp}")
                if done:
                    changed.extend(done)
                    logger.warning("schema step %r applied: %s", name, ", ".join(done))
    except Exception:
        logger.exception("schema reconcile failed; continuing with existing schema")
    finally:
        conn.close()
    return changed
