import logging
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

from crawlers.ctrip import CtripCrawler
from core.collection import FetchFailure
import main


class BrowserCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.crawler = CtripCrawler({"user_data_dir": self.tmp.name,
                                     "pc_browser_channel": "msedge"}, logging.getLogger("compat-test"))

    def test_native_pc_uses_browser_defaults(self):
        self.crawler.use_mobile = False
        with patch("crawlers.base.sync_playwright") as playwright:
            browser = playwright.return_value.__enter__.return_value.chromium
            with self.crawler.browser(headless=False):
                options = browser.launch_persistent_context.call_args.kwargs
                self.assertEqual(options["channel"], "msedge")
                for key in ("user_agent", "args", "is_mobile", "has_touch"):
                    self.assertNotIn(key, options)
                browser.launch_persistent_context.return_value.add_init_script.assert_not_called()
            browser.launch_persistent_context.return_value.close.assert_called_once()

    def test_mobile_configuration_unchanged(self):
        with patch("crawlers.base.sync_playwright") as playwright:
            browser = playwright.return_value.__enter__.return_value.chromium
            with self.crawler.browser():
                options = browser.launch_persistent_context.call_args.kwargs
                self.assertNotIn("channel", options)
                self.assertTrue(options["is_mobile"])
                self.assertIn("iPhone", options["user_agent"])

    def test_browser_profiles_are_separate_and_reusable(self):
        mobile = self.crawler.user_data_dir
        self.crawler.use_mobile = False
        edge = self.crawler.user_data_dir
        self.assertTrue(edge.endswith("ctrip-msedge"))
        self.assertNotEqual(edge, mobile)
        self.assertEqual(self.crawler.user_data_dir, edge)
        self.crawler.config["pc_browser_channel"] = "chromium"
        self.assertEqual(self.crawler.user_data_dir, mobile)

    def test_whaleguard_block_raises_explicit_outcome(self):
        page = Mock(url="https://flights.ctrip.com/")
        page.inner_text.return_value = "WhaleGuard Block"
        with self.assertRaises(FetchFailure) as error:
            self.crawler._check_pc_block(page)
        self.assertEqual(error.exception.status, "blocked")

    def test_whaleguard_redirect_detected_even_with_blank_body(self):
        page = Mock(url="https://example.test/whaleguard")
        page.inner_text.return_value = ""
        with self.assertRaises(FetchFailure):
            self.crawler._check_pc_block(page)

    def test_invalid_browser_channel_fails_before_launch(self):
        self.crawler.use_mobile = False
        self.crawler.config["pc_browser_channel"] = "invalid"
        with patch("crawlers.base.sync_playwright") as playwright:
            with self.assertRaises(ValueError):
                with self.crawler.browser():
                    pass
            playwright.assert_not_called()

    def test_cli_channel_reaches_login_and_block_exits_without_database(self):
        crawler, factory = Mock(), Mock()
        factory.return_value = crawler
        crawler.interactive_login_pc.side_effect = FetchFailure("blocked", "whaleguard")
        with patch("sys.argv", ["main.py", "-c", "config.roundtrip.example.yaml", "--login",
                                "ctrip-pc", "--browser-channel", "msedge"]), \
                patch("main.load_config", return_value={}), patch("main.setup_logger"), \
                patch.dict("main.REGISTRY", {"ctrip": factory}), patch("main.PriceStorage") as storage, \
                patch("sys.stderr"), self.assertRaises(SystemExit) as error:
            main.main()
        self.assertEqual(error.exception.code, 3)
        self.assertEqual(factory.call_args.args[0]["pc_browser_channel"], "msedge")
        storage.assert_not_called()
