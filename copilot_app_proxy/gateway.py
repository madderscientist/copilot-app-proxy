"""Local Messages API gateway that forwards loopback requests with the relayed Copilot token."""

from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp import web

from copilot_app_proxy.relay_store import (
    InvalidTokenError,
    RelayTokenStore,
    TokenError,
    TokenPersistError,
)

DEFAULT_UPSTREAM = "https://loki.delve.office.com/api/v1/llmapi/"
DEFAULT_BODY_LIMIT = 8 * 1024 * 1024
DEFAULT_TIMEOUT_MS = 120_000
INGEST_BODY_LIMIT = 64 * 1024
MIN_INGEST_SECRET_LENGTH = 32

CLIENT_SESSION_KEY = web.AppKey("http_client", aiohttp.ClientSession)

# Local path -> (method, upstream path relative to the upstream base URL).
MODEL_ROUTES = {
    "/v1/messages": ("POST", "v1/messages"),
    "/v1/models": ("GET", "models"),
}

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

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class RequestError(Exception):
    """HTTP error rendered as an Anthropic-compatible JSON error payload."""

    def __init__(self, status: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.message = message


def _json_response(
    status: int, payload: Mapping[str, Any], *, headers: Mapping[str, str] | None = None
) -> web.Response:
    response_headers = {
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "connection": "close",
        **(headers or {}),
    }
    return web.json_response(payload, status=status, headers=response_headers)


def _error_response(
    status: int, error_type: str, message: str, *, headers: Mapping[str, str] | None = None
) -> web.Response:
    payload = {"type": "error", "error": {"type": error_type, "message": message}}
    return _json_response(status, payload, headers=headers)


def _token_request_error(exc: TokenError) -> RequestError:
    if isinstance(exc, InvalidTokenError):
        return RequestError(400, "invalid_request_error", str(exc))
    if isinstance(exc, TokenPersistError):
        return RequestError(500, "api_error", str(exc))
    return RequestError(401, "authentication_error", str(exc))


def _validate_local_request(request: web.Request) -> None:
    """Reject anything a browser page could send: foreign Host, Origin, or absolute paths."""
    hosts = request.headers.getall("host", [])
    if len(hosts) != 1:
        raise RequestError(403, "permission_error", "A loopback Host header is required.")
    try:
        authority = urlsplit(f"http://{hosts[0]}")
        host_port = authority.port or 80
    except ValueError as exc:
        raise RequestError(
            403, "permission_error", "Only this loopback endpoint is allowed."
        ) from exc

    sockname = request.transport.get_extra_info("sockname") if request.transport else None
    local_port = sockname[1] if isinstance(sockname, tuple) and len(sockname) >= 2 else None
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


@web.middleware
async def _local_api_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Validate every request before routing and render all failures as JSON errors."""
    try:
        _validate_local_request(request)
        return await handler(request)
    except RequestError as exc:
        return _error_response(exc.status, exc.error_type, exc.message)
    except TokenError as exc:
        error = _token_request_error(exc)
        return _error_response(error.status, error.error_type, error.message)
    except web.HTTPMethodNotAllowed as exc:
        allow = ", ".join(sorted(exc.allowed_methods))
        return _error_response(
            405, "invalid_request_error", "Method not allowed.", headers={"allow": allow}
        )
    except web.HTTPNotFound:
        return _error_response(404, "not_found_error", "Unknown API route.")
    except Exception:  # noqa: BLE001
        return _error_response(400, "invalid_request_error", "Invalid request.")


async def _read_json_body(
    request: web.Request, limit: int, timeout_sec: float
) -> tuple[bytes, dict[str, Any]]:
    if (length := request.content_length) is not None and length > limit:
        raise RequestError(413, "invalid_request_error", "Request body exceeds the configured limit.")
    buffer = bytearray()
    try:
        async with asyncio.timeout(timeout_sec):
            async for chunk in request.content.iter_any():
                if len(buffer) + len(chunk) > limit:
                    raise RequestError(
                        413,
                        "invalid_request_error",
                        "Request body exceeds the configured limit.",
                    )
                buffer.extend(chunk)
    except TimeoutError as exc:
        raise RequestError(408, "timeout_error", "Request body timed out.") from exc
    except RequestError:
        raise
    except Exception as exc:
        raise RequestError(400, "invalid_request_error", "Request body could not be read.") from exc

    body = bytes(buffer)
    try:
        parsed = json.loads(body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise TypeError("Not a JSON object")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise RequestError(
            400, "invalid_request_error", "Request body must be a JSON object."
        ) from exc
    # Keep the original bytes for forwarding; internal routes reuse the parsed object.
    return body, parsed


async def _forward(
    request: web.Request,
    target_url: str,
    token: str,
    body: bytes | None,
    timeout_sec: float,
) -> web.StreamResponse:
    # Built from scratch so caller credentials and other headers never reach the upstream.
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
            async for chunk in upstream_resp.content.iter_any():
                if chunk:
                    await stream_resp.write(chunk)
            await stream_resp.write_eof()
            return stream_resp
    except (aiohttp.ClientError, OSError) as exc:
        # A committed stream cannot be replaced by a JSON error.
        if stream_resp is not None and stream_resp.prepared:
            return stream_resp
        if isinstance(exc, TimeoutError):
            raise RequestError(504, "api_error", "Upstream timed out.") from exc
        raise RequestError(502, "api_error", "Upstream connection failed.") from exc


async def _http_client(app: web.Application) -> AsyncGenerator[None, None]:
    async with aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(limit=64), auto_decompress=False
    ) as session:
        app[CLIENT_SESSION_KEY] = session
        yield


def _validate_upstream_base(upstream_base: str) -> None:
    parsed = urlsplit(upstream_base)
    if (
        parsed.scheme not in ("http", "https")
        or parsed.username is not None
        or parsed.password is not None
        or not parsed.path.endswith("/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Invalid upstream base URL.")


def create_app(
    *,
    relay_store: RelayTokenStore,
    relay_ingest_secret: str,
    upstream_base: str = DEFAULT_UPSTREAM,
    body_limit: int = DEFAULT_BODY_LIMIT,
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
) -> web.Application:
    """Create the gateway app.

    Model routes need no caller credentials: any local program may call them, while the
    Host/Origin checks keep browser pages out. Only the relay addon may push tokens, using
    the per-run ``relay_ingest_secret``.
    """
    if not isinstance(relay_ingest_secret, str) or len(relay_ingest_secret) < (
        MIN_INGEST_SECRET_LENGTH
    ):
        raise ValueError(
            f"relay_ingest_secret must be at least {MIN_INGEST_SECRET_LENGTH} characters."
        )
    _validate_upstream_base(upstream_base)
    timeout_sec = timeout_ms / 1000.0
    expected_secret = relay_ingest_secret.encode("utf-8")

    async def healthz(request: web.Request) -> web.Response:
        status = relay_store.status()
        return _json_response(
            200,
            {
                "status": "ok",
                "upstreamAuthenticated": status["hasToken"],
                "tokenExpiresOn": status["tokenExpiresOn"],
                "expiresInSeconds": status["expiresInSeconds"],
            },
        )

    async def ingest_token(request: web.Request) -> web.Response:
        secret = request.headers.get("x-relay-secret")
        if secret is None:
            raise RequestError(401, "authentication_error", "Missing relay ingest secret.")
        if not hmac.compare_digest(secret.encode("utf-8"), expected_secret):
            raise RequestError(401, "authentication_error", "Invalid relay ingest secret.")
        _, payload = await _read_json_body(request, INGEST_BODY_LIMIT, timeout_sec)
        info = await relay_store.set_token(payload.get("token"))
        return _json_response(200, {"status": "updated", "tokenExpiresOn": info["expiresOn"]})

    def model_route(method: str, upstream_path: str) -> Handler:
        target_url = urljoin(upstream_base, upstream_path)

        async def forward(request: web.Request) -> web.StreamResponse:
            token = relay_store.get_token()
            body = None
            if method == "POST":
                body, _ = await _read_json_body(request, body_limit, timeout_sec)
            return await _forward(request, target_url, token, body, timeout_sec)

        return forward

    app = web.Application(client_max_size=0, middlewares=[_local_api_middleware])
    app.cleanup_ctx.append(_http_client)
    app.router.add_route("GET", "/healthz", healthz)
    app.router.add_route("POST", "/_internal/token", ingest_token)
    for path, (method, upstream_path) in MODEL_ROUTES.items():
        app.router.add_route(method, path, model_route(method, upstream_path))
    return app


@asynccontextmanager
async def listen(app: web.Application, port: int) -> AsyncGenerator[web.AppRunner, None]:
    """Run an app on IPv4 loopback; port 0 lets the OS select a free port."""
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        yield runner
    finally:
        await runner.cleanup()
