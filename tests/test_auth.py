"""Tests for the bearer-token auth middleware used by the GUIs."""

from __future__ import annotations

import unittest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from go2_local_brain.auth import (
    TOKEN_PLACEHOLDER,
    generate_token,
    inject_token,
    make_auth_middleware,
)


def _build_app(token: str | None) -> web.Application:
    async def index(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def api_echo(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    app = web.Application(middlewares=[make_auth_middleware(token)])
    app.router.add_get("/", index)
    app.router.add_post("/api/echo", api_echo)
    return app


class TokenHelperTests(unittest.TestCase):
    def test_generate_token_is_32_hex(self) -> None:
        tok = generate_token()
        self.assertEqual(len(tok), 32)
        int(tok, 16)

    def test_generate_token_unique(self) -> None:
        self.assertNotEqual(generate_token(), generate_token())

    def test_inject_substitutes(self) -> None:
        html = f"X={TOKEN_PLACEHOLDER};"
        self.assertIn("X=abc;", inject_token(html, "abc"))
        self.assertNotIn(TOKEN_PLACEHOLDER, inject_token(html, "abc"))

    def test_inject_none_is_empty(self) -> None:
        html = f"X={TOKEN_PLACEHOLDER};"
        self.assertIn("X=;", inject_token(html, None))


class AuthMiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_never_gated(self) -> None:
        async with TestClient(TestServer(_build_app("secret"))) as c:
            self.assertEqual((await c.get("/")).status, 200)

    async def test_post_without_token_401(self) -> None:
        async with TestClient(TestServer(_build_app("secret"))) as c:
            self.assertEqual((await c.post("/api/echo")).status, 401)

    async def test_post_bearer_ok(self) -> None:
        async with TestClient(TestServer(_build_app("secret"))) as c:
            r = await c.post("/api/echo", headers={"Authorization": "Bearer secret"})
            self.assertEqual(r.status, 200)

    async def test_post_query_token_ok(self) -> None:
        async with TestClient(TestServer(_build_app("secret"))) as c:
            self.assertEqual((await c.post("/api/echo?token=secret")).status, 200)

    async def test_post_wrong_token_401(self) -> None:
        async with TestClient(TestServer(_build_app("secret"))) as c:
            r = await c.post("/api/echo", headers={"Authorization": "Bearer wrong"})
            self.assertEqual(r.status, 401)

    async def test_no_token_disables_auth(self) -> None:
        async with TestClient(TestServer(_build_app(None))) as c:
            self.assertEqual((await c.post("/api/echo")).status, 200)


if __name__ == "__main__":
    unittest.main()
