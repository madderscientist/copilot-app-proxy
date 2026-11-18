"""Optional interactive Entra sign-in using official MSAL."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import msal

RESOURCE_ID = "42c8f7bd-c7a8-4ba9-bc1c-91edfb716247"
GUID_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.IGNORECASE)
_AADSTS_RE = re.compile(r"AADSTS[0-9]+")
_SAFE_CODE_RE = re.compile(r"^[a-z0-9_]{1,80}$", re.IGNORECASE)


def config_from_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read and validate ENTRA_CLIENT_ID and ENTRA_TENANT_ID from environment."""
    env = os.environ if environ is None else environ
    client_id = env.get("ENTRA_CLIENT_ID", "")
    tenant_id = env.get("ENTRA_TENANT_ID", "")
    if not GUID_RE.fullmatch(client_id) or not GUID_RE.fullmatch(tenant_id):
        raise ValueError(
            "Set ENTRA_CLIENT_ID and ENTRA_TENANT_ID to your own registered public-client IDs."
        )
    return {"client_id": client_id, "tenant_id": tenant_id}


def _parse_expires_on(result: Mapping[str, Any]) -> str | None:
    expires_on = result.get("expires_on")
    if isinstance(expires_on, datetime):
        dt = expires_on if expires_on.tzinfo else expires_on.replace(tzinfo=UTC)
        return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
    expires_in = result.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        dt = datetime.now(UTC) + timedelta(seconds=float(expires_in))
        return dt.isoformat().replace("+00:00", "Z")
    return None


class EntraSession:
    """In-memory MSAL public client session for optional --login mode."""

    def __init__(
        self,
        config: Mapping[str, str],
        application: Any | None = None,
    ) -> None:
        client_id = config.get("client_id", "")
        tenant_id = config.get("tenant_id", "")
        if not GUID_RE.fullmatch(client_id) or not GUID_RE.fullmatch(tenant_id):
            raise ValueError("Invalid public-client registration.")
        self._client_id = client_id
        self._tenant_id = tenant_id
        self._scopes = [f"{RESOURCE_ID}/.default"]
        self._account: dict[str, Any] | None = None
        self._expires_on: str | None = None
        self._application = application

    def _get_application(self) -> Any:
        if self._application is None:
            self._application = msal.PublicClientApplication(
                client_id=self._client_id,
                authority=f"https://login.microsoftonline.com/{self._tenant_id}",
            )
        return self._application

    def sign_in(self, open_browser: Callable[..., Any] | None = None) -> dict[str, Any]:
        """Run interactive browser sign-in and cache the resulting account in memory."""
        app = self._get_application()
        kwargs: dict[str, Any] = {
            "scopes": self._scopes,
            "success_template": "<!doctype html><title>Signed in</title><p>You can close this window.</p>",
            "error_template": "<!doctype html><title>Sign-in failed</title><p>Return to the gateway terminal.</p>",
        }
        if open_browser is not None:
            kwargs["open_browser"] = open_browser
        result = app.acquire_token_interactive(**kwargs)
        if not isinstance(result, Mapping) or not result.get("access_token"):
            raise RuntimeError(sign_in_error_summary(result))
        account = result.get("account")
        if not account and hasattr(app, "get_accounts"):
            accounts = app.get_accounts()
            account = accounts[0] if accounts else None
        if not account:
            raise RuntimeError("Sign-in did not return an account and access token.")
        self._account = account
        self._expires_on = _parse_expires_on(result)
        return self.status()

    def get_access_token(self) -> str:
        """Silently acquire or refresh an access token for the signed-in account."""
        if not self._account:
            raise RuntimeError("Interactive sign-in is required.")
        result = self._get_application().acquire_token_silent(
            scopes=self._scopes,
            account=self._account,
        )
        if not isinstance(result, Mapping) or not result.get("access_token"):
            raise RuntimeError("No upstream access token is available.")
        self._expires_on = _parse_expires_on(result)
        return str(result["access_token"])

    def status(self) -> dict[str, Any]:
        """Return non-sensitive sign-in status."""
        return {
            "signedIn": bool(self._account),
            "expiresOn": self._expires_on,
            "resource": RESOURCE_ID,
        }


def sign_in_error_summary(error: Any) -> str:
    """Return a sanitized MSAL error code / AADSTS summary without leaking tokens or emails."""
    if isinstance(error, Mapping):
        raw_code = error.get("error")
        raw_message = error.get("error_description") or error.get("error_message") or ""
    else:
        raw_code = getattr(error, "error_code", None) or getattr(error, "errorCode", None)
        raw_message = (
            getattr(error, "error_message", None)
            or getattr(error, "errorMessage", None)
            or str(error or "")
        )
    aadsts_match = _AADSTS_RE.search(str(raw_message))
    code = (
        raw_code
        if isinstance(raw_code, str) and _SAFE_CODE_RE.fullmatch(raw_code)
        else "sign_in_failed"
    )
    return f"{code} ({aadsts_match.group(0)})" if aadsts_match else code
