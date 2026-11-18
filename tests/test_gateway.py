"""Comprehensive tests for the Python local model API gateway."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from contextlib import asynccontextmanager
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web

from copilot_app_proxy.cli.diagnostics import fetch_healthz
from copilot_app_proxy.dpapi import DpapiError
from copilot_app_proxy.gateway import CLIENT_SESSION_KEY, create_app, listen
from copilot_app_proxy.relay_store import RelayTokenStore, TokenPersistError

SYNTHETIC_TOKEN = "synthetic-test-token-not-a-real-credential"
CALLER_CREDENTIAL = "caller-credential-must-not-forward"
RELAY_SECRET = "synthetic-relay-ingest-secret-at-least-thirty-two-chars"


@asynccontextmanager
async def gateway_fixture(upstream_handler, *, relay_store=None, **gateway_kwargs):
    if relay_store is None:
        relay_store = RelayTokenStore()
        await relay_store.set_token(SYNTHETIC_TOKEN)
    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{tail:.*}", upstream_handler)
    async with listen(upstream_app, 0) as upstream:
        up_url = f"http://127.0.0.1:{upstream.addresses[0][1]}"
        gw_app = create_app(
            upstream_base=f"{up_url}/api/v1/llmapi/",
            relay_store=relay_store,
            relay_ingest_secret=RELAY_SECRET,
            **gateway_kwargs,
        )
        async with listen(gw_app, 0) as gateway, aiohttp.ClientSession() as client:
            yield f"http://127.0.0.1:{gateway.addresses[0][1]}", client
    assert gw_app[CLIENT_SESSION_KEY].closed


@pytest.mark.asyncio
async def test_core_listener_and_health_probe():
    def app() -> web.Application:
        return create_app(relay_store=RelayTokenStore(), relay_ingest_secret=RELAY_SECRET)

    with pytest.raises(ValueError, match="relay_ingest_secret"):
        create_app(relay_store=RelayTokenStore(), relay_ingest_secret="too-short")
    async with listen(app(), 0) as gateway:
        port = gateway.addresses[0][1]
        health = await asyncio.to_thread(fetch_healthz, port)
        assert health and health["status"] == "ok"
        with pytest.raises(OSError):
            async with listen(second := app(), port):
                pytest.fail("The same loopback port cannot be bound twice.")
        assert second[CLIENT_SESSION_KEY].closed


def _make_jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode())
    return f"e30.{payload.decode().rstrip('=')}.sig"


@pytest.mark.asyncio
async def test_messages_forwarded_with_relayed_token_instead_of_caller_credentials():
    captured: dict = {}

    async def handler(request: web.Request) -> web.Response:
        captured["path"] = request.raw_path
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = await request.text()
        return web.Response(
            status=200,
            text=json.dumps({"type": "message", "content": [{"type": "text", "text": "OK"}]}),
            content_type="application/json",
            headers={"set-cookie": "must-not-forward=1"},
        )

    body = json.dumps(
        {
            "model": "claude-opus-5.5",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Only say OK."}],
            "tools": [{"name": "read_file", "input_schema": {"type": "object", "properties": {}}}],
        }
    )

    headers = {
        "authorization": f"Bearer {CALLER_CREDENTIAL}",
        "x-api-key": CALLER_CREDENTIAL,
        "cookie": "must-not-forward=1",
        "x-unrelated-secret": "must-not-forward",
    }
    async with (
        gateway_fixture(handler) as (url, client),
        client.post(f"{url}/v1/messages", data=body, headers=headers) as resp,
    ):
        assert resp.status == 200
        data = await resp.json()
        assert data["content"][0]["text"] == "OK"
        assert resp.headers.get("set-cookie") is None

    assert captured["path"] == "/api/v1/llmapi/v1/messages"
    assert captured["body"] == body
    assert captured["headers"]["authorization"] == f"Bearer {SYNTHETIC_TOKEN}"
    assert "x-api-key" not in captured["headers"]
    assert captured["headers"]["x-clienttype"] == "Copilot"
    assert captured["headers"]["x-clientscenario"] == "Code"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["headers"]["x-llm-api-version"] == "2026-10-21"
    assert re.fullmatch(r"[0-9a-f-]{36}", captured["headers"]["x-conversationid"])
    assert "cookie" not in captured["headers"]
    assert "x-unrelated-secret" not in captured["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body", "upstream_path"),
    [
        ("GET", "/v1/models", None, "/api/v1/llmapi/models"),
        ("POST", "/v1/responses", '{"model":"gpt-x","input":"hi"}', "/api/v1/llmapi/responses"),
    ],
)
async def test_route_path_mapped_without_caller_credentials(method, path, body, upstream_path):
    captured: dict = {}

    async def handler(request: web.Request) -> web.Response:
        captured["method"] = request.method
        captured["path"] = request.raw_path
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = await request.text() or None
        return web.json_response({"ok": True})

    headers = {"authorization": f"Bearer {CALLER_CREDENTIAL}"}
    async with (
        gateway_fixture(handler) as (url, client),
        client.request(method, f"{url}{path}", data=body, headers=headers) as resp,
    ):
        assert resp.status == 200
        assert await resp.json() == {"ok": True}

    assert captured == {
        "method": method,
        "path": upstream_path,
        "authorization": f"Bearer {SYNTHETIC_TOKEN}",
        "body": body,
    }


@pytest.mark.asyncio
async def test_sse_first_chunk_arrives_before_upstream_finishes():
    can_end = asyncio.Event()
    upstream_ended = False

    async def handler(request: web.Request) -> web.StreamResponse:
        nonlocal upstream_ended
        await request.read()
        resp = web.StreamResponse(status=200, headers={"content-type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'event: message_start\ndata: {"type":"message_start"}\n\n')
        await can_end.wait()
        upstream_ended = True
        await resp.write(b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
        await resp.write_eof()
        return resp

    async with (
        gateway_fixture(handler) as (url, client),
        client.post(f"{url}/v1/messages", data="{}") as resp,
    ):
        assert resp.headers.get("content-type") == "text/event-stream"
        first_chunk = await asyncio.wait_for(resp.content.readany(), timeout=2.0)
        assert b"message_start" in first_chunk
        assert upstream_ended is False
        can_end.set()
        rest = await asyncio.wait_for(resp.content.read(), timeout=2.0)
        assert b"message_stop" in rest


@pytest.mark.asyncio
async def test_upstream_auth_failures_preserved():
    async def handler(request: web.Request) -> web.Response:
        await request.read()
        return web.Response(
            status=401,
            text='{"error":{"code":"AuthTokenMissing"}}',
            content_type="application/json",
            headers={"www-authenticate": "Bearer"},
        )

    async with (
        gateway_fixture(handler) as (url, client),
        client.post(f"{url}/v1/messages", data="{}") as resp,
    ):
        assert resp.status == 401
        assert resp.headers.get("www-authenticate") == "Bearer"
        data = await resp.json()
        assert data["error"]["code"] == "AuthTokenMissing"


@pytest.mark.asyncio
async def test_invalid_requests_never_reach_upstream():
    upstream_calls = 0

    async def handler(_: web.Request) -> web.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return web.Response(text="ok")

    cases = [
        ("POST", "/v1/messages", {}, "not-json", 400),
        ("POST", "/v1/messages", {}, "[]", 400),
        ("POST", "/v1/messages", {}, b"\xff", 400),
        ("POST", "/v1/messages", {}, json.dumps({"text": "x" * 300}), 413),
        ("GET", "/healthz", {"origin": "https://untrusted.invalid"}, None, 403),
        ("GET", "/healthz", {"host": "untrusted.invalid"}, None, 403),
        ("POST", "/v1/messages", {"origin": "https://untrusted.invalid"}, "{}", 403),
        ("POST", "/v1/messages?access_token=must-not-leak", {}, "{}", 400),
        ("GET", "/proxy/https://untrusted.invalid", {}, None, 404),
        ("GET", "/v1/messages", {}, None, 405),
    ]

    async with gateway_fixture(handler, body_limit=256) as (url, client):
        async with client.get(f"{url}/healthz") as resp:
            assert resp.status == 200
            assert (await resp.json())["upstreamAuthenticated"] is True
        for method, path, headers, body, expected_status in cases:
            async with client.request(method, f"{url}{path}", headers=headers, data=body) as resp:
                assert resp.status == expected_status
                text = await resp.text()
                assert SYNTHETIC_TOKEN not in text
                assert "must-not-leak" not in text
    assert upstream_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [502, 504])
async def test_upstream_errors_do_not_leak_credentials(status):
    async def handler(request: web.Request) -> web.Response:
        await request.read()
        if status == 502:
            transport = request.transport
            assert transport is not None
            transport.abort()
        else:
            await asyncio.sleep(1.0)
        return web.Response()

    timeout_ms = 100 if status == 504 else 2_000
    async with (
        gateway_fixture(handler, timeout_ms=timeout_ms) as (url, client),
        client.post(f"{url}/v1/messages", data="{}") as resp,
    ):
        assert resp.status == status
        assert resp.headers["cache-control"] == "no-store"
        message = "Upstream timed out." if status == 504 else "Upstream connection failed."
        assert await resp.json() == {
            "type": "error",
            "error": {"type": "api_error", "message": message},
        }


@pytest.mark.asyncio
async def test_relay_ingests_token_stores_dpapi_cache_and_forwards(tmp_path):
    token_file = tmp_path / "token.dpapi"
    relay_store = RelayTokenStore(token_file=token_file)
    upstream_auth: str | None = None

    async def handler(request: web.Request) -> web.Response:
        nonlocal upstream_auth
        upstream_auth = request.headers.get("authorization")
        await request.read()
        return web.json_response({"type": "message", "content": [{"type": "text", "text": "OK"}]})

    async with gateway_fixture(handler, relay_store=relay_store) as (url, client):
        async with client.post(f"{url}/v1/messages", data="{}") as before:
            assert before.status == 401

        for secret_headers in ({}, {"x-relay-secret": "wrong-secret"}):
            async with client.post(
                f"{url}/_internal/token", headers=secret_headers, json={"token": SYNTHETIC_TOKEN}
            ) as bad_secret:
                assert bad_secret.status == 401
        assert relay_store.status()["hasToken"] is False

        jwt_token = _make_jwt(int(time.time()) + 3600)
        async with client.post(
            f"{url}/_internal/token",
            headers={"x-relay-secret": RELAY_SECRET},
            json={"token": jwt_token},
        ) as ingest:
            assert ingest.status == 200
            ingest_body = await ingest.json()
            assert ingest_body["status"] == "updated"
            assert jwt_token not in json.dumps(ingest_body)

        raw_cipher = token_file.read_text(encoding="utf-8")
        assert jwt_token not in raw_cipher

        reloaded = RelayTokenStore(token_file=token_file)
        assert await reloaded.load_cached() is True
        assert reloaded.get_token() == jwt_token

        with patch("copilot_app_proxy.relay_store.save_protected_text") as save:
            await reloaded.set_token(jwt_token)
            save.assert_not_called()
            save.side_effect = OSError("synthetic write failure")
            with pytest.raises(TokenPersistError, match="Could not persist the relay token"):
                await reloaded.set_token(SYNTHETIC_TOKEN)
            assert reloaded.get_token() == jwt_token
            save.side_effect = None
            await reloaded.set_token(SYNTHETIC_TOKEN)
            assert reloaded.get_token() == SYNTHETIC_TOKEN

        async with client.get(f"{url}/healthz") as health:
            assert (await health.json())["upstreamAuthenticated"] is True

        async with client.post(
            f"{url}/v1/messages",
            json={"model": "claude-opus-5.5", "messages": [{"role": "user", "content": "hi"}]},
        ) as ok_resp:
            assert ok_resp.status == 200
            assert upstream_auth == f"Bearer {jwt_token}"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_type", [OSError, DpapiError])
async def test_relay_ingest_reports_storage_failure_as_server_error(tmp_path, failure_type):
    store = RelayTokenStore(tmp_path / "token.dpapi")
    headers = {"x-relay-secret": RELAY_SECRET}

    async def upstream(_: web.Request) -> web.Response:
        pytest.fail("Token ingestion must not contact the upstream.")

    async with gateway_fixture(upstream, relay_store=store) as (url, client):
        endpoint = f"{url}/_internal/token"
        with patch(
            "copilot_app_proxy.relay_store.save_protected_text",
            side_effect=[failure_type("private storage details"), None],
        ) as save:
            async with client.post(endpoint, headers=headers, json={"token": None}) as resp:
                assert resp.status == 400
            save.assert_not_called()
            async with client.post(endpoint, headers=headers, json={"token": SYNTHETIC_TOKEN}) as resp:
                assert resp.status == 500
                assert await resp.json() == {
                    "type": "error",
                    "error": {"type": "api_error", "message": "Could not persist the relay token."},
                }
            assert not store.status()["hasToken"]
            async with client.post(endpoint, headers=headers, json={"token": SYNTHETIC_TOKEN}) as resp:
                assert resp.status == 200
            assert store.get_token() == SYNTHETIC_TOKEN


@pytest.mark.asyncio
async def test_relay_missing_and_expired_token_errors_preserve_copilot_instructions():
    relay_store = RelayTokenStore()
    upstream_calls = 0

    async def handler(_: web.Request) -> web.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return web.Response(text="ok")

    async with gateway_fixture(handler, relay_store=relay_store) as (url, client):
        for expired in (False, True):
            if expired:
                await relay_store.set_token(_make_jwt(1))
            async with client.post(f"{url}/v1/messages", data="{}") as resp:
                assert resp.status == 401
                result = await resp.json()
                msg = result["error"]["message"]
                assert ("has expired" if expired else "No Copilot upstream token") in msg
                assert "Copilot App" in msg
                assert "Code 页发送一次消息" in msg
                assert "--login" not in msg
                assert relay_store.status()["hasToken"] is False
        assert upstream_calls == 0
