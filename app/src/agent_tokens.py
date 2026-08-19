# Copyright (C) 2026 Joseph E. Coleman
# This file is part of Tzara, licensed under the GNU Affero General
# Public License v3.0 or later. See LICENSE.txt.
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Stateless HMAC-scoped tokens for the agent-API (gap #3).

A token encodes (agent, vault, run_id, exp) and is verified purely by
signature - no token table; the only persistent state is one secret key.
Both minting (run start, background_agents) and verification (agent_api
middleware) happen in the WORKER process, so a random-at-import secret is
coherent; set AGENT_API_SECRET in .env only if tokens must survive worker
restarts (they never need to today - tokens are per-run and short-lived).

Scope IS the boundary: possession by the kernel doesn't escalate, because the
claims pin the vault and run, and the endpoints derive everything from claims,
never the request body.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time

from config import AGENT_API_SECRET

_SECRET = (AGENT_API_SECRET or secrets.token_hex(32)).encode("utf-8")


def _sign(payload: bytes) -> str:
    return hmac.new(_SECRET, payload, hashlib.sha256).hexdigest()


def mint(agent: str, vault: str, run_id: str, ttl_s: float = 7200,
         mode: str = "propose") -> str:
    claims = {"agent": agent, "vault": vault, "run_id": run_id,
              "mode": mode, "exp": time.time() + ttl_s}
    payload = base64.urlsafe_b64encode(
        json.dumps(claims, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"{payload}.{_sign(payload.encode('ascii'))}"


def verify(token: str) -> dict | None:
    """Claims dict if the signature is valid and unexpired, else None."""
    try:
        payload, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, _sign(payload.encode("ascii"))):
            return None
        claims = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        if claims.get("exp", 0) < time.time():
            return None
        return claims
    except Exception:
        return None
