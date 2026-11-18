"""Local Messages API gateway using aiohttp."""

from __future__ import annotations

import asyncio
import hmac
import inspect
import json
import os
import re
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import web

from copilot_app_proxy.relay_store import TOKEN_PATTERN, RelayTokenStore, RequestError

DEFAULT_UPSTREAM = "https://loki.delve.office.com/api/v1/llmapi/"
CLIENT_SESSION_KEY = web.AppKey("http_client", aiohttp.ClientSession)

RESPONSE_HEADERS = (
    "content-type",
    "content-encoding",
    "retry-after",
    "request-id",
    "x-request-id",
    "www-authenticate",
    "anthropic-ratelimit-requests-limit",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-tokens-reset",
)

_BEARER_RE = re.compile(r"^Bearer\s+(\S+)$", re.IGNORECASE)


@dataclass(frozen=True)
class RouteSpec:
    method: str
    upstream_path: str


ROUTES: dict[str, RouteSpec] = {
    "/v1/messages": RouteSpec(method="POST", upstream_path="v1/messages"),
    "/v1/messages/count_tokens": RouteSpec(
        method="POST", upstream_path="v1/messages/count_tokens"
    ),
    "/v1/models": RouteSpec(method="GET", upstream_path="models"),
}

TokenProvider = Callable[[], str | Awaitable[str]]


def _json_response(
    status: int,
    payload: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
) -> web.Response:
    merged_headers = {
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "connection": "close",
    }
    if headers:
        merged_headers.update(headers)
    return web.Response(
        status=status,
        text=json.dumps(payload),
        content_type="application/json",
        charset="utf-8",
        headers=merged_headers,
    )


def _require_secret(candidate: str | None, expected: str, message: str) -> None:
    if isinstance(candidate, str) and hmac.compare_digest(
        candidate.encode("utf-8"), expected.encode("utf-8")
    ):
        return
    raise RequestError(401, "authentication_error", message)


def _caller_token(request: web.Request) -> str:
    cred_headers = [
        name
        for name, _ in request.raw_headers
        if name.decode("latin-1").lower() in ("authorization", "x-api-key")
    ]
    if len(cred_headers) != 1:
        raise RequestError(
            401,
            "authentication_error",
            "Provide exactly one Authorization: Bearer or x-api-key "
            "containing a valid upstream access token.",
        )
    authorization = request.headers.get("authorization")
    if authorization is not None:
        match = _BEARER_RE.match(authorization)
        token = match.group(1) if match else None
    else:
        token = request.headers.get("x-api-key")
    if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
        raise RequestError(401, "authentication_error", "Invalid access token format.")
    return token


def _validate_local_request(request: web.Request) -> None:
    hosts = request.headers.getall("host", [])
    if len(hosts) != 1:
        raise RequestError(403, "permission_error", "A loopback Host header is required.")
    host = hosts[0]
    try:
        authority = urlsplit(f"http://{host}")
    except ValueError as exc:
        raise RequestError(403, "permission_error", "Only this loopback endpoint is allowed.") from exc

    sockname = request.transport.get_extra_info("sockname") if request.transport else None
    local_port = sockname[1] if isinstance(sockname, tuple) and len(sockname) >= 2 else None
    try:
        host_port = authority.port or 80
    except ValueError as exc:
        raise RequestError(403, "permission_error", "Only this loopback endpoint is allowed.") from exc

    if (
        authority.hostname not in ("localhost", "127.0.0.1")
        or authority.username is not None
        or authority.password is not None
        or authority.path not in ("", "/")
        or authority.query
        or authority.fragment
        or (local_port is not None and host_port != local_port)
    ):
        raise RequestError(403, "permission_error", "Only this loopback endpoint is allowed.")

    if "origin" in request.headers:
        raise RequestError(403, "permission_error", "Browser-origin requests are not allowed.")

    raw_path = request.raw_path or ""
    if not raw_path.startswith("/") or raw_path.startswith("//"):
        raise RequestError(400, "invalid_request_error", "Only relative API paths are allowed.")
    if "?" in raw_path:
        raise RequestError(400, "invalid_request_error", "Query parameters are not accepted.")


async def _read_json_body(request: web.Request, limit: int, timeout_sec: float) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                raise RequestError(
                    413, "invalid_request_error", "Request body exceeds the configured limit."
                )
        except ValueError:
            pass

    chunks: list[bytes] = []
    total = 0
    try:
        async with asyncio.timeout(timeout_sec):
            async for chunk in request.content.iter_any():
                total += len(chunk)
                if total > limit:
                    raise RequestError(
                        413,
                        "invalid_request_error",
                        "Request body exceeds the configured limit.",
                    )
                chunks.append(chunk)
    except TimeoutError as exc:
        raise RequestError(408, "timeout_error", "Request body timed out.") from exc
    except RequestError:
        raise
    except Exception as exc:
        raise RequestError(400, "invalid_request_error", "Request body could not be read.") from exc

    body = b"".join(chunks)
    try:
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise TypeError("Not a JSON object")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise RequestError(
            400, "invalid_request_error", "Request body must be a JSON object."
        ) from exc
    return body


async def _forward(
    request: web.Request,
    target_url: str,
    token: str,
    body: bytes | None,
    timeout_sec: float,
) -> web.StreamResponse:
    headers: dict[str, str] = {
        "authorization": f"Bearer {token}",
        "accept": "application/json, text/event-stream",
        "accept-encoding": "identity",
        "user-agent": "local-messages-gateway/0.2",
        "x-clienttype": "Copilot",
        "x-clientscenario": "Code",
        "x-clientcallid": str(uuid.uuid4()),
        "x-conversationid": request.headers.get("x-conversationid") or str(uuid.uuid4()),
        "x-llm-api-version": request.headers.get("x-llm-api-version") or "2026-10-21",
        "anthropic-version": request.headers.get("anthropic-version") or "2023-06-01",
    }
    if "anthropic-beta" in request.headers:
        headers["anthropic-beta"] = request.headers["anthropic-beta"]
    if body is not None:
        headers["content-type"] = "application/json"
        headers["content-length"] = str(len(body))

    session = request.app[CLIENT_SESSION_KEY]
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    prepared = False
    stream_resp: web.StreamResponse | None = None

    try:
        async with session.request(
            request.method,
            target_url,
            headers=headers,
            data=body,
            timeout=timeout,
            allow_redirects=False,
        ) as upstream_resp:
            stream_resp = web.StreamResponse(status=upstream_resp.status or 502)
            stream_resp.headers["cache-control"] = "no-store"
            stream_resp.headers["x-content-type-options"] = "nosniff"
            stream_resp.headers["x-accel-buffering"] = "no"
            for name in RESPONSE_HEADERS:
                for value in upstream_resp.headers.getall(name, ()):
                    stream_resp.headers.add(name, value)
            await stream_resp.prepare(request)
            prepared = True

            async for chunk in upstream_resp.content.iter_any():
                if chunk:
                    await stream_resp.write(chunk)
            await stream_resp.write_eof()
            return stream_resp
    except TimeoutError:
        if prepared and stream_resp is not None:
            return stream_resp
        return _json_response(
            504,
            {"type": "error", "error": {"type": "api_error", "message": "Upstream timed out."}},
        )
    except (aiohttp.ClientError, OSError):
        if prepared and stream_resp is not None:
            return stream_resp
        return _json_response(
            502,
            {
                "type": "error",
                "error": {"type": "api_error", "message": "Upstream connection failed."},
            },
        )
    except asyncio.CancelledError:
        raise


def create_app(
    *,
    upstream_base: str = DEFAULT_UPSTREAM,
    body_limit: int = 8 * 1024 * 1024,
    timeout_ms: int = 120_000,
    token_provider: TokenProvider | None = None,
    local_api_key: str | None = None,
    relay_store: RelayTokenStore | None = None,
    relay_ingest_secret: str | None = None,
    auth_mode: str | None = None,
) -> web.Application:
    """Create the aiohttp gateway application."""
    if token_provider is not None and (
        not callable(token_provider) or not isinstance(local_api_key, str) or len(local_api_key) < 32
    ):
        raise ValueError(
            "Managed login requires a token provider and a local API key of at least 32 characters."
        )
    if relay_store is not None and (
        not isinstance(relay_ingest_secret, str) or len(relay_ingest_secret) < 32
    ):
        raise ValueError("Relay mode requires a relay_ingest_secret of at least 32 characters.")

    parsed_base = urlsplit(upstream_base)
    if (
        parsed_base.scheme not in ("http", "https")
        or parsed_base.username is not None
        or parsed_base.password is not None
        or not parsed_base.path.endswith("/")
        or parsed_base.query
        or parsed_base.fragment
    ):
        raise ValueError("Invalid upstream base URL.")

    timeout_sec = timeout_ms / 1000.0
    effective_auth_mode = auth_mode or (
        "local-gateway-key" if token_provider else "caller-provided-upstream-token"
    )

    async def on_startup(app: web.Application) -> None:
        connector = aiohttp.TCPConnector(limit=64)
        app[CLIENT_SESSION_KEY] = aiohttp.ClientSession(connector=connector, auto_decompress=False)

    async def on_cleanup(app: web.Application) -> None:
        session = app.get(CLIENT_SESSION_KEY)
        if session is not None and not session.closed:
            await session.close()

    async def handle_all(request: web.Request) -> web.StreamResponse:
        extra_headers: dict[str, str] = {}
        try:
            _validate_local_request(request)
            path = request.path

            if path == "/healthz" and request.method == "GET":
                relay_status = relay_store.status() if relay_store is not None else None
                payload: dict[str, Any] = {
                    "status": "ok",
                    "authentication": effective_auth_mode,
                    "upstreamAuthenticated": relay_status["hasToken"] if relay_status else False,
                }
                if relay_status is not None:
                    payload["tokenExpiresOn"] = relay_status["tokenExpiresOn"]
                    payload["expiresInSeconds"] = relay_status["expiresInSeconds"]
                return _json_response(200, payload)

            if path == "/_internal/token" and relay_store is not None:
                if request.method != "POST":
                    extra_headers["allow"] = "POST"
                    raise RequestError(405, "invalid_request_error", "Method not allowed.")
                secret = request.headers.get("x-relay-secret")
                if not isinstance(secret, str):
                    raise RequestError(401, "authentication_error", "Missing relay ingest secret.")
                assert relay_ingest_secret is not None
                _require_secret(secret, relay_ingest_secret, "Invalid relay ingest secret.")
                raw_body = await _read_json_body(request, 64 * 1024, timeout_sec)
                parsed = json.loads(raw_body.decode("utf-8"))
                info = await relay_store.set_token(parsed.get("token"))
                return _json_response(200, {"status": "updated", "tokenExpiresOn": info["expiresOn"]})

            route = ROUTES.get(path)
            if route is None:
                raise RequestError(404, "not_found_error", "Unknown API route.")
            if request.method != route.method:
                extra_headers["allow"] = route.method
                raise RequestError(405, "invalid_request_error", "Method not allowed.")

            token = _caller_token(request)
            if token_provider is not None:
                assert local_api_key is not None
                _require_secret(token, local_api_key, "Invalid local gateway key.")
                try:
                    provided = token_provider()
                    if inspect.isawaitable(provided):
                        provided = await provided
                    token = provided
                except RequestError:
                    raise
                except Exception as exc:
                    raise RequestError(
                        401,
                        "authentication_error",
                        "Upstream token is unavailable or expired. "
                        "Trigger a request in Copilot App to refresh it.",
                    ) from exc
                if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
                    raise RequestError(
                        401, "authentication_error", "No valid upstream access token is available."
                    )

            body = (
                await _read_json_body(request, body_limit, timeout_sec)
                if route.method == "POST"
                else None
            )
            target_url = urljoin(upstream_base, route.upstream_path)
            return await _forward(request, target_url, token, body, timeout_sec)
        except RequestError as exc:
            return _json_response(
                exc.status,
                {"type": "error", "error": {"type": exc.error_type, "message": exc.message}},
                headers=extra_headers,
            )
        except Exception:  # noqa: BLE001
            return _json_response(
                400,
                {"type": "error", "error": {"type": "invalid_request_error", "message": "Invalid request."}},
            )

    app = web.Application(client_max_size=0)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_route("*", "/{tail:.*}", handle_all)
    return app


async def serve_gateway(mode: str = "passthrough", port: int | None = None) -> int:
    """Run the gateway server in the given mode ('passthrough' or 'relay')."""
    if port is None:
        raw_port = os.environ.get("PORT", "19324")
        try:
            port = int(raw_port)
        except ValueError:
            print("PORT must be an integer between 1 and 65535.", file=sys.stderr)
            return 1
    if port < 1 or port > 65535:
        print("PORT must be an integer between 1 and 65535.", file=sys.stderr)
        return 1

    kwargs: dict[str, Any] = {}
    if mode == "relay":
        local_api_key = os.environ.get("LOCAL_GATEWAY_KEY", "")
        if len(local_api_key) < 32:
            print(
                "Set LOCAL_GATEWAY_KEY to a random local-only key of at least 32 characters.",
                file=sys.stderr,
            )
            return 1
        kwargs["local_api_key"] = local_api_key

        relay_ingest_secret = os.environ.get("RELAY_INGEST_SECRET", "")
        if len(relay_ingest_secret) < 32:
            print(
                "Set RELAY_INGEST_SECRET to a random internal secret of at least 32 characters.",
                file=sys.stderr,
            )
            return 1
        token_file = Path(os.environ.get("RELAY_TOKEN_FILE") or Path(".local") / "token.dpapi")
        relay_store = RelayTokenStore(token_file=token_file)
        if await relay_store.load_cached():
            st = relay_store.status()
            print(
                f"Loaded DPAPI-protected cached token "
                f"(valid={st['hasToken']}, exp={st['tokenExpiresOn'] or 'unknown'}).",
                flush=True,
            )
        kwargs["relay_store"] = relay_store
        kwargs["relay_ingest_secret"] = relay_ingest_secret
        kwargs["auth_mode"] = "copilot-token-relay"
        kwargs["token_provider"] = relay_store.get_token

    app = create_app(**kwargs)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    try:
        await site.start()
    except OSError as exc:
        await runner.cleanup()
        if exc.errno in (98, 10048):
            print("Loopback port is already in use.", file=sys.stderr)
        else:
            print("Gateway failed to start.", file=sys.stderr)
        return 1

    print(f"Messages endpoint: http://127.0.0.1:{port}/v1/messages", flush=True)
    instructions = {
        "relay": "Use the local gateway key in VS Code. Copilot App requests update the upstream token.",
        "passthrough": "Caller must supply a valid upstream access token; no tokens are acquired or stored.",
    }
    print(instructions[mode], flush=True)

    stop_event = asyncio.Event()
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await runner.cleanup()
    return 0
