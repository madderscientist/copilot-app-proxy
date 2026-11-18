"""Tests for optional Entra MSAL authentication helpers."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from copilot_app_proxy.auth import (
    RESOURCE_ID,
    EntraSession,
    config_from_environment,
    sign_in_error_summary,
)

SYNTHETIC_TOKEN = "synthetic-test-token-not-a-real-credential"


def test_entra_login_uses_independent_client_and_keeps_tokens_out_of_status():
    config = config_from_environment(
        {
            "ENTRA_CLIENT_ID": "11111111-1111-4111-8111-111111111111",
            "ENTRA_TENANT_ID": "22222222-2222-4222-8222-222222222222",
        }
    )
    account = {"home_account_id": "synthetic-account"}
    captured_interactive: dict = {}
    captured_silent: dict = {}

    class FakeApp:
        def acquire_token_interactive(self, **kwargs):
            captured_interactive.update(kwargs)
            return {
                "account": account,
                "access_token": SYNTHETIC_TOKEN,
                "expires_on": datetime(2030, 1, 1, 0, 0, 0, tzinfo=UTC),
            }

        def acquire_token_silent(self, **kwargs):
            captured_silent.update(kwargs)
            return {
                "account": account,
                "access_token": f"{SYNTHETIC_TOKEN}-refreshed",
                "expires_on": datetime(2030, 1, 1, 1, 0, 0, tzinfo=UTC),
            }

    session = EntraSession(config, FakeApp())
    with pytest.raises(RuntimeError, match="sign-in is required"):
        session.get_access_token()

    def open_browser(url: str) -> None:
        pass

    status = session.sign_in(open_browser)
    assert status["signedIn"] is True
    assert SYNTHETIC_TOKEN not in str(status)
    assert captured_interactive["scopes"] == [f"{RESOURCE_ID}/.default"]
    assert captured_interactive["open_browser"] is open_browser

    assert session.get_access_token() == f"{SYNTHETIC_TOKEN}-refreshed"
    assert captured_silent["account"] == account
    assert captured_silent["scopes"] == captured_interactive["scopes"]
    assert session.status()["expiresOn"] == "2030-01-01T01:00:00Z"
    assert "synthetic-account" not in str(session.status())


def test_invalid_entra_config_fails_closed_and_errors_do_not_leak_credentials():
    with pytest.raises(ValueError, match="own registered public-client"):
        config_from_environment({})

    summary = sign_in_error_summary(
        {
            "error": "consent_required",
            "error_description": f"AADSTS65001: token={SYNTHETIC_TOKEN}; account=private@example.test",
        }
    )
    assert summary == "consent_required (AADSTS65001)"
    assert SYNTHETIC_TOKEN not in summary
    assert "private@example.test" not in summary


def test_real_msal_initializes_with_empty_cache_without_claiming_signin():
    session = EntraSession(
        {
            "client_id": "11111111-1111-4111-8111-111111111111",
            "tenant_id": "22222222-2222-4222-8222-222222222222",
        }
    )
    assert session.status()["signedIn"] is False
    assert session.status()["expiresOn"] is None
