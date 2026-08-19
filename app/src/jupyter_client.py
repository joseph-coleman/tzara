# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

# jupyter_client.py
import asyncio
import base64
import json
import struct
import uuid
from datetime import datetime, timezone

import httpx
import websockets


def deserialize_binary_message(bmsg):
    """Deserialize a Jupyter binary WebSocket message.

    Format (from jupyter_server's serialize_binary_message):
      [4B nbufs][4B*nbufs offsets][full_msg_json][buffer0][buffer1]...
    bufs[0] is the complete message JSON (with header, parent_header, content,
    etc. but without buffers). bufs[1:] are the binary buffers.
    """
    nbufs = struct.unpack("!i", bmsg[:4])[0]
    offsets = list(struct.unpack("!" + "I" * nbufs, bmsg[4 : 4 * (nbufs + 1)]))
    offsets.append(None)
    bufs = []
    for start, stop in zip(offsets[:-1], offsets[1:]):
        bufs.append(bmsg[start:stop])
    msg = json.loads(bufs[0].decode("utf8"))
    msg["buffers"] = bufs[1:]
    return msg


def serialize_binary_message(msg: dict, buffers) -> bytes:
    """Inverse of deserialize_binary_message.

    Produces the Jupyter kernel wire format:
      [4B nbufs][4B*nbufs offsets][msg_json][buffer0][buffer1]...
    nbufs counts the JSON buffer too (1 + len(buffers)). Offsets are
    absolute byte positions into the full frame.
    """
    msg_json = json.dumps(msg).encode("utf-8")
    nbufs = 1 + len(buffers)
    header = struct.pack("!i", nbufs)
    # Frame layout starts at: 4 (nbufs) + 4*nbufs (offsets table).
    offset = 4 * (nbufs + 1)
    offsets = [offset]
    offset += len(msg_json)
    for buf in buffers:
        offsets.append(offset)
        offset += len(buf)
    offsets_bytes = struct.pack("!" + "I" * nbufs, *offsets)
    return b"".join([header, offsets_bytes, msg_json, *buffers])

JUPYTER_HOST = "http://jupyter:8888"  # Hostname defined in docker-compose
JUPYTER_WS = "ws://jupyter:8888"

# Body of the `wiki` client injected into each freshly spawned kernel so cell
# code can query this vault's index (search / related / tagged / backlinks /
# frontmatter). It lives in the jupyterserver container, so it must be pure
# stdlib (urllib + json) and call back to tzaraserver over the compose
# network. _WIKI_BASE / _WIKI_VAULT are prepended per-kernel by
# `_wiki_client_setup`; only `wiki` survives in the user namespace.
_WIKI_CLIENT_BODY = '''
class _WikiClient:
    """Query this vault's index. Methods return plain lists/dicts ready for
    pandas.DataFrame(...). Available: search, related, tagged, backlinks,
    frontmatter; whole-table reads queryDocuments/queryEdges/queryDocumentTags;
    and the analysis views list_orphans/find_near_duplicates/find_missing_links/
    list_stale_stubs. (Read-only: writing goes through the editor, not here.)"""
    def __init__(self, base, vault):
        self._base, self._vault = base, vault
    def _call(self, op, **args):
        import json as _json
        import urllib.request as _req
        import urllib.error as _err
        body = _json.dumps({"op": op, "args": args}).encode("utf-8")
        url = self._base + "/api/kernel/" + self._vault + "/query"
        request = _req.Request(
            url, data=body, headers={"Content-Type": "application/json"})
        try:
            with _req.urlopen(request, timeout=30) as resp:
                payload = _json.load(resp)
        except _err.HTTPError as e:
            try:
                msg = _json.load(e).get("error", str(e))
            except Exception:
                msg = str(e)
            raise RuntimeError("wiki query failed: " + str(msg)) from None
        return payload.get("result")
    def search(self, query, top_k=10):
        return self._call("search", query=query, top_k=top_k)
    def related(self, path, top_k=10):
        return self._call("related", path=path, top_k=top_k)
    def tagged(self, tag):
        return self._call("tagged", tag=tag)
    def backlinks(self, path):
        return self._call("backlinks", path=path)
    def frontmatter(self, path):
        return self._call("frontmatter", path=path)
    def _query_all(self, op):
        """Whole-table read, KEYSET-paginated to COMPLETENESS. The server returns
        one page + an opaque cursor; we loop on it and return the full row list
        (so pandas.DataFrame(...) still works), so vault size never silently caps
        the result."""
        rows, cursor, pages = [], None, 0
        while True:
            page = self._call(op, after=cursor)
            if not isinstance(page, dict):        # defensive: legacy bare list
                return page
            rows.extend(page.get("rows", []))
            pages += 1
            if not page.get("has_more") or pages >= 10000:  # runaway guard
                if pages >= 10000:
                    import sys as _sys
                    print(f"[wiki] WARNING: {op} stopped after {len(rows)} rows "
                          f"(pagination guard hit) - result may be incomplete.",
                          file=_sys.stderr)
                break
            cursor = page.get("next_cursor")
            if cursor is None:
                break
        return rows
    def queryDocuments(self):
        return self._query_all("queryDocuments")
    def queryEdges(self):
        return self._query_all("queryEdges")
    def queryDocumentTags(self):
        return self._query_all("queryDocumentTags")
    def list_orphans(self, path_prefix="", limit=50):
        return self._call("list_orphans", path_prefix=path_prefix, limit=limit)
    def find_near_duplicates(self, path_prefix="", threshold=0.88, limit=30):
        return self._call("find_near_duplicates", path_prefix=path_prefix,
                          threshold=threshold, limit=limit)
    def find_missing_links(self, path_prefix="", low=0.62, high=0.88, limit=40):
        return self._call("find_missing_links", path_prefix=path_prefix,
                          low=low, high=high, limit=limit)
    def list_stale_stubs(self, path_prefix="", max_chars=400, stale_days=180, limit=40):
        return self._call("list_stale_stubs", path_prefix=path_prefix,
                         max_chars=max_chars, stale_days=stale_days, limit=limit)

wiki = _WikiClient(_WIKI_BASE, _WIKI_VAULT)
del _WikiClient, _WIKI_BASE, _WIKI_VAULT
'''


def _wiki_client_setup(vault: str) -> str:
    """Per-kernel injection source: pin base URL + vault, then the client body."""
    from config import SERVER_INTERNAL_URL
    return (
        "_WIKI_BASE = " + repr(SERVER_INTERNAL_URL) + "\n"
        "_WIKI_VAULT = " + repr(vault) + "\n"
        + _WIKI_CLIENT_BODY
    )


class KernelConnection:
    """Persistent WebSocket connection to a single Jupyter kernel."""

    def __init__(self, kernel_id: str, ws_base: str = JUPYTER_WS):
        self.kernel_id = kernel_id
        self._ws_base = ws_base
        self.ws = None
        self._session_id = uuid.uuid4().hex
        self._listener_task = None
        # Set when the kernel-side WS has closed (listener exits) or any
        # send raises ConnectionClosed. The manager treats a dead
        # KernelConnection as cache-invalid and reconnects on next use.
        self._dead = False
        # Guard so the kernel_dead envelope is sent to subscribed browsers
        # at most once per connection lifetime - both the listener-exit
        # path and the send-failure path can race to notify.
        self._dead_notified = False

        # Execution tracking: msg_id -> asyncio.Queue for results
        self._pending_executions: dict[str, asyncio.Queue] = {}

        # Track comm_msg origins: msg_id -> comm_id, so we can tell the
        # browser which widget triggered callback output.
        self._comm_msg_origins: dict[str, str] = {}

        # Browser WebSocket connections subscribed to this kernel
        self._browser_subscribers: set = set()

        # Full comm_open messages keyed by comm_id, for late-joiner replay
        self._comm_opens: dict[str, dict] = {}

        # Latest widget state per comm_id for late-joiner replay
        self._widget_state_snapshot: dict[str, dict] = {}

        # Binary buffers associated with widget state, keyed by
        # (comm_id, tuple(buffer_path)) -> base64 string. Replayed to
        # late-joining browsers alongside the state snapshot so that
        # e.g. a FileUpload whose value was set before they joined
        # still shows the uploaded bytes.
        self._widget_buffer_snapshot: dict[str, dict[tuple, str]] = {}

    async def connect(self):
        """Establish persistent WebSocket to Jupyter kernel."""
        ws_url = f"{self._ws_base}/api/kernels/{self.kernel_id}/channels"
        self.ws = await websockets.connect(ws_url, max_size=16 * 1024 * 1024)
        self._listener_task = asyncio.create_task(self._listen_loop())

    async def _listen_loop(self):
        """Continuously read messages from kernel and route them."""
        try:
            async for raw_msg in self.ws:
                if isinstance(raw_msg, bytes):
                    msg = deserialize_binary_message(raw_msg)
                else:
                    msg = json.loads(raw_msg)
                msg_type = msg.get("msg_type")
                parent_msg_id = msg.get("parent_header", {}).get("msg_id")

                # Route execution results to the specific execution queue
                if parent_msg_id and parent_msg_id in self._pending_executions:
                    await self._pending_executions[parent_msg_id].put(msg)

                # Route comm messages to all browser subscribers
                if msg_type in ("comm_open", "comm_msg", "comm_close"):
                    await self._broadcast_to_browsers(msg)

                # Capture comm_open for late-joiner replay; clean up on close
                if msg_type == "comm_open":
                    comm_id = msg.get("content", {}).get("comm_id", "")
                    if comm_id:
                        self._comm_opens[comm_id] = msg
                elif msg_type == "comm_close":
                    comm_id = msg.get("content", {}).get("comm_id", "")
                    self._comm_opens.pop(comm_id, None)
                    self._widget_state_snapshot.pop(comm_id, None)

                # Forward output from widget callbacks to browsers.
                # These are stream/display messages whose parent is NOT a
                # pending execution (e.g. triggered by a button on_click).
                # Include source_comm_id so the browser can route orphaned
                # output to the cell containing the widget that triggered it.
                elif msg_type in (
                    "stream", "display_data", "execute_result",
                    "error", "update_display_data", "clear_output",
                ):
                    if not (parent_msg_id and parent_msg_id in self._pending_executions):
                        source_comm_id = self._comm_msg_origins.get(
                            parent_msg_id, ""
                        )
                        await self._broadcast_to_browsers(
                            msg, source_comm_id=source_comm_id
                        )

        except websockets.ConnectionClosed:
            print(f"Kernel {self.kernel_id} WebSocket closed")
        except Exception as e:
            print(f"Kernel {self.kernel_id} listener error: {e}")
        finally:
            self._dead = True
            # Tell every subscribed browser that the kernel is gone so
            # any rendered widgets on the page can show a "disconnected"
            # banner instead of looking clickable.
            try:
                await self._broadcast_kernel_dead("kernel_socket_closed")
            except Exception as e:
                print(f"kernel_dead broadcast failed: {e}")

    async def _broadcast_kernel_dead(self, reason: str):
        """Notify every subscribed browser once that this kernel is gone."""
        if self._dead_notified:
            return
        self._dead_notified = True
        payload = json.dumps({"kernel_dead": True, "reason": reason})
        dead = set()
        for browser_ws in list(self._browser_subscribers):
            try:
                await browser_ws.send_text(payload)
            except Exception:
                dead.add(browser_ws)
        self._browser_subscribers -= dead

    async def _broadcast_to_browsers(self, msg, source_comm_id=None):
        """Forward a kernel message to all subscribed browser connections."""
        # Separate binary buffers for base64 encoding (don't mutate original msg)
        buffers = msg.get("buffers")
        msg_without_buffers = {k: v for k, v in msg.items() if k != "buffers"}
        payload_dict = {"kernel_msg": msg_without_buffers}
        if source_comm_id:
            payload_dict["source_comm_id"] = source_comm_id
        if buffers:
            payload_dict["buffers_base64"] = [
                base64.b64encode(buf).decode("ascii") for buf in buffers
            ]
        payload = json.dumps(payload_dict)
        dead = set()
        for browser_ws in self._browser_subscribers:
            try:
                await browser_ws.send_text(payload)
            except Exception:
                dead.add(browser_ws)
        self._browser_subscribers -= dead

    async def echo_comm_msg_to_others(self, content: dict, sender_ws, buffers_base64=None):
        """Broadcast a browser comm_msg as a kernel_msg to all other subscribers.

        When buffers_base64 is provided, each buffer is forwarded to other
        browsers and (for state updates with buffer_paths) snapshotted so
        that late-joiners can be replayed the same binary values.
        """
        data = content.get("data", {}) or {}
        comm_id = content.get("comm_id", "")
        if data.get("method") == "update" and comm_id:
            snapshot = self._widget_state_snapshot.setdefault(comm_id, {})
            snapshot.update(data.get("state", {}))
            buffer_paths = data.get("buffer_paths") or []
            if buffers_base64 and buffer_paths:
                buf_snap = self._widget_buffer_snapshot.setdefault(comm_id, {})
                for path, b64 in zip(buffer_paths, buffers_base64):
                    buf_snap[tuple(path)] = b64

        synthetic_msg = {
            "msg_type": "comm_msg",
            "header": {
                "msg_id": uuid.uuid4().hex,
                "msg_type": "comm_msg",
                "session": self._session_id,
                "date": datetime.now(timezone.utc).isoformat(),
            },
            "parent_header": {},
            "metadata": {},
            "content": content,
        }
        payload_dict = {"kernel_msg": synthetic_msg}
        if buffers_base64:
            payload_dict["buffers_base64"] = buffers_base64
        payload = json.dumps(payload_dict)
        dead = set()
        for browser_ws in self._browser_subscribers:
            if browser_ws is sender_ws:
                continue
            try:
                await browser_ws.send_text(payload)
            except Exception:
                dead.add(browser_ws)
        self._browser_subscribers -= dead

    async def replay_state_to(self, browser_ws):
        """Send comm_open registrations then state snapshots to a newly connected browser."""
        # 1. Register all known widget models
        for msg in self._comm_opens.values():
            try:
                payload = {"kernel_msg": {k: v for k, v in msg.items() if k != "buffers"}}
                buffers = msg.get("buffers")
                if buffers:
                    payload["buffers_base64"] = [
                        base64.b64encode(buf).decode("ascii") for buf in buffers
                    ]
                await browser_ws.send_text(json.dumps(payload))
            except Exception:
                return
        # 2. Apply any accumulated state updates on top
        for comm_id, state in self._widget_state_snapshot.items():
            buf_snap = self._widget_buffer_snapshot.get(comm_id, {})
            buffer_paths = [list(p) for p in buf_snap.keys()]
            buffers_base64 = [buf_snap[tuple(p)] for p in buffer_paths]
            synthetic_msg = {
                "msg_type": "comm_msg",
                "header": {
                    "msg_id": uuid.uuid4().hex,
                    "msg_type": "comm_msg",
                    "session": self._session_id,
                    "date": datetime.now(timezone.utc).isoformat(),
                },
                "parent_header": {},
                "metadata": {},
                "content": {
                    "comm_id": comm_id,
                    "data": {
                        "method": "update",
                        "state": state,
                        "buffer_paths": buffer_paths,
                    },
                },
            }
            payload_dict = {"kernel_msg": synthetic_msg}
            if buffers_base64:
                payload_dict["buffers_base64"] = buffers_base64
            try:
                await browser_ws.send_text(json.dumps(payload_dict))
            except Exception:
                return

    def subscribe(self, browser_ws):
        self._browser_subscribers.add(browser_ws)

    def unsubscribe(self, browser_ws):
        self._browser_subscribers.discard(browser_ws)

    async def execute(self, code: str):
        """Send execute_request, return (msg_id, queue) for streaming results."""
        msg_id = uuid.uuid4().hex
        queue = asyncio.Queue()
        self._pending_executions[msg_id] = queue

        message = {
            "header": {
                "msg_id": msg_id,
                "username": "wiki_user",
                "session": self._session_id,
                "msg_type": "execute_request",
                "date": datetime.now(timezone.utc).isoformat(),
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": False,
                "store_history": True,
                "stop_on_error": True,
                "allow_stdin": True,
            },
        }
        try:
            await self.ws.send(json.dumps(message))
        except (websockets.ConnectionClosed, Exception):
            self._dead = True
            self._pending_executions.pop(msg_id, None)
            raise
        return msg_id, queue

    async def send_comm_msg(self, content: dict, buffers=None):
        """Forward a comm_msg from the browser to the kernel.

        When buffers are provided, the message is sent using Jupyter's
        binary framed wire format so widgets like FileUpload and
        anywidget custom messages can transport raw bytes.
        """
        msg_id = uuid.uuid4().hex
        # Remember which comm_id this msg_id came from, so we can
        # attribute callback output back to the originating widget.
        comm_id = content.get("comm_id", "")
        if comm_id:
            self._comm_msg_origins[msg_id] = comm_id
        message = {
            "header": {
                "msg_id": msg_id,
                "username": "wiki_user",
                "session": self._session_id,
                "msg_type": "comm_msg",
                "date": datetime.now(timezone.utc).isoformat(),
            },
            "parent_header": {},
            "metadata": {},
            "content": content,
            "channel": "shell",
        }
        try:
            if buffers:
                await self.ws.send(serialize_binary_message(message, buffers))
            else:
                await self.ws.send(json.dumps(message))
        except (websockets.ConnectionClosed, Exception):
            self._dead = True
            raise

    async def run_setup(self, code: str):
        """Fire-and-forget a silent setup execution against a fresh kernel.

        Used to initialise a newly spawned kernel (e.g. chdir into the page's
        vault folder so cells can read attachments by their relative link name).
        silent=True / store_history=False keep it out of the cell-output stream
        and the kernel's In/Out history; the result is intentionally not tracked
        in _pending_executions, so it never surfaces in any cell.
        """
        message = {
            "header": {
                "msg_id": uuid.uuid4().hex,
                "username": "wiki_user",
                "session": self._session_id,
                "msg_type": "execute_request",
                "date": datetime.now(timezone.utc).isoformat(),
            },
            "parent_header": {},
            "metadata": {},
            "content": {
                "code": code,
                "silent": True,
                "store_history": False,
                "stop_on_error": False,
                "allow_stdin": False,
            },
        }
        try:
            await self.ws.send(json.dumps(message))
        except (websockets.ConnectionClosed, Exception):
            self._dead = True
            raise

    async def send_input_reply(self, value: str):
        """Send input_reply to kernel's stdin channel (for Python input())."""
        message = {
            "header": {
                "msg_id": uuid.uuid4().hex,
                "username": "wiki_user",
                "session": self._session_id,
                "msg_type": "input_reply",
                "date": datetime.now(timezone.utc).isoformat(),
            },
            "parent_header": {},
            "metadata": {},
            "content": {"value": value},
            "channel": "stdin",
        }
        try:
            await self.ws.send(json.dumps(message))
        except (websockets.ConnectionClosed, Exception):
            self._dead = True
            raise

    def remove_pending_execution(self, msg_id: str):
        self._pending_executions.pop(msg_id, None)

    async def close(self):
        if self._listener_task:
            self._listener_task.cancel()
        if self.ws:
            await self.ws.close()


class AsyncJupyterManager:
    def __init__(self, host: str = JUPYTER_HOST, ws: str = JUPYTER_WS):
        # Base URLs of the jupyter server this manager drives. The default
        # instance serves the interactive vault-mounted server; the agent
        # framework instantiates a second manager against jupyter-agent
        # (src.agent_kernel) - same class, different host, separate caches.
        self.host = host
        self.ws = ws
        # In-memory store: { "page_id": "kernel_uuid" }
        self.kernels = {}
        # Persistent connections: { "kernel_uuid": KernelConnection }
        self.connections: dict[str, KernelConnection] = {}

    async def get_or_create_kernel(self, page_id: str):
        """
        Checks if a kernel exists for the page. If not, creates one.

        Returns (kernel_id, is_new) where is_new is True only when this call
        actually spawned a fresh upstream kernel - callers use it to run
        one-time per-kernel setup (e.g. chdir) without re-running it on every
        browser reconnect to a still-alive kernel.
        """
        async with httpx.AsyncClient() as client:
            if page_id in self.kernels:
                cached_id = self.kernels[page_id]
                # Verify the upstream Jupyter still has this kernel - it
                # may have been culled by Jupyter's own idle timeout
                # without us noticing. If gone, drop our caches and fall
                # through to spawn a fresh one.
                try:
                    probe = await client.get(
                        f"{self.host}/api/kernels/{cached_id}"
                    )
                except Exception:
                    probe = None
                if probe is not None and probe.status_code == 200:
                    return cached_id, False
                await self._delete_kernel(client, cached_id)

            # Spawn a new kernel
            response = await client.post(f"{self.host}/api/kernels")
            if response.status_code == 201:
                kernel_id = response.json()["id"]
                self.kernels[page_id] = kernel_id
                return kernel_id, True
            else:
                raise Exception(f"Failed to spawn kernel: {response.text}")

    async def get_or_create_connection(
        self, page_id: str, cwd: str | None = None, vault: str | None = None
    ) -> KernelConnection:
        """Get or create a persistent KernelConnection for a page.

        When ``cwd`` is given and this call spawns a brand-new kernel, the
        kernel is chdir'd into that directory so cell code can reference the
        page's attachments by the same relative name the markdown link uses.

        When ``vault`` is given and a brand-new kernel is spawned, a ``wiki``
        client is injected so cell code can query that vault's index. Both are
        applied only on a fresh kernel, never on reconnect to a live one.
        """
        kernel_id, kernel_is_new = await self.get_or_create_kernel(page_id)

        cached = self.connections.get(kernel_id)
        if cached is not None:
            listener_dead = (
                cached._listener_task is not None
                and cached._listener_task.done()
            )
            if cached._dead or listener_dead:
                try:
                    await cached.close()
                except Exception:
                    pass
                self.connections.pop(kernel_id, None)
                cached = None

        if cached is None:
            conn = KernelConnection(kernel_id, ws_base=self.ws)
            await conn.connect()
            self.connections[kernel_id] = conn
            # Only on a freshly spawned kernel - never clobber a working dir the
            # user may have changed in a long-lived kernel we're just reconnecting,
            # nor re-inject the wiki client over a namespace the user has been using.
            if kernel_is_new and cwd:
                setup = (
                    "import os as _os\n"
                    f"try:\n    _os.chdir({cwd!r})\nexcept Exception:\n    pass\n"
                )
                try:
                    await conn.run_setup(setup)
                except Exception as e:
                    print(f"kernel chdir setup failed: {e}")
            if kernel_is_new and vault:
                try:
                    await conn.run_setup(_wiki_client_setup(vault))
                except Exception as e:
                    print(f"kernel wiki client setup failed: {e}")

        return self.connections[kernel_id]

    async def list_kernels(self, max_age_seconds=3600):

        kernel_list = []

        async with httpx.AsyncClient() as client:
            try:
                # 1. Get list of running kernels from Docker service
                response = await client.get(f"{self.host}/api/kernels")
                if response.status_code != 200:
                    kernel_list = "Error fetching kernels from Jupyter"
                    # await asyncio.sleep(1)
                    return kernel_list

                active_kernels = response.json()

                for kernel in active_kernels:
                    kernel_id = kernel["id"]
                    print("list kernel ", kernel)
                    last_activity_str = kernel["last_activity"]

                    # Parse ISO 8601 string (Handle 'Z' manually if on older Python)
                    # Example: "2023-11-19T12:00:00.000000Z"
                    last_activity = datetime.fromisoformat(
                        last_activity_str.replace("Z", "+00:00")
                    )
                    now = datetime.now(timezone.utc)

                    idle_seconds = (now - last_activity).total_seconds()

                    page_list = []
                    for each_page in self.kernels:
                        if kernel_id == self.kernels[each_page]:
                            page_list.append(each_page)

                    kc = kernel.copy()
                    kc.update({"pages": page_list, "idle": idle_seconds})
                    print(kc)
                    kernel_list.append(kc)

            except Exception as e:
                kernel_list = f"List error: {e}"

        return kernel_list

    async def delete_kernel_by_id(self, kernel_id):
        async with httpx.AsyncClient() as client:
            try:
                await self._delete_kernel(client, kernel_id)
            except Exception as e:
                print(f"Error deleting kernel: {e}")

    def wrap_msg(self, msg_type, msg_data):
        return json.dumps({msg_type: msg_data})

    async def execute_code_stream(self, kernel_id: str, code: str):
        """
        Yields output chunks as they arrive from Jupyter.
        Legacy method kept for backward compatibility.
        """
        ws_url = f"{JUPYTER_WS}/api/kernels/{kernel_id}/channels"

        async with websockets.connect(ws_url) as ws:
            msg_id = uuid.uuid4().hex

            # Send Execute Request
            message = {
                "header": {
                    "msg_id": msg_id,
                    "username": "wiki_user",
                    "session": uuid.uuid4().hex,
                    "msg_type": "execute_request",
                    "date": datetime.now(timezone.utc).isoformat(),
                },
                "parent_header": {},
                "metadata": {},
                "content": {
                    "code": code,
                    "silent": False,
                    "store_history": True,
                    "stop_on_error": True,
                },
            }
            await ws.send(json.dumps(message))

            # Stream Results
            while True:
                response = await ws.recv()
                msg = json.loads(response)

                # Filter messages unrelated to our request
                if msg["parent_header"].get("msg_id") != msg_id:
                    continue

                msg_type = msg["msg_type"]
                content = msg["content"]

                # Standard Output (print statements)
                if msg_type == "stream":
                    d = content["text"]
                    d = d.replace("\n", "<br/>")
                    yield self.wrap_msg("html", d)

                # Errors
                elif msg_type == "error":
                    d = f"<pre>Error: {content['evalue']}</pre>"
                    yield self.wrap_msg("html", d)

                #
                elif (msg_type == "execute_result") | (msg_type == "display_data"):
                    data = content["data"]
                    if (d := data.get("text/plain")) and not data.get(
                        "application/vnd.jupyter.widget-view+json"
                    ):
                        d = f"<pre>{d}</pre>"
                        yield self.wrap_msg("html", d)

                    if d := data.get("text/html"):
                        yield self.wrap_msg("html", d)
                    if d := data.get("image/png"):
                        d = f'<img src="data:image/png;base64,{d}">'
                        yield self.wrap_msg("html", d)
                    if d := data.get("image/svg+xml"):
                        yield self.wrap_msg("html", d)

                # Execution Finished
                elif msg_type == "status":
                    if content["execution_state"] == "idle":
                        break

    async def prune_stale_kernels(self, max_age_seconds=3600):
        """
        Query Jupyter for active kernels, check their last_activity,
        and kill them if they are too old.
        """
        print(f"[{datetime.now()}] 🧹 Reaper running...")

        async with httpx.AsyncClient() as client:
            try:
                # 1. Get list of running kernels from Docker service
                response = await client.get(f"{self.host}/api/kernels")
                if response.status_code != 200:
                    print("Error fetching kernels from Jupyter")
                    return

                active_kernels = response.json()

                for kernel in active_kernels:
                    kernel_id = kernel["id"]
                    last_activity_str = kernel["last_activity"]

                    # Parse ISO 8601 string (Handle 'Z' manually if on older Python)
                    # Example: "2023-11-19T12:00:00.000000Z"
                    last_activity = datetime.fromisoformat(
                        last_activity_str.replace("Z", "+00:00")
                    )
                    now = datetime.now(timezone.utc)

                    idle_seconds = (now - last_activity).total_seconds()

                    print(kernel_id, idle_seconds)

                    if idle_seconds > max_age_seconds:
                        print(
                            f"💀 Killing stale kernel {kernel_id} (Idle: {idle_seconds:.0f}s)"
                        )
                        await self._delete_kernel(client, kernel_id)

            except Exception as e:
                print(f"Reaper error: {e}")

    async def interrupt_kernel(self, kernel_id: str) -> bool:
        """SIGINT the running cell (control-channel interrupt via the REST API).
        The kernel SURVIVES: the aborted execution returns a normal error reply,
        so a timed-out tool call becomes a clean tool error, not a crash."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.host}/api/kernels/{kernel_id}/interrupt")
                return resp.status_code in (200, 204)
        except Exception as e:
            print(f"interrupt_kernel failed for {kernel_id}: {e}")
            return False

    async def restart_kernel(self, kernel_id: str) -> bool:
        """Hard restart: fresh namespace, same kernel id. Any in-flight
        executions are lost; callers must re-seed setup code afterwards."""
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.host}/api/kernels/{kernel_id}/restart")
                return resp.status_code == 200
        except Exception as e:
            print(f"restart_kernel failed for {kernel_id}: {e}")
            return False

    async def _delete_kernel(self, client, kernel_id):
        # Close persistent connection if exists
        if kernel_id in self.connections:
            await self.connections[kernel_id].close()
            del self.connections[kernel_id]

        # remove kernel from jupyter server
        await client.delete(f"{self.host}/api/kernels/{kernel_id}")

        # We must find which page owns this kernel_id
        pages_to_remove = [
            page for page, k_id in self.kernels.items() if k_id == kernel_id
        ]

        for page in pages_to_remove:
            del self.kernels[page]
            print(f"   - Unmapped from page: {page}")


def format_execution_message(msg, cell_id):
    """Convert a Jupyter kernel message to the browser message format."""
    msg_type = msg["msg_type"]
    content = msg["content"]

    if msg_type == "stream":
        text = content["text"].replace("\n", "<br/>")
        return {"html": text, "cell_id": cell_id}

    elif msg_type == "error":
        return {"html": f"<pre>Error: {content['evalue']}</pre>", "cell_id": cell_id}

    elif msg_type in ("execute_result", "display_data"):
        data = content["data"]

        # Widget view -- send as widget message, not HTML
        if view_spec := data.get("application/vnd.jupyter.widget-view+json"):
            return {"widget_view": view_spec, "cell_id": cell_id}

        if d := data.get("text/html"):
            return {"html": d, "cell_id": cell_id}
        if d := data.get("image/png"):
            return {"html": f'<img src="data:image/png;base64,{d}">', "cell_id": cell_id}
        if d := data.get("image/svg+xml"):
            return {"html": d, "cell_id": cell_id}
        if d := data.get("text/plain"):
            return {"html": f"<pre>{d}</pre>", "cell_id": cell_id}

    elif msg_type == "input_request":
        prompt = content.get("prompt", "")
        password = content.get("password", False)
        return {"input_request": True, "prompt": prompt, "password": password, "cell_id": cell_id}

    return None


# Singleton instance for the app
jupyter_manager = AsyncJupyterManager()
