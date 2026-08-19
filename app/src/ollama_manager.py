# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass

import httpx
import ollama

logger = logging.getLogger("ollama_manager")

EMBEDDING_FAMILIES = {"bert", "nomic-bert"}


@dataclass
class ChatStreamChunk:
    """A single chunk from a streaming chat response with tools."""
    token: str                    # Content fragment (may be empty)
    done: bool                    # True on the last chunk
    tool_calls: list | None       # Only populated on final chunk if model called a tool
    error: str | None = None      # Set on the final chunk if the stream aborted abnormally
                                  # (e.g. Ollama could not parse a malformed tool call). The
                                  # caller must NOT treat such a turn as a clean stop.


def _extract_raw_from_tool_call_error(message: str) -> str:
    """Recover the buffered content Ollama trapped in a tool-call parse error.

    Ollama's server reports these as ``error parsing tool call: raw='<content>'``
    where ``<content>`` is the (possibly multi-line) text the model produced before
    the malformed call. We pull it back out so it isn't lost. Returns "" if the
    error isn't in the expected shape.
    """
    marker = "raw='"
    idx = message.find(marker)
    if idx < 0:
        return ""
    raw = message[idx + len(marker):]
    if raw.endswith("'"):
        raw = raw[:-1]
    return raw


def _is_embedding_model(model_name: str, families: list[str]) -> bool:
    """Detect embedding models by family membership or name heuristics."""
    if set(families) & EMBEDDING_FAMILIES:
        return True
    if any("embed" in f.lower() for f in families):
        return True
    base_name = model_name.split(":")[0].lower()
    if "embed" in base_name:
        return True
    return False


class OllamaManager:
    """
    Manages Ollama model lifecycle: warming, unloading, activity tracking,
    and generation. Follows the pattern of jupyter_client.py's AsyncJupyterManager.
    """

    def __init__(self, url: str, model: str, keep_alive: str = "30m",
                 num_ctx_request: int = 0, context_budget: int = 0):
        self.url = url
        self.model = model
        self.keep_alive = keep_alive
        # The ASK: window to request when loading the model (>0). Sent as the `num_ctx`
        # option on warm here; LemonadeBackend routes it to /v1/load ctx_size instead.
        self.num_ctx_request = num_ctx_request
        # The BUDGET: explicit history-budgeting window (>0 wins over everything). 0 =
        # auto-resolve (measured actual > ask > ceiling > 4096); see _load_capabilities.
        self.context_budget = context_budget
        self.client = ollama.AsyncClient(
            host=url,
            timeout=httpx.Timeout(connect=5.0, read=300.0, write=30.0, pool=5.0),
        )
        self.last_activity: float = 0.0
        self._lock = asyncio.Lock()
        self._capabilities: set[str] | None = None
        self._context_length: int | None = None

    def touch(self):
        """Record a timestamp of user activity (cheap, called on page views)."""
        self.last_activity = time.time()

    def _warm_options(self) -> dict | None:
        """num_ctx ASK for the native warm/generate load (Ollama honors it and (re)loads
        at this window). None when unset. LemonadeBackend ignores this and uses /v1/load."""
        return {"num_ctx": self.num_ctx_request} if self.num_ctx_request > 0 else None

    async def warm_model(self):
        """Load the model into memory without generating text, at the requested window."""
        async with self._lock:
            try:
                await self.client.generate(
                    model=self.model, prompt="", keep_alive=self.keep_alive,
                    options=self._warm_options(),
                )
                print(f"OllamaManager: model '{self.model}' warmed")
            except Exception as e:
                print(f"OllamaManager: failed to warm model: {e}")
        self._invalidate_ctx()

    async def unload_model(self):
        """Explicitly free GPU/memory by setting keep_alive to 0."""
        async with self._lock:
            try:
                await self.client.generate(model=self.model, prompt="", keep_alive="0")
                print(f"OllamaManager: model '{self.model}' unloaded")
            except Exception as e:
                print(f"OllamaManager: failed to unload model: {e}")
        self._invalidate_ctx()

    async def is_model_loaded(self) -> bool:
        """Check if the model is currently loaded in Ollama."""
        try:
            ps = await self.client.ps()
            for m in ps.get("models", []):
                if m.get("model", "").startswith(self.model.split(":")[0]):
                    return True
            return False
        except Exception as e:
            print(f"OllamaManager: failed to check model status: {e}")
            return False

    async def generate(self, prompt: str, max_tokens: int | None = None) -> str:
        """Generate a response. Auto-updates activity timer. `max_tokens` caps the
        response length (Ollama's num_predict) when set."""
        self.touch()
        options = {"num_predict": max_tokens} if max_tokens else None
        result = await self.client.generate(
            model=self.model, prompt=prompt, keep_alive=self.keep_alive, options=options,
        )
        return result["response"]

    async def aclose(self):
        """Release transport resources. No-op for the native client (the ollama
        SDK manages its own httpx pool); overridden by the /v1 seam backends,
        which own an httpx.AsyncClient that must be closed (per-run in the worker)."""
        return None

    async def chat_stream(
        self, messages: list[dict], system: str | None = None,
        model: str | None = None,
    ) -> AsyncGenerator[str]:
        """Stream a multi-turn conversation from Ollama, yielding content tokens.

        `model` overrides the manager's default for this call only - used by
        the /edit/ writing-assist path to point at a smaller, faster model
        than the chat default. None falls back to self.model.
        """
        self.touch()
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        stream = await self.client.chat(
            model=model or self.model, messages=msgs,
            keep_alive=self.keep_alive, stream=True,
        )
        async for chunk in stream:
            token = chunk.get("message", {}).get("content", "")
            if token:
                yield token

    async def get_status(self) -> dict:
        """Return status dict for management page."""
        loaded = await self.is_model_loaded()
        idle_seconds = time.time() - self.last_activity if self.last_activity else None

        status = {
            "model": self.model,
            "url": self.url,
            "keep_alive": self.keep_alive,
            "loaded": loaded,
            "last_activity": self.last_activity,
            "idle_seconds": round(idle_seconds, 1) if idle_seconds else None,
        }

        # Reported context window size (cached after first ollama show())
        try:
            status["context_length"] = await self.get_context_length()
        except Exception:
            status["context_length"] = None

        # Get memory info if model is loaded
        if loaded:
            try:
                ps = await self.client.ps()
                for m in ps.get("models", []):
                    if m.get("model", "").startswith(self.model.split(":")[0]):
                        status["size"] = m.get("size")
                        status["size_vram"] = m.get("size_vram")
                        status["expires_at"] = m.get("expires_at")
                        break
            except Exception:
                pass

        return status

    async def is_any_model_loaded(self, model_name: str) -> bool:
        """Check if a specific model is currently loaded in Ollama."""
        try:
            ps = await self.client.ps()
            for m in ps.get("models", []):
                if m.get("model", "").startswith(model_name.split(":")[0]):
                    return True
            return False
        except Exception as e:
            print(f"OllamaManager: failed to check model status for {model_name}: {e}")
            return False

    async def warm_any_model(self, model_name: str):
        """Load a specific model into memory."""
        async with self._lock:
            try:
                await self.client.generate(
                    model=model_name, prompt="", keep_alive=self.keep_alive
                )
                print(f"OllamaManager: model '{model_name}' warmed")
            except Exception as e:
                print(f"OllamaManager: failed to warm model {model_name}: {e}")

    async def unload_any_model(self, model_name: str):
        """Unload a specific model from memory."""
        async with self._lock:
            try:
                await self.client.generate(model=model_name, prompt="", keep_alive="0")
                print(f"OllamaManager: model '{model_name}' unloaded")
            except Exception as e:
                print(f"OllamaManager: failed to unload model {model_name}: {e}")
        if model_name == self.model:
            self._invalidate_ctx()

    async def get_model_info(self, model_name: str) -> dict | None:
        """Return size/vram/expires for a loaded model, or None if not loaded."""
        try:
            ps = await self.client.ps()
            for m in ps.get("models", []):
                if m.get("model", "").startswith(model_name.split(":")[0]):
                    return {
                        "size": m.get("size"),
                        "size_vram": m.get("size_vram"),
                        "expires_at": m.get("expires_at"),
                    }
            return None
        except Exception:
            return None

    async def _load_capabilities(self) -> set[str]:
        """Query model capabilities and context length via ollama show() and cache.

        Returns a set of capability strings, e.g. {"completion", "tools", "thinking", "vision"}.
        Also populates self._context_length from the same response.
        """
        if self._capabilities is not None:
            return self._capabilities
        ceiling: int | None = None  # model max / capability window (an upper bound)
        try:
            info = await self.client.show(self.model)
            caps = getattr(info, "capabilities", None) or []
            self._capabilities = set(caps)
            # Extract context window size from model metadata. For Ollama this is the
            # model's MAX (a ceiling), not the loaded num_ctx. llama.cpp backends (e.g.
            # Lemonade) report -1 for "inherit the process's -c"; require a real positive
            # value so it isn't stored as a truthy -1.
            model_info = getattr(info, "modelinfo", None) or {}
            arch = model_info.get("general.architecture", "")
            if arch:
                ctx = model_info.get(f"{arch}.context_length")
                if isinstance(ctx, int) and ctx > 0:
                    ceiling = ctx
        except Exception as e:
            print(f"OllamaManager: failed to load capabilities for {self.model}: {e}")
            self._capabilities = set()
        if ceiling is None:
            ceiling = await self._discover_ctx_ceiling()
        # The measured ACTUAL loaded window, if a subclass can observe it (e.g. Lemonade
        # /v1/health ctx_size). It's the "get" -- what the model can really hold.
        loaded = await self._discover_loaded_ctx()
        # Choose a target: explicit budget > measured actual > the ask > ceiling > 4096.
        if self.context_budget > 0:
            chosen = self.context_budget
        elif isinstance(loaded, int) and loaded > 0:
            chosen = loaded
        elif self.num_ctx_request > 0:
            chosen = self.num_ctx_request
        elif isinstance(ceiling, int) and ceiling > 0:
            chosen = ceiling
        else:
            chosen = 4096
        # HARD SAFETY CLAMP: never budget above what the model actually loaded with. The
        # measured window is a physical ceiling, not a preference -- budgeting past it
        # makes compaction over-pack history that the model then silently drops. An
        # explicit budget may only ever go LOWER than the measured window (a deliberate
        # cap for latency/cost); a higher one (or a stale ask) is clamped down here.
        if isinstance(loaded, int) and loaded > 0 and chosen > loaded:
            logger.warning(
                "context budget %d exceeds measured loaded window %d for %s; clamping to %d",
                chosen, loaded, self.model, loaded,
            )
            chosen = loaded
        self._context_length = chosen
        return self._capabilities

    async def _discover_loaded_ctx(self) -> int | None:
        """Hook: the ACTUAL context window the model is currently loaded with, if the
        backend can report it (authoritative for budgeting). Base has no such source and
        returns None; LemonadeBackend reads it from /v1/health. Must not raise."""
        return None

    async def _discover_ctx_ceiling(self) -> int | None:
        """Hook: the model's MAX/ceiling window when native /api/show gave nothing usable
        (e.g. llama.cpp's -1). An upper bound, not the loaded size -- the ask outranks it.
        Base returns None; subclasses may override. Must not raise."""
        return None

    def _invalidate_ctx(self):
        """Drop cached capabilities/context so the next query re-discovers. Called after a
        load/unload that may have changed the loaded window (Lemonade auto-sizes, so the
        cached ctx_size can otherwise go stale across an unload/reload cycle)."""
        self._capabilities = None
        self._context_length = None

    async def supports_tools(self) -> bool:
        """Check if the current model supports native tool calling."""
        caps = await self._load_capabilities()
        return "tools" in caps

    async def supports_thinking(self) -> bool:
        """Check if the current model supports extended thinking."""
        caps = await self._load_capabilities()
        return "thinking" in caps

    async def get_context_length(self) -> int:
        """Return the model's context window size in tokens (cached)."""
        await self._load_capabilities()
        return self._context_length or 4096

    def compute_max_messages(self) -> int:
        """Compute max message history count from cached context length.

        Reserves scale proportionally with context size so larger models
        keep more history while still leaving room for responses.
        Call only after get_context_length() or _load_capabilities() has been awaited.
        """
        ctx = self._context_length or 4096
        AVG_TOKENS_PER_MSG = 300
        SYSTEM_PROMPT_RESERVE = 1500
        response_reserve = max(512, ctx // 8)
        available = ctx - SYSTEM_PROMPT_RESERVE - response_reserve
        computed = available // AVG_TOKENS_PER_MSG
        return max(4, min(computed, 200))

    def compute_history_budget_tokens(self, system_prompt_tokens: int) -> int:
        """Compute available token budget for message history.

        Unlike compute_max_messages() which uses a fixed SYSTEM_PROMPT_RESERVE,
        this uses the measured system prompt token count for accurate budgeting.
        Call only after get_context_length() or _load_capabilities() has been awaited.
        """
        ctx = self._context_length or 4096
        response_reserve = max(512, ctx // 8)
        return max(0, ctx - system_prompt_tokens - response_reserve)

    async def chat_stream_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str | None = None,
        think: bool = False,
    ) -> AsyncGenerator[ChatStreamChunk, None]:
        """Stream a chat response with tool definitions.

        Yields ChatStreamChunk objects. Intermediate chunks carry content
        tokens; the final chunk (done=True) may carry tool_calls if the
        model decided to call a tool.
        """
        self.touch()
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.extend(messages)
        kwargs = dict(
            model=self.model, messages=msgs, tools=tools,
            keep_alive=self.keep_alive, stream=True,
        )
        # Explicit True/False (see chat_with_tools): omitting think would leave the
        # model default on, making think=False a no-op for reasoning models.
        if await self.supports_thinking():
            kwargs["think"] = bool(think)
        stream = await self.client.chat(**kwargs)
        accumulated_tool_calls = []
        try:
            async for chunk in stream:
                msg = chunk.get("message", {})
                token = msg.get("content", "") or ""
                is_done = chunk.get("done", False)
                if msg.get("tool_calls"):
                    accumulated_tool_calls.extend(msg["tool_calls"])
                yield ChatStreamChunk(
                    token=token,
                    done=is_done,
                    tool_calls=accumulated_tool_calls if is_done and accumulated_tool_calls else None,
                )
        except ollama.ResponseError as e:
            # Ollama buffers content while it sniffs for a tool call; when the model
            # emits one it cannot parse, the buffered text (the model's actual intent)
            # is trapped inside the error's `raw='...'`. Salvage it so the caller can
            # text-parse a tool call from it and so the turn is not silently empty, and
            # flag the abnormal end so the loop does NOT mistake it for a clean stop.
            logger.warning("Ollama response error during streaming (likely malformed tool call): %s", e)
            salvaged = _extract_raw_from_tool_call_error(str(e))
            yield ChatStreamChunk(token=salvaged, done=True, tool_calls=None, error=str(e))

    async def pull_model(self, model_name: str):
        """Pull a model from Ollama, yielding progress dicts."""
        stream = await self.client.pull(model=model_name, stream=True)
        async for progress in stream:
            yield {
                "status": progress.get("status", ""),
                "completed": progress.get("completed"),
                "total": progress.get("total"),
                "digest": progress.get("digest"),
            }

    async def set_model(self, new_model: str):
        """Switch the active chat model: unload old, update, warm new."""
        old_model = self.model
        if old_model != new_model:
            await self.unload_any_model(old_model)
        self.model = new_model
        self._capabilities = None  # reset cached capabilities
        self._context_length = None  # reset cached context length
        await self.warm_any_model(new_model)
        print(f"OllamaManager: switched model from '{old_model}' to '{new_model}'")

    async def list_available_models(self) -> list:
        """List models available on the Ollama server."""
        try:
            result = await self.client.list()
            models = []
            for m in result.get("models", []):
                details = m.get("details", {})
                families = details.get("families") or []
                name = m.get("model", m.get("name", "unknown"))
                is_embedding = _is_embedding_model(name, families)
                models.append({
                    "name": name,
                    "size": m.get("size", "0"),
                    "parameter_size": details.get("parameter_size", "Unk"),
                    "quantization_level": details.get("quantization_level", "Unk"),
                    "family": details.get("family", ""),
                    "families": families,
                    "is_embedding": is_embedding,
                })
            return models
        except Exception as e:
            print(f"OllamaManager: failed to list models: {e}")
            return []

    async def get_model_capabilities(self, model_name: str) -> list[str]:
        """Get capabilities for a specific model via show(). Returns empty list on error."""
        try:
            info = await self.client.show(model_name)
            return list(getattr(info, "capabilities", None) or [])
        except Exception as e:
            print(f"OllamaManager: failed to get capabilities for {model_name}: {e}")
            return []

    async def list_available_models_detailed(self) -> list:
        """List models enriched with capabilities from show()."""
        models = await self.list_available_models()
        if models:
            results = await asyncio.gather(
                *(self.get_model_capabilities(m["name"]) for m in models),
                return_exceptions=True,
            )
            for m, result in zip(models, results):
                caps = result if isinstance(result, list) else []
                m["capabilities"] = caps
                m["is_embedding"] = "embedding" in caps
        return models


