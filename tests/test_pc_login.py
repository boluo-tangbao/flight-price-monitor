from contextlib import contextmanager
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from crawlers.ctrip import CtripCrawler
from core.collection import FetchFailure
import main


class PcLoginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.crawler = CtripCrawler({"user_data_dir": self.tmp.name, "timeout_seconds": 35},
                                    logging.getLogger("pc-login-test"))
        self.page = Mock(url="https://flights.ctrip.com/")
        self.page.is_closed.return_value = False
        self.page.inner_text.return_value = "携程机票"
        self.pages = [self.page]
        self.crawler.new_page = Mock(return_value=self.page)

    def browser(self, *, headless):
        @contextmanager
        def context():
            self.assertFalse(headless)
            self.assertFalse(self.crawler.use_mobile)
            self.assertEqual(Path(self.crawler.user_data_dir), Path(self.tmp.name) / "ctrip")
            self.assertEqual(self.crawler._viewport(), {"width": 1366, "height": 900})
            yield Mock(pages=self.pages)
        return context()

    def test_desktop_login_reuses_profile_and_restores_mobile(self):
        with patch.object(self.crawler, "browser", self.browser), patch("builtins.input", return_value=""), patch("builtins.print"):
            self.crawler.interactive_login_pc()
        self.assertTrue(self.crawler.use_mobile)
        self.page.goto.assert_not_called()
        self.page.wait_for_timeout.assert_called_once_with(100)

    def test_manual_navigation_events_are_processed_before_url_check(self):
        self.page.url = "about:blank"
        self.page.wait_for_timeout.side_effect = lambda _: setattr(self.page, "url", "https://flights.ctrip.com/")
        with patch.object(self.crawler, "browser", self.browser), patch("builtins.input", return_value=""), patch("builtins.print"):
            self.crawler.interactive_login_pc()
        self.assertTrue(self.crawler.use_mobile)

    def test_new_tab_events_are_processed_before_page_list_check(self):
        self.page.url = "about:blank"
        tab = Mock(url="https://www.ctrip.com/")
        tab.is_closed.return_value = False
        tab.inner_text.return_value = "携程"
        self.page.wait_for_timeout.side_effect = lambda _: self.pages.append(tab)
        with patch.object(self.crawler, "browser", self.browser), patch("builtins.input", return_value=""), patch("builtins.print"):
            self.crawler.interactive_login_pc()
        tab.inner_text.assert_called_once()

    def test_bare_ctrip_domain_is_accepted(self):
        self.page.url = "https://ctrip.com/"
        with patch.object(self.crawler, "browser", self.browser), patch("builtins.input", return_value=""), patch("builtins.print"):
            self.crawler.interactive_login_pc()

    def test_lookalike_domain_is_not_accepted(self):
        self.page.url = "https://notctrip.com/?token=do-not-print"
        with patch.object(self.crawler, "browser", self.browser), patch("builtins.input", return_value=""), \
                patch("builtins.print"), self.assertRaises(FetchFailure) as error:
            self.crawler.interactive_login_pc()
        self.assertNotIn("do-not-print", str(error.exception))
        self.assertIn("notctrip.com", str(error.exception))

    def test_closed_original_tab_does_not_hide_logged_in_new_tab(self):
        from playwright.sync_api import Error
        self.page.is_closed.return_value = True
        self.page.wait_for_timeout.side_effect = Error("Target closed")
        tab = Mock(url="https://flights.ctrip.com/")
        tab.is_closed.return_value = False
        tab.inner_text.return_value = "携程"
        self.pages.append(tab)
        with patch.object(self.crawler, "browser", self.browser), patch("builtins.input", return_value=""), patch("builtins.print"):
            self.crawler.interactive_login_pc()
        tab.inner_text.assert_called_once()

    def test_page_read_failure_restores_mode(self):
        self.page.inner_text.side_effect = RuntimeError("page unavailable")
        with patch.object(self.crawler, "browser", self.browser), patch("builtins.input", return_value=""), \
                patch("builtins.print"), self.assertRaises(RuntimeError):
            self.crawler.interactive_login_pc()
        self.assertTrue(self.crawler.use_mobile)

    def test_blank_page_is_not_reported_as_login_completed(self):
        self.page.url = "about:blank"
        with patch.object(self.crawler, "browser", self.browser), patch("builtins.input", return_value=""), \
                patch("builtins.print"), self.assertRaises(FetchFailure) as error:
            self.crawler.interactive_login_pc()
        self.assertEqual(error.exception.status, "login_incomplete")

    def test_manual_navigation_can_use_new_tab(self):
        self.page.url = "about:blank"
        tab = Mock(url="https://flights.ctrip.com/")
        tab.is_closed.return_value = False
        tab.inner_text.return_value = "携程机票"
        self.pages.append(tab)
        with patch.object(self.crawler, "browser", self.browser), patch("builtins.input", return_value=""), patch("builtins.print"):
            self.crawler.interactive_login_pc()
        tab.inner_text.assert_called_once()
        self.page.goto.assert_not_called()

    def test_blocked_manual_page_is_not_reported_as_login_completed(self):
        self.page.inner_text.return_value = "whaleguard block"
        with patch.object(self.crawler, "browser", self.browser), patch("builtins.input", return_value=""), \
                patch("builtins.print"), self.assertRaises(FetchFailure) as error:
            self.crawler.interactive_login_pc()
        self.assertEqual(error.exception.status, "blocked")

    def test_noninteractive_input_is_not_reported_as_completed(self):
        with patch.object(self.crawler, "browser", self.browser), patch("builtins.print"), \
                patch("builtins.input", side_effect=EOFError), self.assertRaises(RuntimeError):
            self.crawler.interactive_login_pc()
        self.assertTrue(self.crawler.use_mobile)

    def check_cli(self, platform):
        crawler = Mock()
        factory = Mock(return_value=crawler)
        with patch("sys.argv", ["main.py", "-c", "config.roundtrip.example.yaml", "--login", platform]), \
                patch("main.load_config", return_value={"crawler": {"user_data_dir": "custom-profile"}}), \
                patch("main.setup_logger"), patch("main.PriceStorage") as storage, \
                patch("main.build_notifier") as notifier, patch("main.make_job") as job, \
                patch.dict("main.REGISTRY", {"ctrip": factory}):
            main.main()
        storage.assert_not_called()
        notifier.assert_not_called()
        job.assert_not_called()
        self.assertEqual(factory.call_args.args[0]["user_data_dir"], "custom-profile")
        return crawler

    def test_pc_cli_does_not_start_database_or_monitoring(self):
        crawler = self.check_cli("ctrip-pc")
        crawler.interactive_login_pc.assert_called_once()
        crawler.interactive_login.assert_not_called()

    def test_existing_mobile_cli_remains_available(self):
        crawler = self.check_cli("ctrip")
        crawler.interactive_login.assert_called_once()
        crawler.interactive_login_pc.assert_not_called()


if __name__ == "__main__":
    unittest.main()
