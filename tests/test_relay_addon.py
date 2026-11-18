"""Tests for token relay mitmproxy addon."""

from __future__ import annotations

import base64
import json
import urllib.error
from unittest.mock import MagicMock, Mock

from mitmproxy import connection, http

from copilot_app_proxy.relay_addon import TokenRelayAddon


def test_failed_push_retries_and_success_is_deduplicated(monkeypatch, capsys):
    token = "synthetic-test-token-not-a-real-credential"
    monkeypatch.setenv("RELAY_INGEST_SECRET", "synthetic-secret")
    monkeypatch.setenv("RELAY_INGEST_URL", "http://127.0.0.1:20424/_internal/token")
    addon = TokenRelayAddon()
    response = MagicMock()
    response.__enter__.return_value.status = 200
    opener = addon.opener = Mock()
    opener.open.side_effect = [urllib.error.URLError("temporary outage"), response]
    flow = http.HTTPFlow(
        connection.Client(peername=("127.0.0.1", 1), sockname=("127.0.0.1", 2)),
        connection.Server(address=None),
    )
    flow.request = http.Request.make(
        "POST",
        "https://loki.delve.office.com/api/v1/llmapi/v1/messages",
        headers={"authorization": f"Bearer {token}"},
    )
    addon.request(flow)
    assert addon.last_token == ""
    addon.request(flow)
    addon.request(flow)
    assert opener.open.call_count == 2
    assert opener.open.call_args.args[0].full_url == addon.gateway_url
    assert addon.last_token == token
    assert token not in capsys.readouterr().out

    monkeypatch.delenv("RELAY_INGEST_URL")
    unconfigured = TokenRelayAddon()
    unconfigured.opener = Mock()
    unconfigured.running()
    unconfigured.request(flow)
    unconfigured.opener.open.assert_not_called()
    assert "RELAY_INGEST_URL" in capsys.readouterr().out


def test_observe_log_records_metadata_without_token(monkeypatch, tmp_path):
    payload = base64.urlsafe_b64encode(
        json.dumps({"iat": 100, "exp": 200, "upn": "user@example.com"}).encode()
    ).rstrip(b"=").decode()
    token = f"header.{payload}.signature"
    log = tmp_path / "observe.jsonl"
    monkeypatch.setenv("RELAY_OBSERVE_LOG", str(log))
    addon = TokenRelayAddon()
    flow = http.HTTPFlow(
        connection.Client(peername=("127.0.0.1", 1), sockname=("127.0.0.1", 2)),
        connection.Server(address=None),
    )
    flow.request = http.Request.make(
        "GET",
        "https://loki.delve.office.com/api/v1/llmapi/models?secret=1",
        headers={"authorization": f"Bearer {token}"},
    )
    flow.response = http.Response.make(200, b"", {"content-type": "application/json"})
    addon.responseheaders(flow)
    flow.request.host = "example.com"
    addon.responseheaders(flow)

    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["path"] == "/api/v1/llmapi/models"
    assert (record["status"], record["iat"], record["exp"]) == (200, 100, 200)
    assert len(record["tokenId"]) == 12
    assert token not in lines[0] and "user@example.com" not in lines[0]
