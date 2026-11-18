"""mitmproxy addon for capturing Microsoft Copilot App bearer tokens."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from mitmproxy import http

TARGET_HOST = "loki.delve.office.com"
TARGET_PATH_PREFIX = "/api/v1/llmapi/"


def _log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def _jwt_times(token: str) -> dict[str, int]:
    """Return only the numeric `iat`/`nbf`/`exp` claims; other claims are never read out."""
    parts = token.split(".")
    if len(parts) != 3:
        return {}
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: int(payload[key])
        for key in ("iat", "nbf", "exp")
        if isinstance(payload.get(key), (int, float)) and not isinstance(payload[key], bool)
    }


class TokenRelayAddon:
    def __init__(self) -> None:
        self.gateway_url = os.environ.get("RELAY_INGEST_URL", "")
        self.ingest_secret = os.environ.get("RELAY_INGEST_SECRET", "")
        # Optional JSONL of request metadata on TARGET_HOST, for studying token renewal.
        self.observe_log = os.environ.get("RELAY_OBSERVE_LOG", "")
        self.last_token = ""
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def running(self) -> None:
        if not (self.gateway_url and self.ingest_secret):
            _log("Set RELAY_INGEST_URL and RELAY_INGEST_SECRET to enable relay.")
        else:
            _log(f"Listening for {TARGET_HOST}{TARGET_PATH_PREFIX} tokens.")
        if self.observe_log:
            _log("Observation log enabled (metadata only).")

    def request(self, flow: http.HTTPFlow) -> None:
        if not self.gateway_url or not self.ingest_secret:
            return
        if flow.request.host != TARGET_HOST or not flow.request.path.startswith(TARGET_PATH_PREFIX):
            return
        auth = flow.request.headers.get("authorization", "")
        if not auth.lower().startswith("bearer "):
            return
        token = auth[7:].strip()
        if not token or token == self.last_token:
            return
        if self._push_token(token):
            self.last_token = token

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if flow.response is not None:
            flow.response.stream = True
            if self.observe_log and flow.request.host == TARGET_HOST:
                self._observe(flow)

    def _observe(self, flow: http.HTTPFlow) -> None:
        auth = flow.request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        record: dict[str, object] = {
            "time": int(time.time()),
            "method": flow.request.method,
            "path": flow.request.path.split("?", 1)[0],
            "status": flow.response.status_code if flow.response is not None else None,
            "contentType": flow.response.headers.get("content-type", "") if flow.response else "",
        }
        if token:
            # A short digest distinguishes tokens without making the log a credential store.
            record["tokenId"] = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
            record.update(_jwt_times(token))
        try:
            with open(self.observe_log, "a", encoding="utf-8") as log:
                log.write(json.dumps(record) + "\n")
        except OSError as exc:
            _log(f"Observation log write failed: {type(exc).__name__}")

    def _push_token(self, token: str) -> bool:
        body = json.dumps({"token": token}).encode("utf-8")
        req = urllib.request.Request(
            self.gateway_url,
            data=body,
            method="POST",
            headers={
                "content-type": "application/json",
                "x-relay-secret": self.ingest_secret,
            },
        )
        try:
            with self.opener.open(req, timeout=3) as resp:
                # The gateway logs the update with its expiry.
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError) as exc:
            _log(f"Failed to push token to gateway: {type(exc).__name__}")
        return False


addons = [TokenRelayAddon()]
