"""Comprehensive tests for the Python local Messages API gateway."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from copilot_app_proxy.gateway import create_app
from copilot_app_proxy.relay_store import RelayTokenStore

SYNTHETIC_TOKEN = "synthetic-test-token-not-a-real-credential"
AUTH = {"authorization": f"Bearer {SYNTHETIC_TOKEN}"}
LOCAL_KEY = "synthetic-local-gateway-key-at-least-thirty-two-characters"
RELAY_SECRET = "synthetic-relay-ingest-secret-at-least-thirty-two-chars"


async def _start_app(app: web.Application) -> tuple[web.AppRunner, str]:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


@asynccontextmanager
async def gateway_fixture(upstream_handler, **gateway_kwargs):
    upstream_app = web.Application()
    upstream_app.router.add_route("*", "/{tail:.*}", upstream_handler)
    up_runner, up_url = await _start_app(upstream_app)
    gw_app = create_app(upstream_base=f"{up_url}/api/v1/llmapi/", **gateway_kwargs)
    gw_runner, gw_url = await _start_app(gw_app)
    try:
        async with aiohttp.ClientSession() as client:
            yield gw_url, client
    finally:
        await gw_runner.cleanup()
        await up_runner.cleanup()


def _make_jwt(exp: int) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode("utf-8")).decode("ascii").rstrip("=")
    return f"e30.{payload}.sig"


@pytest.mark.asyncio
async def test_health_does_not_imply_upstream_auth_and_missing_token_never_reaches_upstream():
    upstream_calls = 0

    async def handler(_request: web.Request) -> web.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return web.Response(text="ok")

    async with gateway_fixture(handler) as (url, client):
        async with client.get(f"{url}/healthz") as resp:
            assert resp.status == 200
            data = await resp.json()
            assert data["upstreamAuthenticated"] is False
        async with client.post(f"{url}/v1/messages", data="{}") as resp:
            assert resp.status == 401
        assert upstream_calls == 0


@pytest.mark.asyncio
async def test_messages_json_and_tools_forwarded_unchanged_with_scoped_headers():
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

    async with gateway_fixture(handler) as (url, client), client.post(
        f"{url}/v1/messages",
        data=body,
        headers={**AUTH, "cookie": "must-not-forward=1", "x-unrelated-secret": "must-not-forward"},
    ) as resp:
        assert resp.status == 200
        data = await resp.json()
        assert data["content"][0]["text"] == "OK"
        assert resp.headers.get("set-cookie") is None

    assert captured["path"] == "/api/v1/llmapi/v1/messages"
    assert captured["body"] == body
    assert captured["headers"]["authorization"] == AUTH["authorization"]
    assert captured["headers"]["x-clienttype"] == "Copilot"
    assert captured["headers"]["x-clientscenario"] == "Code"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert captured["headers"]["x-llm-api-version"] == "2026-10-21"
    assert re.fullmatch(r"[0-9a-f-]{36}", captured["headers"]["x-conversationid"])
    assert "cookie" not in captured["headers"]
    assert "x-unrelated-secret" not in captured["headers"]


@pytest.mark.asyncio
async def test_x_api_key_accepted_and_models_path_mapped():
    captured: dict = {}

    async def handler(request: web.Request) -> web.Response:
        captured["path"] = request.raw_path
        captured["authorization"] = request.headers.get("authorization")
        return web.json_response({"data": [{"id": "claude-opus-5.5"}]})

    async with (
        gateway_fixture(handler) as (url, client),
        client.get(f"{url}/v1/models", headers={"x-api-key": SYNTHETIC_TOKEN}) as resp,
    ):
        assert resp.status == 200
        data = await resp.json()
        assert data["data"][0]["id"] == "claude-opus-5.5"

    assert captured == {"path": "/api/v1/llmapi/models", "authorization": AUTH["authorization"]}


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
        client.post(f"{url}/v1/messages", headers=AUTH, data="{}") as resp,
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
        client.post(f"{url}/v1/messages", headers=AUTH, data="{}") as resp,
    ):
        assert resp.status == 401
        assert resp.headers.get("www-authenticate") == "Bearer"
        data = await resp.json()
        assert data["error"]["code"] == "AuthTokenMissing"


@pytest.mark.asyncio
async def test_bad_json_excessive_bodies_ambiguous_creds_origins_and_routes_rejected():
    upstream_calls = 0

    async def handler(_request: web.Request) -> web.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return web.Response(text="ok")

    cases = [
        ("POST", "/v1/messages", AUTH, "not-json", 400),
        ("POST", "/v1/messages", AUTH, json.dumps({"text": "x" * 300}), 413),
        ("POST", "/v1/messages", {**AUTH, "x-api-key": SYNTHETIC_TOKEN}, "{}", 401),
        ("GET", "/healthz", {"origin": "https://untrusted.invalid"}, None, 403),
        ("POST", "/v1/messages?access_token=must-not-leak", AUTH, "{}", 400),
        ("GET", "/proxy/https://untrusted.invalid", AUTH, None, 404),
        ("GET", "/v1/messages", AUTH, None, 405),
    ]

    async with gateway_fixture(handler, body_limit=256) as (url, client):
        for method, path, headers, body, expected_status in cases:
            async with client.request(method, f"{url}{path}", headers=headers, data=body) as resp:
                assert resp.status == expected_status
                text = await resp.text()
                assert SYNTHETIC_TOKEN not in text
                assert "must-not-leak" not in text
    assert upstream_calls == 0


@pytest.mark.asyncio
async def test_non_loopback_host_header_rejected():
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async with (
        gateway_fixture(handler) as (url, client),
        client.get(f"{url}/healthz", headers={"Host": "untrusted.invalid"}) as resp,
    ):
        assert resp.status == 403


@pytest.mark.asyncio
async def test_unresponsive_upstream_times_out_without_leaking_credentials():
    async def handler(request: web.Request) -> web.Response:
        await request.read()
        await asyncio.sleep(1.0)
        return web.Response(text="late")

    async with (
        gateway_fixture(handler, timeout_ms=100) as (url, client),
        client.post(f"{url}/v1/messages", headers=AUTH, data="{}") as resp,
    ):
        assert resp.status == 504
        text = await resp.text()
        assert "timed out" in text
        assert SYNTHETIC_TOKEN not in text


@pytest.mark.asyncio
async def test_token_provider_validates_local_key_and_injects_upstream_token():
    provider_calls = 0
    upstream_auth: str | None = None

    async def handler(request: web.Request) -> web.Response:
        nonlocal upstream_auth
        upstream_auth = request.headers.get("authorization")
        await request.read()
        return web.json_response({"type": "message", "content": []})

    async def token_provider() -> str:
        nonlocal provider_calls
        provider_calls += 1
        return SYNTHETIC_TOKEN

    async with gateway_fixture(handler, local_api_key=LOCAL_KEY, token_provider=token_provider) as (
        url,
        client,
    ):
        async with client.post(f"{url}/v1/messages", headers=AUTH, data="{}") as denied:
            assert denied.status == 401
        assert provider_calls == 0

        async with client.post(
            f"{url}/v1/messages", headers={"x-api-key": LOCAL_KEY}, data="{}"
        ) as accepted:
            assert accepted.status == 200
        assert provider_calls == 1
        assert upstream_auth == f"Bearer {SYNTHETIC_TOKEN}"
        assert LOCAL_KEY not in (upstream_auth or "")

        async with client.get(f"{url}/healthz") as health:
            assert (await health.json())["authentication"] == "local-gateway-key"


@pytest.mark.asyncio
async def test_token_provider_fails_closed_on_errors():
    upstream_calls = 0

    async def handler(_request: web.Request) -> web.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return web.Response(text="ok")

    async def failing_provider() -> str:
        raise RuntimeError(f"private upstream failure: {SYNTHETIC_TOKEN}")

    async with gateway_fixture(
        handler, local_api_key=LOCAL_KEY, token_provider=failing_provider
    ) as (url, client):
        async with client.post(
            f"{url}/v1/messages", headers={"x-api-key": LOCAL_KEY}, data="{}"
        ) as resp:
            assert resp.status == 401
            text = await resp.text()
            assert SYNTHETIC_TOKEN not in text
            assert LOCAL_KEY not in text
        assert upstream_calls == 0

    with pytest.raises(ValueError, match="local API key"):
        create_app(token_provider=lambda: SYNTHETIC_TOKEN)


@pytest.mark.asyncio
async def test_relay_mode_ingests_token_stores_dpapi_cache_and_forwards(tmp_path):
    token_file = tmp_path / "token.dpapi"
    relay_store = RelayTokenStore(token_file=token_file)
    upstream_auth: str | None = None

    async def handler(request: web.Request) -> web.Response:
        nonlocal upstream_auth
        upstream_auth = request.headers.get("authorization")
        await request.read()
        return web.json_response({"type": "message", "content": [{"type": "text", "text": "OK"}]})

    async with gateway_fixture(
        handler,
        local_api_key=LOCAL_KEY,
        relay_store=relay_store,
        relay_ingest_secret=RELAY_SECRET,
        auth_mode="copilot-token-relay",
        token_provider=relay_store.get_token,
    ) as (url, client):
        async with client.post(
            f"{url}/v1/messages", headers={"x-api-key": LOCAL_KEY}, data="{}"
        ) as before:
            assert before.status == 401

        async with client.post(
            f"{url}/_internal/token",
            headers={"x-relay-secret": "wrong-secret"},
            json={"token": SYNTHETIC_TOKEN},
        ) as bad_secret:
            assert bad_secret.status == 401

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

        async with client.get(f"{url}/healthz") as health:
            health_json = await health.json()
            assert health_json["authentication"] == "copilot-token-relay"
            assert health_json["upstreamAuthenticated"] is True

        async with client.post(
            f"{url}/v1/messages",
            headers={"x-api-key": LOCAL_KEY},
            json={"model": "claude-opus-5.5", "messages": [{"role": "user", "content": "hi"}]},
        ) as ok_resp:
            assert ok_resp.status == 200
            assert upstream_auth == f"Bearer {jwt_token}"


@pytest.mark.asyncio
async def test_relay_missing_and_expired_token_errors_preserve_copilot_instructions():
    relay_store = RelayTokenStore()
    upstream_calls = 0

    async def handler(_request: web.Request) -> web.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return web.Response(text="ok")

    async with gateway_fixture(
        handler, local_api_key=LOCAL_KEY, token_provider=relay_store.get_token
    ) as (url, client):
        for expired in (False, True):
            if expired:
                await relay_store.set_token(_make_jwt(1))
            async with client.post(
                f"{url}/v1/messages", headers={"x-api-key": LOCAL_KEY}, data="{}"
            ) as resp:
                assert resp.status == 401
                result = await resp.json()
                msg = result["error"]["message"]
                assert ("has expired" if expired else "No Copilot upstream token") in msg
                assert "Copilot App" in msg
                assert "--login" not in msg
                assert relay_store.status()["hasToken"] is False
        assert upstream_calls == 0
