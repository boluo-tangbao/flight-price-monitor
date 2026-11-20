import logging
import os
import re
import tempfile
import unittest
from unittest.mock import patch

from core.cloud_session import SECRET_NAME, encode_state
from crawlers.ctrip import CtripCrawler
from crawlers.ctrip_browser_identity import desktop_profile, initialization_script


def synthetic_state():
    return {"cookies": [{"name": "cticket", "value": "synthetic-only",
                         "domain": ".ctrip.com", "path": "/", "expires": -1,
                         "httpOnly": True, "secure": True, "sameSite": "Lax"}],
            "origins": []}


class BrowserIdentityTests(unittest.TestCase):
    def test_profile_and_scripts_are_stable_for_same_runtime(self):
        profile = desktop_profile("159.0.8422.0", {"width": 1366, "height": 900})
        again = desktop_profile("159.0.8422.0", {"width": 1366, "height": 900})
        self.assertEqual(profile, again)
        self.assertEqual(initialization_script(profile), initialization_script(again))
        self.assertNotRegex(initialization_script(profile), r"__[A-Z][A-Z0-9_]*__")
        self.assertIn("Chrome/159.0.0.0", profile["ua"])
        self.assertNotIn("synthetic-only", initialization_script(profile))

    def test_version_is_runtime_aligned_and_missing_version_fails_before_navigation(self):
        for major in (145, 159, 160):
            profile = desktop_profile(f"{major}.0.1.2", {"width": 1366, "height": 900})
            self.assertIn(f"Chrome/{major}.0.0.0", profile["ua"])
            self.assertIn(f'"version": "{major}"', initialization_script(profile))
        for value in (None, "", "unknown"):
            with self.assertRaises(ValueError):
                desktop_profile(value, {})

    def test_cloud_installs_upstream_before_first_page_and_retains_secret_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            crawler = CtripCrawler({"user_data_dir": root}, logging.getLogger("identity-test"))
            crawler.use_mobile = False
            with patch.dict(os.environ, {SECRET_NAME: encode_state(synthetic_state())}), \
                    patch("crawlers.base.sync_playwright") as pw:
                chromium = pw.return_value.__enter__.return_value.chromium
                chromium.launch.return_value.version = "159.0.8422.0"
                with crawler.browser() as ctx:
                    self.assertNotIn(SECRET_NAME, os.environ)
                    chromium.launch_persistent_context.assert_not_called()
                    self.assertIn("--disable-blink-features=AutomationControlled",
                                  chromium.launch.call_args.kwargs["args"])
                    options = chromium.launch.return_value.new_context.call_args.kwargs
                    self.assertEqual(options["storage_state"], synthetic_state())
                    self.assertIn("Chrome/159.0.0.0", options["user_agent"])
                    self.assertFalse(options["is_mobile"])
                    ctx.add_init_script.assert_called_once()
                    ctx.new_page.assert_not_called()
                    self.assertEqual(crawler.cloud_browser_diagnostics,
                                     {"mode": "upstream", "initialization_installed": True,
                                      "kernel_major": 159})
                ctx.close.assert_called_once()
                chromium.launch.return_value.close.assert_called_once()

    def test_explicit_native_cloud_mode_is_a_reversible_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            crawler = CtripCrawler({"user_data_dir": root, "cloud_pc_browser_mode": "native"},
                                   logging.getLogger("identity-test"))
            crawler.use_mobile = False
            with patch.dict(os.environ, {SECRET_NAME: encode_state(synthetic_state())}), \
                    patch("crawlers.base.sync_playwright") as pw:
                chromium = pw.return_value.__enter__.return_value.chromium
                with crawler.browser() as ctx:
                    self.assertNotIn("args", chromium.launch.call_args.kwargs)
                    self.assertNotIn("user_agent", chromium.launch.return_value.new_context.call_args.kwargs)
                    ctx.add_init_script.assert_not_called()

    def test_init_failure_closes_browser_without_query_or_secret_in_error(self):
        with tempfile.TemporaryDirectory() as root:
            crawler = CtripCrawler({"user_data_dir": root}, logging.getLogger("identity-test"))
            crawler.use_mobile = False
            with patch.dict(os.environ, {SECRET_NAME: encode_state(synthetic_state())}), \
                    patch("crawlers.base.sync_playwright") as pw:
                chromium = pw.return_value.__enter__.return_value.chromium
                chromium.launch.return_value.version = "159.0.8422.0"
                chromium.launch.return_value.new_context.return_value.add_init_script.side_effect = RuntimeError("init failed")
                with self.assertRaisesRegex(RuntimeError, "init failed"):
                    with crawler.browser():
                        self.fail("Must not yield an uninitialized browser")
                chromium.launch.return_value.new_context.return_value.new_page.assert_not_called()
                chromium.launch.return_value.close.assert_called_once()
                self.assertNotIn(SECRET_NAME, os.environ)


@unittest.skipUnless(os.environ.get("RUN_OFFLINE_BROWSER_CHECK") == "1", "explicit local browser check only")
class OfflineBrowserTests(unittest.TestCase):
    def test_upstream_script_runs_on_real_browser_without_external_access(self):
        from playwright.sync_api import sync_playwright
        from crawlers.ctrip_stealth_ext import launch_args
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True,
                proxy={"server": "http://127.0.0.1:9"},
                args=launch_args({"stealth": True}) + [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-background-networking", "--disable-component-update",
                    "--disable-sync", "--no-first-run"])
            try:
                profile = desktop_profile(browser.version, {"width": 1366, "height": 900})
                observed = []
                for _ in range(2):
                    ctx = browser.new_context(user_agent=profile["ua"], viewport=profile["viewport"],
                                              locale="zh-CN", timezone_id="Asia/Shanghai")
                    try:
                        ctx.route("**/*", lambda route: route.abort())
                        ctx.add_init_script(initialization_script(profile))
                        page = ctx.new_page()
                        errors, requests = [], []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.on("request", lambda request: requests.append(request.url))
                        page.goto("data:text/html,<title>offline</title><p>local check</p>", timeout=35000)
                        properties = page.evaluate("""() => ({
                            automated: navigator.webdriver === true,
                            ua: navigator.userAgent, mobile: navigator.userAgentData.mobile,
                            platform: navigator.platform, touch: navigator.maxTouchPoints,
                            brands: navigator.userAgentData.brands,
                            width: screen.width, height: screen.height,
                            language: navigator.language,
                            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone
                        })""")
                        self.assertEqual(errors, [])
                        self.assertFalse(properties["automated"])
                        self.assertFalse(properties["mobile"])
                        self.assertEqual(properties["touch"], 0)
                        self.assertEqual(properties["platform"], "Win32")
                        self.assertEqual((properties["width"], properties["height"]), (1366, 900))
                        self.assertEqual(properties["timezone"], "Asia/Shanghai")
                        major = re.search(r"Chrome/(\d+)", properties["ua"]).group(1)
                        self.assertEqual(properties["brands"][0]["version"], major)
                        self.assertFalse(any(url.startswith(("https://", "http://")) for url in requests))
                        observed.append(properties)
                    finally:
                        ctx.close()
                self.assertEqual(observed[0], observed[1])
            finally:
                browser.close()
