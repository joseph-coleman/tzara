# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Vault registry: the single source of truth for which vaults exist.

A *vault* is an isolated Obsidian-style document tree. On disk it is an immediate
subdirectory of ``VAULTS_DIR`` (the documents parent mount); its git history lives in
a *separate* parent, ``HISTORY_DIR``, so git's churning temp files never race a
Dropbox syncer that may be watching the documents tree.

Existence is determined by the **filesystem** (a vault is a real subdirectory), the
same philosophy as ``vault_index`` -- this keeps a vault that was created directly on
disk (or via an external Docker mount) working without a registration step. Metadata
(display name, settings) is likewise filesystem-authoritative, living in each vault's
``.tzara/config.json``; there is no DB registry table (the old ``vaults`` cache was
dropped once every vault was self-describing).

Repo layout per vault (see ``init_vault_repo``): the work tree carries a tiny, static
``.git`` gitlink file whose ``gitdir:`` points at the **host** git-dir
(``HOST_HISTORY_LOCATION/{slug}``). That file lives on the shared worktree so both host
and container read it, but only the host obeys it -- the container pins ``--git-dir``
explicitly (see ``docversioning``) and ignores the file's contents. All git churn lives
off-Dropbox under ``vault-history/{slug}``.
"""

import json
import os
import re
import shutil
import subprocess
import time

from config import (
    DEFAULT_VAULT,
    DEFAULT_WIKI_PAGE,
    HOST_HISTORY_LOCATION,
    SYSTEM_VAULT,
    TEMPLATE,
    VAULTS_DIR,
    is_template,
    seed_abs_root,
    vault_abs_root,
    vault_git_dir,
)

# Slug rules: lowercase alphanumerics plus - and _, must start alphanumeric. This is
# deliberately permissive on *meaning* (a vault may be named "edit" or "api" -- the
# router reserves only the first path segment, and the vault is the second), but
# strict on *characters* so a slug can never contain a path separator or traversal.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Per-vault properties live in a `.tzara/` directory in the vault root (mirroring
# Obsidian's `.obsidian/`), which makes a vault SELF-DESCRIBING on the filesystem --
# the SOLE source of truth for its metadata, the same way the filesystem is already
# the source of truth for its EXISTENCE (see module docstring). There is no DB mirror:
# metadata survives a DB reset, travels with the vault across machines via Dropbox/git,
# and a vault created directly on disk is fully described without a registration step.
# (A `vaults` cache table existed during the migration to filesystem-authoritative
# metadata; it was dropped once every vault had a `.tzara/config.json`.) `.tzara` is in
# the watcher's IGNORED_DIRS so it is never ingested/served as content, and it is a
# RESERVED_CONTROL_DIR in write_gate so an agent can never write vault config.
TZARA_DIR = ".tzara"
TZARA_CONFIG = "config.json"
TZARA_CONFIG_VERSION = 1


# ---------------------------------------------------------------------------
# Slug validation
# ---------------------------------------------------------------------------

def validate_slug(slug: str) -> str:
    """Return the slug if it is a legal vault id, else raise ValueError."""
    if not slug or not _SLUG_RE.match(slug):
        raise ValueError(f"Invalid vault slug: {slug!r}")
    return slug


# ---------------------------------------------------------------------------
# Existence / listing (filesystem is the source of truth)
# ---------------------------------------------------------------------------

def _vaults_parent() -> str:
    return os.path.join(os.getcwd(), VAULTS_DIR)


def vault_exists(slug: str) -> bool:
    """True if ``slug`` is a legal id naming a real subdirectory under VAULTS_DIR."""
    try:
        validate_slug(slug)
    except ValueError:
        return False
    return os.path.isdir(vault_abs_root(slug))


def _scan_vault_slugs() -> list[str]:
    """Immediate subdirectories of VAULTS_DIR (skipping dot-dirs), sorted."""
    parent = _vaults_parent()
    if not os.path.isdir(parent):
        return []
    slugs = [
        name
        for name in os.listdir(parent)
        if not name.startswith(".")
        and os.path.isdir(os.path.join(parent, name))
        and _SLUG_RE.match(name)
    ]
    return sorted(slugs)


# ---------------------------------------------------------------------------
# `.tzara/config.json` -- authoritative per-vault metadata (filesystem is truth)
# ---------------------------------------------------------------------------

def _tzara_config_path(slug: str) -> str:
    return os.path.join(vault_abs_root(slug), TZARA_DIR, TZARA_CONFIG)


# Two-tier read cache. The resolvers below (vault_default_page, vault_theme) run
# several times per page render, and the vaults mount is routinely a Windows/9p bind
# where a bare stat() measures ~1.3ms -- so even the stat has to be rationed:
#
#   tier 1 (TTL)   within _TTL_SECONDS of the last check, reuse the parsed config
#                  outright -- no syscall at all.
#   tier 2 (mtime) after that, one stat(); reparse only if st_mtime_ns moved.
#
# The file remains the source of truth. A write through this module invalidates the
# entry outright (see write_vault_config), so app-driven changes are visible at once;
# a change made BEHIND the module's back -- by hand on the host, or by the worker
# process -- is picked up within _TTL_SECONDS, no restart required. Same shape and
# rationale as vault_index's cache, which rations a much costlier tree walk.
#
# That window covers DELETION too: tier 1 skips the stat() by design, so a vault
# whose config is removed out-of-band keeps resolving its old values for up to
# _TTL_SECONDS. Deliberate -- checking would cost exactly the syscall tier 1 exists
# to avoid -- and harmless, because the affected callers are presentational
# (default page, theme, palette) and routing uses vault_exists(), which stats.
_TTL_SECONDS = 1.0

# slug -> (checked_at, st_mtime_ns, parsed config)
_config_cache: dict[str, tuple[float, int, dict]] = {}


def read_vault_config(slug: str) -> dict:
    """Parsed ``.tzara/config.json`` for ``slug``, or ``{}`` if it is absent or
    unreadable (a vault created directly on disk simply has no config yet).

    Every return hands out a COPY: the cached dict outlives the call, and callers
    (list_vaults exposes it wholesale as ``settings``) must not be able to reach in
    and mutate what the next reader will see.
    """
    now = time.monotonic()
    hit = _config_cache.get(slug)
    if hit is not None and (now - hit[0]) < _TTL_SECONDS:
        return dict(hit[2])

    path = _tzara_config_path(slug)
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError:
        _config_cache.pop(slug, None)
        return {}
    if hit is not None and hit[1] == mtime:
        _config_cache[slug] = (now, mtime, hit[2])  # unchanged: just re-arm the TTL
        return dict(hit[2])
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    data = data if isinstance(data, dict) else {}
    _config_cache[slug] = (now, mtime, data)
    return dict(data)


def write_vault_config(slug: str, config: dict) -> None:
    """Authoritatively write ``.tzara/config.json`` (atomic temp+rename), version it in
    git, and refresh the system-vault cache. A ``version`` stamp is always injected. Not
    the change-detecting entry point -- callers that want write-only-on-change use
    :func:`update_vault_config`."""
    validate_slug(slug)
    config = {"version": TZARA_CONFIG_VERSION, **config}
    path = _tzara_config_path(slug)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(config, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)  # atomic on POSIX; a reader never sees a half-written file
    # Invalidate explicitly rather than trusting the mtime to have moved: the vaults
    # mount may be a filesystem with coarse timestamp granularity, where two writes
    # inside one tick are indistinguishable.
    _config_cache.pop(slug, None)
    _refresh_system_cache()
    _commit_tzara(slug, path)


def update_vault_config(slug: str, **changes) -> dict:
    """Read-modify-write ``.tzara/config.json``, writing ONLY on an actual change (so a
    no-op call never churns git). Returns the merged config. This is the single write
    chokepoint through which all metadata mutations flow."""
    current = read_vault_config(slug)
    merged = {"version": TZARA_CONFIG_VERSION, **current, **changes}
    if merged != current:
        write_vault_config(slug, merged)
    return merged


def set_vault_settings(slug: str, changes: dict) -> dict:
    """Like :func:`update_vault_config`, but a value of ``None`` REMOVES its key.

    That distinction is what lets a settings form mean "fall back to the site
    default": clearing a field has to delete the key, not store an empty string,
    or the vault would be pinned to `""` forever. Keys the caller does not mention
    are preserved untouched -- notably ``system``, ``seeded`` and ``version``, which
    the UI must never be able to clobber.

    Change-detecting like the chokepoint it wraps, so a save that alters nothing
    writes nothing and adds no commit.
    """
    validate_slug(slug)
    merged = {"version": TZARA_CONFIG_VERSION, **read_vault_config(slug)}
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    if merged != read_vault_config(slug):
        write_vault_config(slug, merged)
    return merged


# ---------------------------------------------------------------------------
# Per-vault resolvers (config key -> effective value, with a site-wide fallback)
# ---------------------------------------------------------------------------
#
# Both run on EVERY page render, so both go through the mtime-cached
# read_vault_config above. Both take values from a HAND-EDITED file and feed them
# into filesystem paths and HTML attributes, so both validate here, at the read
# boundary -- a caller can use the result without re-checking it. An invalid value
# degrades to the site-wide default rather than raising: a typo in config.json
# should not take a vault offline.

def normalize_default_page(name) -> str | None:
    """``name`` as a usable start page, or None if it cannot be one.

    Shared by the resolver below and by the settings form, so "what the renderer
    accepts" and "what the form accepts" can never drift apart. They differ only in
    what they do with a rejection: the resolver falls back silently (a typo in
    config.json must not take a vault offline) while the form reports it.
    """
    if not isinstance(name, str):
        return None
    # A leading "/" is Obsidian's vault-root anchor, which is what a vault-relative
    # path already means here -- so strip it rather than reject the value.
    name = name.strip().strip("/")
    # Must stay a vault-relative path: no traversal, no absolute/UNC form. A
    # subfolder page ("notes/Index") is legal, which is why "/" is not rejected,
    # and spaces are legal because Obsidian page names routinely have them (callers
    # percent-encode when building a URL).
    if not name or "\\" in name or ".." in name.split("/"):
        return None
    # Characters that would change the MEANING of the URL this name is spliced into
    # (query/fragment delimiters) or break out of an HTML attribute, plus controls.
    if any(c in name for c in '?#"\'<>') or any(ord(c) < 32 for c in name):
        return None
    return name


def vault_default_page(slug: str) -> str:
    """The vault's start page name (no extension), falling back to the site-wide
    DEFAULT_WIKI_PAGE when the vault sets no usable ``default_page``."""
    return normalize_default_page(read_vault_config(slug).get("default_page")) \
        or DEFAULT_WIKI_PAGE


def vault_theme(slug: str) -> str:
    """The vault's theme folder name, falling back to the site-wide TEMPLATE
    (TZARA_TEMPLATE) when the vault sets no ``template`` or names one that does not
    exist on disk."""
    name = read_vault_config(slug).get("template")
    return name if isinstance(name, str) and is_template(name) else TEMPLATE


# ---------------------------------------------------------------------------
# Per-vault palette
# ---------------------------------------------------------------------------
#
# A vault may override the SAME four seed tokens a theme file overrides (see
# app/template/ocean/theme.css) without shipping a theme folder -- so a vault can
# carry its own accent while still riding a shared theme. The values are emitted
# INLINE into the page's <head>: a stylesheet is fetched as its own request with no
# vault context, so a per-vault palette cannot live in a .css file at all.
#
# Config -> CSS token. The mapping is a FIXED dict on purpose: the CSS variable
# names are ours, never the caller's, so a config key can't name an arbitrary
# property. An unknown key is simply ignored.
VAULT_COLOR_TOKENS = {
    "base": "--base-color",              # accent; every shade derives from this
    "background": "--background",        # light-mode surface (dark mode reads as INK)
    "foreground": "--foreground",        # light-mode ink (dark mode reads as SURFACE)
    "link": "--base-link-color",         # link hue
}

# Color-syntax allowlist. This is the ONLY thing standing between a hand-edited
# config file and CSS/HTML injection: the Jinja environment renders with
# autoescape OFF, and HTML-escaping is not even available as a fallback here --
# CSS does not decode entities, so an escaped color is simply a broken color.
#
# Two distinct attacks are closed by the same character restriction:
#   * `</style><script>...`  -- the one real HTML-injection vector inside a
#     <style> element is the literal `</style`; barring `<` kills it.
#   * `red; } body { display:none } :root{ --x:`  -- injects arbitrary CSS RULES
#     without ever leaving the style element; barring `;{}` kills it.
# Nothing outside these three shapes is accepted, so `url(...)`, `@import`,
# `expression(...)` and newlines are all rejected by construction.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
_FUNC_COLOR_RE = re.compile(
    r"^(?:rgb|rgba|hsl|hsla|hwb|oklch|oklab|lab|lch|color)\([0-9a-zA-Z.%,/ +-]*\)$"
)
_NAMED_COLOR_RE = re.compile(r"^[a-zA-Z]{3,20}$")


def valid_css_color(value) -> bool:
    """True if ``value`` is a CSS color safe to interpolate into a ``<style>`` block.

    Deliberately an allowlist of three shapes (hex / color function / named color)
    rather than a denylist of dangerous characters -- a denylist here would have to
    anticipate every future CSS escape syntax, and getting it wrong means script
    injection.
    """
    if not isinstance(value, str):
        return False
    v = value.strip()
    if not v or len(v) > 64:
        return False
    return bool(
        _HEX_COLOR_RE.match(v) or _FUNC_COLOR_RE.match(v) or _NAMED_COLOR_RE.match(v)
    )


def vault_colors(slug: str) -> dict:
    """``{css_token: color}`` for the vault's palette overrides; ``{}`` when it sets
    none. Each entry is validated INDEPENDENTLY, so one bad value degrades to the
    theme's color for that token instead of blanking the whole palette."""
    colors = read_vault_config(slug).get("colors")
    if not isinstance(colors, dict):
        return {}
    return {
        token: colors[key].strip()
        for key, token in VAULT_COLOR_TOKENS.items()
        if valid_css_color(colors.get(key))
    }


def vault_css_declarations(slug: str) -> str:
    """The vault's palette as a CSS declaration string (``"--base-color: #fff;"``),
    ready to drop inside a rule. Empty string when the vault sets no palette, so the
    caller can skip emitting the ``<style>`` element entirely."""
    return " ".join(f"{token}: {value};" for token, value in vault_colors(slug).items())


def _commit_tzara(slug: str, path: str) -> None:
    """Version the config file in the vault repo. Non-fatal: an uncommitted (but
    on-disk) config still serves as the source of truth."""
    try:
        from src.docversioning import MarkdownGitVersioning
        MarkdownGitVersioning(vault_abs_root(slug)).save_version(
            path, message="Update .tzara/config.json")
    except Exception:
        pass


def _load_metadata() -> dict[str, dict]:
    """vault_id -> {display_name, settings}. The single source of truth is each vault's
    ``.tzara/config.json``; a vault with no config yet contributes nothing here and
    falls back to a slug-derived label at the :func:`list_vaults` boundary."""
    meta: dict[str, dict] = {}
    for slug in _scan_vault_slugs():
        cfg = read_vault_config(slug)
        if cfg:
            meta[slug] = {"display_name": cfg.get("display_name"), "settings": cfg}
    return meta


def reconcile_vault_configs() -> None:
    """Materialize ``.tzara/config.json`` for any vault that lacks one, so every vault
    on disk becomes self-describing (a fresh label from the slug, plus the system flag
    for the configured SYSTEM_VAULT). Idempotent -- a vault that already has a config is
    skipped -- so it is safe to call on every startup."""
    for slug in _scan_vault_slugs():
        if read_vault_config(slug):
            continue  # already self-describing
        cfg: dict = {"display_name": slug}
        if slug == SYSTEM_VAULT:
            cfg["system"] = True
        write_vault_config(slug, cfg)


def list_vaults(include_system: bool = False) -> list[dict]:
    """All vaults from the filesystem, merged with registry metadata.

    Each entry: {"vault_id", "display_name", "settings"}. ``display_name`` falls back
    to the slug when the registry has no row, so a fs-only vault still shows a
    sensible label. System vaults (settings ``{"system": true}`` or the configured
    SYSTEM_VAULT slug) are EXCLUDED by default -- they are wiki-owned (agent
    definitions, help docs), not user content, so the vault switcher, /vaults landing
    page, and bulk maintenance loops never see them. Pass ``include_system=True`` for
    the rare caller that wants everything (e.g. per-repo git maintenance).
    """
    meta = _load_metadata()
    out = []
    for slug in _scan_vault_slugs():
        m = meta.get(slug) or {}
        settings = m.get("settings") or {}
        if not include_system and (settings.get("system") or slug == SYSTEM_VAULT):
            continue
        out.append({
            "vault_id": slug,
            "display_name": m.get("display_name") or slug,
            "settings": settings,
        })
    return out


# ---------------------------------------------------------------------------
# System-vault membership (hot-path safe)
# ---------------------------------------------------------------------------

# The file watcher checks every filesystem event against this, so the DB-flagged
# slugs are cached once and refreshed only when registry metadata changes.
_system_slugs_cache: set[str] | None = None


def _refresh_system_cache() -> set[str]:
    global _system_slugs_cache
    slugs = set()
    for vault_id, m in _load_metadata().items():
        if (m.get("settings") or {}).get("system"):
            slugs.add(vault_id)
    _system_slugs_cache = slugs
    return slugs


def is_system_vault(slug: str) -> bool:
    """True if ``slug`` names a system vault (wiki-owned, hidden, RAG-excluded).

    The configured SYSTEM_VAULT slug counts even before its registry row exists, so
    the exclusion holds during first startup / pre-migration DB states.
    """
    if slug == SYSTEM_VAULT:
        return True
    cache = _system_slugs_cache if _system_slugs_cache is not None else _refresh_system_cache()
    return slug in cache


# ---------------------------------------------------------------------------
# Registry metadata writes
# ---------------------------------------------------------------------------

def register_vault(
    slug: str, display_name: str | None = None, settings: dict | None = None
) -> None:
    """Upsert vault metadata into the authoritative ``.tzara/config.json``. Idempotent
    and change-detecting: a no-op call writes nothing. Materializes ``.tzara`` if
    missing. Does NOT create the vault directory.

    ``settings`` keys are MERGED into the config (not replaced wholesale), so passing
    ``{"system": True}`` leaves an existing ``seeded`` list intact.
    """
    validate_slug(slug)
    # Always carry a display_name (defaulting to the slug) so a first registration
    # fully materializes the config -- matches the old COALESCE-to-slug behavior.
    changes: dict = {"display_name": display_name or slug}
    if settings:
        changes.update(settings)
    update_vault_config(slug, **changes)


# ---------------------------------------------------------------------------
# Git repo provisioning (separate-git-dir with a RELATIVE gitlink)
# ---------------------------------------------------------------------------

def _gitlink_resolves(gitlink: str, work: str) -> bool:
    """True if a ``.git`` gitlink file names a git-dir that exists in THIS filesystem
    namespace. Mirrors git's own resolution: a non-absolute target is taken relative to
    the work tree, so a Windows ``D:/...`` path read on POSIX resolves under the work
    tree and fails -- which is the answer we want.
    """
    try:
        with open(gitlink, encoding="utf-8") as f:
            line = f.read().strip()
    except OSError:
        return False
    prefix = "gitdir:"
    if not line.startswith(prefix):
        return False
    target = line[len(prefix):].strip()
    if not target:
        return False
    if not os.path.isabs(target):
        target = os.path.join(work, target)
    return os.path.isdir(target)


def _require_writable(path: str, env_var: str) -> None:
    """Raise with a mount-specific message unless ``path`` accepts a file write.

    ``os.access`` lies on several of the filesystems these mounts land on (overlay,
    virtiofs, network shares), so actually create and remove a probe file.
    """
    probe = os.path.join(path, ".tzara-write-probe")
    try:
        os.makedirs(path, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("")
        os.remove(probe)
    except OSError as exc:
        raise RuntimeError(
            f"{path} is not writable from inside the container ({exc.strerror}).\n"
            f"  This directory comes from {env_var} in .env, bind-mounted by "
            f"docker-compose.\n"
            f"  Check that the host path exists, is shared with Docker, and grants "
            f"write access\n"
            f"  (on macOS, ~/Documents, ~/Desktop and ~/Downloads additionally require "
            f"an explicit\n"
            f"  privacy grant for Docker under Privacy & Security > Files and Folders)."
        ) from exc


def init_vault_repo(slug: str) -> None:
    """Ensure ``slug`` has a separated git repo: work tree under VAULTS_DIR, git-dir
    under HISTORY_DIR, joined by a RELATIVE gitlink so host and container git both
    resolve it. Idempotent; safe to call on every factory access.
    """
    validate_slug(slug)
    work = vault_abs_root(slug)
    gitdir = vault_git_dir(slug)
    gitlink = os.path.join(work, ".git")

    os.makedirs(work, exist_ok=True)
    os.makedirs(os.path.dirname(gitdir), exist_ok=True)

    # Already provisioned: real git-dir + a gitlink *file* (not a churn-prone dir).
    if os.path.isdir(gitdir) and os.path.isfile(gitlink):
        return

    # If a normal .git directory somehow exists in the work tree (e.g. a stray plain
    # `git init`), refuse silently rather than clobber history -- the caller/migration
    # should reconcile it. Only provision when the work tree has no .git yet.
    if os.path.isdir(gitlink):
        return

    # A gitlink FILE without a local git-dir means the work tree carries another
    # machine's gitlink: the file lives on the synced vault, so a vault shared between
    # hosts (Dropbox, Syncthing) arrives holding a path only its author can resolve --
    # e.g. `gitdir: D:/DATA/...` on a mac. git init would try to reinitialize *through*
    # that dangling pointer and abort, so drop it and provision this host's own history.
    # History deliberately lives outside the synced tree, so it is never shared between
    # machines; each host keeps its own, and the gitlink is rewritten below.
    if os.path.isfile(gitlink) and not _gitlink_resolves(gitlink, work):
        os.remove(gitlink)

    # Probe both mounts before invoking git. A bind mount that is present but not
    # writable (host-side ACLs, macOS privacy gates on ~/Documents, a read-only mount)
    # makes `git init` fail with a bare exit 128, and the two directories come from two
    # independent .env settings -- so name which one is at fault.
    _require_writable(os.path.dirname(gitdir), "HISTORY_LOCATION")
    _require_writable(work, "VAULTS_LOCATION")

    proc = subprocess.run(
        ["git", "init", "-b", "main", f"--separate-git-dir={gitdir}", work],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Could not provision the git repo for vault {slug!r} "
            f"(git init exited {proc.returncode}).\n"
            f"  work tree: {work}   (VAULTS_LOCATION mount)\n"
            f"  git dir:   {gitdir} (HISTORY_LOCATION mount)\n"
            f"  git said:  {(proc.stderr or proc.stdout or '').strip() or '(no output)'}"
        )

    # git writes an ABSOLUTE (container) gitdir path into the gitlink; overwrite it with
    # a HOST-facing path. This file lives on the shared worktree (Dropbox), so both host
    # and container read it -- but the container ignores its contents (docversioning pins
    # --git-dir explicitly), while the host has no such override, so the bytes serve the
    # host. When HOST_HISTORY_LOCATION is set we point at the absolute host git-dir;
    # otherwise fall back to the old relative form (valid when the host keeps vaults/ and
    # vault-history/ as siblings). Written with LF: git trims the gitlink line, and this
    # is a git control file, not source -- CRLF here is needless churn.
    if HOST_HISTORY_LOCATION:
        target = f"{HOST_HISTORY_LOCATION.rstrip('/' + chr(92))}/{slug}"  # strip trailing / or \
    else:
        target = os.path.relpath(gitdir, work).replace(os.sep, "/")
    with open(gitlink, "w", encoding="utf-8", newline="\n") as f:
        f.write(f"gitdir: {target}\n")

    # Drop any absolute core.worktree so git infers the work tree from the gitlink's
    # location instead of a pinned container path.
    subprocess.run(
        ["git", "--git-dir", gitdir, "config", "--unset", "core.worktree"],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )

    # Seed a tracked .gitattributes so markdown line endings are normalized from the
    # vault's very first commit: LF in the repo, CRLF in the working tree. Without it,
    # files authored in Windows/Obsidian (CRLF) and rewritten by the Linux container
    # (LF) churn the whole file on every metadata pass. Commit it (rather than leaving
    # it untracked) so it stays out of `git status` and the policy is versioned with
    # the vault. Reuse docversioning's commit path (identity + pinned --work-tree).
    # Only fresh provisioning reaches here -- existing vaults early-return above.
    gitattrs = os.path.join(work, ".gitattributes")
    if not os.path.exists(gitattrs):
        with open(gitattrs, "w", encoding="utf-8", newline="\n") as f:
            f.write(
                "# Store LF in the repo, check out CRLF in the working tree for markdown/canvas.\n"
                "# EOL-only differences stop showing as diffs (Obsidian CRLF vs container LF).\n"
                "*.md text eol=crlf\n"
                "*.canvas text eol=crlf\n"
            )
        try:
            from src.docversioning import MarkdownGitVersioning
            MarkdownGitVersioning(work).save_version(
                gitattrs, message="Seed .gitattributes: normalize markdown line endings"
            )
        except Exception:
            # Non-fatal: an untracked .gitattributes still enforces the rule; a later
            # commit can adopt it. Never block vault provisioning on a commit hiccup.
            pass


# ---------------------------------------------------------------------------
# Vault creation / bootstrap
# ---------------------------------------------------------------------------

def create_vault(slug: str, display_name: str | None = None) -> dict:
    """Create a new vault: make the work-tree dir, provision its separated git repo,
    and register metadata. Returns the listing entry. Raises ValueError on a bad slug
    or if the work tree already exists (use a different slug).
    """
    validate_slug(slug)
    if is_system_vault(slug):
        raise ValueError(
            f"Vault slug {slug!r} is reserved for the system vault (agent "
            f"definitions / help docs) and cannot be created here"
        )
    work = vault_abs_root(slug)
    if os.path.isdir(work):
        raise ValueError(f"Vault {slug!r} already exists")
    os.makedirs(work, exist_ok=False)
    init_vault_repo(slug)
    register_vault(slug, display_name)
    return {"vault_id": slug, "display_name": display_name or slug}


# Seed files copied as TEXT (placeholder-substituted); everything else is byte-copied.
SEED_TEXT_EXTS = {".md", ".canvas"}


def render_seed_text(text: str, slug: str) -> str:
    """Substitute the seed placeholders in one text file's content.

    Seed docs link to app ROUTES, which are vault-scoped ("/wiki/{vault}/help",
    "/index/{vault}"). Those cannot be written as relative markdown links the way
    document links can, and a literal slug bakes in whichever vault names the author
    happened to run -- silently breaking every such link for anyone who sets
    SYSTEM_VAULT or DEFAULT_VAULT to something else. So the seed source names the
    placeholder and the copy resolves it:

        {{vault}}         the vault being seeded (its own routes)
        {{system_vault}}  the system vault (cross-vault links to the help docs)

    Exact-token replacement, so unrelated brace text in the docs (a "{{cite web}}"
    example) is left alone.
    """
    return (text
            .replace("{{vault}}", slug)
            .replace("{{system_vault}}", SYSTEM_VAULT))


def _seed_vault_tree(slug: str, seed_name: str) -> None:
    """Copy the baked seed tree ``app/seed/{seed_name}/`` into vault ``slug`` exactly
    ONCE, then never again. After the first pass the vault is fully the user's: edits,
    moves, and -- crucially -- DELETIONS all stick, because the ``seeded`` list in the
    vault's ``.tzara/config.json`` makes every later pass a no-op (a deleted example
    agent does NOT resurrect on the next restart).

    That marker lives in ``.tzara`` (not off-Dropbox) ON PURPOSE: it travels WITH the
    vault via Dropbox/git, so a Dropbox vault opened on a second machine sees it and
    won't re-seed there either -- deletions stick across every machine the vault reaches,
    not just the one it was seeded on. It is also filesystem- not DB-keyed, so a
    registry reset can't un-seed a curated vault.

    A runtime copy (not a baked-into-vaults file) because the VAULTS_DIR mount shadows
    anything the image bakes beneath it -- the seed source lives OUTSIDE the mount
    (see config.seed_abs_root) and we copy THROUGH the mount into persistent storage.

    Within that one pass a pre-existing same-named file is left untouched (so a user
    page sharing a seed file's name is never clobbered), and the ``seeded`` flag is
    recorded even if nothing was copied. Non-fatal throughout: a copy or commit hiccup
    never blocks vault provisioning.
    """
    src_root = seed_abs_root(seed_name)
    if not os.path.isdir(src_root):
        return
    if seed_name in (read_vault_config(slug).get("seeded") or []):
        return  # seeded once already -- the vault is the user's now (deletions stick)

    work = vault_abs_root(slug)
    seeded: list[str] = []
    for dirpath, _dirs, files in os.walk(src_root):
        for name in files:
            src = os.path.join(dirpath, name)
            rel = os.path.relpath(src, src_root)
            dest = os.path.join(work, rel)
            if os.path.exists(dest):
                continue  # never clobber a pre-existing same-named file
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                if os.path.splitext(name)[1].lower() in SEED_TEXT_EXTS:
                    # Read/write as bytes so the seed file's own EOLs survive the copy;
                    # only the placeholder tokens change.
                    with open(src, "rb") as fh:
                        raw = fh.read()
                    rendered = render_seed_text(
                        raw.decode("utf-8"), slug).encode("utf-8")
                    with open(dest, "wb") as fh:
                        fh.write(rendered)
                else:
                    shutil.copyfile(src, dest)
                seeded.append(dest)
            except (OSError, UnicodeDecodeError):
                pass

    if seeded:
        try:
            from src.docversioning import MarkdownGitVersioning
            versioning = MarkdownGitVersioning(work)
            for dest in seeded:
                versioning.save_version(
                    dest, message=f"Seed {os.path.relpath(dest, work)}")
        except Exception:
            # Untracked seeded files still serve fine; a later edit/commit adopts them.
            pass

    # Record the seed LAST (read-modify-write, preserving any other config keys) so a
    # crash mid-pass just re-runs the idempotent, never-clobbering copy next time rather
    # than leaving the tree half-seeded forever.
    done = sorted(set((read_vault_config(slug).get("seeded") or []) + [seed_name]))
    update_vault_config(slug, seeded=done)


def ensure_default_vault() -> None:
    """Guarantee the default vault exists on disk with a repo and registry row.

    Called at startup so a fresh install (or one mid-migration) always has a routable
    vault. If ``VAULTS_DIR/{DEFAULT_VAULT}`` is missing it is created empty, then seeded
    ONCE from ``app/seed/default/``.
    """
    work = vault_abs_root(DEFAULT_VAULT)
    if not os.path.isdir(work):
        os.makedirs(work, exist_ok=True)
    init_vault_repo(DEFAULT_VAULT)
    _seed_vault_tree(DEFAULT_VAULT, "default")
    try:
        register_vault(DEFAULT_VAULT, "Main")
    except Exception:
        # Registry table not migrated yet -- fs vault still works.
        pass


def ensure_system_vault() -> None:
    """Guarantee the SYSTEM vault exists on disk with a repo and a system-flagged
    registry row. Called at startup (server + worker) right after
    ``ensure_default_vault``, and before the file watcher starts so the watcher's
    system-vault skip is authoritative from the first event.
    """
    work = vault_abs_root(SYSTEM_VAULT)
    if not os.path.isdir(work):
        os.makedirs(work, exist_ok=True)
    init_vault_repo(SYSTEM_VAULT)
    _seed_vault_tree(SYSTEM_VAULT, "system")
    try:
        register_vault(SYSTEM_VAULT, SYSTEM_VAULT, settings={"system": True})
    except Exception:
        # Registry table unavailable -- is_system_vault still catches the configured
        # slug, so hiding/RAG-exclusion hold regardless.
        pass
