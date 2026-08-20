# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import os
import time
from dotenv import load_dotenv
load_dotenv()

#
# If a page doesn't exist in some location, or is not specified,
# use the following page name.
#
DEFAULT_WIKI_PAGE = "Main"

#
# Name of the directory in app/template/ that contains theme files.
# Theme folders only need the files being customized (e.g. CSS, header/footer).
# Missing files fall back to the "default" theme automatically.
#
# This is the SITE-WIDE fallback. A vault may override it with a "template" key in
# its .tzara/config.json (see vault_registry.vault_theme), so a vault reads as its
# own place at a glance.
TEMPLATE = os.environ.get("TZARA_TEMPLATE", "default")
#TEMPLATE = "ocean"

# Directory holding the theme folders, relative to CWD (which is app/).
TEMPLATE_DIR = "template"

#
# Show directory as en editable link in /index view.
# This turns /path/location/ into link for /path/location.md
#
DIRECTORY_AS_MD_FILE_LINK = True

#
# If a directory begins with a dot, such as .path,
# then ignore all of it's contents on the /index/ page
# Currently still accessable from /wiki/
#
HIDE_DOT_DIRECTORY = True

#
# Specify file enocding for markdown files.
#
DEFAULT_ENCODING = "utf-8"

#
# Interpreting spaces order of precedence:
#    Spaces in URLs can be mapped to
# either "%20" or "+", and sometimes it seems like " "
# is an option, but that could just a browser thing.
# However, in a file system, these characters are
# distinct, so there could be something like "My File.md"
# and "My+File.md" and "My%20File.md" in the same location.
# So when you have a link to [[My File]] which one
# should be retrieved? Here is the selection order,
# with underscore thrown in the mix because I prefer it.
#
SPACE_CONVERSION_ORDER = ["_", " ", "%20", "+"]

#
# Insert some default tags when creating a new document?
#
INSERT_DEFAULT_TAGS_IN_NEW_DOCUMENT = True

#
# Enable file history using a local git repo
#
USE_GIT_VERSIONING = True
VERSIONING_NAME = "tzara"
VERSIONING_EMAIL = "no_reply@tzara.studio"
VERSIONING_DEFAULT_SAVE_ON_EDIT = True


#
# Redis settings
#

# Redis configuration from environment
REDIS_HOST = "redis"
REDIS_PORT = 6379
#REDIS_SETTINGS = {"url": f"http://{REDIS_HOST}:{REDIS_PORT}/", "ttl": 3600}
#REDIS_SETTINGS = RedisSettings(host=REDIS_HOST, port=REDIS_PORT)



#
# LLM server settings
#
# These were named OLLAMA_* before Tzara spoke to anything but Ollama. The old
# names are still honored so an existing .env keeps working; see _llm_env.
def _llm_env(new: str, old: str, default: str) -> str:
    """Read a renamed LLM setting, falling back to its pre-rename OLLAMA_* name."""
    return os.environ.get(new) or os.environ.get(old) or default


LLM_URL = _llm_env("LLM_URL", "OLLAMA_URL", "http://ollama:11434")
LLM_MODEL = _llm_env("LLM_MODEL", "OLLAMA_MODEL", "llama3.2:3b")
LLM_KEEP_ALIVE = _llm_env("LLM_KEEP_ALIVE", "OLLAMA_KEEP_ALIVE", "30m")
# LLM_NUM_CTX = the context window to ASK the backend to load the chat/agent model
# with, in tokens. 0 = don't request a size (let the backend choose / use its default).
# This is an ASK, not a guarantee: the backend may load smaller under memory pressure.
#   - Ollama: sent as the `num_ctx` option when warming (Ollama (re)loads at this size).
#   - Lemonade: sent as `ctx_size` to POST /v1/load (its per-model llama-server honors it,
#     memory permitting).
# It maps to the provider's real load knob so its meaning is identical across providers.
LLM_NUM_CTX = int(_llm_env("LLM_NUM_CTX", "OLLAMA_NUM_CTX", "0"))

# LLM_CONTEXT_BUDGET = the token window Tzara budgets history against internally
# (compute_max_messages / compute_history_budget_tokens). Decoupled from the load ASK
# above because "what we asked to load" and "what actually loaded" can differ. 0 = auto,
# resolved by precedence: explicit budget > backend-measured actual (e.g. Lemonade
# /v1/health ctx_size) > the LLM_NUM_CTX ask > model ceiling > 4096 floor. Set it only
# to force a specific budget (e.g. cap below the loaded window for latency, or when the
# backend can't report and the ask is unset).
LLM_CONTEXT_BUDGET = int(_llm_env("LLM_CONTEXT_BUDGET", "OLLAMA_CONTEXT_BUDGET", "0"))

#
# LLM backend provider seam (see app/src/llm_backend.py)
#
# Selects HOW Tzara talks to its local LLM. Two ORTHOGONAL surfaces, because the
# APIs split cleanly along them (each verified empirically, app/.test/):
#
#   * INFERENCE (chat/generate/stream/tools) -> OpenAI-compatible /v1. Universal:
#     Ollama, Lemonade, vLLM, LocalAI, llama.cpp all speak it, and /v1 streams
#     tool-call turns token-by-token + exposes reasoning on a separate channel on
#     BOTH Ollama and Lemonade (Lemonade's *Ollama* chat mount buffers when tools
#     are present -> no streaming; that's the whole reason for the seam).
#   * MANAGEMENT + EMBEDDINGS -> Ollama-native /api/*, when the server offers it.
#     OpenAI has no model-management concept (list/show/ps/pull/warm/unload), and
#     embeddings ride the native mount where present (existing, proven path).
#     Capabilities (tools/thinking/context_length) are thus DISCOVERED via
#     /api/show, never declared as config flags.
#
# Model NAMES stay in the LLM_* vars above and are configured per-server
# (Ollama names on an Ollama server, Lemonade names on Lemonade -- never mixed).
# `:latest` is an Ollama tag convention; the /v1 path strips a trailing `:latest`
# defensively (see llm_backend._v1_model_name) for the rare pure-openai embed case.
#
#   ollama   (default) - chat via /v1, embeddings+management native. Unchanged UX.
#   lemonade           - chat via /api/v1, embeddings+management native (Lemonade
#                        serves /api/*, so download/inspect/switch all still work).
#   openai             - pure OpenAI server (vLLM/LocalAI/real OpenAI). Chat AND
#                        embeddings via /v1; management degrades (no pull/ps;
#                        capabilities best-effort) - an inherent limit of that API.
#   ollama-native      - FALLBACK: the pre-seam native Ollama chat path (buffer +
#                        raw= tool-call salvage). Zero-cost kill-switch if an
#                        Ollama /v1 chat quirk ever surfaces; one env flip reverts.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama").strip().lower()

# Whether this provider has a native Ollama /api/* mount (management + embeddings).
LLM_HAS_NATIVE_MOUNT = LLM_PROVIDER in ("ollama", "lemonade", "ollama-native")

# OpenAI-compatible /v1 base URL for the inference path. Auto-derives from
# LLM_URL by provider convention when unset: Ollama mounts OpenAI at bare `/v1`,
# Lemonade at `/api/v1`. Override for a server on a different path (e.g.
# http://host:8000/v1). Unused by the `ollama-native` fallback.
_llm_default_v1 = f"{LLM_URL.rstrip('/')}/api/v1" if LLM_PROVIDER == "lemonade" else f"{LLM_URL.rstrip('/')}/v1"
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "").strip() or _llm_default_v1

# Send `cache_prompt: false` on WORKER (background agent) inference calls only.
# llama.cpp-backed servers reuse a slot's KV across requests; a slot that restores
# a previous request's cache incorrectly lets one agent generate under another
# agent's system prompt. Observed 2026-08-12: math_of_the_day answered in the
# nasa-apod agent's persona 42 minutes after nasa-apod's last call, from a
# verified-clean prompt. Off-hours correctness beats off-hours latency, so the
# worker pays the reprocess cost (measured ~4.6s per 2K-token turn against
# Lemonade/gpt-oss-120b) and interactive chat, which runs in the WEB process,
# keeps its cache untouched. Servers that do not know the field ignore it
# (verified: an unknown body field is accepted, not rejected).
LLM_AGENT_NO_PROMPT_CACHE = os.environ.get(
    "LLM_AGENT_NO_PROMPT_CACHE", "true").strip().lower() in ("1", "true", "yes", "on")

# The agentic tool-calling loop makes ONE streaming LLM call per turn that both
# reasons and (maybe) emits a tool call - there is no separate plan-vs-act call, so
# every loop turn is a "tool-deciding turn". gpt-oss interleaves analysis-channel
# reasoning with tool calls, which Ollama's harmony parser can fail to parse
# (malformed-tool-call → the run truncates; see run_agent_loop's retry/salvage). Set
# False to make those turns answer WITHOUT the reasoning channel, trading some
# tool-selection quality for fewer malformed calls. Only the agent loop is affected;
# memory-consolidation / summary / edit-assist calls keep their own think setting.
AGENT_TOOL_THINK = os.environ.get("AGENT_TOOL_THINK", "true").strip().lower() in ("1", "true", "yes", "on")

# In-network base URL of the wiki server itself. Used by code running OUTSIDE
# the tzaraserver container (e.g. the `wiki` client injected into Jupyter
# kernels) to call back into the internal API. Resolves by compose service
# name on `tzara-net`; override via env if the service name changes.
SERVER_INTERNAL_URL = os.environ.get("SERVER_INTERNAL_URL", "http://tzaraserver:8000")

# /edit/ writing-assist runs on the chat model (LLM_MODEL). A dedicated edit model
# was removed 2026-07-19: it defaulted to LLM_MODEL anyway and, unlike the chat model,
# a distinct edit model was never warmed / ask-loaded / window-measured / clamped, so it
# advertised a separately-managed model that wasn't one. Per-command model pinning still
# exists via WritingCommand.model (falls back to LLM_MODEL) for the rare command that
# wants a specific model; reintroduce a global edit model only alongside that machinery.
# Optional edit-specific history budget (tokens) for /edit/ cursor-mode commands: it
# sizes the document-context tier (full-doc / outline+window / window-only) in
# edit_assist._build_doc_context. 0 (default) = inherit the chat model's resolved window
# (llm_mgr.get_context_length(), already ask/measured/clamped), so the edit path picks
# up all the context handling for free. Set >0 only to cap edit context SMALLER than the
# window (a latency tweak); it's clamped to the model's real window so it can't over-pack.
LLM_EDIT_CONTEXT_BUDGET = int(_llm_env("LLM_EDIT_CONTEXT_BUDGET", "OLLAMA_EDIT_CONTEXT_BUDGET", "0"))

#
# vector search settings
#
LLM_EMBED_MODEL = _llm_env("LLM_EMBED_MODEL", "OLLAMA_EMBED_MODEL", "embeddinggemma:300m")
LLM_EMBED_KEEP_ALIVE = _llm_env("LLM_EMBED_KEEP_ALIVE", "OLLAMA_EMBED_KEEP_ALIVE", "10m")
# Hard ceiling on the token length of a single embedding input. A llama.cpp-backed
# server (e.g. Lemonade) rejects any embedding input longer than its physical batch
# size (--ubatch-size) with "input is too large to process" and, unlike Ollama, does
# NOT silently truncate. We clip inputs to this budget before sending (see
# truncate_for_embedding). Set it to your embed model's context window / the server's
# ubatch; 2048 = embeddinggemma's context.
LLM_EMBED_MAX_TOKENS = int(_llm_env("LLM_EMBED_MAX_TOKENS", "OLLAMA_EMBED_MAX_TOKENS", "2048"))

# Max concurrent background LLM/embed requests against the LLM server (frontmatter
# generation, embedding, model warming). Background tasks all run in the single
# taskiq worker process, so an in-process semaphore of this size caps how hard a
# bulk reindex hammers the shared server. 1 = fully serialized. Interactive
# chat/edit (web process) are intentionally NOT gated by this.
LLM_MAX_CONCURRENCY = int(_llm_env("LLM_MAX_CONCURRENCY", "OLLAMA_MAX_CONCURRENCY", "1"))


#
# PostgreSQL settings
#
# The DB is reachable ONLY on the internal tzara-net bridge; its container port is
# NOT published to the host (the `ports:` mapping in docker-compose.yml is commented
# out, an opt-in dev convenience). HOST/PORT therefore always resolve to the compose
# service name `postgres` on 5432 - they are env-overridable but rarely need to be.
#
# IMPORTANT: these fallbacks are the "no .env present" defaults and MUST stay in sync
# with the pgserver defaults in docker-compose.yml (POSTGRES_USER/PASSWORD/DB). If
# they drift, a bare `up` with no .env initializes the DB role with one set of
# credentials and connects with another -> auth failure. Keep both at tzara/tzara.
POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", 5432)
POSTGRES_DB = os.environ.get("POSTGRES_DB", "tzara")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "tzara")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "change_this_password")


def get_pg_connection():
    """The single per-call psycopg2 connection factory. Every module's
    _get_pg_connection (and inline connect) delegates here so connection params
    live in exactly one place - add sslmode/connect_timeout/pooling once, not in
    8 copies. Lives in config (where POSTGRES_* are defined) to avoid a module
    cycle and the 'a search module owns the DB connection' smell."""
    import psycopg2
    return psycopg2.connect(
        dbname=POSTGRES_DB,
        user=POSTGRES_USER,
        password=POSTGRES_PASSWORD,
        host=POSTGRES_HOST,
        port=POSTGRES_PORT,
    )


#
# Taskiq settings
#
TASKIQ_RETRY = 3


#
# Automatically generate tags for documents via Ollama after each save
#
AUTO_GENERATE_TAGS = True
AUTO_GENERATE_SUMMARY = True
#
# If no Index directive in frontmatter, default indexing to this value
#
INDEX_DOCUMENT_FRONTMATTER_DEFAULT = True

_excluded_raw = os.environ.get("EXCLUDED_FOLDERS", ".obsidian,.git,.trash,__pycache__")
EXCLUDED_FOLDERS = set(f.strip() for f in _excluded_raw.split(",") if f.strip())

#
# Graph-aware retrieval expansion
#
GRAPH_EXPANSION_ENABLED = True
GRAPH_EXPANSION_MAX_NEIGHBOR_CHUNKS = 5   # max chunks to add from graph neighbors
GRAPH_EXPANSION_SEED_DOCS = 3             # how many top-result docs to expand from

#
# Markdown transclusion (include embeds): how many levels of nested
# ![[Page]] includes to expand before stopping. A page including a page
# that includes a page is depth 3. Guards against runaway/cyclic includes
# (cycles are also caught by a visited-set). Mirrors EVENT_MAX_DEPTH.
#
EMBED_INCLUDE_MAX_DEPTH = int(os.environ.get("EMBED_INCLUDE_MAX_DEPTH", "3"))




#
# Token estimation and compaction tuning
#
CHARS_PER_TOKEN = 3.5       # empirical chars-per-token ratio for budget math
MIN_MESSAGES = 4            # absolute floor for conversation history after compaction

_embed_logger = logging.getLogger("embedding")


def truncate_for_embedding(text: str) -> str:
    """Clip an embedding input to LLM_EMBED_MAX_TOKENS (character-approximated).

    Restores the forgiveness Ollama gave us for free: a strict llama.cpp backend
    hard-fails an over-long embedding input instead of truncating it, so an oversized
    chunk would abort the whole embed call. Returns ``text`` unchanged when it is
    already within budget.

    The budget is char-based (no tokenizer dependency) via CHARS_PER_TOKEN. Because
    that ratio is an *average*, a 0.8 safety margin is applied so token-dense inputs
    (code, tables) stay under the server's physical-batch ceiling rather than landing
    just over it -- this guards down to ~2.8 real chars/token.

    This is a LAST-RESORT net: the chunker now bounds every chunk_type to its
    max_chunk_size, so a well-formed corpus should never trip it. When it does fire we
    log a warning, because it means a chunk slipped through oversized and its tail is
    being silently dropped from the embedding -- a retrieval-quality signal worth
    surfacing rather than hiding.
    """
    max_chars = int(LLM_EMBED_MAX_TOKENS * CHARS_PER_TOKEN * 0.8)
    if len(text) <= max_chars:
        return text
    _embed_logger.warning(
        "truncate_for_embedding: clipping oversized embedding input %d -> %d chars "
        "(LLM_EMBED_MAX_TOKENS=%d); a chunk exceeded the model context and its tail "
        "will not be embedded -- check the chunker for an unbounded chunk_type",
        len(text), max_chars, LLM_EMBED_MAX_TOKENS,
    )
    return text[:max_chars]

#
# Chat debug instrumentation. When enabled, the chat agent logs the running
# size of the session message history (total character length + estimated
# tokens, with and without the system prompt) every time a message is added
# and every time the history is checkpoint-summarized or trimmed. Watch the
# context grow in `docker logs tzaraserver`; set CHAT_DEBUG_STATS=false in
# .env to silence it once you're done debugging.
#
CHAT_DEBUG_STATS = os.environ.get("CHAT_DEBUG_STATS", "true").strip().lower() in (
    "1", "true", "yes", "on"
)

# Computational-RAG: when enabled, the chat agent gains a `run_python` tool that
# executes code in the open page's shared Jupyter kernel (where the `wiki` query
# object lives). This is an LLM-authored code-execution sink: when on, every code
# block must be approved by the user before it runs (see the run_python approval
# gate in src/chat.py). Default is "false" for safety: note text read via search
# could carry prompt-injection that steers the agent's code. Set the env var to
# "true" to opt in once you trust your vaults' content.
CHAT_ENABLE_RUN_PYTHON = os.environ.get("CHAT_ENABLE_RUN_PYTHON", "false").strip().lower() in (
    "1", "true", "yes", "on"
)

# Fence languages whose blocks become executable Jupyter cells. Default "jupyter"
# (a pseudo-language for runnable Python, so executable code can coexist with
# read-only illustrative ```python on the same page). Set "python" for
# jupytext-style documents, or "jupyter,python" to make both executable. Each entry
# is matched as the fence language and stamped onto the cell (data-cell-lang); today
# they all route to the single default kernel, but the per-language stamp leaves room
# to later map e.g. ```rust to its own kernel without reworking the markup.
_exec_langs_raw = os.environ.get("EXECUTABLE_CODE_LANGUAGES", "jupyter")
EXECUTABLE_CODE_LANGUAGES = [
    l.strip().lower() for l in _exec_langs_raw.split(",") if l.strip()
] or ["jupyter"]
# "jupyter" has no real Pygments lexer; color it as python. Languages absent from this
# map highlight as themselves (python -> python, a future rust -> rust).
EXECUTABLE_HIGHLIGHT_ALIASES = {"jupyter": "python"}


#############################
# these aren't configurable #
#############################
RESERVED_PATHS = [
    "wiki",
    "edit",
    "save",
    "delete",
    "index",
    "history",
    "raw",
    "search",
    "chat",
]

# Every top-level name the URL namespace owns: RESERVED_PATHS (the document verbs)
# plus the app's own pages and APIs. Used to tell an app route from an authored page
# path when a markdown link omits the optional .md -- "/agents" is the Agent Activity
# page, while a relative "agents" is the sibling document. Only meaningful for
# ROOT-ANCHORED targets, so a page may still be named "agents" or "graph".
# Tracks the Route() table in main.py; add here when a top-level route is added.
APP_ROUTE_PREFIXES = frozenset(RESERVED_PATHS) | frozenset([
    "agents",
    "api",
    "editors",
    "graph",
    "health",
    "manage",
    "test",
    "upload-stream",
    "vaults",
    "ws",
])
RESERVED_FILE_TYPES = [
    "css",
    "js",
    "png",
    "jpg",
    "jpeg",
    "gif",
    "svg",
    "webp",
    "bmp",
    "md",
    "pdf",
    "canvas",
]

# First-class vault DOCUMENT types: editable/viewable pages served from /wiki/ and
# /raw/ and listed in the file manager (as opposed to ATTACHMENT_FILE_TYPES, which
# are static assets). Defined ONCE so a new document type (e.g. a future diagram
# format) can't be known to one resolver/route but forgotten in another -- the gap
# that made .canvas silently render empty. Consumed by WikiDoc._test_existence.
# A bare (extension-less) URL still maps to .md separately; this is the complete
# set of document EXTENSIONS.
DOCUMENT_FILE_TYPES = [
    "md",
    "canvas",
]

# Extensions that may be served as VAULT ATTACHMENTS (user content dropped next to
# a page, Obsidian-style) over /wiki/{vault}/... . Kept deliberately separate from
# RESERVED_FILE_TYPES (which also gates theme/template assets like css/js): this is
# a content-type allowlist, so unknown/active types are NOT served straight off disk.
# Add formats here as real data-science needs arise rather than allowing everything.
ATTACHMENT_FILE_TYPES = [
    # images / figures
    "png", "jpg", "jpeg", "gif", "svg", "webp", "bmp",
    # documents
    "pdf",
    # tabular / data-science
    "csv", "tsv", "txt", "json", "xlsx", "xls", "parquet",
]

# Subsets of ATTACHMENT_FILE_TYPES that get special INLINE treatment. Declared
# once, here, so every consumer agrees instead of each keeping its own copy (the
# upload handler, the markdown embed preprocessor, and the chat attachment
# manifest all used to hardcode their own image/data lists -- a drift source).
# Extensions are stored without a leading dot, like ATTACHMENT_FILE_TYPES.
#
# IMAGE_FILE_TYPES: rendered as an inline <img> (and excluded from the chat
# agent's data-file manifest -- you don't read a PNG into pandas).
IMAGE_FILE_TYPES = ["png", "jpg", "jpeg", "gif", "svg", "webp", "bmp"]
# PREVIEW_EMBED_FILE_TYPES: data/document files the FileEmbedPreprocessor renders
# as an inline preview card (CSV/TSV table, PDF viewer).
PREVIEW_EMBED_FILE_TYPES = ["csv", "tsv", "pdf"]
# Both must stay subsets of the attachment allowlist; fail loudly if an edit
# drifts them apart.
assert set(IMAGE_FILE_TYPES) <= set(ATTACHMENT_FILE_TYPES)
assert set(PREVIEW_EMBED_FILE_TYPES) <= set(ATTACHMENT_FILE_TYPES)
#
# Multi-vault layout. Each vault is an immediate subdirectory of VAULTS_DIR (the
# documents parent mount). Its git history lives in a SEPARATE parent, HISTORY_DIR,
# which is mounted off any Dropbox-synced location so git's churning temp files
# (index/objects/refs locks) never race the syncer. A vault's repo is split via
# `git init --separate-git-dir`.
#
# Gitlink direction: the on-disk `.git` gitlink lives inside the worktree (on Dropbox)
# and is therefore read by BOTH host and container -- but host and container mount the
# tree at different, irreconcilable offsets from the git-dir, so one stored path can't
# serve both. Resolution: the file's contents serve the HOST (the side with no override
# mechanism -- a bare terminal), pointing at HOST_HISTORY_LOCATION/{slug}; the container
# ignores the file entirely by passing --git-dir/--work-tree explicitly on every git
# call (see docversioning). So the container's app path logic stays purely on these
# /app/app-relative constants -- host coordinates enter at exactly one seam
# (vault_registry.init_vault_repo authoring the gitlink), never here.
VAULTS_DIR = "vaults"
HISTORY_DIR = "vault-history"
# Baked seed content (app/seed/ -> /app/app/seed), copied ONCE into a freshly
# provisioned vault at startup, then never again (see vault_registry._seed_vault_tree:
# a durable marker makes later passes a no-op, so user deletions of seeded files stick).
# It lives OUTSIDE the VAULTS_DIR mount ON PURPOSE: a bind/volume mounted at
# /app/app/vaults SHADOWS anything the image baked under that path, so the source
# must sit elsewhere and be COPIED THROUGH the mount into persistent storage at
# runtime. Copy-at-runtime (not bake-into-vaults) is the only mechanism that
# survives every mount kind (host bind, Dropbox path, named volume).
SEED_DIR = "seed"
# Host-facing ONLY: absolute path, on the *host* filesystem, of the git-history parent.
# Same HISTORY_LOCATION the docker-compose volume mount uses; it reaches the container
# as a runtime env var via the compose `env_file: .env` on tzaraserver/tzaraworker
# (load_dotenv() above is a no-op fallback then), so no separate `environment:` entry
# is needed and .env is NOT baked into the image. Used solely to author
# the `.git` gitlink the host's git reads; the container NEVER resolves through it. Empty
# when unset -> init_vault_repo falls back to the old relative gitlink (valid when host
# keeps vaults/ and vault-history/ as siblings, per .env.template).
HOST_HISTORY_LOCATION = os.environ.get("HISTORY_LOCATION", "")
DEFAULT_VAULT = os.environ.get("DEFAULT_VAULT", "main")
#
# The SYSTEM vault holds wiki-owned content (agent definitions, help documentation)
# rather than user notes. It is a real vault on disk (browsable/editable by direct
# URL) but is flagged system:true in the registry, hiding it from the vault switcher,
# the /vaults landing page, and the per-vault maintenance loops.
#
# It IS ingested into RAG and IS searchable -- help documentation is only useful if
# you can search it. Isolation from your notes comes from search being hard
# vault-scoped (every rag_search predicate filters on vault_id), exactly as it
# isolates any two content vaults; it is NOT an indexing exclusion. What the system
# vault is denied is LLM frontmatter generation: blessed files are human-authored,
# enforced in rag_indexer.generate_frontmatter.
SYSTEM_VAULT = os.environ.get("SYSTEM_VAULT", "dada")
#
# Agent KERNEL: the isolated jupyter server (no vault mount, agent-net only)
# where agent-file custom tools execute. The worker reaches it by compose
# hostname; nothing else can (network membership IS the isolation).
JUPYTER_AGENT_HOST = os.environ.get("JUPYTER_AGENT_HOST", "http://jupyter-agent:8888")
JUPYTER_AGENT_WS = os.environ.get("JUPYTER_AGENT_WS", "ws://jupyter-agent:8888")
# Two-layer timeouts: per tool call (interrupt, kernel survives) and per run
# (kill; the time-analog of max-steps).
AGENT_TOOL_TIMEOUT_S = float(os.environ.get("AGENT_TOOL_TIMEOUT_S", "60"))
AGENT_RUN_TIMEOUT_S = float(os.environ.get("AGENT_RUN_TIMEOUT_S", "3600"))
# HMAC secret for agent-API tokens. Minted AND verified in the worker process,
# so the random-at-boot fallback (empty value) is coherent; set it in .env only
# if you want tokens to survive worker restarts.
AGENT_API_SECRET = os.environ.get("AGENT_API_SECRET", "")
# Where the in-kernel wiki proxy reaches the worker-hosted agent-API.
AGENT_API_PORT = int(os.environ.get("AGENT_API_PORT", "8555"))
AGENT_API_URL = os.environ.get("AGENT_API_URL", f"http://tzaraworker:{AGENT_API_PORT}")
# Shared secret for the server->worker editor-kernel broker (the /editor/session/*
# routes on the same worker agent-API app). Unlike the per-run kernel token, this
# authenticates the trusted SERVER calling the worker; both containers read it from
# .env. Defense-in-depth: the broker is only reachable inside tzara-net anyway (the
# port is never host-published), so a fixed default is acceptable for local use.
EDITOR_SERVICE_SECRET = os.environ.get(
    "EDITOR_SERVICE_SECRET", AGENT_API_SECRET or "editor-service-local")
#
# Agent SCHEDULER (worker-side loop): each tick rescans the agent files (the
# rescan IS the registry reconcile), fires agents whose `schedule:` is due per
# their redis last-run stamp, and garbage-collects staged batches older than
# the TTL. Rules without a time clause run at AGENT_DEFAULT_RUN_HOUR.
AGENT_SCHEDULER_ENABLED = os.environ.get("AGENT_SCHEDULER_ENABLED", "false").lower() in ("1", "true", "yes")
# The tick is also the RESOLUTION FLOOR for sub-hour schedules ("every 15
# minutes", "3 times an hour", cron "*/5 * * * *"): a rule finer than the tick fires only
# once per tick. Default 60s so minute-grained schedules are honored to within a
# minute; raise it if the per-minute rescan+GC cost matters more than precision.
AGENT_SCHEDULER_TICK_S = int(os.environ.get("AGENT_SCHEDULER_TICK_S", "60"))
AGENT_DEFAULT_RUN_HOUR = int(os.environ.get("AGENT_DEFAULT_RUN_HOUR", "4"))
AGENT_STAGING_TTL_DAYS = int(os.environ.get("AGENT_STAGING_TTL_DAYS", "14"))
#
# Event triggers: agents' `on:` frontmatter (src.agent_events grammar) fires
# runs off application events (agent lifecycle, staging decisions, uploads).
# Emission is gated here too - disabling stops stream growth at the source.
EVENT_TRIGGERS_ENABLED = os.environ.get("EVENT_TRIGGERS_ENABLED", "false").lower() in ("1", "true", "yes")
# Loop guards for event-triggered runs (see src.agent_events / src.events):
# chained-trigger depth cap; per-agent cooldown between event fires; per-agent
# fires-per-hour budget (over-budget DEFERS, pool retention, never drops);
# pooled events older than this are discarded (post-outage staleness).
EVENT_MAX_DEPTH = int(os.environ.get("EVENT_MAX_DEPTH", "3"))
EVENT_COOLDOWN_S = int(os.environ.get("EVENT_COOLDOWN_S", "600"))
EVENT_BUDGET_PER_HOUR = int(os.environ.get("EVENT_BUDGET_PER_HOUR", "6"))
EVENT_MAX_AGE_S = int(os.environ.get("EVENT_MAX_AGE_S", "86400"))
EVENT_STREAM_MAXLEN = int(os.environ.get("EVENT_STREAM_MAXLEN", "10000"))
#
# Agent-owned output area INSIDE each content vault: vaults/{v}/{AGENT_OUTPUT_DIR}/
# {agent}/... . Ownership is derived from LOCATION: everything under it is freely
# overwritten by its agent, excluded from RAG by path (watcher ignore + indexer
# guard), served/browsable normally, and banner-marked in the document view. A human
# takes ownership of a page by moving it OUT of this directory.
AGENT_OUTPUT_DIR = os.environ.get("AGENT_OUTPUT_DIR", "_dada")
# Location-derived RAG exclusion: joining EXCLUDED_FOLDERS covers ingest_document's
# _is_excluded guard AND the bulk reindex/metadata loops in one move. (The watcher
# ignores the folder separately via file_watcher.IGNORED_DIRS, and the agent's own
# writer owns the git commit - excluded files are never watcher-committed.)
EXCLUDED_FOLDERS.add(AGENT_OUTPUT_DIR)

# Cross-run agent memory (see AGENT_MEMORY_PLAN.md): a self-curated handoff note
# per agent under _dada/{agent}/, written by the reserved memory turn at the end of
# a run and injected into the NEXT run's system prompt. Opt-in via `memory:`
# frontmatter. Distinct from the output page (human-facing report) and the per-run
# log pages.
AGENT_MEMORY_FILE = os.environ.get("AGENT_MEMORY_FILE", "memory.md")
# Memory budget. ONE number governs both how much memory text is injected into the
# system prompt AND how much the memory turn may generate - see
# background_agents.memory_budget(), which derives them together.
#
# They used to be independent constants and they disagreed: generation was capped
# at 2048 tokens (~7168 chars) while only the last 6000 chars were ever injected,
# so 1168 chars of every verbose consolidation were silently discarded - and being
# a TAIL cap, what got dropped was the opening, where a handoff note puts its
# durable facts.
#
# The cap now scales with the model's context window instead of being a fixed
# char count. A fixed 6000 is 1.3% of a 128K window but 42% of a 4K one: far too
# timid for the first model and ruinous for the second. INJECT_CHARS remains the
# fallback for servers that do not report a context length.
AGENT_MEMORY_INJECT_CHARS = int(os.environ.get("AGENT_MEMORY_INJECT_CHARS", "6000"))
# Share of the context window memory may occupy. Deliberately small: memory rides
# in the system prompt of EVERY turn, so raising this raises the cost of the whole
# run, not just the one consolidation call.
AGENT_MEMORY_CONTEXT_FRACTION = float(
    os.environ.get("AGENT_MEMORY_CONTEXT_FRACTION", "0.03"))
AGENT_MEMORY_MIN_CHARS = int(os.environ.get("AGENT_MEMORY_MIN_CHARS", "1000"))
AGENT_MEMORY_MAX_CHARS = int(os.environ.get("AGENT_MEMORY_MAX_CHARS", "16000"))
# Append-only ledgers, beside memory.md in the same owned directory. Memory is
# rewritten wholesale by an LLM every run and so cannot hold a fact that must
# survive many runs; ledger rows are merged by code (see src/ledgers.py) and are
# the durable half. Caps are per owner.
AGENT_LEDGERS_FILE = os.environ.get("AGENT_LEDGERS_FILE", "ledgers.md")
# Rows per ledger and ledgers per owner. Overflow REFUSES the write and warns
# rather than trimming: dropping the oldest row is undefined without per-row
# timestamps, and a silently capped list is the exact failure ledgers exist to
# prevent.
#
# These are STORAGE caps, and they are deliberately NOT derived from the model's
# context window - injection is. The two answer different questions:
#   write caps  = durability policy, static, human-facing
#   injection   = display policy, dynamic, model-facing (see the fraction below)
# Scaling these to the context window would LOWER them on a small model and start
# refusing writes, converting a display problem into data loss. Since the ledgers
# page degrades gracefully rather than disappearing (ledgers.render_capped plus
# the `recall` tool), storage no longer has to fit the prompt at all.
#
# MAX_COUNT is anchored: the ledger INDEX - one heading line per ledger - is the
# one thing injection can never degrade, since a ledger the model cannot name is
# a ledger it cannot recall. Ten headings at ~50 chars is ~500 chars, already
# half of AGENT_MEMORY_MIN_CHARS (what a 4K-context model gets). So the ceiling
# is roughly MIN_CHARS / (2 * heading_chars).
# MAX_ITEMS is a HUMAN threshold, not a machine one: ~10KB on a page whose whole
# premise is that a person can open it and hand-edit it (see src/ledgers.py).
AGENT_LEDGER_MAX_ITEMS = int(os.environ.get("AGENT_LEDGER_MAX_ITEMS", "500"))
AGENT_LEDGER_MAX_COUNT = int(os.environ.get("AGENT_LEDGER_MAX_COUNT", "10"))
# Share of the context window the ledgers page may occupy when injected. Separate
# from the memory fraction: ledgers are terse rows, memory is prose.
AGENT_LEDGER_CONTEXT_FRACTION = float(
    os.environ.get("AGENT_LEDGER_CONTEXT_FRACTION", "0.03"))
AGENT_LEDGER_MAX_CHARS = int(os.environ.get("AGENT_LEDGER_MAX_CHARS", "16000"))
# Default row window for the `recall` tool, which fetches rows the injected view
# elided. Static, unlike the injection budget above, and that is the point: the
# system prompt rides EVERY turn and so must scale with the window, while a tool
# result is one-shot. The schema's ceiling is AGENT_LEDGER_MAX_ITEMS - asking for
# more rows than a ledger can hold is meaningless.
AGENT_LEDGER_RECALL_ROWS = int(os.environ.get("AGENT_LEDGER_RECALL_ROWS", "200"))

# --- Output-collapse guard ---------------------------------------------------
# A run whose final message is a small fraction of the page it would overwrite is
# treated as SUSPECT: the output page is preserved, and the reserved memory +
# ledger turns are skipped so a bad turn cannot rewrite the note or append a row
# for work that never happened. Deliberately CONTENT-AGNOSTIC - the failure this
# guards against (a wrong-context reply) reads perfectly well, so no refusal
# phrase or prompt-regurgitation marker can catch it, but the size collapse is
# unmistakable. Both conditions must hold, so an agent whose report is legitimately
# terse is not permanently blocked from shrinking its own page, and an agent with
# no previous output is never suspect. Neither number is derivable; these are a
# starting point tuned to leave normal day-to-day variation alone. 0 disables.
AGENT_OUTPUT_COLLAPSE_RATIO = float(
    os.environ.get("AGENT_OUTPUT_COLLAPSE_RATIO", "0.25"))
AGENT_OUTPUT_COLLAPSE_FLOOR = int(
    os.environ.get("AGENT_OUTPUT_COLLAPSE_FLOOR", "800"))

# Wall-clock bound on the single reserved memory-turn LLM call (the +1 iteration).
AGENT_MEMORY_TURN_TIMEOUT_S = float(os.environ.get("AGENT_MEMORY_TURN_TIMEOUT_S", "300"))
# The memory step's SECOND call - a tool-only turn that records to ledgers. Its
# own (shorter) bound: it emits a tool call, not prose, and its input is the note
# plus a transcript tail rather than the whole session. Measured ~3.6s.
AGENT_LEDGER_TURN_TIMEOUT_S = float(os.environ.get("AGENT_LEDGER_TURN_TIMEOUT_S", "120"))
# How much of the run transcript that call sees. A tail: what a run established
# is near its end, and keeping this small is the whole cost argument for a second
# call over a second full loop iteration.
AGENT_LEDGER_TURN_TRANSCRIPT_CHARS = int(
    os.environ.get("AGENT_LEDGER_TURN_TRANSCRIPT_CHARS", "6000"))
# NOTE: the memory turn's generation cap is NOT a separate constant. It is derived
# from the injection budget by background_agents.memory_budget(), because any text
# generated past what will be injected is discarded - see AGENT_MEMORY_INJECT_CHARS
# above for what happened when the two were set independently.

# --- Agents standing aside for a person -------------------------------------
# The LLM server is serial, so an agent turn in flight delays interactive chat no
# matter how the in-process gate is configured: measured 2026-08-02, agent turns
# average 26.3s and memory consolidation 169.2s, and a chat request arriving
# mid-call waits it out. Being "ungated" never protected interactive work; this
# does, by having agents pause BETWEEN turns while a person is around.
# Deterministic spread for schedules that name no time. `daily` and `weekly` both
# resolve to AGENT_DEFAULT_RUN_HOUR:00, so every such agent fires on the same
# minute - a thundering herd that grows with each agent added. Each agent gets a
# stable offset derived from its slug, so they fan out across this window without
# anyone editing a schedule. Deterministic rather than random so an agent's run
# time stays predictable across restarts. 0 disables.
AGENT_SCHEDULE_JITTER_MINUTES = int(
    os.environ.get("AGENT_SCHEDULE_JITTER_MINUTES", "60"))

# How long the durable failure log keeps a row after it was LAST seen (not first:
# with coalescing, pruning on first-seen would delete a continuously-failing thing
# out from under an open badge). Pruned on the always-on maintenance tick.
FAILURE_LOG_TTL_DAYS = int(os.environ.get("FAILURE_LOG_TTL_DAYS", "30"))

HUMAN_ACTIVE_TTL_S = int(os.environ.get("HUMAN_ACTIVE_TTL_S", "90"))
AGENT_YIELD_TO_HUMAN = os.environ.get("AGENT_YIELD_TO_HUMAN", "true").lower() in (
    "1", "true", "yes")
# Poll interval while standing aside, and the cap on how long an agent will do so
# before proceeding anyway - a scheduled agent must still make progress on a day
# when someone is reading the wiki all afternoon.
#
# AGENT_YIELD_MAX_S is spent PER RUN, not per step (agent_runner enforces this).
# It is drawn from the same budget as the run itself: a run must finish inside
# AGENT_RUN_LOCK_TTL_S (7200s) or the global run lock expires under it and a
# second mutating run can start. Keep yield + work comfortably below that.
AGENT_YIELD_POLL_S = float(os.environ.get("AGENT_YIELD_POLL_S", "5"))
AGENT_YIELD_MAX_S = float(os.environ.get("AGENT_YIELD_MAX_S", "300"))


def vault_root(slug: str) -> str:
    """Working-tree root of a vault, relative to CWD (e.g. 'vaults/main')."""
    return os.path.join(VAULTS_DIR, slug)


def vault_abs_root(slug: str) -> str:
    """Absolute container path of a vault's working tree."""
    return os.path.join(os.getcwd(), VAULTS_DIR, slug)


def vault_git_dir(slug: str) -> str:
    """Absolute container path of a vault's separated git directory."""
    return os.path.join(os.getcwd(), HISTORY_DIR, slug)


# Theme folder names. Membership in this set is the SOLE validator for a theme name
# arriving from a hand-edited .tzara/config.json or a URL segment: an entry here is
# by construction a single path component that exists on disk, so it can neither
# traverse out of TEMPLATE_DIR nor break out of an HTML attribute.
#
# TTL-cached rather than scanned once at import, because the vault settings UI
# renders a theme picker from it: a theme folder added to a running server would
# otherwise be missing from the dropdown, and a vault set to it would silently
# render `default`. Same idiom (and roughly the same window) as vault_index's cache.
_TEMPLATE_TTL_SECONDS = 3.0
_template_cache: tuple[float, frozenset] | None = None


def _scan_templates() -> frozenset:
    root = os.path.join(os.getcwd(), TEMPLATE_DIR)
    try:
        return frozenset(
            n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n))
        )
    except OSError:
        return frozenset({"default"})


def available_templates() -> frozenset:
    """Theme folder names currently on disk (TTL-cached)."""
    global _template_cache
    now = time.monotonic()
    if _template_cache is None or (now - _template_cache[0]) > _TEMPLATE_TTL_SECONDS:
        _template_cache = (now, _scan_templates())
    return _template_cache[1]


def is_template(name: str) -> bool:
    """True if ``name`` is a real theme folder. False for None/empty/unknown."""
    return bool(name) and name in available_templates()


def seed_abs_root(name: str) -> str:
    """Absolute container path of a baked seed tree (e.g. 'system', 'default').
    Deliberately NOT under VAULTS_DIR, so the vaults mount can't shadow it."""
    return os.path.join(os.getcwd(), SEED_DIR, name)
