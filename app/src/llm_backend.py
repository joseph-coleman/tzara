# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""LLM backend provider seam.

Tzara historically spoke only to Ollama, via the `ollama` SDK, through
`OllamaManager`. This module generalizes that into a small provider seam so the
chat/agent stack can run against Ollama, Lemonade, or any OpenAI-compatible
server, chosen by `LLM_PROVIDER` (see config.py).

The design rests on two ORTHOGONAL surfaces, each verified empirically
(app/.test/lemonade_v1_tool_stream_probe.py, compare_embedding_endpoints.py):

  * INFERENCE (chat / generate / stream / tools) -> the OpenAI-compatible /v1
    surface, which is UNIVERSAL. Ollama (`/v1`), Lemonade (`/api/v1`), vLLM,
    LocalAI, llama.cpp all speak it; and unlike Lemonade's Ollama-compat *chat*
    mount (which BUFFERS the whole reply into one chunk when a tool list is
    present, killing streaming), the /v1 mount streams content tokens even with a
    non-empty tool set, on BOTH Ollama and Lemonade. Reasoning arrives on a
    SEPARATE channel (`delta.reasoning_content`/`reasoning`) so it never pollutes
    content nor breaks tool-call parsing -- which also means the native-API
    malformed-tool-call `raw='...'` salvage hack is unnecessary here.

  * MANAGEMENT + EMBEDDINGS (list/show/ps/pull/warm/unload, capability discovery,
    embed) -> the Ollama-native `/api/*` surface, WHEN the server offers it.
    OpenAI has no model-management concept, and embeddings are coupled to the
    model catalog (Lemonade's /v1 mount uses different model names than its /api
    mount). So these ride the native mount where present, and capabilities
    (tools/thinking/context_length) are DISCOVERED via /api/show -- never declared
    as config flags.

Structure: `OllamaManager` (ollama_manager.py) remains the native implementation
of the management/embed/capability surface AND the `ollama-native` fallback. The
two seam backends SUBCLASS it and override ONLY the five inference methods to go
through /v1 (`_OpenAIChatMixin`):

  HybridBackend  -- ollama & lemonade: native management/embeddings + /v1 chat.
  OpenAIBackend  -- pure OpenAI servers: /v1 chat, degraded management.

`create_llm_backend()` is the single factory the app constructs through.
"""
import json
import logging
import time
from collections.abc import AsyncGenerator
from contextvars import ContextVar

import httpx

from config import (
    LLM_BASE_URL,
    LLM_HAS_NATIVE_MOUNT,
    LLM_PROVIDER,
    OLLAMA_CONTEXT_BUDGET,
    OLLAMA_EMBED_MODEL,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_NUM_CTX,
    OLLAMA_URL,
)
from src.ollama_manager import ChatStreamChunk, OllamaManager

logger = logging.getLogger("llm_backend")

# httpx timeouts for the /v1 inference path: mirror OllamaManager's generous read
# window (long generations) with a short connect so a dead host fails fast.
_V1_TIMEOUT = httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0)

# Management/introspection calls (health, unload, system-stats) must fail FAST -- they
# gate page renders, so borrowing the 300s inference read timeout would hang the manage
# page for minutes on a wedged backend. Loading a model, by contrast, is genuinely slow
# (a 120B into memory), so /v1/load gets its own patient read window.
_MGMT_TIMEOUT = httpx.Timeout(connect=3.0, read=15.0, write=10.0, pool=3.0)
_LOAD_TIMEOUT = httpx.Timeout(connect=5.0, read=240.0, write=10.0, pool=5.0)


def _v1_model_name(name: str) -> str:
    """Normalize an Ollama-style model name for the OpenAI /v1 surface.

    `:latest` is an OLLAMA tag convention: the same model is `foo:latest` on the
    Ollama /api mount but bare `foo` on the /v1 mount (Lemonade's /v1/embeddings
    404s the `:latest` form; /v1/chat tolerates it). Stripping ONLY a trailing
    `:latest` is universally safe -- Ollama re-defaults a missing tag to `:latest`,
    and real tags (`:20b`, `:33m`) are left intact. In practice names are
    configured per-server so this rarely fires; it's defensive.
    """
    if name and name.endswith(":latest"):
        return name[: -len(":latest")]
    return name


def _to_openai_tools(tools: list[dict] | None) -> list[dict] | None:
    """Ollama tool definitions are already OpenAI-shaped
    (`{"type":"function","function":{name,description,parameters}}`), so this is a
    passthrough today -- kept as the single seam point should the shapes diverge.
    """
    return tools or None


# --- Session boundaries: starting on a cold slot --------------------------
# llama.cpp-backed servers hold each request's KV in a slot and restore it for the
# next request whose prompt matches. A slot that restores the WRONG conversation
# lets one caller generate under another's system prompt - across vaults, which is
# the isolation boundary Tzara otherwise enforces everywhere.
#
# `cache_prompt: false` is the lever: verified against Lemonade/llama.cpp, it stops
# the server RESTORING a cached prefix but still lets it STORE this request's
# tokens. So one cold call at the head of a session buys a slot that cannot carry
# a foreign context, and every later turn of that session prefix-matches its OWN
# first turn at full speed. We hope.
#
# A ContextVar rather than an attribute because the web process shares ONE backend
# instance across every concurrent request (main.py lifespan); per-task context is
# what keeps one chat's cold start from disarming another's. Set it in the same
# task that makes the call - generators run in the task that iterates them.
#
# This narrows exposure to session boundaries; it does not eliminate it. Callers
# that need the guarantee on every turn set `no_prompt_cache` instead (the worker
# does, via LLM_AGENT_NO_PROMPT_CACHE).
#
# Three levers in total, by scope: this ContextVar (per SESSION, armed at the
# boundary), `no_prompt_cache` (per BACKEND, the worker's blanket opt-out), and
# `_v1_body(no_cache=True)` (per CALL, for a prompt that is a session of one -
# see `generate`). None of them reach the `ollama-native` fallback, whose
# generate/chat go through the Ollama SDK with no /v1 body to carry the field.
_cold_start: ContextVar[bool] = ContextVar("llm_cold_start", default=False)


def begin_cold_session() -> None:
    """Make the NEXT inference call in this task ignore the server's prompt cache.

    Call at a session boundary: a new chat conversation, an agent run, an editor
    tool invocation. One-shot - the call that consumes it disarms it, so the rest
    of the session caches normally. No-op on the `ollama-native` fallback, which
    has no /v1 body to carry the field.
    """
    _cold_start.set(True)


def _to_openai_messages(messages: list[dict], system: str | None) -> list[dict]:
    """Translate Tzara's Ollama-shaped message history into OpenAI /v1 shape.

    The load-bearing difference is the tool round-trip. The agent loop appends
    history as:
        assistant: {"role":"assistant","content":..,"tool_calls":[
                        {"type":"function","function":{"index","name","arguments":<dict>}}]}
        result:    {"role":"tool","tool_name":<name>,"content":<result>}
    OpenAI /v1 instead requires each assistant tool_call to carry an `id` (with
    args as a JSON *string*) and each tool result to reference it via
    `tool_call_id`. Ollama-shaped history has no ids at all, so we synthesize
    deterministic per-request ids and pair each `role:tool` message to the
    matching assistant tool_call (by name, falling back to FIFO order). Ids only
    need to be consistent WITHIN one request payload (verified: an arbitrary id
    string round-trips), so nothing has to persist across turns.

    Any `thinking`/`reasoning` field on historical assistant messages is dropped
    (reasoning is a display channel, not part of the model-facing transcript).
    """
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})

    # ids from the most recent assistant tool_calls, awaiting their tool results
    pending: list[tuple[str, str]] = []  # (tool_call_id, function_name)

    for i, m in enumerate(messages):
        role = m.get("role")

        if role == "assistant" and m.get("tool_calls"):
            pending = []
            oai_calls = []
            for j, tc in enumerate(m["tool_calls"]):
                func = tc.get("function", {}) if isinstance(tc, dict) else {}
                name = func.get("name", "")
                args = func.get("arguments", {})
                args_str = args if isinstance(args, str) else json.dumps(args or {})
                cid = f"call_{i}_{j}"
                oai_calls.append({
                    "id": cid,
                    "type": "function",
                    "function": {"name": name, "arguments": args_str},
                })
                pending.append((cid, name))
            content = m.get("content")
            out.append({
                "role": "assistant",
                "content": content if content else None,
                "tool_calls": oai_calls,
            })

        elif role == "tool":
            name = m.get("tool_name") or m.get("name") or ""
            cid = None
            for k, (pid, pname) in enumerate(pending):
                if pname == name:
                    cid = pid
                    pending.pop(k)
                    break
            if cid is None and pending:
                cid, _ = pending.pop(0)
            if cid is None:
                cid = f"call_orphan_{i}"
            out.append({
                "role": "tool",
                "tool_call_id": cid,
                "content": m.get("content", ""),
            })

        else:
            # user, plain assistant, or system: pass content straight through.
            out.append({"role": role, "content": m.get("content", "")})

    return out


def _finalize_tool_calls(acc: dict[int, dict]) -> tuple[list[dict] | None, str | None]:
    """Turn the index-keyed /v1 tool-call accumulator into Ollama-shaped calls.

    Returns (tool_calls, error). Each entry is `{"function":{"name","arguments":
    <dict>}}` -- the shape agent_runner._normalize_tool_call consumes (it does
    `dict(arguments)`). A JSON parse failure on the streamed args string surfaces
    as an error string so run_agent_loop treats the turn as a broken stream and
    RETRIES, rather than silently continuing with an empty/garbled call.
    """
    if not acc:
        return None, None
    calls = []
    for idx in sorted(acc):
        slot = acc[idx]
        name = slot.get("name") or ""
        raw = slot.get("args_str") or ""
        try:
            args = json.loads(raw) if raw.strip() else {}
        except (ValueError, TypeError) as e:
            return None, f"malformed tool-call arguments for {name!r}: {e} (raw={raw!r})"
        calls.append({"function": {"name": name, "arguments": args}})
    return calls, None


class _OpenAIChatMixin:
    """The five inference methods, routed through an OpenAI-compatible /v1 server.

    Mixed in AHEAD of OllamaManager in the MRO so these override the native chat
    implementations while every other method (management, embeddings, capability
    discovery, history math) is inherited unchanged. Subclasses must call
    `_init_v1(base_url)` from __init__ after OllamaManager.__init__.
    """

    # populated by _init_v1
    _v1_base: str
    _v1_client: httpx.AsyncClient
    # EVERY call bypasses the cache, not just the session's first. Opt-in per
    # CALLER, not per provider: set by the worker (see
    # background_agents.make_worker_ollama) so background runs forgo the server's
    # prompt cache, while the web process keeps it and cold-starts per session.
    # `cache_prompt` is a llama.cpp SERVER parameter, not a model one - servers
    # that do not implement it ignore the field.
    no_prompt_cache: bool = False

    def _init_v1(self, base_url: str):
        self._v1_base = base_url.rstrip("/")
        self._v1_client = httpx.AsyncClient(timeout=_V1_TIMEOUT)

    def _v1_body(self, messages, system, *, tools=None, stream=False, model=None,
                 no_cache=False):
        body = {
            "model": _v1_model_name(model or self.model),  # type: ignore[attr-defined]
            "messages": _to_openai_messages(messages, system),
            "stream": stream,
        }
        # A call that is already bypassing the cache must not CONSUME the
        # session's one-shot token: the token means "the next call that would
        # otherwise restore a prefix", and this one would not. Reading it here
        # would let a utility `generate` disarm the cold start its caller armed
        # for the real first turn.
        already_cold = self.no_prompt_cache or no_cache
        cold = False
        if not already_cold:
            cold = _cold_start.get()
            if cold:
                _cold_start.set(False)  # one-shot: the rest of the session caches
        if already_cold or cold:
            body["cache_prompt"] = False
        oai_tools = _to_openai_tools(tools)
        if oai_tools:
            body["tools"] = oai_tools
        return body

    async def chat_stream_with_tools(
        self, messages: list[dict], tools: list[dict],
        system: str | None = None, think: bool = False,
    ) -> AsyncGenerator[ChatStreamChunk, None]:
        """Stream a tool-enabled turn. Yields ChatStreamChunk; the final chunk
        (done=True) carries any tool_calls in Ollama shape. `think` is accepted
        for signature parity but is a no-op on /v1: reasoning arrives on a
        separate channel and is dropped, so it never interleaves with content or
        tool calls (the very failure `think=False` guarded against natively)."""
        self.touch()  # type: ignore[attr-defined]
        body = self._v1_body(messages, system, tools=tools, stream=True)
        acc: dict[int, dict] = {}
        try:
            async with self._v1_client.stream(
                "POST", f"{self._v1_base}/chat/completions", json=body
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[len("data:"):].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        continue
                    choice = (obj.get("choices") or [{}])[0]
                    delta = choice.get("delta", {}) or {}
                    content = delta.get("content")
                    if content:
                        yield ChatStreamChunk(token=content, done=False, tool_calls=None)
                    for call in delta.get("tool_calls") or []:
                        idx = call.get("index", 0)
                        slot = acc.setdefault(idx, {"name": None, "args_str": ""})
                        fn = call.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args_str"] += fn["arguments"]
        except (httpx.HTTPError, httpx.StreamError) as e:
            logger.warning("/v1 chat stream failed: %s", e)
            yield ChatStreamChunk(token="", done=True, tool_calls=None, error=f"stream failed: {e!r}")
            return

        tool_calls, err = _finalize_tool_calls(acc)
        yield ChatStreamChunk(token="", done=True, tool_calls=tool_calls, error=err)

    async def chat_stream(
        self, messages: list[dict], system: str | None = None, model: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream content tokens (no tools). Mirrors OllamaManager.chat_stream,
        including the per-call `model` override used by the /edit/ path."""
        self.touch()  # type: ignore[attr-defined]
        body = self._v1_body(messages, system, stream=True, model=model)
        async with self._v1_client.stream(
            "POST", f"{self._v1_base}/chat/completions", json=body
        ) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                choice = (obj.get("choices") or [{}])[0]
                token = (choice.get("delta", {}) or {}).get("content")
                if token:
                    yield token

    async def _chat_once(self, messages, system, *, model=None, max_tokens=None,
                         no_cache=False) -> dict:
        """Non-streaming /v1 chat; returns the raw OpenAI message dict."""
        self.touch()  # type: ignore[attr-defined]
        body = self._v1_body(messages, system, stream=False, model=model,
                             no_cache=no_cache)
        if max_tokens:
            body["max_tokens"] = max_tokens
        resp = await self._v1_client.post(f"{self._v1_base}/chat/completions", json=body)
        resp.raise_for_status()
        return (resp.json().get("choices") or [{}])[0].get("message", {}) or {}

    async def generate(self, prompt: str, max_tokens: int | None = None) -> str:
        """Single-prompt completion, expressed as a one-message /v1 chat so it
        works on any OpenAI-compatible server. `max_tokens` caps the response
        (the num_predict analog) - used by the background metadata task.

        Always starts cold. A single-prompt completion is a session of ONE: there
        is no later turn to prefix-match, so restoring a cached prefix can only
        supply another caller's context. Every caller here (doc tags/summary,
        chat checkpoints, agent memory notes, query rewrite) is a one-off whose
        prompt shares no prefix with its own session, so nothing reusable is
        given up. Passed explicitly rather than via `begin_cold_session` so a
        utility call cannot consume the token its CALLER armed for a real session
        boundary (query rewrite runs before the chat turn it belongs to)."""
        msg = await self._chat_once([{"role": "user", "content": prompt}], None,
                                    max_tokens=max_tokens, no_cache=True)
        return msg.get("content", "") or ""

    async def aclose(self):
        """Close the /v1 httpx client. Called per-run in the worker (each agent
        run builds its own backend) and on server shutdown, so the client isn't
        leaked."""
        await self._v1_client.aclose()


class HybridBackend(_OpenAIChatMixin, OllamaManager):
    """ollama / lemonade: native management + embeddings + capability discovery
    (inherited from OllamaManager, all working against the server's /api/* mount),
    with chat/generate/streaming overridden to the /v1 mount for correct token
    streaming with tools. `v1_base` is the only per-provider difference (Ollama
    `/v1` vs Lemonade `/api/v1`); management still targets `url`."""

    def __init__(self, url: str, model: str, keep_alive: str = "30m",
                 num_ctx_request: int = 0, context_budget: int = 0,
                 v1_base: str | None = None):
        super().__init__(url=url, model=model, keep_alive=keep_alive,
                         num_ctx_request=num_ctx_request, context_budget=context_budget)
        self._init_v1(v1_base or f"{url.rstrip('/')}/v1")

    async def _discover_ctx_ceiling(self) -> int | None:
        """Model ceiling from the OpenAI /v1/models/{id} route. Lemonade exposes
        `max_context_window` (the capability ceiling) there even when native /api/show
        returns -1; also honors `recipe_options.ctx_size` if present as a tighter bound.
        Harmless on Ollama: /api/show already gave a ceiling so this never fires, and its
        /v1/models carries neither field. (Loaded-actual discovery is separate, in
        LemonadeBackend._discover_loaded_ctx.)"""
        try:
            resp = await self._v1_client.get(
                f"{self._v1_base}/models/{_v1_model_name(self.model)}",
                timeout=_MGMT_TIMEOUT,
            )
            resp.raise_for_status()
            body = resp.json()
            loaded = (body.get("recipe_options") or {}).get("ctx_size")
            if isinstance(loaded, int) and loaded > 0:
                return loaded
            ceiling = body.get("max_context_window")
            if isinstance(ceiling, int) and ceiling > 0:
                return ceiling
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("v1 models context discovery failed: %s", e)
        return None


class LemonadeBackend(HybridBackend):
    """Lemonade (AMD) exposes native /v1/* management endpoints beyond the Ollama
    /api/* mount HybridBackend inherits, and on those endpoints it tells the truth
    where the /api/* surface lies or no-ops:

      - /v1/health       real per-model process view (PID + loaded ctx_size), the
                         authoritative "is it loaded / at what window" source;
      - /v1/unload       real eviction -- keep_alive='0' is a no-op against
                         Lemonade's per-model llama-server processes;
      - /v1/system-stats host CPU/GPU/mem (Lemonade reports no truthful *per-model*
                         VRAM on unified memory, so the /api/ps size_vram is bogus).

    These hang off the server ROOT (self.url), NOT the /api/v1 chat mount (_v1_base).
    Only management/introspection is overridden; inference + embeddings stay inherited."""

    # True until a health probe hits a connection-level failure; surfaced in get_status
    # so the manage page can say "unreachable" instead of a plausible-but-wrong "not
    # loaded" (#6). Reset on every _health_models() call.
    _health_reachable: bool = True

    def _lm_url(self, path: str) -> str:
        """Build a Lemonade native /v1/* management URL off the server root."""
        return f"{self.url.rstrip('/')}{path}"

    async def _health_models(self) -> list[dict]:
        """Return /v1/health's all_models_loaded list (the running-process view), and
        record reachability. A connection-level failure (server down) sets
        _health_reachable=False so callers can distinguish it from "reachable, nothing
        loaded"; an HTTP/parse error is still reachable. Never raises."""
        try:
            resp = await self._v1_client.get(self._lm_url("/v1/health"), timeout=_MGMT_TIMEOUT)
            resp.raise_for_status()
            self._health_reachable = True
            return resp.json().get("all_models_loaded", []) or []
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.PoolTimeout) as e:
            self._health_reachable = False
            logger.debug("lemonade /v1/health unreachable: %s", e)
            return []
        except (httpx.HTTPError, ValueError) as e:
            self._health_reachable = True
            logger.debug("lemonade /v1/health failed: %s", e)
            return []

    # --- Context: measured loaded window (authoritative) from the process view ---
    async def _discover_loaded_ctx(self) -> int | None:
        """The ACTUAL loaded window: /v1/health's `recipe_options.ctx_size` for this
        model. Authoritative for budgeting -- it's what Lemonade really loaded, which
        may differ from the ask. None when the model isn't loaded (then the base
        precedence falls through to the ask, then the /v1/models ceiling)."""
        entry = self._entry_for(await self._health_models(), self.model)
        if entry is not None:
            cs = (entry.get("recipe_options") or {}).get("ctx_size")
            if isinstance(cs, int) and cs > 0:
                return cs
        return None

    # --- The ASK: request a specific window via /v1/load ---
    async def _load(self, ctx_size: int | None = None):
        body: dict[str, object] = {"model_name": _v1_model_name(self.model)}
        if isinstance(ctx_size, int) and ctx_size > 0:
            body["ctx_size"] = ctx_size
        try:
            resp = await self._v1_client.post(
                self._lm_url("/v1/load"), json=body, timeout=_LOAD_TIMEOUT
            )
            resp.raise_for_status()
            logger.info("lemonade load %s ctx_size=%s: %s",
                        body["model_name"], ctx_size, resp.json())
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("lemonade /v1/load failed for %s: %s", body["model_name"], e)

    async def warm_model(self):
        """Warm = apply the ASK. With OLLAMA_NUM_CTX set, load explicitly at that window
        via /v1/load (Lemonade ignores native generate's num_ctx). Without it, use the
        inherited native generate-warm (Lemonade auto-sizes)."""
        if self.num_ctx_request > 0:
            await self._load(ctx_size=self.num_ctx_request)
        else:
            await super().warm_model()
        self._invalidate_ctx()

    # --- Real eviction via /v1/unload ---
    async def _unload(self, model_name: str | None):
        body = {"model_name": model_name} if model_name else {}
        try:
            resp = await self._v1_client.post(
                self._lm_url("/v1/unload"), json=body, timeout=_MGMT_TIMEOUT
            )
            resp.raise_for_status()
            logger.info("lemonade unload %s: %s", model_name or "(all)", resp.json())
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("lemonade /v1/unload failed for %s: %s", model_name, e)

    async def unload_model(self):
        await self._unload(_v1_model_name(self.model))
        self._invalidate_ctx()

    async def unload_any_model(self, model_name: str):
        await self._unload(_v1_model_name(model_name))
        if _v1_model_name(model_name) == _v1_model_name(self.model):
            self._invalidate_ctx()

    # --- Truthful liveness + status from the process view ---
    @staticmethod
    def _entry_for(models: list[dict], model_name: str) -> dict | None:
        target = _v1_model_name(model_name)
        return next((m for m in models if m.get("model_name") == target), None)

    async def is_model_loaded(self) -> bool:
        return self._entry_for(await self._health_models(), self.model) is not None

    async def is_any_model_loaded(self, model_name: str) -> bool:
        return self._entry_for(await self._health_models(), model_name) is not None

    async def _system_stats(self) -> dict:
        try:
            resp = await self._v1_client.get(
                self._lm_url("/v1/system-stats"), timeout=_MGMT_TIMEOUT
            )
            resp.raise_for_status()
            return resp.json() or {}
        except (httpx.HTTPError, ValueError) as e:
            logger.debug("lemonade /v1/system-stats failed: %s", e)
            return {}

    async def get_status(self) -> dict:
        models = await self._health_models()
        reachable = self._health_reachable
        entry = self._entry_for(models, self.model)
        idle_seconds = time.time() - self.last_activity if self.last_activity else None
        status = {
            "model": self.model,
            "url": self.url,
            "keep_alive": self.keep_alive,
            "reachable": reachable,
            "loaded": entry is not None,
            "last_activity": self.last_activity,
            "idle_seconds": round(idle_seconds, 1) if idle_seconds else None,
        }
        try:
            status["context_length"] = await self.get_context_length()
        except Exception:
            status["context_length"] = None
        if entry is not None:
            status["device"] = entry.get("device")
            # No truthful per-model VRAM on unified memory; report the HOST figure
            # from /v1/system-stats, honestly labeled by the render layer.
            stats = await self._system_stats()
            if stats.get("vram_gb") is not None:
                status["vram_gb"] = stats["vram_gb"]
            if stats.get("memory_gb") is not None:
                status["memory_gb"] = stats["memory_gb"]
        return status

    async def get_model_info(self, model_name: str) -> dict | None:
        """Loaded-model info for the embedding section. Lemonade's health entry carries
        device but no per-model size, so this returns {device} when loaded (enough to
        drive the 'Loaded' display) or None when absent."""
        entry = self._entry_for(await self._health_models(), model_name)
        return {"device": entry.get("device")} if entry is not None else None


class OpenAIBackend(_OpenAIChatMixin, OllamaManager):
    """Pure OpenAI-compatible server (vLLM / LocalAI / real OpenAI / llama.cpp):
    /v1 for chat AND embeddings, management DEGRADED because such servers do not
    expose Ollama's /api/* model-management surface. warm/unload/pull are no-ops,
    ps-based status is omitted (the UI hides VRAM panels when absent), and
    capabilities are assumed rather than discovered -- an inherent limit of the
    OpenAI API, surfaced honestly rather than papered over."""

    def __init__(self, v1_base: str, model: str, num_ctx_request: int = 0,
                 context_budget: int = 0):
        # url is unused for real calls (every management method is overridden to
        # degrade); pass v1_base so any stray reference has a sane host.
        super().__init__(url=v1_base, model=model, num_ctx_request=num_ctx_request,
                         context_budget=context_budget)
        self._init_v1(v1_base)

    async def _load_capabilities(self) -> set:
        # No /api/show on a pure OpenAI server. Assume tool support (every modern
        # /v1 server implements it and Tzara relies on it to pick the agent loop);
        # no measured/ceiling source, so budget from the explicit budget, then the ask,
        # then the shared 4096 floor.
        if self._capabilities is not None:
            return self._capabilities
        self._capabilities = {"completion", "tools"}
        self._context_length = (
            self.context_budget if self.context_budget > 0
            else self.num_ctx_request if self.num_ctx_request > 0
            else 4096
        )
        return self._capabilities

    async def warm_model(self):
        logger.info("OpenAIBackend: warm_model is a no-op (no /api/* management surface)")

    async def unload_model(self):
        logger.info("OpenAIBackend: unload_model is a no-op (no /api/* management surface)")

    async def warm_any_model(self, model_name: str):
        logger.info("OpenAIBackend: warm_any_model no-op for %s", model_name)

    async def unload_any_model(self, model_name: str):
        logger.info("OpenAIBackend: unload_any_model no-op for %s", model_name)

    async def is_model_loaded(self) -> bool:
        return True  # a /v1 server loads on demand; best-effort truthy

    async def is_any_model_loaded(self, model_name: str) -> bool:
        return True

    async def get_model_info(self, model_name: str) -> dict | None:
        return None  # no ps() equivalent

    async def get_status(self) -> dict:
        # Minimal status: no size_vram/expires_at, so main.py's VRAM panels hide.
        ctx = None
        try:
            ctx = await self.get_context_length()
        except Exception:
            pass
        return {
            "model": self.model, "url": self._v1_base, "keep_alive": self.keep_alive,
            "loaded": True, "last_activity": self.last_activity,
            "idle_seconds": None, "context_length": ctx,
        }

    async def pull_model(self, model_name: str):
        # No /api/pull; surface a single readable status rather than raising.
        yield {"status": "pull not supported by this OpenAI-compatible backend",
               "completed": None, "total": None, "digest": None}

    async def set_model(self, new_model: str):
        self.model = new_model
        self._capabilities = None
        self._context_length = None

    async def list_available_models(self) -> list:
        try:
            resp = await self._v1_client.get(f"{self._v1_base}/models")
            resp.raise_for_status()
            data = resp.json().get("data", [])
            return [{
                "name": m.get("id", "unknown"), "size": "0",
                "parameter_size": "Unk", "quantization_level": "Unk",
                "family": "", "families": [], "is_embedding": False,
            } for m in data]
        except Exception as e:
            logger.warning("OpenAIBackend: /v1/models list failed: %s", e)
            return []

    async def list_available_models_detailed(self) -> list:
        models = await self.list_available_models()
        for m in models:
            m["capabilities"] = []  # /v1/models exposes no capability metadata
        return models


# ---------------------------------------------------------------------------
# Embeddings seam
# ---------------------------------------------------------------------------
# Embeddings follow the MANAGEMENT axis: native /api/embed when the server has an
# Ollama mount (existing, proven path, correct model catalog), else /v1/embeddings
# (proven to work with :latest-stripping) for a pure OpenAI server. Two flavors
# because rag_indexer embeds SYNCHRONOUSLY in the worker while rag_search embeds
# async in the web process. Callers pre-truncate inputs (truncate_for_embedding),
# so these stay truncation-agnostic.

def embed_texts_sync(texts: list[str], model: str | None = None) -> list[list[float]]:
    model = model or OLLAMA_EMBED_MODEL
    if LLM_HAS_NATIVE_MOUNT:
        import ollama as _ollama
        client = _ollama.Client(host=OLLAMA_URL)
        return client.embed(model=model, input=texts)["embeddings"]
    with httpx.Client(timeout=_V1_TIMEOUT) as client:
        resp = client.post(f"{LLM_BASE_URL.rstrip('/')}/embeddings",
                           json={"model": _v1_model_name(model), "input": texts})
        resp.raise_for_status()
        return [d["embedding"] for d in resp.json().get("data", [])]


async def embed_texts_async(texts: list[str], model: str | None = None) -> list[list[float]]:
    model = model or OLLAMA_EMBED_MODEL
    if LLM_HAS_NATIVE_MOUNT:
        import ollama as _ollama
        client = _ollama.AsyncClient(host=OLLAMA_URL)
        resp = await client.embed(model=model, input=texts)
        return resp["embeddings"]
    async with httpx.AsyncClient(timeout=_V1_TIMEOUT) as client:
        resp = await client.post(f"{LLM_BASE_URL.rstrip('/')}/embeddings",
                                json={"model": _v1_model_name(model), "input": texts})
        resp.raise_for_status()
        return [d["embedding"] for d in resp.json().get("data", [])]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_llm_backend(
    url: str | None = None, model: str | None = None,
    keep_alive: str | None = None, num_ctx_request: int | None = None,
    context_budget: int | None = None,
):
    """Construct the LLM backend for the configured LLM_PROVIDER. Callers pass the
    same args they gave OllamaManager; provider selection and /v1 base URL come
    from config. Returns an object duck-compatible with OllamaManager (same method
    surface), so no call site changes beyond construction. `num_ctx_request` is the
    load ASK (OLLAMA_NUM_CTX); `context_budget` is the internal history budget
    (OLLAMA_CONTEXT_BUDGET); both default from config when omitted."""
    url = url if url is not None else OLLAMA_URL
    model = model if model is not None else ""
    keep_alive = keep_alive if keep_alive is not None else OLLAMA_KEEP_ALIVE
    num_ctx_request = num_ctx_request if num_ctx_request is not None else OLLAMA_NUM_CTX
    context_budget = context_budget if context_budget is not None else OLLAMA_CONTEXT_BUDGET

    if LLM_PROVIDER == "ollama-native":
        # Pre-seam native path: full Ollama SDK for chat too. Kill-switch.
        return OllamaManager(url=url, model=model, keep_alive=keep_alive,
                             num_ctx_request=num_ctx_request, context_budget=context_budget)
    if LLM_PROVIDER == "openai":
        return OpenAIBackend(v1_base=LLM_BASE_URL, model=model,
                             num_ctx_request=num_ctx_request, context_budget=context_budget)
    if LLM_PROVIDER == "lemonade":
        # Hybrid + native /v1/* management overrides (health/unload/system-stats).
        return LemonadeBackend(url=url, model=model, keep_alive=keep_alive,
                               num_ctx_request=num_ctx_request, context_budget=context_budget,
                               v1_base=LLM_BASE_URL)
    # ollama (default): hybrid. v1_base derived in config.LLM_BASE_URL.
    return HybridBackend(url=url, model=model, keep_alive=keep_alive,
                         num_ctx_request=num_ctx_request, context_budget=context_budget,
                         v1_base=LLM_BASE_URL)
