import io
import os
import unittest
import urllib.error
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from mitmproxy import http

from relay_addon import TokenRelayAddon


class TokenRelayTests(unittest.TestCase):
    def test_failed_push_retries_and_success_is_deduplicated(self):
        token = "synthetic-test-token-not-a-real-credential"
        with patch.dict(os.environ, {"RELAY_INGEST_SECRET": "synthetic-secret"}):
            addon = TokenRelayAddon()
        response = MagicMock()
        response.__enter__.return_value.status = 200
        addon.opener = Mock()
        addon.opener.open.side_effect = [urllib.error.URLError("temporary outage"), response]
        flow = SimpleNamespace(request=http.Request.make(
            "POST", "https://loki.delve.office.com/api/v1/llmapi/v1/messages",
            headers={"authorization": f"Bearer {token}"},
        ))
        with redirect_stdout(io.StringIO()) as output:
            addon.request(flow)
            self.assertEqual(addon.last_token, "")
            addon.request(flow)
            addon.request(flow)
        self.assertEqual(addon.opener.open.call_count, 2)
        self.assertEqual(addon.last_token, token)
        self.assertNotIn(token, output.getvalue())


if __name__ == "__main__":
    unittest.main()