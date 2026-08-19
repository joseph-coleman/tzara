# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import base64
import datetime
import glob
import logging
import os
import re
from contextlib import asynccontextmanager
from html import escape
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, quote, unquote, urlencode


import aiofiles

# from jinja2 import Template
from contextvars import ContextVar
from jinja2 import ChoiceLoader, Environment, FileSystemLoader
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.staticfiles import NotModifiedResponse

# from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    PlainTextResponse,
    StreamingResponse,

)
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket
from starlette.concurrency import run_in_threadpool

from config import (
    AGENT_OUTPUT_DIR,
    ATTACHMENT_FILE_TYPES,
    IMAGE_FILE_TYPES,
    PREVIEW_EMBED_FILE_TYPES,
    DEFAULT_ENCODING,
    DEFAULT_VAULT,
    DEFAULT_WIKI_PAGE,
    DIRECTORY_AS_MD_FILE_LINK,
    HIDE_DOT_DIRECTORY,
    INDEX_DOCUMENT_FRONTMATTER_DEFAULT,
    LLM_HAS_NATIVE_MOUNT,
    LLM_PROVIDER,
    OLLAMA_CONTEXT_BUDGET,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_URL,
    OLLAMA_EMBED_MODEL,
    OLLAMA_EMBED_KEEP_ALIVE,
    

    AGENT_SCHEDULER_TICK_S,
    AGENT_STAGING_TTL_DAYS,
    RESERVED_PATHS,
    SYSTEM_VAULT,
    TEMPLATE,
    USE_GIT_VERSIONING,
    VERSIONING_DEFAULT_SAVE_ON_EDIT,
    available_templates,
    is_template,

)


from src.doc_templates import starter_document
from src.doctransform import MarkdownDocTransform
from src.docversioning import MarkdownGitVersioning
from src.jupyter_client import jupyter_manager, format_execution_message
from src import kernel_api
# from src.jupyter_extension import JupyterCellExtension
# from src.markdown_extensions import (
#     AutoLinkExtension,
#     HighLightExtension,
#     ImageEmbedExtension,
#     LaTeXExtension,
#     StrikeThroughExtension,
#     WikiLinkExtension,
# )
from src.ollama_manager import OllamaManager
from src.llm_backend import create_llm_backend

from src.tasks import kernel_reaper_loop
from src import schema_upgrade
from src.snippets import make_snippet
from src import timefmt
from src.wikidoc import WikiDoc

#################
import asyncio
import json
import os
import sys
import datetime
from enum import Enum
from typing import Optional

import uvicorn

from src.task_broker import broker, REDIS_URL, get_async_redis
from src.task_tracker import TaskTracker
from src.llm_gate import mark_human_active
from src.task_definitions import (
    generate_all_metadata_task,
    reindex_all_task,
    test_postgresql,
    run_agent_task,
    warm_model_task,
)


import uuid
#from typing import Dict, Optional, Any


##################

# profiling

import os
import time
import pyinstrument
from starlette.middleware.base import BaseHTTPMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


class PyinstrumentMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, report_dir="/app/app/reports"):
        super().__init__(app)
        self.report_dir = report_dir
        # Ensure the directory exists so the app doesn't crash
        os.makedirs(self.report_dir, exist_ok=True)

    async def dispatch(self, request, call_next):
        profiler = pyinstrument.Profiler()
        profiler.start()

        response = await call_next(request)

        profiler.stop()

        # Generate a filename based on the path and time
        # Example: GET_users_1706112000.html
        timestamp = int(time.time())
        path_slug = request.url.path.replace("/", "_").strip("_") or "root"
        filename = f"{request.method}_{path_slug}_{timestamp}.html"
        filepath = os.path.join(self.report_dir, filename)

        with open(filepath, "w") as f:
            f.write(profiler.output_html())

        # Optional: Add a header so you know which file to look for
        response.headers["X-Profile-Report"] = filename
        return response


###############

# Ensure the default vault exists on disk with its SEPARATED git repo (gitlink ->
# off-Dropbox vault-history) before any MarkdownGitVersioning is constructed -- a plain
# `git init` in the work tree would otherwise create a .git dir on the synced tree.
from src import vault_registry
from src import vault_index
vault_registry.ensure_default_vault()
# The system vault (config.SYSTEM_VAULT, default "dada") holds wiki-owned content --
# agent definitions and help docs. Hidden from enumeration, excluded from RAG, but
# fully browsable/editable by direct URL.
vault_registry.ensure_system_vault()
# Backfill each vault's authoritative `.tzara/config.json` from the DB cache (metadata's
# pre-`.tzara` home) so existing display names / system flags survive the switch to
# filesystem-authoritative metadata. Idempotent: vaults already self-describing are
# skipped, so this is a cheap no-op on every boot after the first.
vault_registry.reconcile_vault_configs()

# Themes are per-VAULT (the `template` key in .tzara/config.json, falling back to
# TZARA_TEMPLATE), so there is one Environment per theme rather than one per process:
# each carries its own template cache, and two themes both asking for "document.html"
# would otherwise collide in it. The active theme rides a ContextVar set by
# _request_vault; every request runs in its own task and a task COPIES the context at
# creation, so a set() inside one handler can never leak into another (the same
# property content_ops._active_vault already relies on).
_active_theme: ContextVar[str] = ContextVar("active_theme", default=TEMPLATE)
# The request's vault, or None for a vault-less page. Separate from _active_theme
# because the palette is per-VAULT while the Environment is per-THEME: two vaults
# sharing a theme share an Environment, so anything vault-specific has to be looked
# up per request rather than baked into env.globals as a value.
_active_vault_slug: ContextVar[str | None] = ContextVar("active_vault", default=None)
_theme_envs: dict[str, Environment] = {}


def _theme_loader(theme: str):
    """Active theme first, `default` second, so a theme need only ship the files it
    overrides. `default` is complete by definition and needs no fallback chain."""
    if theme == "default":
        return FileSystemLoader(os.path.join("template", "default"))
    return ChoiceLoader([
        FileSystemLoader(os.path.join("template", theme)),
        FileSystemLoader(os.path.join("template", "default")),
    ])


def _vault_css_vars() -> str:
    """The current vault's palette as CSS declarations, or "" when there is none.

    A no-argument Jinja global rather than a value on the Environment: the
    Environment is per-THEME, so two vaults sharing a theme would share whatever
    value was baked in. Reading the ContextVar per call keeps it per-request.

    The returned string is interpolated into a <style> block by an environment with
    autoescape OFF -- see vault_registry.valid_css_color, which is what makes that
    safe. Nothing here re-escapes, because escaping a CSS value would break it.
    """
    slug = _active_vault_slug.get()
    return vault_registry.vault_css_declarations(slug) if slug else ""


def _theme_env() -> Environment:
    theme = _active_theme.get()
    env = _theme_envs.get(theme)
    if env is None:
        env = Environment(loader=_theme_loader(theme))
        # Expose the system-vault slug to every template (header/footer link to its
        # help hub). It is a process-lifetime constant, so a Jinja global beats
        # threading it through each route's context dict.
        env.globals["system_vault"] = SYSTEM_VAULT
        # Per-vault start page. The header/footer already have the vault in scope as
        # `v`, so they call this directly and no route has to pass the value along.
        env.globals["vault_default_page"] = vault_registry.vault_default_page
        # Per-vault palette, emitted inline by base.html. Must be a FUNCTION, not a
        # value: see _vault_css_vars.
        env.globals["vault_css_vars"] = _vault_css_vars
        # Because this Environment IS one theme, the theme name is a constant here --
        # which is what lets templates write {{theme}} into asset URLs with no route
        # plumbing at all. Those URLs must carry it: a stylesheet is fetched on its
        # own, with no vault context, so the name is the only thing separating two
        # vaults' cache entries.
        env.globals["theme"] = theme
        _theme_envs[theme] = env
    return env


class _ThemeEnvProxy:
    """Resolves attribute access to the current request's theme Environment, so the
    ~17 `jinja_env.get_template(...)` call sites need no change."""

    def __getattr__(self, name):
        return getattr(_theme_env(), name)


jinja_env = _ThemeEnvProxy()


def _vault_for_scope(path: str, query: bytes) -> str | None:
    """Best-effort vault slug for an incoming request path, or None when the request
    belongs to no vault.

    Routes are action-first (/wiki/{vault}/..., /graph/{vault}), with a bare vault
    name also legal (/expanse/foo), so try the second segment then the first.
    None means a vault-less page (/manage/*, /health, /vaults) or a themed asset --
    those render with the SITE-WIDE theme and no vault palette, which is what makes
    /vaults a neutral canvas for comparing the vaults listed on it.
    """
    parts = [p for p in path.split("/") if p]
    # Assets render no template and are the bulk of requests -- skip the stat()s.
    if parts and parts[0] == "template":
        return None
    if len(parts) >= 2 and vault_registry.vault_exists(parts[1]):
        return parts[1]
    if parts and vault_registry.vault_exists(parts[0]):
        return parts[0]
    if b"vault=" in query:
        candidate = parse_qs(query.decode("latin-1")).get("vault", [""])[0]
        if candidate and vault_registry.vault_exists(candidate):
            return candidate
    return None


class VaultThemeMiddleware:
    """Bind the request's vault theme BEFORE any endpoint runs.

    _request_vault would be the natural seam, but several routes call
    jinja_env.get_template() at the top of the handler, before they resolve the
    vault -- and a Template is bound to the Environment it came from, so a theme
    set later in the same handler arrives too late to affect that render. Doing it
    in middleware makes the binding independent of statement order inside handlers.

    Pure ASGI on purpose: it awaits the downstream app in the SAME task, so the
    ContextVar set here is visible to the endpoint. BaseHTTPMiddleware would run
    the endpoint in a child task instead.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            try:
                vault = _vault_for_scope(
                    scope.get("path", ""), scope.get("query_string", b"")
                )
                _active_vault_slug.set(vault)
                _active_theme.set(
                    vault_registry.vault_theme(vault) if vault else TEMPLATE
                )
            except Exception:
                # Never fail a request over presentation.
                _active_vault_slug.set(None)
                _active_theme.set(TEMPLATE)
        await self.app(scope, receive, send)

# Per-vault git-versioning trackers. Each vault is a separate repo (work tree under
# vaults/{slug}, git-dir off-Dropbox under vault-history/{slug}); init_vault_repo
# guarantees the separated repo exists before MarkdownGitVersioning runs its own
# `git init`, so a plain .git dir is never created on the synced tree.
from config import vault_root as _vault_root
from config import vault_abs_root as _vault_abs_root
_version_trackers: dict[str, MarkdownGitVersioning] = {}


def get_version_tracker(vault: str = DEFAULT_VAULT) -> MarkdownGitVersioning:
    vt = _version_trackers.get(vault)
    if vt is None:
        vault_registry.init_vault_repo(vault)
        vt = MarkdownGitVersioning(_vault_root(vault))
        _version_trackers[vault] = vt
    return vt


def _request_vault(request: Request, default: str = DEFAULT_VAULT) -> str:
    """Read + validate the {vault} route param (or query/default). 404 on unknown.

    Also binds the vault's theme for this request, so every template rendered
    downstream resolves through that theme (see _theme_env). Routes that are not
    vault-scoped (/manage/*, /health, /vaults) never call this and so keep the
    ContextVar default, the site-wide TZARA_TEMPLATE -- which is the right answer
    for a page that belongs to no vault.
    """
    vault = request.path_params.get("vault") or request.query_params.get("vault") or default
    if not vault_registry.vault_exists(vault):
        raise HTTPException(status_code=404, detail=f"Unknown vault: {vault}")
    _active_theme.set(vault_registry.vault_theme(vault))
    return vault


def _doc_from_request(request: Request) -> tuple[WikiDoc, str]:
    """Build a WikiDoc for a /{action}/{vault}/{path} route from its path params."""
    vault = _request_vault(request)
    path = request.path_params.get("path", "")
    return WikiDoc("/wiki/" + path, vault=vault), vault


async def _render_message_page(title, markdown, *, status_code=200, vault=DEFAULT_VAULT):
    """Render standalone markdown as a full themed page through the CANONICAL pipeline
    (MarkdownDocTransform + document.html), rather than hand-rolling HTML that would
    drift from the site chrome. For synthetic pages (e.g. 404) that aren't backed by a
    file on disk: give a WikiDoc in-memory content via set_content, exactly like the
    /api/markdown preview path does. hide_edit_link/show_chat keep the doc-specific
    controls (Edit/History/chat) off a page that has no real document behind it."""
    wd = WikiDoc(f"/wiki/{vault}/_message", vault=vault)
    wd.set_content(markdown)
    html = await asyncio.to_thread(MarkdownDocTransform(wd).get_content)
    doc_template = jinja_env.get_template("document.html")
    doc_data = {
        "unlinked_title": title,
        "vault": vault,
        "document": html,
        "document_mode": "view",
        "hide_edit_link": True,
        "show_chat": False,
        "show_raw": False,
    }
    return HTMLResponse(doc_template.render(doc_data), status_code=status_code)


async def _not_found_response(path: str = "") -> HTMLResponse:
    """A themed 404 rendered as a normal wiki page (so it inherits header/footer/nav
    and the active theme)."""
    # Show the missing path but neutralize markdown/HTML-significant chars so the
    # path can't break out of the code span or inject markup.
    shown = escape(path).replace("`", "")
    # No leading `#` heading: the page title chrome already shows "404 - Not Found".
    body = (
        f"There is no page or file at `{shown}`.\n\n"
        f"- [Return to the main page]"
        f"(/wiki/{DEFAULT_VAULT}/{_default_page_url(DEFAULT_VAULT)})\n"
        "- [Browse all vaults](/vaults)\n"
    )
    return await _render_message_page("404 - Not Found", body, status_code=404)


# Define the catch-all endpoint
def _default_page_url(vault: str) -> str:
    """A vault's start page, percent-encoded for splicing into a URL path. Page names
    routinely contain spaces (Obsidian), and the surrounding path segments here are
    already URL-encoded, so the two must not be mixed raw."""
    return quote(vault_registry.vault_default_page(vault), safe="/")


def _asset_response(fs_path, request: Request):
    """Serve a static file, revalidating stylesheets on every use.

    This is the one place themed assets (/template/...) reach the client. Without a
    Cache-Control header a browser falls back to HEURISTIC freshness -- roughly 10% of
    the time since Last-Modified -- so a stylesheet untouched for a month is served
    from disk for days with no request at all, and an edit to it needs a hard reload
    to surface. `no-cache` means "store, but revalidate", which turns each reload into
    a conditional GET.

    That conditional has to be answered HERE. FileResponse emits an ETag but never
    honours If-None-Match -- in Starlette that check lives in StaticFiles, which this
    app does not use (assets resolve per-theme through WikiDoc, see
    _test_existence). Without the check below, `no-cache` would mean re-sending all
    ~95KB of tzara.css on every navigation instead of an empty 304.

    Scoped to CSS deliberately: the stylesheets are what get edited in place, while
    the JS bundles are large, near-static, and want the long heuristic caching.
    (Switching THEMES needs none of this -- the theme name is part of the asset URL,
    so a different theme is a different cache entry by construction.)
    """
    if not str(fs_path).lower().endswith(".css"):
        return FileResponse(fs_path)

    # stat up front so FileResponse fills in etag/last-modified at construction,
    # which is what makes them available to compare against the request.
    try:
        stat_result = os.stat(fs_path)
    except OSError:
        return FileResponse(fs_path)

    response = FileResponse(
        fs_path, stat_result=stat_result, headers={"Cache-Control": "no-cache"}
    )
    etag = response.headers.get("etag")
    if etag:
        client_tags = [t.strip() for t in request.headers.get("if-none-match", "").split(",")]
        if etag in client_tags:
            return NotModifiedResponse(response.headers)
    return response


async def catch_all(request: Request):
    """Anything without a recognized route prefix. Resolve the user's INTENT into a
    proper /wiki/... URL, but never (a) dump a document's raw markdown as text, nor
    (b) blindly bounce an unknown path to the default page (which would mask bad
    links or malformed HTML in a page). Unresolvable paths get a real 404."""
    print("CATCH ALL ", request.url.path)

    raw_path = request.url.path

    # A trailing slash on what is otherwise a real route (e.g. /search/expanse/)
    # is swallowed by this catch-all, so Starlette's built-in slash-redirect never
    # fires and the request would 404. Canonicalize to the slash-less path -
    # preserving the query string - so the real route can match it. Root "/" is
    # left alone for the empty-parts branch below.
    if len(raw_path) > 1 and raw_path.endswith("/"):
        target = raw_path.rstrip("/")
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(target, status_code=307)

    parts = [p for p in raw_path.split("/") if p]

    # Root ("/") -> the default vault's start page (rendered).
    if not parts:
        return RedirectResponse(
            f"/wiki/{DEFAULT_VAULT}/{_default_page_url(DEFAULT_VAULT)}",
            status_code=307,
        )

    # A bare vault name ("/expanse", "/expanse/foo") -> that vault's namespace,
    # mirroring wiki_bare_redirect so /expanse/ lands in the expanse vault rather
    # than the default one. (DEFAULT_VAULT is itself a vault, so "/main" -> its
    # start page here too - rendered, never raw.)
    if vault_registry.vault_exists(parts[0]):
        rest = "/".join(parts[1:]) or _default_page_url(parts[0])
        return RedirectResponse(f"/wiki/{parts[0]}/{rest}", status_code=307)

    # An extension-less path that resolves to a markdown doc in the default vault
    # ("/Programming") -> redirect to the RENDERED page instead of serving raw text.
    wikidoc = WikiDoc(raw_path, vault=DEFAULT_VAULT)
    if wikidoc.exists():
        fs_path = wikidoc.file_path()
        if str(fs_path).lower().endswith(".md"):
            return RedirectResponse(
                f"/wiki/{DEFAULT_VAULT}/{'/'.join(parts)}", status_code=307
            )
        # A genuine non-markdown asset (image, etc.) - serve the file as before.
        return _asset_response(fs_path, request)

    # Genuinely unknown - a real 404, not a blind redirect.
    return await _not_found_response(raw_path)


# /wiki/{path}  (no vault) -> 302 to a vault-explicit URL.
async def wiki_bare_redirect(request: Request):
    """Convenience redirect for un-vaulted /wiki/<path> links. If the first segment is
    itself a known vault (e.g. /wiki/work), go to that vault's start page; otherwise
    treat the whole path as a document in the default vault."""
    path = request.path_params.get("path", "")
    parts = [p for p in path.split("/") if p]
    if parts and vault_registry.vault_exists(parts[0]):
        rest = "/".join(parts[1:]) or _default_page_url(parts[0])
        return RedirectResponse(f"/wiki/{parts[0]}/{rest}", status_code=302)
    target = path or _default_page_url(DEFAULT_VAULT)
    return RedirectResponse(f"/wiki/{DEFAULT_VAULT}/{target}", status_code=302)


# /wiki/{vault}/{path}
async def view_document(request: Request):
    print("VIEW DOCUMENT ", request.url.path)

    doc_template = jinja_env.get_template("document.html")

    target_sha = None
    if request.query_params:
        target_sha = request.query_params.get("revision", None)

    doc_data = {}
    has_history = False

    print("-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-=-")

    wikidoc, vault = _doc_from_request(request)
    print("url path = ", request.url.path)

    file_path = wikidoc.file_path()

    print("file path ", file_path)

    if wikidoc.exists() and wikidoc.extension() in ["md", ""]:

        # Track activity and warm the LLM model in the background. mark_human_active
        # publishes to Redis so the WORKER can see it and have agents stand aside;
        # touch() only ever set a process-local timestamp nothing read.
        if ollama_mgr:
            ollama_mgr.touch()
            await mark_human_active()
            await _fire_warm_task()

        if USE_GIT_VERSIONING:
            if target_sha:
                try:
                    # print(
                    #     "Trying to get specific historical data for ",
                    #     wikidoc.normalized_url_path(),
                    # )
                    historical_data = await asyncio.to_thread(
                        get_version_tracker(vault).get_file_at_commit,
                        file_path=wikidoc.normalized_url_path(),
                        commit_sha=target_sha,
                    )
                    wikidoc.set_content(historical_data)
                    has_history = True
                except FileNotFoundError:
                    historical_data = None
                    target_sha = None

            has_history = await asyncio.to_thread(
                get_version_tracker(vault).file_in_repo, wikidoc.normalized_url_path()
            )

        page_name = WikiDoc.markdown_page_name(wikidoc.url_pieces)

        markdown_doc = MarkdownDocTransform(wikidoc)
        html = await asyncio.to_thread(markdown_doc.get_content)
        md = markdown_doc.get_md()

        params = {"revision": target_sha}
        params = {k: v for k, v in params.items() if v is not None}
        doc_data["doc_query_string"] = urlencode(params)
        doc_data["revision"] = target_sha
        doc_data["has_history"] = has_history
        doc_data["show_chat"] = True
        doc_data["title"] = wikidoc._file_name_no_ext
        doc_data["page_name"] = page_name
        doc_data["page_path"] = wikidoc.url_pieces["path"]
        doc_data["vault"] = vault
        doc_data["toc"] = md.toc  # pylint: disable=no-member

        meta = getattr(md, "Meta", {})

        # A non-empty frontmatter `title:` overrides the slug as the *display*
        # title. The slug still drives every URL (edit link, page_name/page_path);
        # only the human-facing label changes, so navigation is unaffected. Plain
        # scalar keys arrive as strings, but list-style keys arrive as lists (see
        # FrontmatterPreprocessor) - coerce and guard against an empty value.
        fm_title = meta.get("title", "")
        if isinstance(fm_title, list):
            fm_title = fm_title[0] if fm_title else ""
        fm_title = str(fm_title).strip()
        if fm_title:
            doc_data["title"] = fm_title

        raw_tags = meta.get("tags", [])
        if isinstance(raw_tags, str):
            tag_list = [t.strip() for t in raw_tags.split(",") if t.strip()]
        else:
            tag_list = [t.strip() for t in raw_tags if t.strip()]
        doc_data["tags"] = [t.lstrip("!") for t in tag_list]
        doc_data["summary"] = meta.get("summary", "").strip()

        doc_data["scripts"] = ""
        doc_data["file_path"] = escape(file_path)
        doc_data["url_path"] = escape(wikidoc.display_url_path())

        # this here allows for including it only on the document page.
        # and only if LaTeX was in the markdown and got processed.
        doc_data["has_latex"] = False
        if md.tzara_has_latex:  # pylint: disable=no-member
            doc_data["has_latex"] = True
            doc_data["scripts"] += """"""

        if md.tzara_has_jupyter:  # pylint: disable=no-member
            doc_data["has_jupyter"] = True

        if md.tzara_has_mermaid:  # pylint: disable=no-member
            doc_data["has_mermaid"] = True

        # Only pages that embed a .canvas pay for tzara-canvas.js (loaded in
        # base.html on this flag). getattr guards the render paths that skip the
        # vault-scoped CanvasEmbedExtension (use_wiki_link=False).
        if getattr(md, "tzara_has_canvas_embed", False):
            doc_data["has_canvas_embed"] = True

        # Agent-owned banner, derived from LOCATION (never a frontmatter flag): any
        # page under {AGENT_OUTPUT_DIR}/{agent}/ may be overwritten by that agent.
        # Moving the page out of the folder is how a human takes ownership - the
        # banner (and the agent's write claim) disappear with the move.
        _rel = (wikidoc.relative_file_path() or "").replace(os.sep, "/")
        if _rel.startswith(AGENT_OUTPUT_DIR + "/"):
            _segs = _rel.split("/")
            # Layout: _dada/{ns}/{slug}/...  (ns = agents | editors) -> owner is the
            # SLUG, kind is the namespace singular ("agent"/"editor"). Legacy flat
            # _dada/{slug}/... (pre-migration) falls back to showing the slug as an agent.
            if len(_segs) > 3 and _segs[1] in ("agents", "editors"):
                _owner, _kind = _segs[2], _segs[1][:-1]
            else:
                _owner, _kind = (_segs[1] if len(_segs) > 2 else "?"), "agent"
            doc_data["agent_banner"] = {
                "agent": _owner, "kind": _kind, "dir": AGENT_OUTPUT_DIR,
            }

        doc_data["document"] = html
        doc_data["document_mode"] = "view"
        doc_data["show_raw"] = True

        response_content = doc_template.render(doc_data)

        return HTMLResponse(response_content)
    else:
        if wikidoc.extension() == "canvas":
            # Render the canvas editor for both existing and brand-new (missing)
            # canvases. A missing file gets an empty canvas; TzaraCanvas writes
            # it to disk lazily on the first edit via its /save/ auto-save, so
            # there is nothing to create here. This is also what stops a missing
            # .canvas from falling through to the /edit/ markdown redirect below.
            canvas_template = jinja_env.get_template("canvas.html")
            canvas_json = '{"nodes":[],"edges":[]}'
            if wikidoc.exists():
                raw = wikidoc.get_content(data_type="text")
                try:
                    json.loads(raw)
                    canvas_json = raw
                except Exception:
                    canvas_json = '{"nodes":[],"edges":[]}'
            return HTMLResponse(canvas_template.render({
                "title": wikidoc._file_name_no_ext,
                "page_name": wikidoc.file_name(),
                "page_path": wikidoc.url_pieces["path"],
                "vault": vault,
                # Identifier the canvas auto-save JS posts back to /save/ as
                # document_name. Must be the vault-explicit request URL path (what
                # constructed this WikiDoc); from_url_with_vault re-parses the vault.
                "doc_name": request.url.path,
                "document_mode": "view",
                "hide_edit_link": True,
                "canvas_json": canvas_json,
                "scripts": "",
            }))

        # Obsidian attachment model: any other file that exists on disk (CSV, PDF,
        # datasets, images, ...) is a real file living in the vault next to the page.
        # Serve it as a static asset so [embeds]/[links] resolve AND out-of-process
        # consumers (e.g. a Jupyter kernel on another container) can fetch it by URL.
        # FileResponse infers Content-Type from the filename.
        if wikidoc.exists():
            return FileResponse(wikidoc.file_path())

        # A path with a concrete asset extension that does NOT exist is a broken
        # link, not a new markdown page - 404 rather than opening the (binary) name
        # in the markdown editor. Extension-less paths fall through to create/edit.
        if wikidoc.extension() not in ("", "md"):
            raise HTTPException(status_code=404, detail="File not found.")

        # Filter empty segments so top-level files (_path_list == [""])
        # don't produce "/edit/{vault}//foo.ext".
        parts = [p for p in wikidoc._path_list if p]
        return RedirectResponse(
            "/".join(["/edit", vault, *parts, wikidoc.file_name()])
        )


# /raw/*
async def view_raw_document(request: Request):
    print("VIEW RAW DOCUMENT ", request.url.path)

    target_sha = None
    if request.query_params:
        target_sha = request.query_params.get("revision", None)

    print(request.url.path)
    wikidoc, vault = _doc_from_request(request)

    print("Does this exist?")
    print(wikidoc.exists())

    file_path = wikidoc.file_path()

    if wikidoc.exists() and wikidoc.extension() in ["md", "canvas", ""]:

        if USE_GIT_VERSIONING:
            if target_sha:
                try:
                    historical_data = await asyncio.to_thread(
                        get_version_tracker(vault).get_file_at_commit,
                        file_path=wikidoc.normalized_url_path(),
                        commit_sha=target_sha,
                    )
                    wikidoc.set_content(historical_data)

                except FileNotFoundError:
                    ...

        raw_content = wikidoc.get_content()

        return PlainTextResponse(raw_content)
    elif wikidoc.exists():
        # Attachment (image / PDF / data file): serve raw bytes. The canvas
        # resolveFile builds /raw/{vault}/{path} for ALL referenced files
        # (canvas.html), so /raw must serve non-text files too, not just md/canvas.
        # /raw is the right route here (not /wiki): resolveFile is generic across
        # file types, and /raw returns the RAW content (image bytes, or markdown
        # SOURCE for an embedded .md node), whereas /wiki would render markdown/
        # canvas to HTML. exists() is True only for allowlisted types
        # (ATTACHMENT_FILE_TYPES via _test_existence), so this can't serve
        # arbitrary/active files. FileResponse infers Content-Type from the name.
        return FileResponse(wikidoc.file_path())
    else:
        raise HTTPException(status_code=404, detail="File not found.")


# /edit/
async def edit_document(request: Request):

    doc_template = jinja_env.get_template("edit.html")

    target_sha = None
    if request.query_params:
        target_sha = request.query_params.get("revision", None)

    doc_data = {}

    wikidoc, vault = _doc_from_request(request)
    file_path = wikidoc.file_path()
    file_ext = wikidoc.extension()

    # Canvas files are edited in place under /wiki/ (TzaraCanvas), never in the
    # markdown editor. Bounce any stray /edit/<file>.canvas link back to the
    # canvas view so it never tries to open raw JSON in a textarea.
    if file_ext == "canvas":
        parts = [p for p in wikidoc._path_list if p]
        return RedirectResponse(
            "/".join(["/wiki", vault, *parts, wikidoc.file_name()])
        )

    path_list = wikidoc.path_list()
    file_name = wikidoc.file_name()
    file_name_base = wikidoc.file_name_no_ext()
    has_history = False
    path = wikidoc.path()

    page_name = file_name
    if file_ext == "":
        page_name = file_name_base
    elif file_ext == "md":
        page_name = file_name_base
    # else:
    #    # what happens when the file isn't markdown? yikes!

    # document_name posted back to /save/: a vault-explicit, round-trippable id
    # (parsed by WikiDoc.from_url_with_vault), NOT the filesystem normalized path.
    file_path = wikidoc.display_url_path()

    if wikidoc.exists():
        if USE_GIT_VERSIONING:
            if target_sha:
                try:
                    historical_data = await asyncio.to_thread(
                        get_version_tracker(vault).get_file_at_commit,
                        file_path=wikidoc.normalized_url_path(),
                        commit_sha=target_sha,
                    )
                    wikidoc.set_content(historical_data)
                except FileNotFoundError:
                    historical_data = None
                    target_sha = None

            has_history = await asyncio.to_thread(
                get_version_tracker(vault).file_in_repo, wikidoc.normalized_url_path()
            )

        raw_markdown = wikidoc.get_content(data_type="text")
        doc_data["document_mode"] = "edit"
    else:
        # Prefill depends on WHERE the new file is: a plain page gets the generic
        # stub, but a file in the system vault's agents/ or editors/ folder is a
        # typed definition and starts as that definition's skeleton (src.doc_templates).
        raw_markdown = starter_document(wikidoc, vault)
        doc_data["document_mode"] = "create"

    params = {"revision": target_sha}
    params = {k: v for k, v in params.items() if v is not None}
    doc_data["doc_query_string"] = urlencode(params)
    doc_data["revision"] = target_sha
    doc_data["has_history"] = has_history

    doc_data["history_enabled"] = USE_GIT_VERSIONING
    doc_data["save_on_edit"] = VERSIONING_DEFAULT_SAVE_ON_EDIT

    doc_data["title"] = file_name_base
    doc_data["page_name"] = page_name
    doc_data["page_path"] = path
    doc_data["vault"] = vault
    doc_data["scripts"] = ""
    doc_data["document"] = escape(raw_markdown)
    doc_data["file_path"] = escape(file_path)
    # Vault-relative, extension-less source path for the move/rename prompt. The move
    # posts an explicit vault + vault-relative paths, so both sides of /api/move parse
    # symmetrically (prefilling the prompt with the vault-explicit display path caused
    # the vault segment to be double-applied -- /wiki/{v}/wiki/{v}/...).
    doc_data["move_source_rel"] = escape(wikidoc.relative_display_path() or "")

    response_content = doc_template.render(doc_data)

    return HTMLResponse(response_content)


# /history/
async def history_document(request: Request):
    doc_template = jinja_env.get_template("document.html")

    old_commit_sha = request.query_params.get("revision")

    wikidoc, vault = _doc_from_request(request)
    file_path = wikidoc.normalized_url_path()
    # vault-relative path used to build vault-explicit action links below.
    vault_rel = f"{vault}/{wikidoc.relative_file_path()}"

    if not USE_GIT_VERSIONING:
        # abort
        return RedirectResponse(
            "/".join(["/wiki", vault, *(wikidoc._path_list), wikidoc.file_name()])
        )

    cursor = request.query_params.get("cursor")
    history_result = await asyncio.to_thread(
        get_version_tracker(vault).get_file_history, file_path, cursor=cursor
    )
    history = history_result["commits"]
    next_cursor = history_result["next_cursor"]

    history_markdown = """
| View | Edit | Diff | Timestamp | Author / Email | Sha |
|------|------|------|-----------|----------------|-----|
"""

    for each in history:
        query_params = {"revision": each["sha"]} if each["count"] > 0 else {}
        query_params = {k: v for k, v in query_params.items() if v is not None}
        query_params = urlencode(query_params)
        if len(query_params) > 0:
            query_params = "?" + query_params
        # if each["count"] > 0:
        if each["file_exists"]:
            history_markdown += f"|[🔍](/wiki/{vault_rel}{query_params} ){{: title='View'}}"
            history_markdown += f"|[📝](/edit/{vault_rel}{query_params} ){{: title='Edit'}}"
            history_markdown += f"|[⚖️](/history/{vault_rel}{query_params} ){{: title='Compare'}}"
        else:
            history_markdown += "| | | "

        history_markdown += f"|{each['date_str']}"
        history_markdown += f"|{each['author']}"
        if each["email"] != "None":
            history_markdown += f" / {each['email']} / {each['message']}"
        else:
            history_markdown += f" / {each['message']}"
        history_markdown += f"|{each['short_sha']}"
        # history_markdown += f"|{each['message']}"
        history_markdown += "|\n"

    if next_cursor:
        history_markdown += f"\n[**Load more history...**](/history/{vault_rel}?cursor={next_cursor})\n"

    html_history_markdown = ""
    if old_commit_sha:
        print("Is this working? ")
        print(file_path)
        print(history[-1]["sha"])

        try:
            historical_data = await asyncio.to_thread(
                get_version_tracker(vault).get_file_at_commit,
                file_path=wikidoc.normalized_url_path(),
                commit_sha=old_commit_sha,
            )
            current_data = wikidoc.get_content()
            # wikidoc.set_content(historical_data)
        except FileNotFoundError:
            historical_data = "Nothing found"
            current_data = "Nothing found"
        import difflib

        # file_diff = version_tracker.get_diff(
        #     file_path, old_commit=old_commit_sha, new_commit=history[-1]["sha"]
        # )
        # print("file diff is of type ", type(file_diff))
        diff = difflib.ndiff(historical_data.splitlines(), current_data.splitlines())
        diff_result = []
        for line in diff:
            mod_line = line.replace(" ", "&nbsp;")
            if line.startswith("+"):
                diff_result.append("<tt class='diff_add'>" + mod_line + "</tt>")
            elif line.startswith("-"):
                diff_result.append("<tt class='diff_del'>" + mod_line + "</tt>")
            elif line.startswith("?"):
                diff_result.append("<tt class='diff_not'>" + mod_line + "</tt>")
            else:
                diff_result.append("<tt>" + mod_line + "</tt>")

            # diff_result.append(line)
        html_history_markdown += "<br>".join(diff_result)

        history_markdown += f"\n#Diff\nComparing **{old_commit_sha}** to current **{history[0]['sha']}**. \n\n----\n"
    # history_markdown += version_tracker.get_diff_history(file_path)

    tempwikidoc = WikiDoc("/temp/history")
    tempwikidoc.set_content(history_markdown)
    markdown_doc = MarkdownDocTransform(tempwikidoc)
    html = await asyncio.to_thread(markdown_doc.get_content) + html_history_markdown

    doc_data = {}
    doc_data["unlinked_title"] = f"History of: {wikidoc._file_name_no_ext}"
    # doc_data["page_name"] = f"History of: {wikidoc._file_name_no_ext}"
    # doc_data["page_path"] = wikidoc.url_pieces["path"]
    # Without this the header/footer templates fall back to
    # `vault|default('main', true)`, so every vault-scoped link (Main, Index,
    # Graph, Canvas, Chat, Search) silently points at the "main" vault.
    doc_data["vault"] = vault

    doc_data["document"] = html

    response_content = doc_template.render(doc_data)

    return HTMLResponse(response_content)


# /save/ process document saves
async def save_document(request: Request):
    # do we really care if this was a POST or GET?
    method = request.method

    form = await request.form()
    updated_markdown = form["markdown"]
    document_name = form["document_name"]  # is from file_path in edit
    if "delete_button" in form:
        return await delete_document(request)

    if not document_name:
        return RedirectResponse(
            f"/wiki/{DEFAULT_VAULT}/{_default_page_url(DEFAULT_VAULT)}"
        )

    # document_name is a vault-explicit identifier (display_url_path / canvas URL);
    # parse the vault back out of it. Hard isolation: writes stay within that vault.
    wikidoc = WikiDoc.from_url_with_vault(document_name)
    vault = wikidoc.vault()
    if not vault_registry.vault_exists(vault):
        raise HTTPException(status_code=404, detail=f"Unknown vault: {vault}")
    wikidoc.set_content(updated_markdown)
    await asyncio.to_thread(wikidoc.save)

    # Auto-save callers (e.g. the canvas editor) post redirect=false so they get
    # a lightweight 204 instead of the 302 that would make the browser re-fetch
    # and re-render the whole page on every debounced write. Absent the field,
    # behavior is unchanged (markdown edit form still redirects to the document).
    should_redirect = str(form.get("redirect", "true")).lower() not in (
        "false",
        "0",
        "no",
    )

    should_version = "save_version" in form

    if USE_GIT_VERSIONING and should_version:
        # The per-vault tracker keys off a path under the vault work tree
        # (vaults/{vault}/Foo/bar.md). normalized_url_path() is markdown-only and
        # returns None for .canvas, so build the equivalent path from parsed
        # components for those. Canvas files are still first-class versioned files.
        version_path = wikidoc.normalized_url_path()
        debounce_path = wikidoc.relative_file_path()
        if version_path is None:
            version_path = os.path.join(
                _vault_root(vault), *wikidoc.path_list(), wikidoc.file_name()
            )
            debounce_path = os.path.join(
                *wikidoc.path_list(), wikidoc.file_name()
            )
        commit_sha = await asyncio.to_thread(
            get_version_tracker(vault).save_version,
            version_path,
        )
        # Set debounce key (vault-scoped) so the watcher's git-commit task skips a
        # duplicate commit for this same write. Single key constructor via
        # WikiDoc.debounce_key/set_debounce (async wraps the sync primitive).
        if commit_sha:
            await asyncio.to_thread(WikiDoc.set_debounce, vault, debounce_path)


    if not should_redirect:
        return PlainTextResponse("", status_code=204)

    return RedirectResponse("/" + wikidoc.display_url_path(), status_code=302)


# /delete/
async def delete_document(request: Request):
    """Deletes a single file (markdown document). Does not delete directories.

    Filesystem removal + git commit are handled by content_ops.delete_document_op
    (the same module that owns move/rename); the RAG database is reconciled by the
    file watcher. Inbound [[links]] are intentionally left to become ghost edges.
    """
    from src import content_ops

    vault = DEFAULT_VAULT
    if request.method == "POST":
        form = await request.form()
        document_name = form["document_name"]

        if not document_name:
            return RedirectResponse(
                f"/wiki/{DEFAULT_VAULT}/{_default_page_url(DEFAULT_VAULT)}",
                status_code=302,
            )

        wd = WikiDoc.from_url_with_vault(document_name)
        vault = wd.vault()
        rel = wd.relative_file_path()
        if rel:
            await content_ops.delete_document_op(rel, vault)

    return RedirectResponse(f"/index/{vault}/", status_code=302)


# /api/move
async def move_document_endpoint(request: Request):
    """Move/rename a document (JSON API).

    Rewrites inbound wikilink text in referring files, moves the file on disk, and
    records a git move commit; the RAG database is reconciled by the file watcher
    (see src.content_ops). Returns JSON {status, ...}; on success includes the
    redirect URL for the document's new location.
    """
    from src import content_ops

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {"status": "error", "reason": "invalid JSON body"}, status_code=400
        )

    document_name = (data.get("document_name") or "").strip()
    destination = (data.get("destination") or "").strip()
    if not document_name or not destination:
        return JSONResponse(
            {"status": "error", "reason": "document_name and destination are required"},
            status_code=400,
        )

    # Moves are within a single vault (hard isolation). The vault comes from an explicit
    # "vault" field when present (the /index file-manager sends vault-relative paths +
    # vault); otherwise it is parsed out of a vault-explicit document_name.
    if data.get("vault"):
        vault = str(data["vault"]).strip()
        src_wd = WikiDoc("/wiki/" + document_name, vault=vault)
    else:
        src_wd = WikiDoc.from_url_with_vault(document_name)
        vault = src_wd.vault()
    if not vault_registry.vault_exists(vault):
        return JSONResponse({"status": "error", "reason": f"unknown vault: {vault}"}, status_code=404)
    src_rel = src_wd.relative_file_path()
    dest_wd = WikiDoc("/wiki/" + destination, vault=vault)
    dest_rel = dest_wd.relative_file_path()
    if not src_rel or not dest_rel:
        return JSONResponse(
            {"status": "error", "reason": "markdown documents only"}, status_code=400
        )

    result = await content_ops.move_document_op(src_rel, dest_rel, vault)
    if result["status"] == "ok":
        result["redirect"] = "/" + dest_wd.display_url_path()
        return JSONResponse(result, status_code=200)
    code = 409 if result["status"] == "collision" else 400
    return JSONResponse(result, status_code=code)


async def kernel_query_endpoint(request: Request):
    """Internal vault-query API for the `wiki` object injected into Jupyter kernels.

    The vault is taken from the URL (`/api/kernel/{vault}/query`) and passed to the
    dispatcher -- never trusted from the body -- so a kernel can only ever query its
    own vault. The underlying rag_search calls are synchronous (psycopg2 + Ollama
    embed), so they run in a threadpool to keep the event loop free.
    """
    vault = request.path_params["vault"]
    if not vault_registry.vault_exists(vault):
        return JSONResponse({"error": f"unknown vault: {vault}"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    op = body.get("op")
    args = body.get("args") or {}
    try:
        result = await run_in_threadpool(kernel_api.run_query, op, args, vault)
    except kernel_api.KernelApiError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        logging.getLogger("kernel_api").exception(
            "kernel query failed: op=%s vault=%s", op, vault
        )
        return JSONResponse({"error": f"query failed: {e}"}, status_code=500)
    return JSONResponse({"result": result}, status_code=200)


# /api/batch-move
async def batch_move_endpoint(request: Request):
    """Move many files and/or whole folders into a destination folder (JSON API).

    Unlike /api/move (single markdown rename), this accepts assets and folders:
    folders are expanded to all their contents, inbound [[links]]/![[embeds]] are
    rewritten in one pass, and each file is moved with its own git commit; the RAG
    DB is reconciled by the watcher. Body: {"items": [...], "destination": "..."}.
    ``destination`` is a folder (empty string or "/" means the vault root). Returns
    {status, moved, skipped, referrers_updated}.
    """
    from src import content_ops

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {"status": "error", "reason": "invalid JSON body"}, status_code=400
        )

    items = data.get("items")
    if not isinstance(items, list) or not items:
        return JSONResponse(
            {"status": "error", "reason": "items (a non-empty list) is required"},
            status_code=400,
        )
    if "destination" not in data:
        return JSONResponse(
            {"status": "error", "reason": "destination is required"}, status_code=400
        )
    destination = (data.get("destination") or "").strip()
    vault = (data.get("vault") or DEFAULT_VAULT).strip()
    if not vault_registry.vault_exists(vault):
        return JSONResponse({"status": "error", "reason": f"unknown vault: {vault}"}, status_code=404)

    result = await content_ops.batch_move_op(items, destination, vault)
    code = 200 if result["status"] in ("ok", "noop") else 400
    return JSONResponse(result, status_code=code)


# /api/batch-delete
async def batch_delete_endpoint(request: Request):
    """Delete many files and/or whole folders (JSON API).

    Folders are expanded to all their contents (assets included); each file is
    removed with its own git commit and the RAG DB is reconciled by the watcher.
    Inbound [[links]] are intentionally left to become ghost edges. Body:
    {"items": [...]}. Returns {status, deleted, skipped}.
    """
    from src import content_ops

    try:
        data = await request.json()
    except Exception:
        return JSONResponse(
            {"status": "error", "reason": "invalid JSON body"}, status_code=400
        )

    items = data.get("items")
    if not isinstance(items, list) or not items:
        return JSONResponse(
            {"status": "error", "reason": "items (a non-empty list) is required"},
            status_code=400,
        )
    vault = (data.get("vault") or DEFAULT_VAULT).strip()
    if not vault_registry.vault_exists(vault):
        return JSONResponse({"status": "error", "reason": f"unknown vault: {vault}"}, status_code=404)

    result = await content_ops.batch_delete_op(items, vault)
    code = 200 if result["status"] in ("ok", "noop") else 400
    return JSONResponse(result, status_code=code)


##########################################


def find_last_match_index(A, B):
    """Given two lists, find the index of last match"""
    min_len = min(len(A), len(B))
    if min_len == 0:
        return -1  # or would raise error be better?
    for n in range(min_len):
        if A[n] != B[n]:
            return n - 1
    return min_len - 1


def _index_hidden(rel: str) -> bool:
    """True if any segment of a vault-relative path starts with '.' (hidden dir or
    file). vault_index.get_index() already prunes hidden *directories*, but not
    hidden *files*; callers that mirror the old ``include_hidden=not
    HIDE_DOT_DIRECTORY`` glob behavior use this to also drop dot-files."""
    return any(seg.startswith(".") for seg in rel.split("/"))


def fulltext_search(query, vault, max_results=20):
    """Search all .md files for a query string. Returns list of result dicts."""
    import re

    # Reuse the cached vault index (single walk, dot-dirs pruned) rather than a
    # fresh recursive glob per case-variant on every search.
    #
    # is_excluded is THE exclusion gate the RAG side already honors. Without it this
    # path disagrees with the semantic half of the same results page: `_dada` is not
    # a dot-directory, so agent output and run logs would surface here only.
    from src.rag_indexer import is_excluded
    wiki_root = _vault_root(vault)
    all_paths, _ = vault_index.get_index(vault)
    md_files = [
        os.path.join(wiki_root, p.replace("/", os.sep))
        for p in all_paths
        if p.lower().endswith(".md")
        and not (HIDE_DOT_DIRECTORY and _index_hidden(p))
        and not is_excluded(p)
    ]
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    results = []

    for fpath in md_files:
        try:
            content = WikiDoc._read_raw(fpath)  # canonical read (DEFAULT_ENCODING)
        except Exception:
            continue

        # Skip documents with Index: False in frontmatter
        fm = WikiDoc.parse_frontmatter(content)
        _index_flag = fm.get("index", str(INDEX_DOCUMENT_FRONTMATTER_DEFAULT))
        if _index_flag.lower() in ("false", "no", "0"):
            continue

        matches = pattern.findall(content)
        if not matches:
            continue

        match_count = len(matches)
        rel = os.path.relpath(fpath, wiki_root)
        url_path = rel.replace(os.sep, "/")
        if url_path.endswith(".md"):
            url_path = url_path[:-3]

        title = Path(fpath).stem

        snippet = make_snippet(content, query)

        results.append(
            {
                "title": title,
                "url_path": url_path,
                "snippet": snippet,
                "match_count": match_count,
                "source": "fulltext",
            }
        )

    results.sort(key=lambda r: r["match_count"], reverse=True)
    return results[:max_results]


# /index/
async def index_document(request: Request):

    doc_template = jinja_env.get_template("document.html")

    vault = _request_vault(request)

    doc_data = {}
    doc_data["vault"] = vault

    # Enumerate the vault via the shared, cached index (single os.walk, dot-dirs
    # pruned, 3s TTL) instead of one recursive glob per extension. The old loop ran
    # ~34 whole-tree walks (17 exts x upper/lower) on every load -- catastrophic on
    # the 9p mount, where each stat is a slow syscall. get_index() returns every
    # vault-relative, forward-slash path; we filter to the listable set in-memory.
    # Show every doc type plus every servable attachment (ATTACHMENT_FILE_TYPES),
    # so all uploadable assets are listed -- and therefore selectable, movable, and
    # deletable -- in the file manager. md/canvas are doc types, not attachments,
    # so they're named explicitly here.
    index_exts = {ext.lower() for ext in (["md", "canvas"] + ATTACHMENT_FILE_TYPES)}
    all_paths, _ = vault_index.get_index(vault)
    file_list = [
        p for p in all_paths
        if "." in p and p.rsplit(".", 1)[-1].lower() in index_exts
        and not (HIDE_DOT_DIRECTORY and _index_hidden(p))
    ]

    md_list = ""
    last_list_depth = 0
    current_list_depth = 0

    last_path_list = []
    last_path = ""
    file_list = [f if f[-3:] != ".md" else f[:-3] for f in file_list]
    file_list = list(set(file_list))
    file_list.sort(key=str.lower)

    # Remove entries that also appear as parent directories of other entries.
    # These are .md files whose name matches a folder (e.g., Math.md + Math/).
    # The directory-filling logic in the loop renders them as linked folders.
    parent_paths = set()
    for f in file_list:
        parts = f.split("/")
        for i in range(1, len(parts)):
            parent_paths.add("/".join(parts[:i]))
    file_list = [f for f in file_list if f not in parent_paths]

    for each in file_list:
        d = WikiDoc.parse_url_path(each)
        is_md = d["file_ext"] == "md"
        if is_md:
            # Fold the .md stem into path_list so tree-walking treats
            # /foo/bar.md and /foo/bar/ as the same depth.
            folded = [*d["path_list"], d["file_name_no_ext"]]
            if folded and folded[0] == "":
                folded = folded[1:]
            d = {**d, "path_list": folded, "path": "/".join(folded)}

        if not d["path"]:
            current_list_depth = 0
        else:
            current_list_depth = len(d["path_list"])

            # # ignore any hidden folders, anything that begins with .
            # if any(
            #     [
            #         True if directory_name[0] == "." else False
            #         for directory_name in d["path_list"]
            #     ]
            # ):
            #     if HIDE_DOT_DIRECTORY:
            #         continue

        if not (last_path_list == d["path_list"]):
            # path has changed.
            branch_index = find_last_match_index(last_path_list, d["path_list"])
            if branch_index == -1:
                # branch_index = 0  # just starting
                ...

            # up_depth = last_list_depth - branch_index
            down_depth = current_list_depth - branch_index

            if branch_index == -1:
                # correction
                down_depth = down_depth - 1

            for depth in range(branch_index + 1, current_list_depth):

                if depth == (current_list_depth - 1):
                    if is_md:
                        leaf = "/".join(d["path_list"])
                        dpath = "/".join(p for p in d["path_list"] if p)
                        attr = (
                            f'.list_file .file_{d["file_ext"]} '
                            f'data-path="{dpath}.md" data-type="md"'
                        )
                        md_list += (
                            f"{depth * '    '}* [[{leaf}]]\n"
                            + "{: " + attr + " }\n"
                        )
                        break

                dir_path = "/".join(p for p in d["path_list"][: depth + 1] if p)
                if DIRECTORY_AS_MD_FILE_LINK:
                    attr = f'.list_dir_link data-path="{dir_path}" data-type="dir"'
                    md_list += (
                        f"{depth * '    '}* [[{dir_path}]] \n"
                        + "{: " + attr + " }\n"
                    )
                else:
                    attr = f'.list_dir data-path="{dir_path}" data-type="dir"'
                    md_list += (
                        f"{depth * '    '}* {d['path_list'][depth]}\n"
                        + "{: " + attr + " }\n"
                    )
        else:
            ...

        if not is_md:
            parts = [*d["path_list"], d["file_name"]]
            leaf = "/".join(parts)
            dpath = "/".join(p for p in parts if p)
            attr = (
                f'.list_file .file_{d["file_ext"]} '
                f'data-path="{dpath}" data-type="{d["file_ext"]}"'
            )
            md_list += (
                f"{current_list_depth * '    '}* [[{leaf}]]\n"
                + "{: " + attr + " }\n"
            )
        # else:
        #     md_list += (
        #         f"{current_list_depth * '    '}* <<[["
        #         + "/".join(d["path_list"])
        #         + "]]>>\n"
        #         # + f"{{: .list_file .file_{d['file_ext']} }}\n"
        #     )

        # md_list += (
        #     f"{current_list_depth * '    '}* <<[["
        #     + "/".join(d["path_list"])
        #     + "/"
        #     + d["file_name_no_ext"]
        #     + "]]>>\n"
        #     # + f"{{: .list_file .file_{d['file_ext']} }}\n"
        # )

        # current becomes last on next iteration.
        last_list_depth = current_list_depth
        last_path_list = d["path_list"].copy()
        last_path = d["path"]

    # Render with the active vault so [[wikilinks]] resolve within it and generated
    # hrefs carry the /wiki/{vault}/ prefix.
    wikidoc = WikiDoc("/index/temp_index", vault=vault)
    wikidoc.set_content(md_list)
    markdown_doc = MarkdownDocTransform(wikidoc)
    html = await asyncio.to_thread(markdown_doc.get_content)

    doc_data["scripts"] = ""
    doc_data["is_index"] = True
    doc_data["unlinked_title"] = "Index"
    # doc_data["document"] = "<pre>" + md_list + "</pre><pre>" + debug_text + "</pre>"
    doc_data["document"] = html

    response_content = doc_template.render(doc_data)

    return HTMLResponse(response_content)


# from src.jupyter_client import jupyter_manager
# from src.tasks import kernel_reaper_loop

# import asyncio
# from contextlib import asynccontextmanager


ollama_mgr: OllamaManager | None = None
task_tracker: TaskTracker | None = None


# Keep strong references to background asyncio tasks to prevent GC before completion
_background_tasks: set = set()

# Debounce for fire-and-forget warm tasks (per-model)
_last_warm_fire: dict[str, float] = {}
_WARM_DEBOUNCE_SECONDS = 60


async def _fire_warm_task(
    url: str = None, model: str = None, keep_alive: str = None
):
    """Enqueue a warm_model_task via Taskiq with debounce. Never blocks page load."""
    # Warming loads a model into VRAM via the Ollama-native mount. A pure OpenAI
    # (`openai`) provider has no such concept - it loads on demand - so don't
    # enqueue a warm at all rather than schedule a task that would no-op.
    if not LLM_HAS_NATIVE_MOUNT:
        return
    _model = model or OLLAMA_MODEL
    now = time.time()
    if now - _last_warm_fire.get(_model, 0.0) < _WARM_DEBOUNCE_SECONDS:
        return
    _last_warm_fire[_model] = now

    _url = url or OLLAMA_URL
    _keep_alive = keep_alive or OLLAMA_KEEP_ALIVE
    task_id = f"warm:{_model}"
    # Apply the OLLAMA_NUM_CTX load ASK only to the chat/agent model; embedding models
    # have their own tiny window and must not be loaded at the chat context size.
    _num_ctx = OLLAMA_NUM_CTX if _model == OLLAMA_MODEL else 0

    # Only pass num_ctx when the ask is actually set: keeps the default (ask-off) path
    # compatible with an un-rebuilt worker whose warm_model_task predates the parameter.
    kw = {"num_ctx": _num_ctx} if _num_ctx > 0 else {}
    try:
        if task_tracker and await task_tracker.is_active(task_id):
            return
        await warm_model_task.kicker().with_task_id(task_id).kiq(
            ollama_url=_url,
            model_name=_model,
            keep_alive=_keep_alive,
            **kw,
        )
    except Exception as e:
        print(f"_fire_warm_task: failed to enqueue: {e}")


async def _fire_agent_task(agent_slug: str, vault_id=None):
    """Enqueue one agent run (enqueue-only; the agent loop runs in the worker).
    Deduped by stable job id so a manual click and the scheduler can't double-run."""
    from src.agent_registry import agent_job_id
    job_id = agent_job_id(agent_slug, vault_id)
    try:
        if task_tracker and await task_tracker.is_active(job_id):
            return
        if task_tracker:
            await task_tracker.delete_result(job_id)
        task = await run_agent_task.kicker().with_task_id(job_id).kiq(
            agent_slug=agent_slug, vault_id=vault_id)
        if task_tracker:
            await task_tracker.record_enqueue(task.task_id, "run_agent_task")
        print(f"_fire_agent_task: enqueued {job_id}")
    except Exception as e:
        print(f"_fire_agent_task: failed to enqueue: {e}")


# (The web-process gardener scheduler shim is gone: the generic agent scheduler
# in the WORKER - src/agent_scheduler.py - now fires any agent whose definition
# carries a `schedule:` rule.)


@asynccontextmanager
async def lifespan(app):
    # --- Startup ---
    global task_tracker, ollama_mgr

    # init.sql only runs on a FRESH volume, so a database carried across an
    # upgrade keeps its original schema. Reconcile before anything reads it.
    await asyncio.to_thread(schema_upgrade.reconcile_schema)

    print("Starting Kernel Reaper...")
    reaper_task = asyncio.create_task(kernel_reaper_loop())

    print(f"Starting LLM backend (provider={LLM_PROVIDER})...")
    ollama_mgr = create_llm_backend(
        url=OLLAMA_URL, model=OLLAMA_MODEL, keep_alive=OLLAMA_KEEP_ALIVE,
        num_ctx_request=OLLAMA_NUM_CTX, context_budget=OLLAMA_CONTEXT_BUDGET,
    )

    print("Starting Taskiq broker...")
    task_tracker = TaskTracker(REDIS_URL)
    await task_tracker.startup()
    await broker.startup()

    print("Startup complete")

    yield

    # --- Shutdown ---
    print("Stopping Kernel Reaper...")
    reaper_task.cancel()
    try:
        await reaper_task
    except asyncio.CancelledError:
        pass

    print("Unloading Ollama model...")
    if ollama_mgr:
        await ollama_mgr.unload_model()

    await jupyter_manager.prune_stale_kernels(-1)

    print("Shutting down Taskiq broker...")
    if task_tracker:
        await task_tracker.shutdown()
    await broker.shutdown()


# Cap comm_msg buffers at 32 MiB total (base64-encoded size) to prevent OOM
# from a runaway upload. FileUpload enforces its own limits via traits too.
_COMM_MSG_MAX_BUFFERS_B64 = 32 * 1024 * 1024


async def _relay_browser_comm_msg(kernel_conn, payload: dict, sender_ws):
    """Forward a browser comm_msg (with optional binary buffers) to the
    kernel and echo to other subscribed browsers."""
    comm_content = payload.get("content", {})
    buffers_b64 = payload.get("buffers_base64") or []
    if buffers_b64:
        total = sum(len(b) for b in buffers_b64)
        if total > _COMM_MSG_MAX_BUFFERS_B64:
            print(
                f"comm_msg rejected: base64 buffers total {total} bytes "
                f"exceeds cap {_COMM_MSG_MAX_BUFFERS_B64}"
            )
            return
        buffers = [base64.b64decode(b) for b in buffers_b64]
    else:
        buffers = None
    try:
        await kernel_conn.send_comm_msg(comm_content, buffers=buffers)
    except Exception as e:
        # Kernel WS is dead (e.g. idle-culled). Tell the sender so its
        # rendered widget outputs can show a disconnected banner, and
        # fan out the same notice to any other subscribers - they may
        # have widgets rendered too. Do NOT re-raise: the outer page
        # WebSocket loop should stay alive so the next Run click can
        # spawn a fresh kernel without renegotiating.
        print(f"comm_msg relay failed (kernel dead?): {e}")
        try:
            await sender_ws.send_text(json.dumps({
                "kernel_dead": True,
                "reason": "comm_msg_failed",
                "message": str(e),
            }))
        except Exception:
            pass
        try:
            await kernel_conn._broadcast_kernel_dead("comm_msg_failed")
        except Exception:
            pass
        return
    await kernel_conn.echo_comm_msg_to_others(
        comm_content, sender_ws, buffers_base64=buffers_b64 or None
    )


def _kernel_cwd_for_page(page_id: str) -> str | None:
    """Resolve a page URL path to the kernel's working directory.

    page_id is the browser's window.location.pathname, e.g.
    "/wiki/main/Sports/Baseball". The kernel is chdir'd into the folder that
    CONTAINS the page file (here vaults/main/Sports/) so a cell can read an
    attachment by the same relative name its markdown link uses. The path is
    absolute (vault_abs_root) so it is valid inside the Jupyter container, which
    mounts the vault tree at the same /app/app/vaults path the wiki server uses.
    Returns None for non-vault pages (the kernel then keeps its default home).
    """
    try:
        parts = [p for p in page_id.split("/") if p]
        if len(parts) < 2 or parts[0] != "wiki":
            return None
        vault = parts[1]
        if not vault_registry.vault_exists(vault):
            return None
        # Drop the final segment (the page name); the rest are parent folders.
        folder_parts = parts[2:-1]
        cwd = os.path.join(_vault_abs_root(vault), *folder_parts)
        return cwd if os.path.isdir(cwd) else _vault_abs_root(vault)
    except Exception:
        return None


async def jupyter_websocket_endpoint(websocket: WebSocket):
    await websocket.accept()

    kernel_conn = None

    try:
        # 1. Initial handshake: browser sends {action: "connect", page_id: "..."}
        data = await websocket.receive_json()
        page_id = data.get("page_id")

        if not page_id:
            await websocket.send_text(json.dumps({"error": "Missing page_id"}))
            await websocket.close()
            return

        # Folder a freshly spawned kernel is chdir'd into (page's vault folder),
        # so cell code resolves attachments by their relative link name.
        kernel_cwd = _kernel_cwd_for_page(page_id)
        # Vault slug seeds the injected `wiki` client so cell code can query the
        # page's own vault index. Parsed the same way as _kernel_cwd_for_page.
        _pid_parts = [p for p in page_id.split("/") if p]
        kernel_vault = (
            _pid_parts[1]
            if len(_pid_parts) >= 2 and _pid_parts[0] == "wiki"
            else None
        )

        # 2. Get persistent kernel connection
        kernel_conn = await jupyter_manager.get_or_create_connection(
            page_id, kernel_cwd, kernel_vault
        )
        kernel_conn.subscribe(websocket)
        await kernel_conn.replay_state_to(websocket)

        await websocket.send_text(
            json.dumps({"status": "connected", "page_id": page_id})
        )

        # 3. Message loop: browser sends actions, we process them.
        # The kernel is serial, so a Run click that arrives while another cell
        # is still executing is deferred onto pending_executes (see the inner
        # loop) rather than dropped. Drain those queued executes before reading
        # the next socket message so a second cell isn't lost / its Run button
        # left stuck disabled.
        pending_executes = []
        while True:
            if pending_executes:
                data = pending_executes.pop(0)
            else:
                data = await websocket.receive_json()
            action = data.get("action")

            if action == "execute":
                code = data.get("code", "")
                cell_id = data.get("cell_id", "")
                try:
                    msg_id, queue = await kernel_conn.execute(code)
                except Exception as exec_err:
                    # Kernel-side WS is dead (typically idle-culled). Recover
                    # transparently so a single Run click after the
                    # dead-kernel banner is enough: unsubscribe from the
                    # dead connection, ask the manager for a fresh one
                    # (which spawns a new upstream kernel if the old one
                    # was culled), resubscribe, and retry the execute once.
                    print(f"Kernel execute failed, attempting recovery: {exec_err}")
                    try:
                        kernel_conn.unsubscribe(websocket)
                    except Exception:
                        pass
                    try:
                        kernel_conn = await jupyter_manager.get_or_create_connection(page_id, kernel_cwd, kernel_vault)
                        kernel_conn.subscribe(websocket)
                        msg_id, queue = await kernel_conn.execute(code)
                    except Exception as retry_err:
                        print(f"Kernel recovery failed: {retry_err}")
                        try:
                            await websocket.send_text(json.dumps({
                                "execution_complete": True,
                                "cell_id": cell_id,
                                "error": "kernel_unavailable",
                                "kernel_dead": True,
                                "message": str(retry_err),
                            }))
                        except Exception:
                            pass
                        break

                # During execution, we must read from BOTH the kernel
                # message queue and the browser WebSocket concurrently.
                # The kernel may send input_request (Python input()) which
                # requires an input_reply from the browser before it
                # continues. A background task feeds browser messages
                # into browser_queue so we can race both with asyncio.wait.
                browser_queue = asyncio.Queue()

                async def _read_browser():
                    try:
                        while True:
                            msg = await websocket.receive_json()
                            await browser_queue.put(msg)
                    except Exception:
                        await browser_queue.put(None)

                reader_task = asyncio.create_task(_read_browser())
                try:
                    execution_done = False
                    while not execution_done:
                        kernel_task = asyncio.ensure_future(queue.get())
                        browser_task = asyncio.ensure_future(browser_queue.get())

                        done, pending = await asyncio.wait(
                            {kernel_task, browser_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                            try:
                                await task
                            except (asyncio.CancelledError, Exception):
                                pass

                        for task in done:
                            result = task.result()

                            if task is kernel_task:
                                msg_type = result["msg_type"]
                                content = result["content"]

                                # input_request arrives on stdin, but prior
                                # print() output arrives on iopub - a separate
                                # ZMQ channel that may be slightly behind.
                                # The kernel is now blocked, so yield briefly
                                # to let any in-flight iopub messages land in
                                # the queue, then drain them before showing
                                # the input prompt.
                                if msg_type == "input_request":
                                    await asyncio.sleep(0.05)
                                    while not queue.empty():
                                        prior = queue.get_nowait()
                                        prior_fmt = format_execution_message(prior, cell_id)
                                        if prior_fmt:
                                            await websocket.send_text(json.dumps(prior_fmt))

                                formatted = format_execution_message(result, cell_id)
                                if formatted:
                                    await websocket.send_text(json.dumps(formatted))

                                if msg_type == "status" and content["execution_state"] == "idle":
                                    kernel_conn.remove_pending_execution(msg_id)
                                    await websocket.send_text(
                                        json.dumps({"execution_complete": True, "cell_id": cell_id})
                                    )
                                    execution_done = True

                            elif task is browser_task:
                                if result is None:
                                    execution_done = True
                                    break
                                browser_action = result.get("action")
                                if browser_action == "input_reply":
                                    await kernel_conn.send_input_reply(result.get("value", ""))
                                elif browser_action == "comm_msg":
                                    await _relay_browser_comm_msg(kernel_conn, result, websocket)
                                elif browser_action == "execute":
                                    # A second Run click while this cell is still
                                    # executing: queue it (the kernel is serial)
                                    # instead of dropping it. Dropping it here is
                                    # what left the second cell's Run button
                                    # stuck disabled with no output.
                                    pending_executes.append(result)
                finally:
                    reader_task.cancel()
                    try:
                        await reader_task
                    except (asyncio.CancelledError, Exception):
                        pass
                    # A Run click can land in browser_queue right as this cell
                    # finishes (before the inner loop reads it); preserve any
                    # such queued executes so they still run. Other stale
                    # in-flight messages (input_reply/comm_msg) are dropped.
                    while not browser_queue.empty():
                        leftover = browser_queue.get_nowait()
                        if leftover and leftover.get("action") == "execute":
                            pending_executes.append(leftover)

            elif action == "comm_msg":
                # Browser sending widget interaction to kernel
                await _relay_browser_comm_msg(kernel_conn, data, websocket)

    except Exception as e:
        print(f"WebSocket error: {e}")

    finally:
        if kernel_conn:
            kernel_conn.unsubscribe(websocket)
        try:
            await websocket.close()
        except Exception:
            pass


async def manage_jupyter(request: Request):
    # /manage/jupyter
    k_list = jupyter_manager.list_kernels()
    k_list_response = await k_list

    qp = request.query_params
    if kernel_to_delete := qp.get("delete"):
        await jupyter_manager.delete_kernel_by_id(kernel_to_delete)
        return RedirectResponse(f"/manage/jupyter")

    raw_markdown = ""
    if isinstance(k_list_response, list) and len(k_list_response) > 0:
        raw_markdown = """Action    | Name  | State | Idle | Connections | Pages\n----- | ----- | ----- | ----- | ----- | -----\n"""
        for each_k in k_list_response:
            raw_markdown += f"""[❌](/manage/jupyter?delete={each_k['id']} ) | {each_k['name']} | {each_k['execution_state']} | {each_k['idle']} | {str(each_k['connections'])} | {str(each_k['pages'])} | \n"""
    else:
        raw_markdown = "No kernels."

    ###################################################
    doc_template = jinja_env.get_template("document.html")
    doc_data = {}
    doc_data["has_jupyter"] = True
    doc_data["unlinked_title"] = "Kernel Management"

    wd = WikiDoc("/temp/manage_jupyter")
    wd.set_content(raw_markdown)
    markdown_doc = MarkdownDocTransform(wd)

    html = await asyncio.to_thread(markdown_doc.get_content)
    doc_data["document"] = html
    response_content = doc_template.render(doc_data)
    return HTMLResponse(response_content)


async def manage_ollama(request: Request):
    # /manage/ollama
    qp = request.query_params
    action = qp.get("action")

    if action == "warm":
        await _fire_warm_task(model=ollama_mgr.model)
        return RedirectResponse("/manage/ollama")
    if action == "unload":
        await ollama_mgr.unload_model()
        return RedirectResponse("/manage/ollama")
    if action == "warm_embed":
        await _fire_warm_task(model=OLLAMA_EMBED_MODEL, keep_alive=OLLAMA_EMBED_KEEP_ALIVE)
        return RedirectResponse("/manage/ollama")
    if action == "unload_embed":
        await ollama_mgr.unload_any_model(OLLAMA_EMBED_MODEL)
        return RedirectResponse("/manage/ollama")
    if action == "set_model":
        new_model = qp.get("model", "").strip()
        if new_model:
            await ollama_mgr.set_model(new_model)
        return RedirectResponse("/manage/ollama")

    status = await ollama_mgr.get_status()
    available = await ollama_mgr.list_available_models_detailed()

    # --- Chat model section ---
    # `reachable` is only reported by backends that can distinguish "server down" from
    # "nothing loaded" (LemonadeBackend). False => don't trust the loaded/idle fields.
    unreachable = status.get("reachable") is False
    loaded_str = (
        "⚠️ Server unreachable" if unreachable
        else "Loaded" if status["loaded"] else "Not loaded"
    )
    idle_str = (
        f'{status["idle_seconds"]}s' if status["idle_seconds"] is not None else "N/A"
    )

    raw_markdown = ""
    if unreachable:
        raw_markdown += (
            f"> ⚠️ **Could not reach the LLM server at `{status['url']}`.** "
            "Loaded-model and memory details below are unavailable until it responds.\n\n"
        )
    raw_markdown += f"""## Chat Model

| Property | Value |
|----------|-------|
| Model | `{status['model']}` |
| URL | `{status['url']}` |
| keep_alive | `{status['keep_alive']}` |
| Status | **{loaded_str}** |
| Idle | {idle_str} |
"""

    if status.get("context_length"):
        raw_markdown += f"| Context window | {status['context_length']:,} tokens |\n"
    if status.get("size"):
        size_mb = status["size"] / (1024 * 1024)
        raw_markdown += f"| Memory | {size_mb:.0f} MB |\n"
    if status.get("size_vram"):
        vram_mb = status["size_vram"] / (1024 * 1024)
        raw_markdown += f"| VRAM | {vram_mb:.0f} MB |\n"
    if status.get("expires_at"):
        raw_markdown += f"| Expires | {status['expires_at']} |\n"
    # Lemonade path: per-model VRAM is not truthful on unified memory, so the backend
    # reports device + HOST-level figures from /v1/system-stats instead.
    if status.get("device"):
        raw_markdown += f"| Device | `{status['device']}` |\n"
    if status.get("vram_gb") is not None:
        raw_markdown += f"| GPU memory (host) | {status['vram_gb']:.1f} GB |\n"
    if status.get("memory_gb") is not None:
        raw_markdown += f"| System memory (host) | {status['memory_gb']:.1f} GB |\n"

    raw_markdown += "\n[Warm Model](/manage/ollama?action=warm) | [Unload Model](/manage/ollama?action=unload)\n"

    # --- Embedding model section ---
    embed_loaded = await ollama_mgr.is_any_model_loaded(OLLAMA_EMBED_MODEL)
    embed_info = (
        await ollama_mgr.get_model_info(OLLAMA_EMBED_MODEL) if embed_loaded else None
    )
    embed_loaded_str = "Loaded" if embed_loaded else "Not loaded"

    raw_markdown += f"""
## Embedding Model

| Property | Value |
|----------|-------|
| Model | `{OLLAMA_EMBED_MODEL}` |
| Status | **{embed_loaded_str}** |
"""

    if embed_info:
        if embed_info.get("size"):
            size_mb = embed_info["size"] / (1024 * 1024)
            raw_markdown += f"| Memory | {size_mb:.0f} MB |\n"
        if embed_info.get("size_vram"):
            vram_mb = embed_info["size_vram"] / (1024 * 1024)
            raw_markdown += f"| VRAM | {vram_mb:.0f} MB |\n"
        if embed_info.get("expires_at"):
            raw_markdown += f"| Expires | {embed_info['expires_at']} |\n"

    raw_markdown += "\n[Warm Embedding Model](/manage/ollama?action=warm_embed) | [Unload Embedding Model](/manage/ollama?action=unload_embed)\n"

    # --- Switch chat model section ---
    raw_markdown += "\n## Switch Chat Model\n\n"
    if available:
        for m in available:
            m_name = m["name"]
            m_psize = m["parameter_size"]
            m_qlevel = m["quantization_level"]
            is_embedding = m.get("is_embedding", False)
            caps = m.get("capabilities", [])
            badges = "".join(f" `[{c}]`" for c in caps) if caps else ""
            if m_name == ollama_mgr.model:
                raw_markdown += f"- **`{m_name}`** (active) `({m_psize}, {m_qlevel})`{badges}\n"
            elif is_embedding:
                raw_markdown += f"- `{m_name}` `({m_psize}, {m_qlevel})`{badges}\n"
            else:
                raw_markdown += f"- [`{m_name}`](/manage/ollama?action=set_model&model={m_name}) `({m_psize}, {m_qlevel})`{badges}\n"
    else:
        raw_markdown += "Could not retrieve model list from Ollama.\n"

    raw_markdown += """
## Download Model

<div id="ollama-pull-section">
  <div class="ollama-pull-row">
    <input type="text" id="ollama_pull_input" placeholder="e.g. llama3.2:3b" />
    <button type="button" id="ollama_pull_btn" onclick="pullOllamaModel()">Pull</button>
  </div>
  <div id="ollama_pull_status" class="ollama-pull-status" style="display:none;">
    <div id="ollama_pull_status_text"></div>
    <div class="ollama-pull-progress-bar">
      <div id="ollama_pull_progress_fill" class="ollama-pull-progress-fill"></div>
    </div>
    <div id="ollama_pull_percent"></div>
  </div>
</div>

<script>
(function() {
  'use strict';

  function getEls() {
    return {
      input: document.getElementById('ollama_pull_input'),
      btn: document.getElementById('ollama_pull_btn'),
      statusDiv: document.getElementById('ollama_pull_status'),
      statusText: document.getElementById('ollama_pull_status_text'),
      progressFill: document.getElementById('ollama_pull_progress_fill'),
      percentText: document.getElementById('ollama_pull_percent')
    };
  }

  function handleEvent(evt, els) {
    if (evt.error) {
      els.statusText.textContent = 'Error: ' + evt.error;
      els.progressFill.style.width = '0%';
      els.percentText.textContent = '';
      els.btn.disabled = false;
      return true;
    }
    if (evt.done) {
      els.statusText.textContent = 'Done! Reloading...';
      els.progressFill.style.width = '100%';
      els.percentText.textContent = '100%';
      setTimeout(function() { window.location.reload(); }, 1000);
      return true;
    }
    if (evt.status) {
      els.statusText.textContent = evt.status;
    }
    if (evt.total && evt.completed != null) {
      var pct = Math.round((evt.completed / evt.total) * 100);
      els.progressFill.style.width = pct + '%';
      els.percentText.textContent = pct + '% (' +
        Math.round(evt.completed / 1048576) + ' / ' +
        Math.round(evt.total / 1048576) + ' MB)';
    }
    return false;
  }

  async function readSSE(resp, els) {
    var reader = resp.body.getReader();
    var decoder = new TextDecoder();
    var buffer = '';
    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      var lines = buffer.split('\\n');
      buffer = lines.pop();
      for (var i = 0; i < lines.length; i++) {
        if (!lines[i].startsWith('data: ')) continue;
        var evt;
        try { evt = JSON.parse(lines[i].slice(6)); } catch(e) { continue; }
        if (handleEvent(evt, els)) return;
      }
    }
  }

  async function pullOllamaModel() {
    var els = getEls();
    var model = els.input.value.trim();
    if (!model) return;
    els.btn.disabled = true;
    els.statusDiv.style.display = 'block';
    els.statusText.textContent = 'Starting pull...';
    els.progressFill.style.width = '0%';
    els.percentText.textContent = '';
    try {
      var resp = await fetch('/api/ollama/pull', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model: model })
      });
      await readSSE(resp, els);
    } catch(e) {
      els.statusText.textContent = 'Connection error: ' + e.message;
    }
    els.btn.disabled = false;
  }

  async function checkActivePull() {
    try {
      var resp = await fetch('/api/ollama/pull/status');
      var data = await resp.json();
      if (!data.active) return;
      var els = getEls();
      els.input.value = data.model;
      els.btn.disabled = true;
      els.statusDiv.style.display = 'block';
      if (data.progress && data.progress.status) {
        handleEvent(data.progress, els);
      } else {
        els.statusText.textContent = 'Pulling ' + data.model + '...';
      }
      var stream = await fetch('/api/ollama/pull/stream');
      await readSSE(stream, els);
      els.btn.disabled = false;
    } catch(e) {}
  }

  window.pullOllamaModel = pullOllamaModel;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', checkActivePull);
  } else {
    checkActivePull();
  }
})();
</script>

"""

    doc_template = jinja_env.get_template("document.html")
    doc_data = {}
    doc_data["unlinked_title"] = "Ollama Management"

    wd = WikiDoc("/temp/manage_ollama")
    wd.set_content(raw_markdown)
    markdown_doc = MarkdownDocTransform(wd)
    html = await asyncio.to_thread(markdown_doc.get_content)
    doc_data["document"] = html
    response_content = doc_template.render(doc_data)
    return HTMLResponse(response_content)


# /api/ollama/pull - background pull with shared progress state
_active_pull: dict | None = None  # {"model": str, "progress": dict, "task": asyncio.Task, "event": asyncio.Event}


async def _run_pull(model: str):
    """Background coroutine that drives the pull and updates shared state."""
    global _active_pull
    try:
        async for progress in ollama_mgr.pull_model(model):
            if _active_pull:
                _active_pull["progress"] = progress
                _active_pull["event"].set()
                _active_pull["event"].clear()
        if _active_pull:
            _active_pull["progress"] = {"done": True}
            _active_pull["event"].set()
    except Exception as e:
        if _active_pull:
            _active_pull["progress"] = {"error": str(e)}
            _active_pull["event"].set()
    finally:
        # Keep state around briefly so late-arriving poll requests see the result
        await asyncio.sleep(5)
        _active_pull = None


async def ollama_pull_endpoint(request: Request):
    """POST /api/ollama/pull - start a model pull (or attach to an existing one)."""
    global _active_pull
    data = await request.json()
    model = data.get("model", "").strip()
    if not model:
        return JSONResponse({"error": "No model specified"}, status_code=400)

    # If a pull is already running for a different model, reject
    if _active_pull and not _active_pull["task"].done() and _active_pull["model"] != model:
        return JSONResponse(
            {"error": f"Already pulling {_active_pull['model']}"},
            status_code=409,
        )

    # Start a new pull if none is active
    if not _active_pull or _active_pull["task"].done():
        event = asyncio.Event()
        task = asyncio.create_task(_run_pull(model))
        _active_pull = {"model": model, "progress": {}, "task": task, "event": event}

    return StreamingResponse(_pull_event_stream(), media_type="text/event-stream")


async def _pull_event_stream():
    """Yield SSE events by watching the shared _active_pull state."""
    last_progress = None
    while _active_pull and not _active_pull["task"].done():
        try:
            await asyncio.wait_for(asyncio.shield(_active_pull["event"].wait()), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        progress = _active_pull["progress"] if _active_pull else None
        if progress and progress is not last_progress:
            last_progress = progress
            yield f"data: {json.dumps(progress)}\n\n"
            if progress.get("done") or progress.get("error"):
                return
    # Final state if we missed it
    if _active_pull and _active_pull["progress"]:
        progress = _active_pull["progress"]
        if progress is not last_progress:
            yield f"data: {json.dumps(progress)}\n\n"


async def ollama_pull_status_endpoint(request: Request):
    """GET /api/ollama/pull/status - check if a pull is active, returns JSON."""
    if _active_pull and not _active_pull["task"].done():
        return JSONResponse({
            "active": True,
            "model": _active_pull["model"],
            "progress": _active_pull["progress"],
        })
    return JSONResponse({"active": False})


async def ollama_pull_stream_endpoint(request: Request):
    """GET /api/ollama/pull/stream - attach to an in-progress pull's SSE stream."""
    if not _active_pull or _active_pull["task"].done():
        async def no_pull():
            yield f"data: {json.dumps({'done': True})}\n\n"
        return StreamingResponse(no_pull(), media_type="text/event-stream")
    return StreamingResponse(_pull_event_stream(), media_type="text/event-stream")


# Keys carried purely for machine bookkeeping - never interesting to a human
# reading the task table, so we drop them from the friendly summary.
# Moved to src/task_tracker.py so the failure classifier can share one
# renderer with this page - a second copy would drift in wording.
from src.task_tracker import summarize_task_result as _summarize_task_result


# ---------------------------------------------------------------------------
# /manage/monitor - the one place that says whether anything is broken
# ---------------------------------------------------------------------------
#
# Every fault found in the July/August 2026 background-processing work was
# invisible until a human went looking, and three were found only because one
# happened to notice an anomaly in passing. The app already surfaces work WAITING
# for you (the staged-proposal badge); this is the counterpart for work that
# BROKE.
#
# Structured as independent section builders rather than one long function on
# purpose: this is the page you load DURING an incident, so a section whose data
# source is the thing that is down must degrade to a line of text while the rest
# of the page still renders.

async def _safe_section(title: str, builder) -> str:
    try:
        return await builder()
    except Exception as e:
        return f"## {title}\n\nUnavailable: {e}\n\n"


async def _monitor_failures(ack_id: int | None = None) -> str:
    from config import FAILURE_LOG_TTL_DAYS
    from src import failure_log

    if ack_id is not None:
        await asyncio.to_thread(failure_log.ack, ack_id)

    rows = await asyncio.to_thread(failure_log.recent_failures, FAILURE_LOG_TTL_DAYS)
    md = "## Failures\n\n"
    open_rows = [r for r in rows if r["status"] == "open"]
    if not rows:
        md += (f"Nothing has failed in the last {FAILURE_LOG_TTL_DAYS} days.\n\n")
        return md
    if not open_rows:
        md += "*Nothing open.* Recently resolved or acknowledged:\n\n"
    md += "| | What | Where | Detail | Seen | Last |\n|---|---|---|---|---|---|\n"
    for r in rows[:50]:
        mark = "**!**" if r["status"] == "open" and r["badge"] else ""
        times = f"x{r['occurrences']}" if r["occurrences"] > 1 else ""
        where = _md_esc(r["vault_id"] or "-")
        subj = _md_esc(str(r["subject"])[:60])
        act = (f"[ack](/manage/monitor?ack={r['id']})"
               if r["status"] == "open" else r["status"])
        # No _md_esc inside the code span: backticks already suppress markdown,
        # so escaping there renders the backslash literally ("agent\\_run").
        md += (f"| {mark} | `{r['kind']}` {subj} | {where} "
               f"| {_md_esc(str(r['detail'])[:90])} | {times} "
               f"| {timefmt.ago(r['last_seen_age_s'])} {act} |\n")
    md += ("\nA row is one *distinct broken thing*, not one event - repeats bump the "
           "count. Acknowledging clears it from the badge; if it fails again a fresh "
           "row opens and the badge relights.\n\n")
    return md


async def _monitor_health() -> str:
    from src.health import collect_health

    checks = await collect_health(ollama_mgr)
    md = "## Health\n\n| Dependency | State | Detail |\n|---|---|---|\n"

    def row(name, ok, detail):
        state = "ok" if ok else ("**down**" if ok is False else "unknown")
        return f"| {name} | {state} | {detail} |\n"

    md += row("Postgres", checks["postgres"].get("ok"),
              _md_esc(str(checks["postgres"].get("error", "") or "")))
    md += row("Redis", checks["redis"].get("ok"),
              _md_esc(str(checks["redis"].get("error", "") or "")))
    w = checks["worker"]
    md += row("Worker", w.get("ok"),
              (f"last tick {timefmt.ago(w.get('last_tick_age_s'))}" if w.get("ok")
               else _md_esc(str(w.get("error", "")))))
    o = checks["ollama"]
    _detail = (f"{o.get('provider')} - chat "
               f"{'ok' if o.get('chat_model', {}).get('present') else '**missing**'}, "
               f"embed {'ok' if o.get('embed_model', {}).get('present') else '**missing**'}"
               if "chat_model" in o else _md_esc(str(o.get("error", ""))))
    md += row("LLM backend", o.get("reachable"), _detail)
    return md + "\n"


async def _monitor_gaps(recheck: bool = False) -> str:
    """Disk-vs-index reconcile, from the worker's hourly cache.

    Rendered WITH ITS AGE. A cached number that says when it was taken is more
    honest than a live one with no history - and a live 0.7s glob on every render
    of a page you leave open is not affordable.
    """
    import json

    from src.agent_scheduler import GAPS_KEY, store_gaps

    md = "## Silent gaps\n\n"
    _r = get_async_redis()
    try:
        if recheck:
            # The manual check REPLACES the cache rather than rendering beside it:
            # a click that shows cleared gaps and then reverts on the next plain
            # load reads as the reindex having failed.
            from src.rag_indexer import find_unindexed_documents
            data = await find_unindexed_documents()
            age = 0.0
            await store_gaps(_r, data)
        else:
            raw = await _r.get(GAPS_KEY)
            if not raw:
                return (md + "Not computed yet - the worker refreshes this hourly. "
                        "[Check now](/manage/monitor?recheck=1)\n\n")
            data = json.loads(raw)
            age = time.time() - float(data.get("ts") or time.time())
    finally:
        await _r.close()

    total = data.get("total_missing", 0)
    if not total:
        md += (f"All **{data.get('checked', 0)}** markdown files have an index entry "
               f"*(as of {timefmt.ago(age)})*. ")
    else:
        md += (f"**{total}** of {data.get('checked', 0)} markdown files have NO index "
               f"entry *(as of {timefmt.ago(age)})* - they are invisible to search and chat:\n\n")
        for vid, paths in sorted((data.get("missing") or {}).items()):
            md += (f"* **{vid}** ({len(paths)}) - "
                   f"[reindex](/manage/tasks?reindex=1&vault={vid})\n")
            for pth in paths[:10]:
                md += f"    * {_md_code(pth)}\n"
        md += "\n"

    # The other direction: rows whose file is gone. They stay searchable and serve
    # whatever the page last said, so they are worth showing even when nothing is
    # missing. A reindex is what clears them (it runs prune_deleted_documents).
    stale_total = data.get("total_stale", 0)
    if stale_total:
        md += (f"\n**{stale_total}** index entr{'y' if stale_total == 1 else 'ies'} "
               f"point at files that no longer exist - still searchable, serving "
               f"whatever the page last said:\n\n")
        for vid, paths in sorted((data.get("stale") or {}).items()):
            md += (f"* **{vid}** ({len(paths)}) - "
                   f"[reindex to clear](/manage/tasks?reindex=1&vault={vid})\n")
            for pth in paths[:10]:
                md += f"    * {_md_code(pth)}\n"
        md += "\n"
    md += "[Check now](/manage/monitor?recheck=1)\n\n"
    return md


async def _monitor_contention() -> str:
    """The two shared serial resources. Moved here from /manage/tasks: they are
    health signals, not queue actions."""
    # LLM gate contention. Always rendered (one HGETALL, sub-millisecond) because
    # this is the queue that actually constrains throughput: background LLM work
    # queues in taskiq and THEN queues again here, and only this half is capacity-1.
    # A task can sit in "in progress" while parked on the gate doing nothing.
    gate_md = ""
    try:
        from config import OLLAMA_MAX_CONCURRENCY
        from src.llm_gate import read_gate_stats
        _g = await read_gate_stats()
        if _g["labels"]:
            _live = _g["live"]
            _held = _live.get("holder")
            _depth = int(_live.get("depth") or 0)
            gate_md = "## LLM gate\n\n"
            if _held:
                _since = float(_live.get("holder_since") or 0)
                _for = f" for {time.time() - _since:.0f}s" if _since else ""
                gate_md += (f"**Now:** `{_held}` holds the LLM{_for}"
                            f"{f', {_depth - 1} waiting' if _depth > 1 else ', nothing waiting'}.\n\n")
            else:
                gate_md += "**Now:** idle.\n\n"
            _win = _g.get("since")     # not _since: that's the holder's clock above
            _window = (f", over the last {(time.time() - _win)/3600:.1f}h"
                       if _win else "")
            gate_md += ("Background LLM work is capped at "
                        f"`OLLAMA_MAX_CONCURRENCY={OLLAMA_MAX_CONCURRENCY}`, so this is a "
                        "queue behind the task queue. *Wait* is time spent blocked here; "
                        "*hold* is time actually using the LLM. Counters accumulate "
                        f"until reset{_window}.\n\n")
            gate_md += "| Caller | Calls | Total hold | Avg hold | Total wait | Avg wait | Worst wait |\n"
            gate_md += "|--------|------:|-----------:|---------:|-----------:|---------:|-----------:|\n"
            for _r in _g["labels"]:
                gate_md += (f"| `{_r['label']}` | {_r['count']} "
                            f"| {_r['hold_ms']/1000:.1f}s | {_r['avg_hold_ms']/1000:.1f}s "
                            f"| {_r['wait_ms']/1000:.1f}s | {_r['avg_wait_ms']/1000:.1f}s "
                            f"| {_r['max_wait_ms']/1000:.1f}s |\n")
            _tw = sum(r["wait_ms"] for r in _g["labels"])
            _th = sum(r["hold_ms"] for r in _g["labels"])
            gate_md += (f"\nTotal: **{_th/1000:.0f}s** using the LLM, "
                        f"**{_tw/1000:.0f}s** waiting for it. "
                        "[Reset counters](/manage/tasks?reset_gate=1)\n\n")
    except Exception as e:
        gate_md = f"## LLM gate\n\nStats unavailable: {e}\n\n"

    # The SECOND shared bottleneck. Agents serialize on one global lock across all
    # agents and vaults, and a run shows as "in progress" whether it is working or
    # was deferred - the same blind spot the gate had before it was instrumented.
    lock_md = ""
    try:
        from src.llm_gate import read_runlock_stats
        _l = await read_runlock_stats()
        if _l["agents"]:
            lock_md = "## Agent run lock\n\n"
            lock_md += ("Agents run **one at a time**, across every vault. A run "
                        "that finds the lock held now *defers* and retries on the "
                        "next scheduler tick instead of occupying a worker slot "
                        "waiting - so deferrals are cheap and expected, not "
                        "errors.\n\n")
            lock_md += "| Agent | Runs | Deferrals | Total hold | Avg hold |\n"
            lock_md += "|-------|-----:|----------:|-----------:|---------:|\n"
            for _a in _l["agents"]:
                lock_md += (f"| `{_a['slug']}` | {_a['runs']} | {_a['deferrals']} "
                            f"| {_a['hold_ms']/1000:.0f}s "
                            f"| {_a['avg_hold_ms']/1000:.0f}s |\n")
            _th = sum(a["hold_ms"] for a in _l["agents"])
            _td = sum(a["deferrals"] for a in _l["agents"])
            lock_md += (f"\nTotal: **{_th/1000/60:.0f} min** of agent runs, "
                        f"**{_td}** deferrals.\n\n")
    except Exception as e:
        lock_md = f"## Agent run lock\n\nStats unavailable: {e}\n\n"

    return gate_md + lock_md


# /manage/monitor
async def manage_monitor(request: Request):
    """Health + failures in one place, with a nav badge that pulls you here."""
    qp = request.query_params
    if qp.get("reset_gate"):
        from src.llm_gate import reset_gate_stats
        await reset_gate_stats()
        return RedirectResponse(url="/manage/monitor", status_code=303)

    _ack = qp.get("ack")
    ack_id = int(_ack) if (_ack or "").isdigit() else None
    recheck = bool(qp.get("recheck"))

    parts = [
        await _safe_section("Failures", lambda: _monitor_failures(ack_id)),
        await _safe_section("Health", _monitor_health),
        await _safe_section("Silent gaps", lambda: _monitor_gaps(recheck)),
        await _safe_section("Contention", _monitor_contention),
    ]
    raw_markdown = "".join(parts)
    raw_markdown += ("---\n\n*Queue state and maintenance actions live on "
                     "[Tasks](/manage/tasks). Staged agent proposals awaiting your "
                     "review are in the [agent inbox](/agents).*\n")

    doc_template = jinja_env.get_template("document.html")
    doc_data = {"unlinked_title": "Monitor"}
    wd = WikiDoc("/temp/manage_monitor")
    wd.set_content(raw_markdown)
    markdown_doc = MarkdownDocTransform(wd)
    doc_data["document"] = await asyncio.to_thread(markdown_doc.get_content)
    return HTMLResponse(doc_template.render(doc_data))


# /manage/tasks
async def manage_tasks(request: Request):
    """Display task queue status: in-progress, queued, and recent results."""

    # Optional vault scope for the bulk actions. Absent/"all" = every vault (job id
    # reindex:all / metadata:all); a slug scopes to one vault (reindex:vault:{slug}).
    req_vault = (request.query_params.get("vault") or "").strip()
    if req_vault and req_vault != "all" and not vault_registry.vault_exists(req_vault):
        return JSONResponse({"error": f"Unknown vault: {req_vault}"}, status_code=404)
    scope_vault = req_vault if (req_vault and req_vault != "all") else None

    # Handle reindex request
    if request.query_params.get("reindex"):
        job_id = "reindex:all" if scope_vault is None else f"reindex:vault:{scope_vault}"
        if not await task_tracker.is_active(job_id):
            try:
                await task_tracker.delete_result(job_id)
                task = await reindex_all_task.kicker().with_task_id(job_id).kiq(
                    vault_id=scope_vault,
                )
                await task_tracker.record_enqueue(task.task_id, "reindex_all_task")
            except Exception as e:
                print(f"Failed to enqueue reindex_all_task: {e}")
        return RedirectResponse(url="/manage/tasks", status_code=303)

    # Handle generate_metadata request
    if request.query_params.get("generate_metadata"):
        job_id = "metadata:all" if scope_vault is None else f"metadata:vault:{scope_vault}"
        if not await task_tracker.is_active(job_id):
            try:
                force = request.query_params.get("force", "").lower() in ("1", "true", "yes")
                await task_tracker.delete_result(job_id)
                task = await generate_all_metadata_task.kicker().with_task_id(job_id).kiq(
                    force=force, vault_id=scope_vault,
                )
                await task_tracker.record_enqueue(task.task_id, "generate_all_metadata_task")
            except Exception as e:
                print(f"Failed to enqueue generate_all_metadata_task: {e}")
        return RedirectResponse(url="/manage/tasks", status_code=303)

    # Handle agent-run request (agents = markdown definitions in the system vault)
    if request.query_params.get("agent"):
        _slug = request.query_params.get("agent", "").strip()
        from src import agent_registry
        if agent_registry.get_agent(_slug) is None:
            return JSONResponse({"error": f"Unknown agent: {_slug}"}, status_code=404)
        await _fire_agent_task(_slug, vault_id=scope_vault)
        return RedirectResponse(url="/manage/tasks", status_code=303)

    raw_markdown = "## Actions\n\n"
    raw_markdown += "These are complementary maintenance actions - two halves of the same "
    raw_markdown += "pipeline that a normal edit runs automatically. **Generate metadata** "
    raw_markdown += "writes LLM tags/summary into the files; **Reindex** embeds the current "
    raw_markdown += "file contents. Because the summary is embedded, run them in order: "
    raw_markdown += "*Generate metadata → then Reindex* when you need both (e.g. after a model change).\n\n"
    raw_markdown += "Each action can target **all vaults** or a **single vault** "
    raw_markdown += "(vaults are isolated - reindexing one never touches another).\n\n"
    raw_markdown += "* [Reindex all documents - ALL vaults](/manage/tasks?reindex=1) "
    raw_markdown += "Rebuilds RAG embeddings AND the wiki link graph "
    raw_markdown += "(edges) for every page - including pages with `Index: False`, whose "
    raw_markdown += "links are recorded for the graph view without being embedded. "
    raw_markdown += "Also **prunes orphans**: index entries for files deleted outside the app "
    raw_markdown += "(e.g. removed from disk while the watcher was off) are cleaned out so they "
    raw_markdown += "stop appearing in the graph and search. "
    raw_markdown += "Does **not** regenerate LLM tags/summary - use the metadata actions below for that.\n"
    raw_markdown += "* [Generate missing metadata - ALL vaults](/manage/tasks?generate_metadata=1) "
    raw_markdown += "Generates LLM tags/summary only for pages missing them (writes to each file, no embed).\n"
    raw_markdown += "* [Force regenerate ALL metadata - ALL vaults](/manage/tasks?generate_metadata=1&force=1) "
    raw_markdown += "Regenerates LLM tags/summary for every page (writes to each file, no embed). "
    raw_markdown += "Follow with a Reindex to push the new summaries into search.\n"
    raw_markdown += "* [Check for unindexed files - ALL vaults](/manage/tasks?reconcile=1) "
    raw_markdown += "**Reports only; changes nothing.** Lists markdown on disk with no index entry - "
    raw_markdown += "the mirror image of the orphan prune above. A file lands here if its indexing "
    raw_markdown += "task was lost (a failed enqueue, a worker restarted mid-task) or if it arrived "
    raw_markdown += "while the wiki was down. Reindex the vault to fix any it finds.\n"
    # Per-vault action links (same actions, scoped to one vault via ?vault=slug).
    # System vaults are listed too, but with ONLY the reindex cell live: they are
    # RAG-indexed and searchable within themselves, so reindex genuinely applies,
    # while LLM metadata never will (blessed files are human-authored - refused in
    # rag_indexer.generate_frontmatter). Showing the row with the metadata cells
    # marked n/a states that rule; omitting the vault left /manage/monitor linking
    # a reindex for a vault this table acted like it did not have.
    _vaults = vault_registry.list_vaults()
    _system_vaults = [v for v in vault_registry.list_vaults(include_system=True)
                      if vault_registry.is_system_vault(v["vault_id"])]
    if _vaults or _system_vaults:
        raw_markdown += "\n### Per-vault\n\n"
        raw_markdown += "| Vault | Reindex | Generate missing | Force regenerate |\n"
        raw_markdown += "|-------|---------|------------------|------------------|\n"
        for _v in _vaults:
            _s = _v["vault_id"]
            raw_markdown += (
                f"| **{_v['display_name']}** "
                f"| [reindex](/manage/tasks?reindex=1&vault={_s}) "
                f"| [generate missing](/manage/tasks?generate_metadata=1&vault={_s}) "
                f"| [force regenerate](/manage/tasks?generate_metadata=1&force=1&vault={_s}) |\n"
            )
        for _v in _system_vaults:
            _s = _v["vault_id"]
            raw_markdown += (
                f"| **{_v['display_name']}** *(system)* "
                f"| [reindex](/manage/tasks?reindex=1&vault={_s}) "
                f"| *n/a* | *n/a* |\n"
            )
        if _system_vaults:
            raw_markdown += (
                "\nA **system** vault holds wiki-owned content (agent definitions, help "
                "documentation). It is indexed and searchable *within itself* - searching "
                "it is how you find help - and stays out of your notes the same way any two "
                "vaults stay apart: search is hard vault-scoped. The metadata actions are "
                "n/a because blessed files are human-authored; the LLM never writes tags or "
                "summaries into them.\n"
            )

    # --- Agents (markdown definitions in the system vault, run via the worker) ---
    from src import agent_registry
    from config import AGENT_LEDGERS_FILE

    def _ledgers_exist(vault_id: str, slug: str) -> bool:
        """Only link a ledgers page that exists - an agent may have memory on and
        no ledger yet, and a dead link reads as a broken feature."""
        from src.wikidoc import WikiDoc
        return WikiDoc.read_text(
            vault_id,
            f"{AGENT_OUTPUT_DIR}/agents/{slug}/{AGENT_LEDGERS_FILE}") is not None

    _agents = agent_registry.list_agents()
    raw_markdown += "\n## Agents\n\n"
    raw_markdown += (
        f"Agents are markdown files in `/wiki/{SYSTEM_VAULT}/agents/`. Each writes only "
        f"into its owned `{AGENT_OUTPUT_DIR}/{{agent}}/` folder (output page + run logs) "
        "in the target vault. Proposed changes to YOUR pages are staged - review them in "
        "the [agent activity inbox](/agents).\n\n"
    )
    if not _agents:
        raw_markdown += "*No agent definitions found.*\n"
    else:
        raw_markdown += "| Agent | Description | Run | Memory |\n"
        raw_markdown += "|-------|-------------|-----|--------|\n"
        for _a in _agents:
            _def_link = f"[{_a.slug}](/wiki/{SYSTEM_VAULT}/agents/{_a.slug})"
            if not _a.valid:
                _errs = "; ".join(_a.errors)
                raw_markdown += f"| {_def_link} | ⚠ invalid: {_errs} | - | - |\n"
                continue
            _targets = agent_registry.resolve_target_vaults(_a)
            _runs = f"[all](/manage/tasks?agent={_a.slug})"
            _runs += "".join(
                f" - [{_t}](/manage/tasks?agent={_a.slug}&vault={_t})" for _t in _targets
            )
            # Memory and ledgers are ordinary pages, so linking them IS the
            # documented override path: edit to correct a bad remembered
            # convention, blank the note to reset it. Per target vault, since
            # each vault keeps its own.
            _mem = "-"
            if _a.memory:
                _owner = f"{AGENT_OUTPUT_DIR}/agents/{_a.slug}"
                _parts = []
                for _t in _targets:
                    _links = [f"[note](/wiki/{_t}/{_owner}/memory)"]
                    if _ledgers_exist(_t, _a.slug):
                        _links.append(f"[ledgers](/wiki/{_t}/{_owner}/ledgers)")
                    _parts.append(f"{_t}: " + " · ".join(_links))
                _mem = " - ".join(_parts)
            raw_markdown += f"| {_def_link} | {_a.description} | {_runs} | {_mem} |\n"

    # --- In-progress jobs ---
    in_progress_tasks = await task_tracker.get_in_progress()

    raw_markdown += "\n## In Progress\n\n"
    if in_progress_tasks:
        raw_markdown += "| Job ID | Function | Status |\n"
        raw_markdown += "|--------|----------|--------|\n"
        for t in in_progress_tasks:
            raw_markdown += f"| `{t.task_id}` | `{t.function}` | running |\n"
    else:
        raw_markdown += "*No jobs currently running.*\n"

    # --- Bulk task progress (all-vaults jobs + any per-vault jobs) ---
    bulk_jobs = [("reindex:all", "Reindex All"), ("metadata:all", "Generate All Metadata")]
    for _v in _vaults:
        _s = _v["vault_id"]
        bulk_jobs.append((f"reindex:vault:{_s}", f"Reindex ({_s})"))
        bulk_jobs.append((f"metadata:vault:{_s}", f"Generate Metadata ({_s})"))
    # System vaults get a reindex job (the /manage/monitor gap links fire exactly
    # this id), so poll for it or that run reports no progress at all. No metadata
    # job id can ever exist for them - see the Per-vault table above.
    for _v in _system_vaults:
        _s = _v["vault_id"]
        bulk_jobs.append((f"reindex:vault:{_s}", f"Reindex ({_s})"))
    for _a in _agents:
        if _a.valid:
            bulk_jobs.append((agent_registry.agent_job_id(_a.slug), f"Agent {_a.slug} (all vaults)"))
    bulk_progress_md = ""
    for bulk_id, bulk_label in bulk_jobs:
        progress = await task_tracker.get_progress(bulk_id)
        if progress:
            bulk_progress_md += f"\n**{bulk_label}** - scanned {progress.get('step', '?')}"
            if "enqueued" in progress:
                bulk_progress_md += f", enqueued: {progress['enqueued']}"
            if "skipped" in progress:
                bulk_progress_md += f", skipped: {progress['skipped']}"
            if progress.get("failed", 0) > 0:
                bulk_progress_md += f", failed: {progress['failed']}"
            bulk_progress_md += "\n"
    # Only surface the section when a bulk job is actually reporting progress;
    # otherwise these lines would render under the ``## In Progress`` heading
    # and read as part of it.
    if bulk_progress_md:
        raw_markdown += "\n## Bulk Task Progress\n" + bulk_progress_md

    # --- Queued jobs ---
    raw_markdown += "\n## Queued\n\n"
    pending_tasks = await task_tracker.get_pending()
    if pending_tasks:
        raw_markdown += "| Job ID | Function | Enqueue Time |\n"
        raw_markdown += "|--------|----------|--------------|\n"
        for t in pending_tasks:
            enqueue_str = timefmt.stamp(t.enqueue_time) if t.enqueue_time else "-"
            raw_markdown += f"| `{t.task_id}` | `{t.function}` | {enqueue_str} |\n"
    else:
        raw_markdown += "*No jobs in queue.*\n"

    # --- Recent results ---
    raw_markdown += "\n## Recent Results\n\n"
    completed_tasks = await task_tracker.get_completed()
    if completed_tasks:
        raw_markdown += "| Job ID | Function | Success | Enqueue Time | Finish Time | Duration | Result |\n"
        raw_markdown += "|--------|----------|---------|--------------|-------------|----------|--------|\n"
        for t in completed_tasks:
            success_str = "yes" if t.success else "**no**"
            enqueue_str = timefmt.stamp(t.enqueue_time) if t.enqueue_time else "-"
            finish_str = timefmt.stamp(t.finish_time) if t.finish_time else "-"
            if t.enqueue_time and t.finish_time:
                duration_str = f"{t.finish_time - t.enqueue_time:.1f}s"
            else:
                duration_str = "-"
            result_text = _summarize_task_result(t.result).replace("|", "\\|")
            # Job IDs like ``agent:slug:vault:x`` have no spaces, so the cell
            # can't wrap and the whole table overflows. Insert a zero-width
            # space (invisible) after each colon to give the browser a break
            # opportunity without altering the displayed ID.
            job_display = t.task_id.replace(":", ":​")
            raw_markdown += f"| `{job_display}` | `{t.function}` | {success_str} | {enqueue_str} | {finish_str} | {duration_str} | {result_text} |\n"
    else:
        raw_markdown += "*No completed jobs.*\n"

    doc_template = jinja_env.get_template("document.html")
    doc_data = {}
    doc_data["unlinked_title"] = "Task Queue"

    wd = WikiDoc("/temp/manage_tasks")
    wd.set_content(raw_markdown)
    markdown_doc = MarkdownDocTransform(wd)
    html = await asyncio.to_thread(markdown_doc.get_content)
    doc_data["document"] = html
    response_content = doc_template.render(doc_data)
    return HTMLResponse(response_content)


# ---------------------------------------------------------------------------
# /agents - the agent activity inbox: staged batches awaiting human review.
# The page builds its document HTML directly (no markdown transform): the
# server-rendered diffs are raw HTML blocks that python-markdown could mangle.
# ---------------------------------------------------------------------------

_AGENTS_INBOX_STATIC = """
<style>
    .staging-batch { border: 1px solid var(--callout-color, #b8860b); border-radius: 6px;
                     margin: 0.8em 0; padding: 0.3em 0.8em; }
    .staging-batch > summary { cursor: pointer; font-weight: bold; padding: 0.3em 0; }
    .staging-batch .batch-meta { font-weight: normal; font-size: 0.85em; opacity: 0.8; }
    .staging-file { margin: 0.5em 0 0.5em 1em; border-left: 3px solid var(--callout-color, #b8860b);
                    padding-left: 0.8em; }
    .staging-file > summary { cursor: pointer; }
    .staging-note { font-size: 0.85em; opacity: 0.85; font-style: italic; }
    .drift-badge { color: var(--color-danger, #b00020); font-weight: bold; font-size: 0.85em; }
    .staging-actions { margin: 0.4em 0; display: inline-flex; gap: 0.5em; }
</style>
<script>
    async function stagingAction(action, runId, ids, confirmMsg) {
        if (confirmMsg && !window.confirm(confirmMsg)) return;
        try {
            const res = await fetch('/api/agents/staging', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: action, run_id: runId, ids: ids || null}),
            });
            const data = await res.json();
            if (!res.ok) { alert('Failed: ' + (data.error || res.status)); return; }
            location.reload();
        } catch (e) { alert('Failed: ' + e); }
    }

    async function cancelAgent(jobId) {
        if (!window.confirm('Cancel this run? It stops at the next step boundary.')) return;
        try {
            const res = await fetch('/api/agents/cancel', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({job_id: jobId}),
            });
            const data = await res.json();
            if (!res.ok) { alert('Cancel failed: ' + (data.error || res.status)); return; }
            location.reload();
        } catch (e) { alert('Cancel failed: ' + e); }
    }
</script>
"""


_NEW_DEFINITION_STATIC = """
<style>
    .new-def-row { display: flex; gap: var(--space-sm); align-items: center;
                   flex-wrap: wrap; margin: var(--space-sm) 0 var(--space-lg); }
    .new-def-row input[type="text"] {
        padding: var(--space-xs) var(--space-sm);
        font-size: var(--text-md);
        border: 1px solid var(--darker);
        border-radius: var(--radius-sm);
        background-color: var(--bg-color);
        color: var(--fg-color);
        min-width: 14em;
    }
    .new-def-hint { font-size: var(--text-smd); opacity: 0.8; }
</style>
"""


def _new_definition_form(action: str, label: str, placeholder: str, hint: str) -> str:
    """The one-box "New agent" / "New editor" control for the management pages.

    A definition is addressed by its FILENAME everywhere (registry, scheduler,
    run-lock, `on:` grammar), so creating one needs a name up front - a bare
    link would have to invent one. Deliberately a plain GET form with no
    JavaScript: it posts the name to a route that slugifies it and redirects to
    the definition's ordinary /wiki URL, so creation is the wiki's existing
    not-found -> /edit path (skeleton from src.doc_templates), not a second
    creation mechanism. Nothing is written to disk until the author saves.
    """
    return (f'<form class="new-def-row" action="{action}" method="get">'
            f'<input type="text" name="name" placeholder="{escape(placeholder)}" '
            f'aria-label="{escape(label)}" autocomplete="off" autocapitalize="off">'
            f'<button class="btn-sm" type="submit">{escape(label)}</button>'
            f'<span class="new-def-hint">{escape(hint)}</span></form>')


def _new_definition_redirect(request: Request, subdir: str, listing: str):
    """Turn a typed name into the definition's canonical /wiki URL.

    This exists for ONE reason: an HTML GET form can only put what the author
    typed in the query string, and the slug has to end up in the PATH. So it
    slugifies and hands off - everything after that is machinery the wiki
    already has. Redirecting to /wiki (not /edit) is what keeps it this small:
    a name that is already taken lands on that definition's page, and a name
    that is free falls through the existing not-found bounce to /edit, where
    src.doc_templates supplies the skeleton. An unusable name goes back to the
    listing the form was on - the box is right there to retype in.
    """
    from src.agent_registry import slugify

    slug = slugify(request.query_params.get("name", ""))
    return RedirectResponse(f"/wiki/{SYSTEM_VAULT}/{subdir}/{slug}" if slug else listing)


async def new_agent_definition(request: Request):
    """GET /agents/new?name=... - a blank agent, prefilled and ready to edit."""
    from src.agent_registry import AGENTS_SUBDIR

    return _new_definition_redirect(request, AGENTS_SUBDIR, "/agents")


async def new_editor_definition(request: Request):
    """GET /editors/new?name=... - a blank editor tool, prefilled and ready to edit."""
    from src.editor_registry import EDITORS_SUBDIR

    return _new_definition_redirect(request, EDITORS_SUBDIR, "/editors")


async def agents_inbox(request: Request):
    """Pending staged batches, each expandable to per-file diffs with
    apply/reject controls. The passive half of the agent activity surface."""
    from src import write_gate

    batches = await asyncio.to_thread(write_gate.get_pending_batches)
    parts = [_AGENTS_INBOX_STATIC, _NEW_DEFINITION_STATIC]
    parts.append("<h2>Staged proposals</h2>")
    if not batches:
        parts.append("<p><em>Nothing waiting for review.</em> Agent runs that stage "
                     "changes to your pages will appear here.</p>")
    for b in batches:
        run_id = escape(b["run_id"])
        files = await asyncio.to_thread(write_gate.get_batch_files, b["run_id"])
        n = len(files)
        drift_n = sum(1 for f in files if f["drifted"])
        # created_at is a TIMESTAMPTZ: psycopg hands it back UTC-aware, so it
        # needs converting or the inbox reads hours off from the run tables.
        created = timefmt.stamp_min(b["created_at"]) if b["created_at"] else "?"
        drift_note = (f' - <span class="drift-badge">{drift_n} drifted</span>'
                      if drift_n else "")
        parts.append(f'<details class="staging-batch" open><summary>'
                     f'{escape(b["agent_slug"])} → {escape(b["vault_id"])} '
                     f'<span class="batch-meta">({n} file{"s" if n != 1 else ""} - '
                     f'{created} - <code>{run_id}</code>{drift_note})</span></summary>')
        parts.append(
            f'<div class="staging-actions">'
            f'<button class="btn-sm" onclick="stagingAction(\'apply\', \'{run_id}\')">Apply all</button>'
            f'<button class="btn-sm btn-ghost" onclick="stagingAction(\'reject\', \'{run_id}\', null, '
            f'\'Reject all staged files in this batch?\')">Reject all</button>'
            f'<button class="btn-sm btn-ghost" onclick="stagingAction(\'discard\', \'{run_id}\', null, '
            f'\'Discard this batch entirely?\')">Discard</button></div>')
        for f in files:
            fid = f["id"]
            path_label = escape(f'{f["vault_id"]}/{f["rel_path"]}')
            note = (f'<div class="staging-note">{escape(f["note"])}</div>'
                    if f["note"] else "")
            drift = (' <span class="drift-badge">⚠ drifted - the page changed since '
                     'this was staged; apply is refused</span>' if f["drifted"] else "")
            diff_html = write_gate.unified_diff_html(
                f["current_content"], f["staged_content"] or "", f["rel_path"])
            apply_btn = ("" if f["drifted"] else
                         f'<button class="btn-sm" onclick="stagingAction(\'apply\', \'{run_id}\', [{fid}])">Apply</button>')
            parts.append(
                f'<details class="staging-file"><summary>'
                f'<a href="/wiki/{escape(f["vault_id"])}/{escape(f["rel_path"].rsplit(".", 1)[0])}">{path_label}</a>{drift}</summary>'
                f'{note}'
                f'<div class="staging-actions">{apply_btn}'
                f'<button class="btn-sm btn-ghost" onclick="stagingAction(\'reject\', \'{run_id}\', [{fid}])">Reject</button></div>'
                f'{diff_html}</details>')
        parts.append("</details>")

    # --- Scheduled agents (markdown table -> native wiki styling) ---
    from src import agent_registry
    from src.agent_schedule import due_state
    from src.agent_scheduler import get_last_run

    agents = await asyncio.to_thread(agent_registry.list_agents)
    sa = ["## Scheduled agents", "",
          "| Agent | Mode | Schedule | Triggers | Last run | Next due | Targets |",
          "|---|---|---|---|---|---|---|"]
    _r = get_async_redis()
    try:
        now = datetime.datetime.now()
        for a in agents:
            link = f"[{_md_esc(a.slug)}](/wiki/{SYSTEM_VAULT}/agents/{a.slug})"
            if not a.valid:
                sa.append(f"| {link} | **⚠ invalid**{{: .drift-badge }}: "
                          f"{_md_esc('; '.join(a.errors))} | | | | | |")
                continue
            targets = _md_esc(", ".join(agent_registry.resolve_target_vaults(a)))
            trig = _md_esc(a.on_raw) if a.on_raw else "–"
            if not a.schedule:
                sched = "–" if a.on_raw else "*manual only*"
                sa.append(f"| {link} | {a.mode} | {sched} | {trig} | – | – | {targets} |")
                continue
            last = await get_last_run(_r, a.slug)
            # due_state is the shared implementation the SCHEDULER uses, jitter
            # key and all - so this column cannot show a time it will not fire at.
            _due = due_state(a.schedule, last, now, jitter_key=a.slug)
            due_str = timefmt.stamp_min(_due.next_due) if _due.next_due else "?"
            last_str = timefmt.stamp_min(last) if last else "(never)"
            sa.append(f"| {link} | {a.mode} | {_md_esc(a.schedule)} | {trig} | "
                      f"{last_str} | {due_str} | {targets} |")
    finally:
        await _r.close()
    parts.append(await _md_render("\n".join(sa)))
    parts.append(_new_definition_form(
        "/agents/new", "New agent", "daily digest",
        "A markdown file in the system vault, prefilled and manual until you schedule it."))

    # --- Active runs (markdown table; the Cancel button is inline HTML in a cell,
    # which passes through markdown untouched) ---
    in_progress = [t for t in await task_tracker.get_in_progress()
                   if t.task_id.startswith("agent:")]
    ar = ["## Active runs", ""]
    if not in_progress:
        ar.append("*No agent runs in progress.*")
    else:
        ar += ["| Run | Started | Progress | |", "|---|---|---|---|"]
        for t in in_progress:
            started = timefmt.stamp_min(t.enqueue_time) if t.enqueue_time else "–"
            prog = await task_tracker.get_progress(t.task_id)
            prog_str = (f'step {prog.get("step", "?")} – {_md_esc(str(prog.get("vault", "")))}'
                        if prog else "–")
            jid = escape(t.task_id)
            btn = (f'<button class="btn-sm btn-ghost" '
                   f"onclick=\"cancelAgent('{jid}')\">Cancel</button>")
            ar.append(f"| `{t.task_id}` | {started} | {prog_str} | {btn} |")
    parts.append(await _md_render("\n".join(ar)))

    # --- Recent agent runs (markdown table) ---
    completed = [t for t in await task_tracker.get_completed()
                 if t.task_id.startswith("agent:")]
    rr = ["## Recent runs", ""]
    if not completed:
        rr.append("*No recent agent runs.*")
    else:
        rr += ["| Run | Status | Finished | Duration | Result |",
               "|---|---|---|---|---|"]
        for t in completed[:20]:
            ok = bool(t.success)
            # A cancelled run returns normally (success), but its result carries
            # status "cancelled" - match the VALUE, not the "cancelled" count key
            # (present in every result). Results are now structured dicts, but
            # older cached entries may still be repr/JSON strings - handle both.
            if isinstance(t.result, dict):
                was_cancelled = ok and t.result.get("status") == "cancelled"
            else:
                _res_str = t.result or ""
                was_cancelled = ok and ("'status': 'cancelled'" in _res_str
                                        or '"status": "cancelled"' in _res_str)
            if not ok:
                status = "**⚠ FAILED**{: .drift-badge }"
            elif was_cancelled:
                status = "■ cancelled"
            else:
                status = "ok"
            fin = timefmt.stamp_min(t.finish_time) if t.finish_time else "–"
            dur = (f"{t.finish_time - t.enqueue_time:.0f}s"
                   if t.finish_time and t.enqueue_time else "–")
            res = _md_esc(_summarize_task_result(t.result))
            rr.append(f"| `{t.task_id}` | {status} | {fin} | {dur} | {res} |")
    parts.append(await _md_render("\n".join(rr)))

    # --- Recent events (markdown; warnings use attr_list drift-badges) ---
    from config import EVENT_TRIGGERS_ENABLED
    ev = ["## Recent events", ""]
    if not EVENT_TRIGGERS_ENABLED:
        ev.append("*Event triggers are disabled (EVENT_TRIGGERS_ENABLED=false).*")
    else:
        from src import events as _events
        _er = get_async_redis()
        try:
            recent = await _events.read_recent(_er, 20)
            pooled_ids = set(await _er.hkeys(_events.POOL_KEY))
            status = json.loads(await _er.get(_events.STATUS_KEY) or "null")
        finally:
            await _er.close()
        # Dispatcher heartbeat: a missing/stale status key means the worker is
        # down or the scheduler is disabled - otherwise indistinguishable from
        # "healthy and quiet". Events keep queueing on the stream meanwhile.
        _st_age = None
        if status:
            try:
                _st_age = (datetime.datetime.now() - datetime.datetime
                           .fromisoformat(status.get("ts", ""))).total_seconds()
            except (ValueError, TypeError):
                _st_age = None
        if _st_age is not None and _st_age <= AGENT_SCHEDULER_TICK_S * 3:
            ev.append(f"*Last dispatch tick: {max(_st_age, 0):.0f}s ago.*")
        else:
            ev.append("**⚠ No recent dispatch tick - worker down or scheduler "
                      "disabled? Event triggers are not firing; events queue on the "
                      "stream until dispatch resumes.**{: .drift-badge }")
        # Guard visibility: deferrals (esp. budget = possible trigger storm) and
        # depth-cap chain cuts from the last dispatch tick.
        if status:
            for slug, info in (status.get("deferred") or {}).items():
                reason = info.get("reason", "?")
                badge = "possible trigger storm - " if "budget" in reason else ""
                ev.append(f"**⚠ {_md_esc(slug)}: {info.get('events', '?')} event(s) "
                          f"waiting - {badge}{_md_esc(reason)}**{{: .drift-badge }}")
            if status.get("dropped_depth"):
                ev.append(f"**⚠ {status['dropped_depth']} event(s) hit the depth cap "
                          f"last tick - a trigger chain was cut (EVENT_MAX_DEPTH)"
                          f"**{{: .drift-badge }}")
            if status.get("dropped_expired"):
                ev.append(f"*{status['dropped_expired']} stale event(s) discarded "
                          f"last tick (EVENT_MAX_AGE_S).*")
        if not recent:
            ev += ["", "*No events yet.* Agent lifecycle, staging decisions, and "
                   "uploads will appear here."]
        else:
            ev += ["", "| Age | Type | Vault | Subject | Actor | Depth | Pooled |",
                   "|---|---|---|---|---|---|---|"]
            _now = datetime.datetime.now()
            for evd in recent:
                try:
                    age_s = (_now - datetime.datetime.fromisoformat(
                        evd.get("ts", ""))).total_seconds()
                    age = (f"{age_s / 3600:.1f}h" if age_s >= 3600 else
                           f"{age_s / 60:.0f}m" if age_s >= 60 else
                           f"{max(age_s, 0):.0f}s")
                except ValueError:
                    age = "?"
                pooled = "yes" if evd.get("id") in pooled_ids else "–"
                ev.append(
                    f"| {age} | `{_md_esc(evd.get('type', '?'))}` | "
                    f"{_md_esc(evd.get('vault', ''))} | "
                    f"{_md_esc(str(evd.get('subject', ''))[:60])} | "
                    f"{_md_esc(evd.get('actor', ''))} | {evd.get('depth', 0)} | {pooled} |")
            ev += ["", '*"Pooled" = retained awaiting a deferred fire (agent busy, '
                   'cooling down, or over its hourly budget).*']
    parts.append(await _md_render("\n".join(ev)))

    # --- footer notes ---
    footer = [
        f"Scheduler ticks every {AGENT_SCHEDULER_TICK_S}s in the worker; event "
        f"triggers (`on:` frontmatter) dispatch on the same tick, and deferred events "
        f"wait in the pool. Staged batches are auto-discarded after "
        f"{AGENT_STAGING_TTL_DAYS} days. Run logs live in each vault under "
        f"`{AGENT_OUTPUT_DIR}/agents/{{agent}}/logs/`.",
        "",
        "[Maintenance tasks →](/manage/tasks)",
    ]
    parts.append(await _md_render("\n".join(footer)))

    doc_template = jinja_env.get_template("document.html")
    doc_data = {
        "unlinked_title": "Agent Activity",
        "document": "\n".join(parts),
    }
    return HTMLResponse(doc_template.render(doc_data))


# Only the two status-badge colors; the table itself uses the wiki's own table
# styling (the page is rendered from Markdown, so it looks like any wiki table).
_EDITORS_STATIC = """
<style>
    .drift-badge { color: var(--color-danger); font-weight: bold; }
    .editor-ok { color: var(--color-success); font-weight: bold; }
</style>
"""


def _md_code(s) -> str:
    """Wrap `s` in a markdown code span that its own content cannot break.

    NOT _md_esc: markdown does no emphasis/link/table parsing inside a code span,
    so escaping `_ * [ ] |` there only emits literal backslashes into the path. The
    one character that does break a span is a backtick, and a backslash cannot
    escape it (backslashes are literal inside a span) - the fence has to be longer
    than the longest interior run, same rule the fenced-block handling uses. A
    space pad keeps a leading/trailing backtick from merging with the fence.
    """
    s = str(s)
    longest = max((len(m) for m in re.findall(r"`+", s)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if s.startswith("`") or s.endswith("`") else ""
    return f"{fence}{pad}{s}{pad}{fence}"


def _md_esc(s) -> str:
    """Escape the markdown-significant characters that would break a table cell or
    apply stray emphasis. `|` is the critical one (table column separator)."""
    s = str(s)
    for ch in ("\\", "|", "`", "*", "_", "[", "]"):
        s = s.replace(ch, "\\" + ch)
    return s


async def _md_render(md_str: str) -> str:
    """Render a markdown fragment to HTML through the wiki pipeline (system vault, so
    /wiki links resolve and tables get the wiki's native styling). Shared by the
    /agents and /editors management views instead of hand-building HTML."""
    wd = WikiDoc("/agents/temp_render", vault=SYSTEM_VAULT)
    wd.set_content(md_str)
    return await asyncio.to_thread(MarkdownDocTransform(wd).get_content)


async def editors_page(request: Request):
    """Management view of editor tools (the edit-mode "/" menu commands): validity,
    frontmatter + Python-tool syntax verification, and the editor-relevant settings.
    Read-only counterpart to /agents. Built as MARKDOWN and rendered through
    WikiDoc/MarkdownDocTransform (the /index pattern), so it inherits the wiki's
    table styling and uses real markdown features (callout, table, footnotes,
    attr_list badges) instead of hand-built HTML."""
    from src import editor_registry

    # include_foreign=True so a file that forgot `type: editor` is surfaced too.
    defs = await asyncio.to_thread(editor_registry.list_editor_tools, True)

    lines = [
        '!!! note "Editor tools"',
        f'    Editor tools are the custom commands in the edit-mode "/" menu, authored '
        f'as `type: editor` markdown in `{SYSTEM_VAULT}/editors/`. A tool must be valid '
        f'to appear in the menu; invalid ones are listed with the reason - frontmatter '
        f'and Python tool syntax are both verified.',
        "",
    ]
    if not defs:
        lines.append("*No files in the editors folder yet.*")
    else:
        lines.append("| Editor | Scope | Operation | Tools | Memory | Log | Vaults | Status |")
        lines.append("|---|---|---|---|---|---|---|---|")
        footnotes = []
        for d in defs:
            link = f"[{_md_esc(d.label or d.slug)}](/wiki/{SYSTEM_VAULT}/editors/{d.slug})"
            #clean_description = _md_esc(d.description) if d.description else ""
            if d.description:
                clean_description = _md_esc(d.description)
                clean_description = clean_description.replace('"', "&quot;")
                link += f'{{: title="{clean_description}"}}'
            if not d.is_editor:
                lines.append(f"| {link} | | | | | | | "
                             f"*not an editor - no `type: editor`* |")
                continue
            if not d.valid:
                fn = f"e-{d.slug}"
                footnotes.append(f"[^{fn}]: {_md_esc('; '.join(d.errors))}")
                lines.append(f"| {link} | | | | | | | "
                             f"**⚠ invalid**{{: .drift-badge }}[^{fn}] |")
                continue
            # Valid: the derived custom-tool names ARE the proof the Python parsed.
            tools = []
            if d.capabilities:
                tools.append("`" + ", ".join(d.capabilities) + "`")
            if d.custom_tools:
                tools.append("`" + ", ".join(t["name"] + "()" for t in d.custom_tools) + "`")
            tools_cell = " · ".join(tools) if tools else "–"
            op_cell = f"note → `{d.output}`" if d.operation == "note" else d.operation
            mem_cell = "✓" if d.memory else "–"
            # Mark an overridden consolidation prompt without reproducing it - a
            # custom `# Memory Prompt` is a whole prompt, not a one-line hint.
            if d.memory and d.memory_prompt:
                fn = f"m-{d.slug}"
                footnotes.append(f"[^{fn}]: custom `# Memory Prompt`")
                mem_cell += f"[^{fn}]"
            vaults_cell = "all" if d.vaults == ["*"] else _md_esc(", ".join(d.vaults))
            lines.append(
                f"| {link} |"
                # f"| {link} - {clean_description} {{: colspan=8 }} |"
                # "** {: style='display:none;'} |"
                # "** {: style='display:none;'} |"
                # "** {: style='display:none;'} |"
                # "** {: style='display:none;'} |"
                # "** {: style='display:none;'} |"
                # "** {: style='display:none;'} |"
                # "** {: style='display:none;'} |"
            # )
            # lines.append(
                f"{d.scope} | {op_cell} | {tools_cell} | {mem_cell} | "
                # f"| &#8203; | {d.scope} | {op_cell} | {tools_cell} | {mem_cell} | "
                f"{'✓' if d.log else '–'} | {vaults_cell} | "
                f"**✓ valid**{{: .editor-ok }} |")
        if footnotes:
            lines += ["", *footnotes]

    # Raw HTML block in the markdown stream: footnotes are relocated to the end
    # of the document by the extension, so the form sits under the table rather
    # than under the footnote list - and it is still there when the table isn't.
    lines += ["", _new_definition_form(
        "/editors/new", "New editor", "tighten prose",
        'A saved prompt in the system vault; it joins the edit-mode "/" menu once it parses.')]

    print("\n".join(lines))

    wikidoc = WikiDoc("/editors/temp_editors", vault=SYSTEM_VAULT)
    wikidoc.set_content("\n".join(lines))
    html = await asyncio.to_thread(MarkdownDocTransform(wikidoc).get_content)

    doc_template = jinja_env.get_template("document.html")
    return HTMLResponse(doc_template.render({
        "unlinked_title": "Editor Tools",
        "document": _EDITORS_STATIC + _NEW_DEFINITION_STATIC + html,
    }))


async def agents_pending_count_endpoint(request: Request):
    """GET staged-proposal counts. Superseded by /api/nav/badges for the nav
    badges themselves; kept as a stable alias for anything else pointing here."""
    from src import write_gate

    summary = await asyncio.to_thread(write_gate.pending_summary)
    return JSONResponse(summary)


async def nav_badges_endpoint(request: Request):
    """Every nav badge count in ONE response.

    Polled every 60s by each open tab, so it must stay cheap - two aggregate
    queries, no per-row work. One endpoint rather than one per badge: a
    per-badge poller would multiply requests (and Postgres round trips) by the
    number of badges, forever, as badges are added.

    The two badges mean different things and are deliberately not summed:
      agents  - work WAITING for you (staged proposals). Normal and actionable.
      monitor - something BROKE. Abnormal; only hard failures light it.
    """
    from src import failure_log, write_gate

    out: dict = {}
    try:
        s = await asyncio.to_thread(write_gate.pending_summary)
        files = int(s.get("files") or 0)
        out["agents"] = {
            "count": files,
            "title": (f"{s.get('batches')} batch(es) - {files} file(s) awaiting review"
                      if files else ""),
        }
    except Exception:
        out["agents"] = {"count": 0}
    try:
        n = await asyncio.to_thread(failure_log.open_badge_count)
        out["monitor"] = {
            "count": n,
            "title": f"{n} unresolved failure(s) - see /manage/monitor" if n else "",
        }
    except Exception:
        out["monitor"] = {"count": 0}
    return JSONResponse(out)


async def agents_staging_endpoint(request: Request):
    """POST {action: apply|reject|discard, run_id, ids?} - inbox decisions."""
    from src import write_gate

    data = await request.json()
    action = data.get("action", "")
    run_id = (data.get("run_id") or "").strip()
    ids = data.get("ids")
    if not run_id or action not in ("apply", "reject", "discard"):
        return JSONResponse({"error": "bad request"}, status_code=400)
    if ids is not None:
        ids = [int(i) for i in ids]

    # Meta BEFORE the action: apply/discard cleanup can remove the rows. It
    # only feeds the optional event emission - a fetch failure must NOT block
    # the human's apply/reject decision.
    try:
        meta = await asyncio.to_thread(write_gate.get_run_meta, run_id)
    except Exception:
        logging.exception("staging: get_run_meta failed for %s", run_id)
        meta = None

    if action == "apply":
        result = await asyncio.to_thread(write_gate.apply_batch, run_id, ids)
    elif action == "reject":
        result = await asyncio.to_thread(write_gate.reject_batch, run_id, ids)
    else:
        result = await asyncio.to_thread(write_gate.discard_run, run_id)

    # Human staging decisions are events (never raises; no-op when disabled).
    # actor=human breaks trigger chains here by construction - approval is a
    # click, so a depth-0 event is correct even for an agent-caused batch.
    if meta is not None:
        from src.events import emit
        etype = "staging.approved" if action == "apply" else "staging.rejected"
        payload = dict(result)
        if action == "discard":
            payload["discarded"] = True
        await emit(etype, vault=meta["vault_id"], subject=meta["agent_slug"],
                   actor="human", cause_run_id=run_id, payload=payload)
    return JSONResponse({"status": "ok", **result})


async def agents_cancel_endpoint(request: Request):
    """POST a cooperative cancel of a running agent. Accepts EITHER an exact
    ``{job_id}`` (what the /agents UI sends - no granularity guessing) OR a
    ``{slug, vault_id?}`` that we resolve against the tracker: with no vault_id
    we find whichever of the agent's candidate job ids (``:all`` or a specific
    ``:vault:{v}``) is actually active, so a caller need not know how the run
    was fired (#3). We only flag runs the tracker reports active and say so
    honestly (#2) - no silent "ok" that plants a stale flag.

    The flag is a TTL'd key checked at each step boundary; it does NOT interrupt
    a run wedged inside a single generation (the AGENT_RUN_TIMEOUT_S wall clock
    is the backstop there)."""
    from config import AGENT_RUN_TIMEOUT_S
    from src.task_broker import get_async_redis
    from src import agent_registry

    data = await request.json()
    explicit_job = (data.get("job_id") or "").strip()
    slug = (data.get("slug") or "").strip()
    vault_id = data.get("vault_id") or None

    # Build the candidate job ids to consider, then keep only active ones.
    if explicit_job:
        if not explicit_job.startswith("agent:"):
            return JSONResponse({"error": "not an agent job_id"}, status_code=400)
        candidates = [explicit_job]
    elif slug:
        agent = await asyncio.to_thread(agent_registry.get_agent, slug)
        if agent is None:
            return JSONResponse({"error": f"unknown agent '{slug}'"}, status_code=404)
        if vault_id:
            candidates = [agent_registry.agent_job_id(slug, vault_id)]
        else:
            targets = await asyncio.to_thread(agent_registry.resolve_target_vaults, agent)
            candidates = ([agent_registry.agent_job_id(slug)]
                          + [agent_registry.agent_job_id(slug, v) for v in targets])
    else:
        return JSONResponse({"error": "job_id or slug required"}, status_code=400)

    active = [j for j in candidates
              if task_tracker and await task_tracker.is_active(j)]
    if not active:
        return JSONResponse(
            {"status": "not_running",
             "error": "no active run found for that agent",
             "considered": candidates},
            status_code=409)

    r = get_async_redis()
    try:
        for j in active:
            await r.set(agent_registry.agent_cancel_key(j), "1",
                        ex=int(AGENT_RUN_TIMEOUT_S))
    finally:
        await r.close()
    return JSONResponse({"status": "ok", "cancelled": active,
                         "note": "takes effect at the next step boundary"})


# /api/markdown/code/
# async def markdown_convert_code(request: Request):
#     # This takes as input some python code and
#     # converts it to snippet of markdown
#     form = await request.form()
#     code_snippet = form["code"]
#     raw_markdown = f"```python\n{code_snippet}\n```\n"

#     wd = WikiDoc("/api/convert_code_fragment")
#     wd.set_content(raw_markdown)
#     makdown_doc = MarkdownDocTransform(wd)
#     html = makdown_doc.get_content(use_wiki_link=False)

#     return HTMLResponse(html)


# /api/markdown/code/
async def markdown_convert_code(request: Request):
    # This takes as input some python code and
    # converts it to snippet of markdown

    data = await request.json()

    # print("Got some json data, I think ")
    # print(data)

    code_snippet = data.get("code", "")
    attrs = data.get("attrs", {})
    pre_attrs = data.get("pre_attrs", {})
    code_attrs = data.get("code_attrs", {})

    language = attrs.get("class", code_attrs.get("class", "language-python"))
    language = language.replace("language-", "")

    # print("The language is ", language)
    # print("Add attributes ")
    # print(pre_attrs)
    # print(code_attrs)

    # Wrap the snippet in a fence LONGER than any backtick run it contains, so a
    # code sample that itself shows a ```fence (e.g. a `````markdown example that
    # embeds a ```python block) can't terminate our wrapper early and truncate
    # everything after the inner fence. Use the resolved language, not a hardcoded
    # one, so the block highlights as what it actually is.
    longest_ticks = 0
    run = 0
    for ch in code_snippet:
        if ch == "`":
            run += 1
            longest_ticks = max(longest_ticks, run)
        else:
            run = 0
    fence = "`" * max(3, longest_ticks + 1)
    raw_markdown = f"{fence}{language}\n{code_snippet}\n{fence}\n"

    wd = WikiDoc("/api/convert_code_fragment")
    wd.set_content(raw_markdown)
    makdown_doc = MarkdownDocTransform(wd)
    html = await asyncio.to_thread(
        makdown_doc.get_content, use_wiki_link=False, format_code=True
    )

    return HTMLResponse(html)


def _walk_wiki(extensions, vault=DEFAULT_VAULT):
    """List vault-relative paths for files matching the given extensions, in ``vault``.

    Used by /api/files and /api/images to populate the canvas renderer's
    file/image pickers. Returns a flat sorted list of forward-slash paths.
    """
    allow = {ext.lower() for ext in extensions}
    all_paths, _ = vault_index.get_index(vault)
    out = [
        p for p in all_paths
        if "." in p and p.rsplit(".", 1)[-1].lower() in allow
        and not (HIDE_DOT_DIRECTORY and _index_hidden(p))
    ]
    out.sort(key=str.lower)
    return out


# /api/files
async def list_files_endpoint(request: Request):
    return JSONResponse(_walk_wiki(("md", "canvas"), _request_vault(request)))


# /api/images
async def list_images_endpoint(request: Request):
    return JSONResponse(_walk_wiki(("png", "jpg", "jpeg", "gif", "webp", "svg"), _request_vault(request)))


# href/src attribute whose value is a URL we may need to resolve against a
# document's vault/path. Captures the quote style so it round-trips unchanged.
_URL_ATTR_RE = re.compile(
    r"""(?P<attr>\b(?:href|src))\s*=\s*(?P<q>["'])(?P<url>[^"']*)(?P=q)""",
    re.IGNORECASE,
)

# A URL that already carries its own resolution context and must be left alone:
# fragment-only (#...), root-absolute (/...), protocol-relative (//...), or any
# scheme (http:, https:, mailto:, data:, tel:, ...).
_ABSOLUTE_URL_RE = re.compile(r"^(?:#|/|//|[a-zA-Z][a-zA-Z0-9+.\-]*:)")


def _absolutize_relative_urls(html: str, prefix: str) -> str:
    """Prefix relative ``href``/``src`` URLs in an HTML fragment with ``prefix``.

    This replaces the older page-global ``<base href=...>`` tag that
    ``/api/markdown/`` used to prepend. A ``<base>`` element is *document-wide*:
    when this fragment is injected into a host page -- a canvas embed, a chat
    bubble, or the edit-preview pane -- its ``<base>`` hijacks every relative
    URL on the *whole* page, including a document's own ``[TOC]`` "#anchor"
    links, which then resolve against ``/wiki/{vault}/`` instead of the current
    page. Rewriting each relative URL to an absolute one keeps embedded link and
    image references in-vault without that page-wide trap. URLs that are already
    absolute, protocol-relative, scheme-qualified, or fragment-only are left as
    is (the browser still normalizes ``..`` segments in the result).
    """

    def _sub(match: "re.Match") -> str:
        url = match.group("url")
        if not url or _ABSOLUTE_URL_RE.match(url):
            return match.group(0)
        q = match.group("q")
        return f'{match.group("attr")}={q}{prefix}{url}{q}'

    return _URL_ATTR_RE.sub(_sub, html)


# /api/markdown/
async def markdown_convert(request: Request):
    # This is for the preview button
    form = await request.form()
    raw_markdown = form["markdown"]
    format_code = str(form.get("format_code", "true"))
    format_code = True if format_code.lower() == "true" else False

    document_name = form.get("document_name")

    if document_name:
        # document_name is the vault-explicit display path ("wiki/{vault}/path"), so
        # parse the vault out and render with it -- wikilinks then resolve within the
        # right vault and relative image/link hrefs get a real /wiki/{vault}/ base.
        wd = WikiDoc.from_url_with_vault(str(document_name))
    else:
        wd = WikiDoc("/preview/temp")
    wd.set_content(raw_markdown)
    markdown_doc = MarkdownDocTransform(wd)
    html = await asyncio.to_thread(markdown_doc.get_content, format_code=format_code)

    if document_name:
        base_path = wd.path()
        prefix = f"/wiki/{wd.vault()}/" + (f"{base_path}/" if base_path else "")
    else:
        prefix = f"/wiki/{DEFAULT_VAULT}/"
    # Resolve relative link/image URLs against the document's vault/path here,
    # rather than emitting a page-global <base> tag that leaks into whatever host
    # page injects this fragment (canvas embed, chat, edit preview) and breaks
    # its own "#anchor"/relative links. See _absolutize_relative_urls.
    html = _absolutize_relative_urls(html, prefix)

    # print("#### api/markdown/  this should have codehilite classes, right? ###")
    # print(raw_markdown)
    # print("# # # # # # # # # # # # ## # ")
    # print(html)
    # print("###########################")
    return HTMLResponse(html)


async def upload_file_streaming(request: Request):
    """Stream large file upload without loading entire file into memory"""
    form = await request.form()
    file = form.get("file")

    if not file:
        return JSONResponse({"error": "No file provided"}, status_code=400)

    MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB

    # Chunk size for streaming (e.g., 1MB)
    CHUNK_SIZE = 1024 * 1024
    # UPLOAD_DIR = Path("wiki")

    document_name = form["document_name"]  # is from file_path in edit
    if not document_name:
        return JSONResponse({"error": "document path not specified"}, status_code=500)

    # document_name is the vault-explicit display path; recover the vault so the upload
    # lands in the right vault's working tree (and folder, alongside the document).
    wd = WikiDoc.from_url_with_vault(str(document_name))
    vault = wd.vault()
    if not vault_registry.vault_exists(vault):
        return JSONResponse({"error": f"Unknown vault: {vault}"}, status_code=404)
    vroot = _vault_root(vault)

    filename = Path(file.filename).name  # strip any directory components

    file_path = os.path.join(vroot, *wd.path_list(), filename)

    try:
        WikiDoc.validate_path(file_path, vroot)
    except ValueError:
        return JSONResponse({"error": "Invalid file path"}, status_code=400)

    file_path = Path(file_path)

    total_size = 0

    # Stream file in chunks
    try:
        async with aiofiles.open(file_path, "wb") as f:
            while chunk := await file.read(CHUNK_SIZE):
                total_size += len(chunk)

                # Check size limit
                if MAX_FILE_SIZE and (total_size > MAX_FILE_SIZE):
                    # Clean up partial file
                    await f.close()
                    file_path.unlink(missing_ok=True)
                    return JSONResponse(
                        {"error": f"File too large. Max size: {MAX_FILE_SIZE} bytes"},
                        status_code=413,
                    )

                await f.write(chunk)

        url_file_location = str(filename)

        # print("What is the file extension?")
        parsed_url = WikiDoc.parse_url_path(filename)  # to get uploaded file extension.

        file_ext = str(parsed_url["file_ext"]).lower()
        if file_ext in IMAGE_FILE_TYPES:
            # Images embed as a standard inline image.
            markdown_text = f"![{filename}]({url_file_location})"
        elif file_ext in PREVIEW_EMBED_FILE_TYPES:
            # Data files we can preview: insert the Obsidian embed form so the
            # upload renders as a table/PDF card (FileEmbedPreprocessor) the same
            # way images render inline. A hand-written [x](x.pdf) link stays a
            # link; the embed syntax is the explicit "preview this" signal.
            markdown_text = f"![[{filename}]]"
        else:
            markdown_text = f"[{filename}]({url_file_location})"

        # Upload event: guaranteed-human actor (this endpoint IS the human
        # path). subject = vault-relative path, so `uploads in <prefix>`
        # triggers can scope by folder. emit never raises.
        from src.events import emit
        await emit("upload", vault=vault,
                   subject="/".join([*wd.path_list(), filename]),
                   actor="human",
                   payload={"size": total_size, "filename": filename})
        return JSONResponse(
            {
                "filename": filename,
                "size": total_size,
                "path": url_file_location,
                "markdownText": markdown_text,
            }
        )
    except Exception as e:
        # Clean up on error
        file_path.unlink(missing_ok=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# async def ponder_document(request: Request):
#     # This is allows for asking a question about a document.

#     form = await request.form()
#     query = form["chat"]
#     document_name = form["document_name"]

#     wikidoc = WikiDoc(document_name)
#     context = wikidoc.get_content()

#     prompt = f"""Based on the following context, answer the question.

# Context:
# {context}

# Question: {query}

# Answer:"""

#     try:
#         response = await ollama_mgr.generate(prompt)
#     except Exception as e:
#         response = f"**Error communicating with Ollama:** {e}"

#     wd = WikiDoc("/ponder/temp")
#     wd.set_content(response)
#     markdown_doc = MarkdownDocTransform(wd)
#     html = await asyncio.to_thread(markdown_doc.get_content)

#     return HTMLResponse(html)




# /api/chat
async def chat_endpoint(request: Request):
    """POST /api/chat/ - streaming chat via SSE."""
    from src import chat

    data = await request.json()
    message = data.get("message", "").strip()
    session_id = data.get("session_id")
    document_url_path = data.get("document_url_path")
    mode = data.get("mode", "document")

    if not message:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    # The strongest "a person is here NOW" signal in the app - someone is waiting
    # on a token stream. Refreshed per turn so a long conversation keeps agents
    # standing aside for its whole duration, not just the first 90 seconds.
    await mark_human_active()

    # The document_url_path is the vault-explicit display path ("wiki/{vault}/path").
    # Split out the vault (an explicit "vault" field overrides) and store the
    # vault-relative path on the session; the session is bound to that single vault.
    vault = (data.get("vault") or DEFAULT_VAULT)
    doc_rel = ""
    if document_url_path:
        wd = WikiDoc.from_url_with_vault(document_url_path)
        if not data.get("vault"):
            vault = wd.vault()
        doc_rel = wd.relative_file_path() or ""

    # Get or create session
    session = None
    if session_id:
        session = chat.get_session(session_id)
    if not session:
        session = chat.create_session(doc_rel, mode, vault)

    # The browser's window.location.pathname for the open page. Stored so the
    # run_python tool can attach to the EXACT kernel the page's own cells use
    # (kernels are keyed by this string). Refreshed each turn in case the user
    # navigated to a different page within the same chat session.
    page_id = data.get("page_id")
    if page_id:
        session.page_id = page_id

    # The ?revision= the page was opened with, so the chat's working copy is the
    # version the user is actually looking at. Set unconditionally: navigating from
    # a revision back to the live page within one session must drop back to HEAD.
    session.revision = (data.get("revision") or "").strip()

    return StreamingResponse(
        chat.chat_response_generator(session, message, ollama_mgr),
        media_type="text/event-stream",
    )


async def chat_reset_endpoint(request: Request):
    """POST /api/chat/reset - delete a chat session."""
    from src import chat

    data = await request.json()
    session_id = data.get("session_id")
    if session_id:
        chat.delete_session(session_id)
    return JSONResponse({"ok": True})


async def chat_confirm_endpoint(request: Request):
    """POST /api/chat/confirm - confirm or reject, then optionally continue the agent loop."""
    from src import chat

    data = await request.json()
    session_id = data.get("session_id")
    confirmed = data.get("confirmed", False)

    if not session_id:
        return JSONResponse({"error": "Missing session_id"}, status_code=400)

    session = chat.get_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    return StreamingResponse(
        chat.confirm_and_continue_generator(session, confirmed, ollama_mgr),
        media_type="text/event-stream",
    )


async def chat_continue_endpoint(request: Request):
    """POST /api/chat/continue - resume the agent loop after it hit max steps."""
    from src import chat

    data = await request.json()
    session_id = data.get("session_id")

    if not session_id:
        return JSONResponse({"error": "Missing session_id"}, status_code=400)

    session = chat.get_session(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    return StreamingResponse(
        chat.continue_generator(session, ollama_mgr),
        media_type="text/event-stream",
    )




async def edit_assist_endpoint(request: Request):
    """POST /api/edit/assist - single-shot writing assistance via SSE.

    Body: {command, before, selection, after, frontmatter?, path?,
           content?, cursor_offset?, selection_start?, selection_end?}

    `path` is the editor file_path (URL-style, no .md extension); when
    present, retrieval-using commands like `continue_with_sources` use it
    to self-exclude the current document from grounding results.

    `content` + `cursor_offset` carry the live editor state (full document
    + caret position). When supplied, edit_assist uses them to (1) pick a
    tiered doc-context for the LLM prompt and (2) derive live wikilinks/
    tags so retrieval reflects in-progress edits rather than the on-disk
    indexed version.
    """
    from src import edit_assist

    data = await request.json()
    command_id = data.get("command")
    before = data.get("before", "") or ""
    after = data.get("after", "") or ""
    selection = data.get("selection", "") or ""
    frontmatter = data.get("frontmatter") or None
    path = data.get("path") or None
    content = data.get("content")
    cursor_offset = data.get("cursor_offset")
    # Working range within `content`. Equal to each other (and to cursor_offset)
    # when nothing is selected - a caret is a zero-width selection.
    selection_start = data.get("selection_start")
    selection_end = data.get("selection_end")
    instruction = data.get("instruction", "") or ""

    if not command_id:
        return JSONResponse({"error": "Missing command"}, status_code=400)
    # Editor tools are id "editor:<slug>", resolved from the system-vault registry
    # inside stream_assist rather than the static COMMANDS dict.
    if command_id not in edit_assist.COMMANDS and not command_id.startswith("editor:"):
        return JSONResponse({"error": f"Unknown command: {command_id}"}, status_code=400)

    # path is the editor file_path (vault-explicit display form, "wiki/{vault}/..").
    # Split out the vault and reduce path to the vault-relative form so doc_id lookups
    # and retrieval scope to the right vault.
    vault = data.get("vault") or DEFAULT_VAULT
    if path:
        wd = WikiDoc.from_url_with_vault(path)
        if not data.get("vault"):
            vault = wd.vault()
        path = wd.relative_file_path() or path

    gen = edit_assist.stream_assist(
        ollama_mgr, command_id, before, selection, after, frontmatter, path,
        content=content, cursor_offset=cursor_offset,
        selection_start=selection_start, selection_end=selection_end,
        vault=vault, instruction=instruction,
    )
    return StreamingResponse(gen, media_type="text/event-stream")


async def edit_commands_endpoint(request: Request):
    """GET /api/edit/commands - public registry metadata for the slash menu.

    Optional `?path=` (the vault-explicit editor file_path) or `?vault=` scopes
    the menu: editor tools declare a `vaults:` availability whitelist, so the
    current file's vault decides which appear. Absent -> unscoped (all shown).
    """
    from src import edit_assist
    vault = request.query_params.get("vault")
    if not vault:
        path = request.query_params.get("path")
        if path:
            try:
                vault = WikiDoc.from_url_with_vault(path).vault()
            except Exception:
                vault = None
    return JSONResponse(edit_assist.list_commands(vault))


async def related_documents_endpoint(request: Request):
    """GET /api/related/{path} - find semantically similar documents."""
    return JSONResponse({"error": "Not implemented"}, status_code=400)


# /chat
async def global_chat_page(request: Request):
    """GET /chat/{vault} - standalone corpus chat page, scoped to one vault."""
    vault = _request_vault(request)
    chat_template = jinja_env.get_template("chat.html")
    doc_data = {
        "title": "Chat",
        "unlinked_title": f"Chat with your Wiki ({vault})",
        "vault": vault,
    }
    return HTMLResponse(chat_template.render(doc_data))


####################################################################################


# /graph
async def graph_global(request: Request):
    """GET /graph - whole-wiki connection graph rendered as a read-only canvas.

    The graph is generated live from the Postgres `edges`/`documents` tables on
    each visit (never persisted), so it always reflects the current link state.
    """
    from src import graph_canvas

    vault = _request_vault(request)
    tags_on = request.query_params.get("tags") in ("1", "true", "on")

    try:
        canvas = await asyncio.to_thread(
            graph_canvas.build_canvas, None, 1, False, tags_on, vault)
    except Exception as e:
        print(f"graph_global failed: {e}")
        canvas = {"nodes": [], "edges": []}

    graph_template = jinja_env.get_template("graph.html")
    return HTMLResponse(graph_template.render({
        "title": "Wiki Graph",
        "unlinked_title": "Wiki Graph",
        "document_mode": "view",
        "hide_edit_link": True,
        "vault": vault,
        "canvas_json": json.dumps(canvas),
        "scripts": "",
        # Chrome state for the control bar / legend.
        "graph_mode": "global",
        "graph_root_url": f"/graph/{vault}",
        "graph_depth": 1,
        "graph_tags_on": tags_on,
    }))


# /graph/{path}
async def graph_local(request: Request):
    """GET /graph/{path} - local neighborhood graph around a single page."""
    from src import graph_canvas

    vault = _request_vault(request)
    path_param = request.path_params.get("path", "")
    try:
        depth = int(request.query_params.get("depth", "1"))
    except ValueError:
        depth = 1
    depth = max(1, min(depth, 4))
    tags_on = request.query_params.get("tags") in ("1", "true", "on")

    # The .md-form, vault-relative path is the DB doc_id (e.g. "Programming/AI.md").
    wikidoc = WikiDoc("/wiki/" + path_param, vault=vault)
    root_doc_id = wikidoc.relative_file_path() or (
        path_param if path_param.endswith(".md") else path_param + ".md"
    )

    try:
        canvas = await asyncio.to_thread(
            graph_canvas.build_canvas, root_doc_id, depth, False, tags_on, vault)
    except Exception as e:
        print(f"graph_local failed: {e}")
        canvas = {"nodes": [], "edges": []}

    name = wikidoc.file_name_no_ext() or "Graph"
    graph_template = jinja_env.get_template("graph.html")
    return HTMLResponse(graph_template.render({
        "title": f"Graph: {name}",
        "unlinked_title": f"Local graph - {name}",
        "document_mode": "view",
        "hide_edit_link": True,
        "vault": vault,
        "canvas_json": json.dumps(canvas),
        "scripts": "",
        # Chrome state for the control bar / legend.
        "graph_mode": "local",
        "graph_root_url": f"/graph/{vault}/" + quote(path_param, safe="/"),
        "graph_depth": depth,
        "graph_tags_on": tags_on,
    }))


# /search
async def search_document(request: Request):
    """GET /search/{vault}?q=<query> - full-text and semantic search, scoped to vault."""
    import time as _time
    vault = _request_vault(request)
    query = request.query_params.get("q", "").strip()

    doc_template = jinja_env.get_template("search.html")
    doc_data = {
        "unlinked_title": "Search",
        "search_query": query,
        "vault": vault,
        "results": [],
        "search_time": "",
    }

    if query:
        t0 = _time.monotonic()
        results = []
        seen_urls = set()

        # RAG hybrid + graph search, scoped to this vault.
        try:
            from src.rag_search import search as rag_search
            rag_results = await asyncio.to_thread(
                rag_search, query, top_k=10, include_graph_expansion=True, vault_id=vault
            )
            for r in rag_results.get("chunk_results", []):
                doc_id = r.get("doc_id", "")
                rel = doc_id[:-3] if doc_id.endswith(".md") else doc_id
                # search.html builds /wiki/{{url_path}} -> include the vault segment.
                url_path = f"{vault}/{quote(rel, safe='/')}"
                if url_path in seen_urls:
                    continue
                seen_urls.add(url_path)
                header = r.get("header_path", "")
                if isinstance(header, list):
                    header = " > ".join(str(h) for h in header)
                snippet = make_snippet(r.get("content", "") or "", query)
                results.append({
                    "title": r.get("doc_title", "") or Path(doc_id).stem,
                    "url_path": url_path,
                    "header_path": header,
                    "snippet": snippet,
                    "source": r.get("source", "semantic"),
                    "match_count": 0,
                })
        except Exception as e:
            print(f"RAG search failed, falling back to fulltext: {e}")

        # File-based fulltext search (scoped to the vault's working tree) for anything
        # RAG missed.
        try:
            ft_results = fulltext_search(query, vault, max_results=10)
            for r in ft_results:
                # Prefix the vault so the template's /wiki/{{url_path}} stays in-vault.
                r["url_path"] = f"{vault}/{quote(r['url_path'], safe='/')}"
                if r["url_path"] not in seen_urls:
                    seen_urls.add(r["url_path"])
                    r["header_path"] = ""
                    results.append(r)
        except Exception as e:
            print(f"Fulltext search failed: {e}")

        elapsed = _time.monotonic() - t0
        doc_data["results"] = results
        doc_data["search_time"] = f"{elapsed:.2f}s"

    html = doc_template.render(**doc_data)
    return HTMLResponse(html)


# /api/vaults - JSON list of vaults (powers the header switcher dropdown).
async def list_vaults_endpoint(request: Request):
    # The start page is per-vault, so it rides on each entry rather than sitting
    # beside the list as one site-wide value.
    vaults = vault_registry.list_vaults()
    for v in vaults:
        v["default_page"] = vault_registry.vault_default_page(v["vault_id"])
    return JSONResponse({"vaults": vaults})


# /vaults  - list vaults + create a new one (mkdir under the parent mount).
# Styles + behavior for the /vaults settings panels. Follows the _AGENTS_INBOX_STATIC
# pattern (a Python constant concatenated into the page) rather than a committed .js:
# the only script here is the color picker <-> text sync, well under the size at which
# /index/ graduated to its own file. All CSS consumes existing tokens so the page
# themes correctly in light and dark and under any theme folder.
_VAULTS_STATIC = """
<style>
    .vault-row { border-bottom: 1px solid var(--lighter); padding: var(--space-xs) 0; }
    /* `display:flex` on a <summary> suppresses the default disclosure marker, so
       draw one. ::marker is unreliable on a flex summary across browsers. */
    .vault-row > summary { list-style: none; }
    .vault-row > summary::-webkit-details-marker { display: none; }
    .vault-row > summary {
        cursor: pointer; display: flex; align-items: center; gap: var(--space-sm);
        padding: var(--space-xs) 0;
    }
    .vault-row > summary::before {
        content: "\\25B6"; flex: none; opacity: 1.0;
        transition: transform 0.3s ease-in-out;
    }
    .vault-row[open] > summary::before { transform: rotate(90deg); }
    .vault-row > summary .vault-name { font-weight: bold; }
    .vault-row > summary .vault-slug { font-size: var(--text-smd); opacity: 0.7; }
    .vault-row > summary .vault-links { margin-left: auto; font-size: var(--text-smd); }
    .vault-swatch {
        display: inline-block; width: 1em; height: 1em; border-radius: var(--radius-sm);
        border: 1px solid var(--darker); vertical-align: middle;
    }
    .vault-settings { padding: var(--space-sm) 0 var(--space-md) var(--space-lg); }
    .vault-field {
        display: flex; align-items: center; gap: var(--space-sm);
        margin-bottom: var(--space-xs);
    }
    .vault-field > label { min-width: 8em; font-size: var(--text-smd); }
    .vault-field input[type="text"], .vault-field select {
        padding: var(--space-xs) var(--space-sm);
        font-size: var(--text-md);
        border: 1px solid var(--darker);
        border-radius: var(--radius-sm);
        background-color: var(--bg-color);
        color: var(--fg-color);
        min-width: 18em;
    }
    .vault-field input[type="color"] {
        width: 2.2em; height: 2em; padding: 0; border: 1px solid var(--darker);
        border-radius: var(--radius-sm); background-color: var(--bg-color);
        cursor: pointer; flex: none;
    }
    .vault-hint { font-size: var(--text-smd); opacity: 0.8; margin-left: var(--space-sm); }
    .vault-banner {
        padding: var(--space-sm) var(--space-md); border-radius: var(--radius-sm);
        margin-bottom: var(--space-md); border: 1px solid var(--darker);
    }
    .vault-banner.ok { background-color: var(--color-success-bg); }
    .vault-banner.err { background-color: var(--color-danger-bg); }
</style>
<script>
(function () {
    'use strict';
    // The TEXT field is authoritative and is what submits, so an oklch() value
    // survives round-tripping. <input type="color"> only speaks #rrggbb, so it is a
    // one-way convenience: picking writes hex into the text box, and the swatch
    // best-effort follows the text when the browser can resolve it to rgb().
    function toHex(value) {
        var probe = document.createElement('span');
        probe.style.color = '';
        probe.style.color = value;
        if (!probe.style.color) return null;      // browser rejected it outright
        document.body.appendChild(probe);
        var computed = getComputedStyle(probe).color;
        probe.remove();
        var m = computed.match(/^rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
        if (!m) return null;                      // e.g. oklch() reported as-is
        return '#' + [1, 2, 3].map(function (i) {
            return ('0' + parseInt(m[i], 10).toString(16)).slice(-2);
        }).join('');
    }
    function init() {
        document.querySelectorAll('.vault-field[data-color]').forEach(function (row) {
            var picker = row.querySelector('input[type="color"]');
            var text = row.querySelector('input[type="text"]');
            if (!picker || !text) return;
            var sync = function () {
                var hex = text.value.trim() ? toHex(text.value.trim()) : null;
                if (hex) picker.value = hex;
            };
            sync();
            text.addEventListener('change', sync);
            picker.addEventListener('input', function () { text.value = picker.value; });
        });
    }
    // This block is emitted BEFORE the panels it wires up, so the rows do not exist
    // yet at parse time -- wait for the document rather than binding nothing.
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
</script>
"""


def _vault_settings_panel(v: dict, open_slug: str | None = None) -> str:
    """One <details> row: the vault's links when collapsed, its settings form when
    open. Values come straight from the vault's config (not the resolved fallbacks),
    so an empty field genuinely means "inherit", and saving an untouched form is a
    no-op rather than a silent pin to the current default."""
    vid = v["vault_id"]
    cfg = v.get("settings") or {}
    colors = cfg.get("colors") if isinstance(cfg.get("colors"), dict) else {}
    page = _default_page_url(vid)

    themes = "".join(
        f'<option value="{escape(t)}"'
        f'{" selected" if cfg.get("template") == t else ""}>{escape(t)}</option>'
        for t in sorted(available_templates())
    )
    accent = colors.get("base")
    swatch = (
        f'<span class="vault-swatch" style="background:{escape(accent)}"></span>'
        if vault_registry.valid_css_color(accent) else ""
    )

    fields = [
        _vault_text_field(vid, "display_name", "Display name", cfg.get("display_name", ""),
                          "shown in the switcher"),
        _vault_text_field(vid, "default_page", "Start page", cfg.get("default_page", ""),
                          f"blank = {escape(DEFAULT_WIKI_PAGE)}"),
        f'<div class="vault-field"><label for="template-{escape(vid)}">Theme</label>'
        f'<select id="template-{escape(vid)}" name="template">'
        f'<option value="">(site default: {escape(TEMPLATE)})</option>{themes}</select></div>',
    ]
    for key, label in (("base", "Accent"), ("background", "Surface"),
                       ("foreground", "Ink"), ("link", "Links")):
        fields.append(_vault_color_field(vid, key, label, colors.get(key, "")))

    # Stay open after a save (or an explicit ?open=), so the panel you were editing
    # is still in front of you rather than collapsing out from under the redirect.
    return (
        f'<details class="vault-row"{" open" if open_slug == vid else ""}>'
        f'<summary>'
        f'<span class="vault-name">{escape(v["display_name"])}</span>'
        f'<span class="vault-slug">{escape(vid)}</span>{swatch}'
        f'<span class="vault-links">'
        f'<a href="/wiki/{escape(vid)}/{page}">open</a> | '
        f'<a href="/index/{escape(vid)}/">index</a> | '
        f'<a href="/graph/{escape(vid)}">graph</a></span>'
        f'</summary>'
        f'<form class="vault-settings" method="post" action="/vaults">'
        f'<input type="hidden" name="action" value="update">'
        f'<input type="hidden" name="slug" value="{escape(vid)}">'
        + "".join(fields) +
        f'<div class="vault-field"><label></label>'
        f'<button type="submit">Save</button>'
        f'<span class="vault-hint">Clear a field to fall back to the site default.</span>'
        f'</div></form></details>'
    )


def _vault_text_field(vid: str, name: str, label: str, value: str, hint: str) -> str:
    return (
        f'<div class="vault-field">'
        f'<label for="{escape(name)}-{escape(vid)}">{escape(label)}</label>'
        f'<input type="text" id="{escape(name)}-{escape(vid)}" name="{escape(name)}" '
        f'value="{escape(str(value))}" autocomplete="off">'
        f'<span class="vault-hint">{hint}</span></div>'
    )


def _vault_color_field(vid: str, key: str, label: str, value: str) -> str:
    """A swatch plus a text box. The text box carries the real value (so oklch()
    survives); the swatch is a convenience that writes hex into it."""
    return (
        f'<div class="vault-field" data-color>'
        f'<label for="color_{escape(key)}-{escape(vid)}">{escape(label)}</label>'
        f'<input type="color" aria-label="{escape(label)} picker" tabindex="-1">'
        f'<input type="text" id="color_{escape(key)}-{escape(vid)}" '
        f'name="color_{escape(key)}" value="{escape(str(value))}" '
        f'placeholder="inherit" autocomplete="off" spellcheck="false"></div>'
    )


async def _vaults_update(form) -> RedirectResponse:
    """Apply one settings panel. Every field is optional and an EMPTY field REMOVES
    its key, so clearing a box means "inherit the site default" rather than pinning
    the vault to an empty string.

    Validation happens here as well as at the read boundary, and for a different
    reason: the resolvers deliberately fall back on bad input (right for rendering,
    since a typo must not take a vault offline), but a form that silently discarded
    what you typed would be a bug. So a bad value is reported instead.
    """
    slug = str(form.get("slug", "")).strip().lower()

    def fail(message: str) -> RedirectResponse:
        """Bounce back with the message AND the vault, so the panel that failed is
        the one still open when the page comes back."""
        return RedirectResponse(
            f"/vaults?error={quote(message)}&open={quote(slug)}", status_code=303)

    if not vault_registry.vault_exists(slug):
        return RedirectResponse(f"/vaults?error={quote('Unknown vault')}", status_code=303)
    # list_vaults() already hides system vaults so no panel is rendered for one;
    # re-check anyway, since this is a POST target a client can reach directly.
    if vault_registry.is_system_vault(slug):
        return fail("That vault is wiki-owned and not editable here")

    changes: dict = {}

    display_name = str(form.get("display_name", "")).strip()
    changes["display_name"] = display_name or None

    page = str(form.get("default_page", "")).strip()
    if page:
        normalized = vault_registry.normalize_default_page(page)
        if not normalized:
            return fail(f"Not a usable start page: {page}")
        changes["default_page"] = normalized
    else:
        changes["default_page"] = None

    template = str(form.get("template", "")).strip()
    if template and not is_template(template):
        return fail(f"No such theme: {template}")
    changes["template"] = template or None

    colors: dict = {}
    for key in vault_registry.VAULT_COLOR_TOKENS:
        value = str(form.get(f"color_{key}", "")).strip()
        if not value:
            continue
        if not vault_registry.valid_css_color(value):
            return fail(f"Not a usable color for {key}: {value}")
        colors[key] = value
    changes["colors"] = colors or None

    # Disk I/O plus a git commit -- keep it off the event loop.
    await asyncio.to_thread(vault_registry.set_vault_settings, slug, changes)
    return RedirectResponse(f"/vaults?saved={quote(slug)}", status_code=303)


async def vaults_landing(request: Request):
    if request.method == "POST":
        form = await request.form()
        # `action` discriminates the two forms on this page. Absent means create,
        # which is what the original single-form page posted.
        if str(form.get("action", "")).strip() == "update":
            return await _vaults_update(form)
        slug = str(form.get("slug", "")).strip().lower()
        display_name = str(form.get("display_name", "")).strip() or slug
        try:
            vault_registry.create_vault(slug, display_name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # A new vault has no pages yet, so send the user to the EDITOR in create
        # mode (prefilled by starter_document) rather than to /wiki/, which would
        # render a 404 whose "return to the main page" link leaves the new vault.
        page = _default_page_url(slug)
        return RedirectResponse(f"/edit/{slug}/{page}", status_code=302)

    # Built as raw HTML rather than markdown: the settings panels are nested forms,
    # and python-markdown mangles substantial raw HTML blocks (the same reason
    # /agents abandoned the transform). Nothing is lost -- the old content was a
    # link list plus a form.
    banner = ""
    saved = request.query_params.get("saved")
    error = request.query_params.get("error")
    if error:
        banner = f'<p class="vault-banner err">{escape(error)}</p>'
    elif saved:
        banner = f'<p class="vault-banner ok">Saved {escape(saved)}.</p>'

    open_slug = saved or request.query_params.get("open")
    vaults = await asyncio.to_thread(vault_registry.list_vaults)
    panels = "".join(_vault_settings_panel(v, open_slug) for v in vaults)

    create_form = (
        '<h2>Create a vault</h2>'
        '<form class="vault-field" method="post" action="/vaults">'
        '<input type="hidden" name="action" value="create">'
        '<input type="text" name="slug" placeholder="slug (lowercase, a-z0-9-_)" required>'
        '<input type="text" name="display_name" placeholder="Display name">'
        '<button type="submit">Create</button></form>'
    )

    doc_template = jinja_env.get_template("document.html")
    return HTMLResponse(doc_template.render({
        "unlinked_title": "Vaults",
        "document": _VAULTS_STATIC + banner + panels + create_form,
        "scripts": "",
    }))


async def health_endpoint(request: Request):
    """Single readiness probe for the whole stack. A new user hits GET /health
    to answer 'is everything wired up?' without grepping five containers' logs.
    Returns 200 when everything the app needs is ready, 503 while something is
    still coming up (e.g. models mid-pull by the ollama-init service).

    The CHECKS live in src/health.py, shared with /manage/monitor so the JSON
    probe and the page can never disagree. What stays here is this endpoint's
    public contract: the `ready` boolean, the `hints`, and the status code.
    """
    from src.health import collect_health

    checks = await collect_health(ollama_mgr)
    chat_ok = bool(checks["ollama"].get("chat_model", {}).get("present"))
    embed_ok = bool(checks["ollama"].get("embed_model", {}).get("present"))

    # The app serves pages without models, but chat/RAG - the point of the stack -
    # need Postgres, Redis and the chat model. The worker check is REPORTED but
    # deliberately excluded: a routine worker rebuild would otherwise flip this
    # endpoint to 503 for anything polling it.
    ready = bool(checks["postgres"].get("ok") and checks["redis"].get("ok") and chat_ok)

    hints = []
    if not checks["postgres"].get("ok"):
        hints.append("Postgres unreachable - is the pgserver container healthy?")
    if not checks["redis"].get("ok"):
        hints.append("Redis unreachable - is the redisserver container up?")
    if not checks["worker"].get("ok"):
        hints.append("No recent worker tick - background tasks, agents and indexing "
                     "are not running (is tzaraworker up?).")
    if not chat_ok:
        hints.append(
            f"Chat model '{OLLAMA_MODEL}' not found - pull it "
            f"(the ollama-init service does this on first `up`, or use /manage/ollama)."
        )
    if not embed_ok:
        hints.append(
            f"Embedding model '{OLLAMA_EMBED_MODEL}' not found - RAG indexing will "
            f"fail until it is pulled."
        )

    payload = {"status": "ok" if ready else "degraded", "checks": checks}
    if hints:
        payload["hints"] = hints
    return JSONResponse(payload, status_code=200 if ready else 503)


async def _noop_models() -> list:
    """Fallback when the LLM backend hasn't finished starting (ollama_mgr is
    None during a brief startup window); treated as 'no models visible yet'."""
    return []


async def test_pg(request: Request):
    response = ""
    connection = None
    try:
        # Connect to PostgreSQL (single canonical factory)
        from config import get_pg_connection
        connection = get_pg_connection()
        response += "Connection successful! <br>"
        # Create a cursor object to execute SQL queries
        cursor = connection.cursor()
        # Example query: Fetch PostgreSQL version
        cursor.execute("SELECT version();")
        db_version = cursor.fetchone()
        response += f"PostgreSQL version: {db_version} <br>"
    except Exception as error:
        response += f"Error connecting to PostgreSQL: {error} <br>"
    finally:
        # Close the connection
        if connection:
            cursor.close()
            connection.close()
            response += "Connection closed."

    return HTMLResponse(response)

async def test_pg_taskiq(request: Request):
    print("Testing postgres by doing a background worker")

    job_id = "pg:test"

    task = await test_postgresql.kicker().with_task_id(job_id).kiq()
    
    result = await task.wait_result(timeout=10)

    print("That background worker work?")
    

    if result.is_err:
        return HTMLResponse(f"Task failed: {result.error}")

    return HTMLResponse(result.return_value)

###################################################################################

# Example of a long running task,


from src.task_broker import broker, REDIS_URL, result_backend
from src.task_definitions import example_long_task
from taskiq_redis.exceptions import ResultIsMissingError

########
from src.task_definitions import example_long_task


# --- Start the task 

async def start_example_task(request: Request):
    total = int(request.query_params.get("total", "15"))
    job_id = "example:all"

    if await task_tracker.is_active(job_id):
        return JSONResponse({"error": "Task already running"}, status_code=409)

    await task_tracker.delete_result(job_id)

    task = await example_long_task.kicker().with_task_id(job_id).kiq(job_id, total)
    await task_tracker.record_enqueue(task.task_id, "example_long_task")

    return JSONResponse({"started": True, "job_id": job_id})


# --- Check progress 
async def check_example_task(request: Request):
    job_id = "example:all"

    progress = await task_tracker.get_progress(job_id)

    if await task_tracker._redis.exists(f"taskiq:tracker:result:{job_id}"):  # completed
        raw = await task_tracker._redis.get(f"taskiq:tracker:result:{job_id}")
        info = json.loads(raw)
        return JSONResponse({
            "state": "completed",
            "success": info.get("success"),
            "result": info.get("result"),
            "progress": progress,
        })
    elif await task_tracker._redis.hexists("taskiq:tracker:in_progress", job_id):  # in progress
        return JSONResponse({"state": "in_progress", "progress": progress})
    elif await task_tracker._redis.hexists("taskiq:tracker:pending", job_id):  # pending
        return JSONResponse({"state": "pending", "progress": None})
    else:
        return JSONResponse({"state": "not_found"})


# --- Delete / clean up 
async def delete_example_task(request: Request):
    job_id = "example:all"
    await task_tracker.delete_result(job_id)
    return JSONResponse({"deleted": True})




####################################################################################
routes = [
    Route("/health", endpoint=health_endpoint, methods=["GET"]),
    WebSocketRoute("/ws/run_jupyter", jupyter_websocket_endpoint),
    
    Route("/test/example/start", endpoint=start_example_task, methods=["GET"]),
    Route("/test/example/status", endpoint=check_example_task, methods=["GET"]),
    #Route("/test/example/result", endpoint=get_example_result, methods=["GET"]),
    Route("/test/example/delete", endpoint=delete_example_task, methods=["GET"]),

    Route("/manage/jupyter", endpoint=manage_jupyter, methods=["GET", "POST"]),
    Route("/manage/ollama", endpoint=manage_ollama, methods=["GET", "POST"]),
    Route("/api/ollama/pull", endpoint=ollama_pull_endpoint, methods=["POST"]),
    Route("/api/ollama/pull/status", endpoint=ollama_pull_status_endpoint, methods=["GET"]),
    Route("/api/ollama/pull/stream", endpoint=ollama_pull_stream_endpoint, methods=["GET"]),
    Route("/manage/tasks", endpoint=manage_tasks, methods=["GET"]),
    Route("/manage/monitor", endpoint=manage_monitor, methods=["GET"]),
    Route("/agents", endpoint=agents_inbox, methods=["GET"]),
    Route("/agents/new", endpoint=new_agent_definition, methods=["GET"]),
    Route("/editors", endpoint=editors_page, methods=["GET"]),
    Route("/editors/new", endpoint=new_editor_definition, methods=["GET"]),
    Route("/api/agents/pending-count", endpoint=agents_pending_count_endpoint, methods=["GET"]),
    Route("/api/nav/badges", endpoint=nav_badges_endpoint, methods=["GET"]),
    Route("/api/agents/staging", endpoint=agents_staging_endpoint, methods=["POST"]),
    Route("/api/agents/cancel", endpoint=agents_cancel_endpoint, methods=["POST"]),
    Route("/api/markdown/code/", endpoint=markdown_convert_code, methods=["POST"]),
    Route("/api/markdown/", endpoint=markdown_convert, methods=["POST"]),
    Route("/api/kernel/{vault}/query", endpoint=kernel_query_endpoint, methods=["POST"]),
    Route("/api/move", endpoint=move_document_endpoint, methods=["POST"]),
    Route("/api/batch-move", endpoint=batch_move_endpoint, methods=["POST"]),
    Route("/api/batch-delete", endpoint=batch_delete_endpoint, methods=["POST"]),
    Route("/api/files", endpoint=list_files_endpoint, methods=["GET"]),
    Route("/api/images", endpoint=list_images_endpoint, methods=["GET"]),
    Route("/api/vaults", endpoint=list_vaults_endpoint, methods=["GET"]),
    # ----------------------------------------------------------------------
    Route("/api/chat/confirm", endpoint=chat_confirm_endpoint, methods=["POST"]),
    Route("/api/chat/continue", endpoint=chat_continue_endpoint, methods=["POST"]),
    Route("/api/chat/reset", endpoint=chat_reset_endpoint, methods=["POST"]),
    Route("/api/chat/", endpoint=chat_endpoint, methods=["POST"]),
    Route("/api/edit/assist", endpoint=edit_assist_endpoint, methods=["POST"]),
    Route("/api/edit/commands", endpoint=edit_commands_endpoint, methods=["GET"]),
    # Route(
    #     "/api/ponder/", endpoint=ponder_document, methods=["POST", "GET"]
    # ),  # chat or ponder? 
    Route(
        "/api/related/{path:path}", endpoint=related_documents_endpoint, methods=["GET"]
    ),
    
    Route("/test/pg", endpoint=test_pg, methods=["GET"]),
    Route("/test/pgtaskiq", endpoint=test_pg_taskiq, methods=["GET"]),

    Route("/upload-stream", upload_file_streaming, methods=["POST"]),
    # Vault management landing page.
    Route("/vaults", endpoint=vaults_landing, methods=["GET", "POST"]),
    # save/delete are form-driven: the vault rides inside document_name (parsed by
    # WikiDoc.from_url_with_vault), so their routes stay loose.
    Route("/save/{path:path}", endpoint=save_document, methods=["POST"]),
    Route("/delete/{path:path}", endpoint=delete_document, methods=["GET", "POST"]),
    # Action-first, vault-segmented document routes. The vaulted form is registered
    # BEFORE the bare fallback so a 2+ segment path resolves to (vault, path).
    Route("/index/{vault}/{path:path}", endpoint=index_document, methods=["GET"]),
    Route("/index/{vault}", endpoint=index_document, methods=["GET"]),
    Route("/edit/{vault}/{path:path}", endpoint=edit_document, methods=["GET", "POST"]),
    Route("/raw/{vault}/{path:path}", endpoint=view_raw_document, methods=["GET"]),
    Route("/history/{vault}/{path:path}", endpoint=history_document, methods=["GET", "POST"]),
    Route("/wiki/{vault}/{path:path}", endpoint=view_document, methods=["GET"]),
    # Bare (un-vaulted) convenience fallback: 302 to the default (or named) vault.
    Route("/wiki/{path:path}", endpoint=wiki_bare_redirect, methods=["GET"]),
    Route("/search/{vault}", endpoint=search_document, methods=["GET"]),
    Route("/chat/{vault}", endpoint=global_chat_page, methods=["GET"]),
    Route("/graph/{vault}/{path:path}", endpoint=graph_local, methods=["GET"]),
    Route("/graph/{vault}", endpoint=graph_global, methods=["GET"]),
    Route("/{path:path}", endpoint=catch_all, methods=["GET", "POST"]),
]


app = Starlette(
    debug=True,
    routes=routes,
    # on_startup=[on_startup],
    # on_shutdown=[on_shutdown],
    lifespan=lifespan,
)

from starlette.middleware.cors import CORSMiddleware

app.add_middleware(VaultThemeMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# app.add_middleware(PyinstrumentMiddleware)

if __name__ == "__main__":
    print("Starting API server...")
    uvicorn.run(app, host="0.0.0.0", port=8000)
