"""mitmproxy addon for capturing Microsoft Copilot App bearer tokens."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from mitmproxy import http

TARGET_HOST = "loki.delve.office.com"
TARGET_PATH_PREFIX = "/api/v1/llmapi/"


class TokenRelayAddon:
    def __init__(self) -> None:
        self.gateway_url = os.environ.get("RELAY_INGEST_URL", "")
        self.ingest_secret = os.environ.get("RELAY_INGEST_SECRET", "")
        self.last_token = ""
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def running(self) -> None:
        if not (self.gateway_url and self.ingest_secret):
            print("[relay] Set RELAY_INGEST_URL and RELAY_INGEST_SECRET to enable relay.", flush=True)
        else:
            print(f"[relay] Listening for {TARGET_HOST}{TARGET_PATH_PREFIX} tokens.", flush=True)

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
                if resp.status == 200:
                    print("[relay] Updated upstream token.", flush=True)
                    return True
        except (urllib.error.URLError, TimeoutError) as exc:
            print(f"[relay] Failed to push token to gateway: {type(exc).__name__}", flush=True)
        return False


addons = [TokenRelayAddon()]
