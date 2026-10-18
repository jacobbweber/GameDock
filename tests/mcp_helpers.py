"""Helpers to talk to the app's /mcp endpoint with the official MCP SDK client, in-process."""
import asyncio
import contextlib
import json

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from gamedock.mcp_api import McpConfig
from gamedock.web.app import create_app

URL = "http://127.0.0.1:8080/mcp"


def make_app(service, mode="full", token=None, confirmations=None, **cfg):
    return create_app(service, mcp_config=McpConfig(mode=mode, token=token, **cfg), mcp_confirmations=confirmations)


@contextlib.asynccontextmanager
async def mcp_client(app, headers=None, url=URL):
    """Yield an SDK Client connected to `app` (lifespan started) without opening a network port."""
    async with app.router.lifespan_context(app):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers or {})
        async with http:
            async with Client(streamable_http_client(url, http_client=http)) as c:
                yield c


async def call(c, tool, /, **args):
    """Call a tool. Returns (is_error, parsed JSON dict, or the error text)."""
    r = await c.call_tool(tool, args)
    text = "".join(getattr(b, "text", "") for b in r.content)
    if r.is_error:
        return True, text
    return False, json.loads(text)


def run(app, fn, **kw):
    """Run `async fn(client)` against the app and return its result."""
    async def go():
        async with mcp_client(app, **kw) as c:
            return await fn(c)
    return asyncio.run(go())
