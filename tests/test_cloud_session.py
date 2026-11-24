import base64
import json
import logging
import os
import unittest
from unittest.mock import patch
from core.cloud_session import (encode_state, decode_state, consume_cloud_state, minimal_state,
                               SECRET_NAME, SESSION_SECRETS, detach_cloud_states)
from core.collection import FetchFailure
from crawlers.ctrip import CtripCrawler


def sample():
    return {"cookies": [{"domain": ".ctrip.com", "name": "test", "value": "synthetic-secret"},
                        {"domain": ".unrelated.test", "name": "other", "value": "discard"}],
            "origins": [{"origin": "https://flights.ctrip.com", "localStorage": []},
                        {"origin": "https://ctrip.com.evil.test", "localStorage": []}]}


class CloudSessionTests(unittest.TestCase):
    def test_additional_platform_states_cannot_cross_platforms(self):
        cookies = [{"domain": ".ly.com", "name": "tc"},
                   {"domain": ".fliggy.com", "name": "fg"},
                   {"domain": ".taobao.com", "name": "fg-login"},
                   {"domain": "fliggy.com.unrelated.test", "name": "other"}]
        state = {"cookies": cookies, "origins": []}
        for platform, names in (("tongcheng", ["tc"]), ("fliggy", ["fg", "fg-login"])):
            encoded = encode_state(state, now=1000, platform=platform)
            restored = decode_state(encoded, now=1001, platform=platform)
            self.assertEqual([cookie["name"] for cookie in restored["cookies"]], names)
            with self.assertRaises(FetchFailure):
                decode_state(encoded, now=1001, platform="ctrip")

    def test_all_platform_secrets_are_removed_from_process_environment(self):
        with patch.dict(os.environ, {secret: platform for platform, secret in SESSION_SECRETS.items()}):
            self.assertEqual(detach_cloud_states(), {platform: platform for platform in SESSION_SECRETS})
            self.assertTrue(all(secret not in os.environ for secret in SESSION_SECRETS.values()))

    def test_only_ctrip_state_exported(self):
        filtered = minimal_state(sample())
        self.assertEqual(len(filtered["cookies"]), 1)
        self.assertEqual(len(filtered["origins"]), 1)

    def test_explicit_package_expiry_is_enforced(self):
        value = encode_state(sample(), now=1000, ttl_days=7)
        self.assertEqual(decode_state(value, now=1001), minimal_state(sample()))
        with self.assertRaises(FetchFailure): decode_state(value, now=1000 + 7 * 86400)

    def test_default_package_is_not_rejected_after_seven_days(self):
        value = encode_state(sample(), now=1000)
        self.assertEqual(decode_state(value, now=1000 + 90 * 86400), minimal_state(sample()))

    def test_previously_uploaded_v1_package_ignores_old_seven_day_cap(self):
        package = {"version": 1, "issued_at": 1000, "expires_at": 1000 + 7 * 86400,
                   "state": minimal_state(sample())}
        value = base64.b64encode(json.dumps(package).encode()).decode()
        self.assertEqual(decode_state(value, now=1000 + 30 * 86400), minimal_state(sample()))

    def test_platform_cookie_expiry_is_never_extended(self):
        state = sample()
        state["cookies"][0]["expires"] = 1100
        restored = decode_state(encode_state(state, now=1000), now=2000)
        self.assertEqual(restored["cookies"][0]["expires"], 1100)

    def test_invalid_optional_validity_refused(self):
        for days in (0, -1, True):
            with self.assertRaises(ValueError): encode_state(sample(), ttl_days=days)

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
            browser_type.launch.return_value.version = "159.0.8422.0"
            with crawler.browser():
                browser_type.launch_persistent_context.assert_not_called()
                self.assertNotIn(SECRET_NAME, os.environ)
                self.assertEqual(browser_type.launch.return_value.new_context.call_args.kwargs["storage_state"], minimal_state(sample()))
            browser_type.launch.return_value.close.assert_called_once()
