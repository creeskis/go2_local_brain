"""Lightweight bearer-token auth for the aiohttp browser GUIs.

Why
---
The autonomy GUI exposes ``/api/manual/move``, ``/api/autonomy/start``,
``/api/follow/start``, ``/api/nerf/arm``, etc. — anyone who can reach the
port can drive the dog (and, with the Nerf launcher, arm it). Previously
the default bind was ``0.0.0.0`` with no auth, so anyone on the same WiFi
could control the robot.

This module provides:

* ``generate_token()`` — 32-hex-char random token (~128 bits entropy).
* ``make_auth_middleware(token)`` — aiohttp middleware that rejects POSTs
  to ``/api/...`` without the right ``Authorization: Bearer <token>`` or
  ``?token=<token>`` query parameter. Returns 401 on failure.
* ``inject_token(html, token)`` — substitutes the token into the HTML
  template's auth placeholder so the browser-side JS can attach it to
  every ``fetch()`` call.

If ``token`` is ``None`` the middleware becomes a no-op — useful for
``--no-auth`` opt-out, but never the default.
"""

from __future__ import annotations

import logging
import secrets
from typing import Awaitable, Callable

from aiohttp import web

log = logging.getLogger(__name__)

# Sentinel that gets replaced by ``inject_token``. Picked to be unique
# enough that no existing CSS/JS string will accidentally match.
TOKEN_PLACEHOLDER = "__GO2_AUTH_TOKEN__"


def generate_token() -> str:
    """Return a 32-hex-char random token (~128 bits)."""
    return secrets.token_hex(16)


def make_auth_middleware(token: str | None) -> Callable[[web.Request, Callable[[web.Request], Awaitable[web.StreamResponse]]], Awaitable[web.StreamResponse]]:
    """Build an aiohttp middleware that gates POSTs to ``/api/...``.

    GET routes (the HTML index, status polling, MJPEG video) are *not*
    gated — they only leak state, not control. POSTs are the dangerous
    surface.
    """

    @web.middleware
    async def auth_middleware(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        if token is None:
            # Auth disabled. Be loud about it once.
            return await handler(request)
        if request.method != "POST" or not request.path.startswith("/api/"):
            return await handler(request)
        if _request_has_valid_token(request, token):
            return await handler(request)
        log.warning(
            "rejected unauthenticated %s %s from %s",
            request.method, request.path, request.remote,
        )
        return web.json_response({"error": "unauthorized"}, status=401)

    return auth_middleware


def inject_token(html: str, token: str | None) -> str:
    """Replace ``TOKEN_PLACEHOLDER`` in ``html`` with the live token.

    When auth is disabled (``token is None``), the placeholder becomes
    an empty string and the JS fetch shim is a no-op.
    """
    return html.replace(TOKEN_PLACEHOLDER, token or "")


def _request_has_valid_token(request: web.Request, token: str) -> bool:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        # constant-time compare so we don't leak the token via timing.
        if secrets.compare_digest(header[len("Bearer "):], token):
            return True
    query = request.query.get("token", "")
    if query and secrets.compare_digest(query, token):
        return True
    return False
