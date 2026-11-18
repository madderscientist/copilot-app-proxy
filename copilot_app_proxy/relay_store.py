"""Token cache and request error types for Copilot App relay mode."""

from __future__ import annotations

import asyncio
import base64
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from copilot_app_proxy.dpapi import DpapiError, load_protected_text, save_protected_text

TOKEN_PATTERN = re.compile(r"^[^\s,\x00-\x1f\x7f]{1,32768}$")


class RequestError(Exception):
    """HTTP error with Anthropic-compatible JSON payload metadata."""

    def __init__(self, status: int, error_type: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error_type = error_type
        self.message = message


def decode_jwt_exp(token: str) -> datetime | None:
    """Extract JWT `exp` claim as a UTC datetime, or None if not a JWT or missing `exp`."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        try:
            return datetime.fromtimestamp(float(exp), tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _format_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class RelayTokenStore:
    """Thread/async-safe in-memory and DPAPI-backed upstream token store."""

    def __init__(self, token_file: str | Path | None = None) -> None:
        self._token: str | None = None
        self._expires_on: datetime | None = None
        self._token_file = Path(token_file) if token_file is not None else None
        self._lock = asyncio.Lock()

    async def load_cached(self) -> bool:
        """Load cached token from DPAPI file if present and valid in format."""
        if self._token_file is None:
            return False
        async with self._lock:
            try:
                token = (await asyncio.to_thread(load_protected_text, self._token_file)).strip()
            except (OSError, DpapiError, UnicodeDecodeError):
                return False
            if not TOKEN_PATTERN.fullmatch(token):
                return False
            self._token = token
            self._expires_on = decode_jwt_exp(token)
            return True

    async def set_token(self, token: Any) -> dict[str, str | None]:
        """Validate, store in memory, and persist token to DPAPI file."""
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise RequestError(400, "invalid_request_error", "Invalid token format.")
        expires_on = decode_jwt_exp(token)
        async with self._lock:
            if self._token_file is not None:
                await asyncio.to_thread(save_protected_text, self._token_file, token)
            self._token = token
            self._expires_on = expires_on
        return {"expiresOn": _format_iso(expires_on)}

    def get_token(self) -> str:
        """Return active upstream token or raise RequestError(401) if missing/expired."""
        if not self._token:
            raise RequestError(
                401,
                "authentication_error",
                "No Copilot upstream token captured yet. "
                "请在 Copilot App 的 Code 页发送一次消息，让中继获取当前模型 API 的访问令牌。",
            )
        if self._expires_on is not None and self._expires_on <= datetime.now(UTC):
            raise RequestError(
                401,
                "authentication_error",
                "Captured Copilot token has expired. "
                "请在 Copilot App 的 Code 页发送一次消息，让中继获取当前模型 API 的访问令牌。",
            )
        return self._token

    def status(self) -> dict[str, Any]:
        """Return non-sensitive token availability and expiry status."""
        now = datetime.now(UTC)
        has_token = bool(self._token)
        expired = bool(self._expires_on is not None and self._expires_on <= now)
        expires_in_seconds: int | None = None
        if self._expires_on is not None:
            expires_in_seconds = max(0, int((self._expires_on - now).total_seconds()))
        return {
            "hasToken": has_token and not expired,
            "tokenExpiresOn": _format_iso(self._expires_on),
            "expiresInSeconds": expires_in_seconds,
        }
