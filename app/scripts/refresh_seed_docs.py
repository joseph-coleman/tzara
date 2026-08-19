# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Force-refresh the shipped seed documentation into an existing vault.

Seed content is copied into a vault exactly ONCE (``vault_registry._seed_vault_tree``),
and the ``seeded`` marker in the vault's ``.tzara/config.json`` makes every later pass a
no-op. That is deliberate -- it is what makes a user's edits and deletions stick, on
every machine the vault syncs to. The cost is that help documentation shipped with a
newer Tzara can never reach a vault that was seeded by an older one.

This script is the manual escape hatch. It is destructive on purpose, so:

  * it is DRY RUN by default -- ``--apply`` is required to write anything;
  * it refreshes only DOCUMENTATION (``help/**`` plus root-level ``*.md``). The example
    ``agents/`` and ``editors/`` are blessed, executable definitions, not docs, and are
    opt-in one file at a time via ``--include``;
  * it never deletes. Files the user removed stay removed (``--restore-missing`` opts
    in to re-adding them); vault files the seed no longer ships are REPORTED, never
    touched;
  * every write goes through ``WikiDoc.commit``, which commits the pre-image first, so
    the whole run is revertable from the vault's own git history.

Run it inside the SERVER container, which bind-mounts ``app/`` and therefore sees the
live seed (the worker bakes its copy at build time)::

    docker exec -it tzara-tzaraserver-1 python scripts/refresh_seed_docs.py
    docker exec -it tzara-tzaraserver-1 python scripts/refresh_seed_docs.py --apply

Running on the host instead is a trap: ``.env`` carries Windows paths, and the vault's
``.git`` is a gitlink holding a host-facing ``D:/...`` git-dir that WSL git can't follow.
Inside the container the destination is just ``vault_abs_root(SYSTEM_VAULT)``.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (  # noqa: E402
    AGENT_OUTPUT_DIR,
    SYSTEM_VAULT,
    USE_GIT_VERSIONING,
    seed_abs_root,
    vault_abs_root,
    vault_git_dir,
)
from src.chunker import wikilink_key  # noqa: E402
from src.wikidoc import WikiDoc  # noqa: E402

SEED_NAME = "system"
HELP_DIR = "help"

# Files refreshed as text (LF-normalized compare, EOL-preserving write). Everything
# else in scope -- today just help/jupyter-run-button.png -- is compared and written
# as raw bytes.
TEXT_EXTS = {".md", ".canvas"}

# Optional tiers. These are examples that may legitimately need to grow to showcase a
# new feature, but they are also blessed executables: an agent's blessing IS its
# location in the system vault, so overwriting one re-blesses a different definition
# (and a `schedule:` makes it run). Opt in per file.
OPTIONAL_DIRS = ("agents", "editors")

SAME, UPDATE, MISSING = "SAME", "UPDATE", "MISSING"


# --------------------------------------------------------------------------- scope


def _reject_reason(rel: str) -> str | None:
    """Why ``rel`` must never be copied, or None if it is safe.

    The dev compose override mounts ``app/seed/system`` as a live vault, so the seed
    tree on a developer's disk also contains ``.git`` (a gitlink pointing at the WRONG
    git-dir), ``.tzara/config.json`` (which would overwrite the target vault's identity
    and seed marker) and ``_dada/`` agent output. The scope globs below already exclude
    all of it; this is the second lock, so a mistyped --include can't reach them either.
    """
    for seg in rel.split("/"):
        if seg.startswith("."):
            return f"control/dot path ({seg})"
        if seg == AGENT_OUTPUT_DIR:
            return f"agent output dir ({seg})"
    return None


def _same_dir(a: str, b: str) -> bool:
    """Whether two paths are the same directory.

    ``samefile`` (device + inode), not a realpath compare: the dev compose override
    bind-mounts ``app/seed/system`` a SECOND time as the vault ``seedsys``, so the two
    container paths differ textually while resolving to one host directory. Only inode
    identity sees through that.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.realpath(a) == os.path.realpath(b)


def _walk_rel(root: str, sub: str = "") -> list[str]:
    """Vault-relative paths of every file under ``root/sub``, dot-dirs skipped."""
    base = os.path.join(root, sub) if sub else root
    out: list[str] = []
    for cur, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != AGENT_OUTPUT_DIR]
        for fn in files:
            if fn.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(cur, fn), root).replace(os.sep, "/")
            out.append(rel)
    return sorted(out)


def default_scope(seed_root: str) -> list[str]:
    """The documentation set: root-level ``*.md`` plus everything under ``help/``."""
    rels = [
        n for n in sorted(os.listdir(seed_root))
        if n.endswith(".md") and os.path.isfile(os.path.join(seed_root, n))
    ]
    if os.path.isdir(os.path.join(seed_root, HELP_DIR)):
        rels += _walk_rel(seed_root, HELP_DIR)
    return rels


def expand_includes(seed_root: str, args) -> tuple[list[str], list[str]]:
    """Resolve the opt-in tier into (rels, errors)."""
    rels: list[str] = []
    errors: list[str] = []

    for group, flag in ((d, f"--include-{d}") for d in OPTIONAL_DIRS):
        if getattr(args, f"include_{group}") and os.path.isdir(os.path.join(seed_root, group)):
            rels += _walk_rel(seed_root, group)

    for raw in args.include or []:
        try:
            rel = WikiDoc.safe_rel(raw)
        except ValueError as exc:
            errors.append(f"{raw}: {exc}")
            continue
        why = _reject_reason(rel)
        if why:
            errors.append(f"{rel}: refused -- {why}")
            continue
        src = os.path.join(seed_root, rel)
        if os.path.realpath(src) != os.path.normpath(os.path.realpath(seed_root) + "/" + rel):
            errors.append(f"{rel}: resolves outside the seed tree")
            continue
        if not os.path.isfile(src):
            errors.append(f"{rel}: no such file in the seed")
            continue
        rels.append(rel)

    return rels, errors


# ---------------------------------------------------------------------- comparison


def ondisk_name(vault: str, rel: str) -> str | None:
    """The vault's ACTUAL basename for ``rel``, or None if absent.

    Returned separately from ``rel`` because NTFS/Dropbox is case-insensitive and the
    vault repos run ``core.ignoreCase=true``: the seed's ``Main.md`` lands in an
    existing ``main.md``. Writing to the on-disk name (rather than the seed's) keeps
    the worktree and the git index agreeing on one spelling.
    """
    abs_dir = os.path.dirname(os.path.join(vault_abs_root(vault), rel))
    base = os.path.basename(rel)
    if not os.path.isdir(abs_dir):
        return None
    try:
        entries = os.listdir(abs_dir)
    except OSError:
        return None
    if base in entries:
        return base
    lowered = base.lower()
    for entry in entries:
        if entry.lower() == lowered:
            return entry
    return None


def classify(vault: str, seed_root: str, rel: str) -> dict:
    """Compare one seed file against the vault. Never reads through a case alias."""
    from src import vault_registry

    found = ondisk_name(vault, rel)
    parent = os.path.dirname(rel)
    dest_rel = f"{parent}/{found}" if (parent and found) else (found or rel)
    info = {
        "rel": rel,
        "dest_rel": dest_rel,
        "case_drift": bool(found) and found != os.path.basename(rel),
        "text": os.path.splitext(rel)[1].lower() in TEXT_EXTS,
    }

    # Load the seed payload up front, absent destination or not: --restore-missing
    # writes exactly these entries, so it must never depend on the compare branch.
    src_abs = os.path.join(seed_root, rel)
    if info["text"]:
        # Resolve seed placeholders before the compare, exactly as _seed_vault_tree
        # does on the first pass -- otherwise every placeholder-bearing doc reads as a
        # permanent UPDATE against its own correctly-substituted copy in the vault.
        info["content"] = vault_registry.render_seed_text(
            WikiDoc.read_text_at(src_abs), vault)
    else:
        with open(src_abs, "rb") as fh:
            info["data"] = fh.read()

    if found is None:
        info["status"] = MISSING
        return info

    if info["text"]:
        pair = WikiDoc.read_text(vault, dest_rel)
        same = pair is not None and pair[0] == info["content"]
    else:
        with open(os.path.join(vault_abs_root(vault), dest_rel), "rb") as fh:
            same = fh.read() == info["data"]

    info["status"] = SAME if same else UPDATE
    return info


def find_orphans(vault: str, seed_root: str) -> list[str]:
    """Vault files under ``help/`` that the seed no longer ships. Reported, never removed."""
    vault_help = os.path.join(vault_abs_root(vault), HELP_DIR)
    if not os.path.isdir(vault_help):
        return []
    seed_set = {r.lower() for r in _walk_rel(seed_root, HELP_DIR)}
    return [r for r in _walk_rel(vault_abs_root(vault), HELP_DIR) if r.lower() not in seed_set]


def find_collisions(vault: str, adding: list[str]) -> list[tuple[str, list[str]]]:
    """Basename clashes an addition would create, using the renderer's own index.

    ``vault_index`` keys candidates by ``wikilink_key(stem)`` -- the exact map
    ``[[link]]`` resolution consults -- so a key that already holds a path at a
    DIFFERENT location means adding this file makes that wikilink ambiguous (and gives
    RAG a duplicate document). This is the failure mode a plain copy creates silently.
    """
    from src import vault_index

    _, by_stem = vault_index.get_index(vault, force=True)
    out = []
    for rel in adding:
        stem = (rel[:-3] if rel.endswith(".md") else rel).split("/")[-1]
        existing = [p for p in by_stem.get(wikilink_key(stem), []) if p.lower() != rel.lower()]
        if existing:
            out.append((rel, existing))
    return out


# --------------------------------------------------------------------------- write


def write_one(vault: str, info: dict, message: str) -> None:
    """Checkpoint-then-write a single file.

    Text goes through ``WikiDoc.commit``: it commits the pre-image (so this run is
    revertable), preserves the destination's existing EOL, and sets the watcher's
    ``git:debounce`` key so the worker reindexes without racing us for the repo lock.
    Binary writes are the same shape done by hand, because ``write_bytes`` is
    deliberately git-free.
    """
    dest_rel = info["dest_rel"]
    if info["text"]:
        WikiDoc.commit(vault, dest_rel, info["content"], message=message)
        return

    abs_path = os.path.join(vault_abs_root(vault), dest_rel)
    WikiDoc.set_debounce(vault, dest_rel)
    versioning = None
    if USE_GIT_VERSIONING:
        from src import vault_registry
        from src.docversioning import MarkdownGitVersioning

        vault_registry.init_vault_repo(vault)
        versioning = MarkdownGitVersioning(vault_abs_root(vault))
        if info["status"] != MISSING:
            versioning.save_version(abs_path, message=f"checkpoint before write: {dest_rel}")
    WikiDoc.write_bytes(vault, dest_rel, info["data"])
    if versioning is not None:
        versioning.save_version(abs_path, message=message)


# -------------------------------------------------------------------------- report


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Force-refresh shipped seed documentation into a vault.",
        epilog="Dry run by default. Nothing is ever deleted.",
    )
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default: report only)")
    ap.add_argument("--vault", default=SYSTEM_VAULT,
                    help=f"target vault slug (default: {SYSTEM_VAULT})")
    ap.add_argument("--restore-missing", action="store_true",
                    help="also re-add docs that are absent from the vault "
                         "(they may have been deliberately deleted)")
    ap.add_argument("--include", action="append", metavar="REL",
                    help="additionally refresh one seed file, e.g. agents/nasa-apod.md "
                         "(repeatable)")
    for group in OPTIONAL_DIRS:
        ap.add_argument(f"--include-{group}", action="store_true",
                        help=f"additionally refresh every seed file under {group}/")
    args = ap.parse_args()

    vault = args.vault
    seed_root = seed_abs_root(SEED_NAME)
    vault_root = vault_abs_root(vault)

    if not os.path.isdir(seed_root):
        print(f"ERROR: no seed tree at {seed_root}")
        return 2
    if not os.path.isdir(vault_root):
        print(f"ERROR: no vault {vault!r} at {vault_root}")
        return 2
    if _same_dir(seed_root, vault_root):
        # The dev override mounts app/seed/system as the vault 'seedsys'.
        print(f"ERROR: vault {vault!r} IS the seed tree ({seed_root}) -- refusing to "
              f"copy it onto itself.")
        return 2

    rels = default_scope(seed_root)
    extra, errors = expand_includes(seed_root, args)
    if errors:
        for err in errors:
            print(f"ERROR: --include {err}")
        return 2
    rels = sorted(set(rels) | set(extra))

    blocked = [(r, _reject_reason(r)) for r in rels if _reject_reason(r)]
    if blocked:
        for rel, why in blocked:
            print(f"ERROR: {rel} refused -- {why}")
        return 2

    mode = "APPLY" if args.apply else "DRY RUN (no changes; re-run with --apply)"
    print(f"Tzara seed-doc refresh -- {mode}")
    print(f"  seed  : {seed_root}")
    print(f"  vault : {vault}  ->  {vault_root}")
    print(f"  scope : {len(rels)} seed file(s)")
    print()

    results = [classify(vault, seed_root, r) for r in rels]
    to_write = [i for i in results if i["status"] == UPDATE]
    absent = [i for i in results if i["status"] == MISSING]
    if args.restore_missing:
        to_write += absent
    same = [i for i in results if i["status"] == SAME]

    print("DOCUMENTS")
    for info in sorted(results, key=lambda i: (i["status"], i["rel"])):
        rel, status = info["rel"], info["status"]
        if status == SAME:
            continue
        if status == MISSING:
            note = "" if args.restore_missing else "  (skipped; --restore-missing to add)"
            print(f"  ADD      {rel}{note}")
        else:
            note = f"  -> writes existing '{info['dest_rel']}'" if info["case_drift"] else ""
            print(f"  UPDATE   {rel}{note}")
    print(f"  SAME     {len(same)} file(s) already current")
    print()

    orphans = find_orphans(vault, seed_root)
    if orphans:
        print("ORPHANS -- in the vault, not in the seed (never touched)")
        for rel in orphans:
            print(f"  {rel}")
        print()

    collisions = find_collisions(vault, [i["rel"] for i in to_write if i["status"] == MISSING])
    if collisions:
        print("BASENAME COLLISIONS -- adding these makes a [[wikilink]] ambiguous")
        for rel, existing in collisions:
            print(f"  {rel}")
            for other in existing:
                print(f"      already: {other}")
        print("  Resolve by removing or re-linking the older copy AFTER this run.")
        print()

    print(f"Summary: {len(to_write)} to write, {len(absent)} absent, "
          f"{len(same)} current, {len(orphans)} orphan(s)")

    if not args.apply:
        print("\nDry run -- nothing written.")
        return 0

    if not to_write:
        print("\nNothing to write.")
        return 0

    print("\nIf this vault syncs (Dropbox / iCloud / OneDrive), a bulk rewrite with "
          "\nanother machine online can produce '... (conflicted copy).md' files, which "
          "\nbecome real wiki pages. Let the sync settle before editing elsewhere.\n")

    written, failed = 0, 0
    for info in sorted(to_write, key=lambda i: i["rel"]):
        verb = "Add" if info["status"] == MISSING else "Refresh"
        try:
            write_one(vault, info, f"{verb} seed doc: {info['dest_rel']}")
            print(f"  {verb.lower():8s} {info['dest_rel']}")
            written += 1
        except Exception as exc:  # keep going; report honestly at the end
            print(f"  FAILED   {info['dest_rel']}: {exc}")
            failed += 1

    print(f"\nWrote {written} file(s), {failed} failure(s).")
    print("Each overwrite committed its pre-image first, so this run is revertable:")
    print(f"  git --git-dir={vault_git_dir(vault)} \\")
    print(f"      --work-tree={vault_root} log --oneline")
    print("(container-side paths; on the host the git-dir is HISTORY_LOCATION/"
          f"{vault})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
