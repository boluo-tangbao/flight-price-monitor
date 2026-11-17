import base64
import json
import logging
import os
import unittest
from unittest.mock import patch
from core.cloud_session import encode_state, decode_state, consume_cloud_state, minimal_state, SECRET_NAME
from core.collection import FetchFailure
from crawlers.ctrip import CtripCrawler


def sample():
    return {"cookies": [{"domain": ".ctrip.com", "name": "test", "value": "synthetic-secret"},
                        {"domain": ".unrelated.test", "name": "other", "value": "discard"}],
            "origins": [{"origin": "https://flights.ctrip.com", "localStorage": []},
                        {"origin": "https://ctrip.com.evil.test", "localStorage": []}]}


class CloudSessionTests(unittest.TestCase):
    def test_only_ctrip_state_exported(self):
        filtered = minimal_state(sample())
        self.assertEqual(len(filtered["cookies"]), 1)
        self.assertEqual(len(filtered["origins"]), 1)

    def test_package_roundtrip_and_expiry(self):
        value = encode_state(sample(), now=1000)
        self.assertEqual(decode_state(value, now=1001), minimal_state(sample()))
        with self.assertRaises(FetchFailure): decode_state(value, now=1000 + 7 * 86400)

    def test_invalid_value_never_echoed_in_error(self):
        with self.assertRaises(FetchFailure) as error: decode_state("sensitive-invalid-value")
        self.assertNotIn("sensitive-invalid-value", str(error.exception))

    def test_missing_ctrip_cookie_refused(self):
        with self.assertRaises(ValueError): encode_state({"cookies": [], "origins": []})

    def test_too_large_state_refused(self):
        data = sample()
        data["cookies"][0]["value"] = "x" * 50000
        with self.assertRaises(ValueError): encode_state(data)

    def test_injected_unrelated_origin_refused(self):
        package = {"version": 1, "issued_at": 1000, "expires_at": 1100, "state": sample()}
        value = base64.b64encode(json.dumps(package).encode()).decode()
        with self.assertRaises(FetchFailure): decode_state(value, now=1001)

    def test_secret_removed_from_environment_after_read(self):
        with patch.dict(os.environ, {SECRET_NAME: encode_state(sample())}):
            self.assertIsNotNone(consume_cloud_state())
            self.assertNotIn(SECRET_NAME, os.environ)

    def test_cloud_uses_ephemeral_browser_and_closes_it(self):
        crawler = CtripCrawler({}, logging.getLogger("session-test"))
        crawler.use_mobile = False
        with patch.dict(os.environ, {SECRET_NAME: encode_state(sample())}), patch("crawlers.base.sync_playwright") as pw:
            browser_type = pw.return_value.__enter__.return_value.chromium
            with crawler.browser():
                browser_type.launch_persistent_context.assert_not_called()
                self.assertNotIn(SECRET_NAME, os.environ)
                self.assertEqual(browser_type.launch.return_value.new_context.call_args.kwargs["storage_state"], minimal_state(sample()))
            browser_type.launch.return_value.close.assert_called_once()
