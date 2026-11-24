"""Offline integration of pinned upstream browser, solver and RT branch."""
from contextlib import nullcontext, redirect_stdout
from io import StringIO
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from core.collection import FetchFailure, FetchResult
from crawlers.tongcheng_pc import TongchengDesktop
from crawlers.tongcheng_roundtrip import cloud_report_diagnostics, desktop, fetch_round_trip
from crawlers.tongcheng_upstream import browser, captcha, challenge
from scripts import verify_tongcheng_round_trip

TRIP = ("SHA", "OSA", "2027-02-15", "2027-02-22")


class Page:
    def __init__(self, responses=(), body="同程旅行", html=""):
        self.url = "about:blank"
        self.responses, self.body, self.html = responses, body, html
        self.events = {}
        self.reloads = 0

    def on(self, event, fn):
        self.events.setdefault(event, []).append(fn)

    def goto(self, url, **kwargs):
        self.url = url
        for fn in self.events.get("request", []):
            fn(SimpleNamespace(url=url, resource_type="document"))
        for response in self.responses:
            self.emit_response(response)
        return SimpleNamespace(status=200)

    def emit_response(self, response):
        for fn in self.events.get("response", []):
            fn(response)

    def reload(self):
        self.reloads += 1
        # Count reloads as search requests, without re-entering the goto guard.
        for fn in self.events.get("request", []):
            fn(SimpleNamespace(url=self.url, resource_type="document"))

    def inner_text(self, *a, **k): return self.body
    def content(self): return self.html
    def wait_for_timeout(self, *a): pass
    def set_default_timeout(self, *a): pass
    def set_default_navigation_timeout(self, *a): pass


def response(payload, status=200):
    r = Mock(url="https://www.ly.com/miflightapi/ts/list", status=status)
    r.json.return_value = payload
    r.text.return_value = json.dumps(payload)
    return r


def offer():
    return {"data": {"res": [{"lng": "NH976_20270215|NH973_20270222",
            "tp": 2450, "acs": [{"an": "NH976", "acn": "ANA"}]}], "done": 1}}


class VerificationScriptTests(unittest.TestCase):
    def test_default_command_enables_baggage_details_and_list_only_opts_out(self):
        route = SimpleNamespace(from_code=TRIP[0], to_code=TRIP[1],
                                dates=[TRIP[2]], return_dates=[TRIP[3]])
        for extra, mode, read_details in (([], "fare_details", True), (["--list-only"], "list_only", False)):
            with self.subTest(mode=mode):
                output = StringIO()
                argv = ["verify_tongcheng_round_trip.py", "--browser-channel", "msedge", *extra]
                with patch.object(sys, "argv", argv), redirect_stdout(output), \
                        patch.object(verify_tongcheng_round_trip, "TongchengCrawler") as crawler, \
                        patch.object(verify_tongcheng_round_trip, "load_config"), \
                        patch.object(verify_tongcheng_round_trip, "build_routes", return_value=[route]), \
                        patch.object(verify_tongcheng_round_trip, "collect", return_value=FetchResult("ok")) as collect:
                    crawler.return_value = SimpleNamespace(round_trip_searches=1)
                    self.assertEqual(verify_tongcheng_round_trip.main(), 0)
                settings = crawler.call_args.args[0]
                self.assertIs(settings["tongcheng_round_trip_fare_details"], read_details)
                self.assertIs(settings["tongcheng_keep_list_quotes"], read_details)
                self.assertFalse(settings["slider"]["auto"])
                collect.assert_called_once_with(crawler.return_value, *TRIP)
                self.assertEqual(json.loads(output.getvalue())["query_mode"], mode)


class UpstreamTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        challenge.unregister_handler()
        self.addCleanup(challenge.unregister_handler)
        self.crawler = SimpleNamespace(config={"headless": False, "timeout_seconds": 35,
            "tongcheng_pc_browser_channel": "msedge"}, user_data_root=self.root.name,
            logger=logging.getLogger("offline-tongcheng"), headless=False)

    def test_cloud_diagnostic_identifies_redirect_without_recording_private_path(self):
        self.crawler.cloud_storage_state = {"cookies": [], "origins": []}
        diagnostics = {}
        worker = TongchengDesktop(self.crawler, diagnostics)
        worker.expected_search_url = worker._intl_rt_url(*TRIP)
        page = Page()
        page.main_frame = object()
        ctx = Mock()
        ctx.new_page.return_value = page
        ctx.cookies.return_value = [{"name": "memberIdNew", "value": "hidden-cookie"}]
        with patch.object(browser, "sync_playwright") as pw, \
                patch.object(browser, "install_navigation_guard") as navigation_guard:
            engine = pw.return_value.__enter__.return_value.chromium
            engine.launch.return_value.new_context.return_value = ctx
            with worker.browser(save_session="login"):
                worker.new_page(ctx)
                req = SimpleNamespace(resource_type="document", frame=page.main_frame)
                page.emit_response(SimpleNamespace(url=worker.expected_search_url, request=req, status=302))
                page.url = "https://passport.ly.com/login/hidden-path?token=hidden-token"
                page.emit_response(SimpleNamespace(url=page.url, request=req, status=200))
            engine.launch.assert_called_once_with(headless=True, channel="chromium")
            engine.launch_persistent_context.assert_not_called()
            self.assertIs(engine.launch.return_value.new_context.call_args.kwargs["storage_state"],
                          self.crawler.cloud_storage_state)
            ctx.close.assert_called_once()
            engine.launch.return_value.close.assert_called_once()
            ctx.storage_state.assert_not_called()
            navigation_guard.assert_not_called()
        self.assertFalse((Path(self.root.name) / "tongcheng-pc").exists())
        diagnostics["detail_fare_inventory"] = [{"text": "hidden-inventory"}]
        report = cloud_report_diagnostics(diagnostics)
        self.assertTrue(report["auth_cookie_in_search_context"])
        self.assertEqual(report["browser_mode"], "upstream_lifecycle_chromium")
        self.assertEqual([row["http"] for row in report["document_trail"]], [302, 200])
        self.assertEqual(report["final_destination"], {"host": "passport.ly.com", "path": "login_related",
                         "search_parameter": "missing", "same_search_location": False})
        self.assertNotIn("hidden-", json.dumps(report))

    def test_actions_persistent_profile_restores_state_and_is_deleted(self):
        cookie = {"name": "memberIdNew", "domain": ".ly.com", "path": "/", "value": "test", "expires": -1}
        expired = dict(cookie, name="expired", expires=1)
        origins = [{"origin": "https://www.ly.com", "localStorage": [{"name": "sample", "value": "test"}]}]
        self.crawler.cloud_storage_state = {"cookies": [cookie, expired], "origins": origins}
        ctx = Mock()
        ctx.cookies.return_value = []

        with patch.dict(os.environ, {"TONGCHENG_PERSISTENT_PROFILE": "true"}), \
                patch.object(browser, "sync_playwright") as pw:
            worker = TongchengDesktop(self.crawler)
            worker.expected_search_url = worker._intl_rt_url(*TRIP)
            engine = pw.return_value.__enter__.return_value.chromium
            engine.launch_persistent_context.return_value = ctx
            with worker.browser(save_session="login"):
                options = engine.launch_persistent_context.call_args.kwargs
                profile = Path(options["user_data_dir"])
                self.assertTrue(profile.is_dir())
                self.assertNotEqual(profile, Path(self.root.name) / "tongcheng-pc")
                self.assertEqual(options["channel"], "chromium")
                self.assertEqual(ctx.method_calls[:3], [
                    call.set_storage_state({"cookies": [], "origins": origins}),
                    call.cookies(), call.add_cookies([cookie]),
                ])
            engine.launch.assert_not_called()
            ctx.close.assert_called_once()
            ctx.storage_state.assert_not_called()
            self.assertFalse(profile.exists())
            self.assertEqual(worker.diagnostics["browser_mode"], "upstream_persistent_chromium")

            ctx.reset_mock()
            ctx.set_storage_state.side_effect = ValueError("private browser diagnostics")
            with self.assertRaisesRegex(FetchFailure, "^tongcheng_cloud_profile_restore_failed$"):
                with worker.browser():
                    self.fail("Query must not start after failed restore")
            failed_profile = Path(engine.launch_persistent_context.call_args.kwargs["user_data_dir"])
            self.assertFalse(failed_profile.exists())
            ctx.close.assert_called_once()

    def run_page(self, page):
        ctx = Mock()
        ctx.new_page.return_value = page
        clock = iter(range(0, 1000))
        with patch.object(browser.BaseCrawler, "browser", return_value=nullcontext(ctx)), \
                patch("crawlers.tongcheng_upstream.roundtrip.time.monotonic", side_effect=lambda: next(clock)):
            return fetch_round_trip(self.crawler, *TRIP)

    def test_original_rt_branch_returns_strict_pair_and_counts_request(self):
        from crawlers.tongcheng_upstream.roundtrip import InternationalRoundTrip
        self.assertIs(TongchengDesktop._fetch_intl_rt, InternationalRoundTrip._fetch_intl_rt)
        reply = response(offer())
        rows = self.run_page(Page([reply]))
        self.assertEqual(rows[0].price, 2450)
        self.assertEqual(rows[0].return_flight_no, "NH973")
        self.assertEqual(self.crawler.round_trip_searches, 1)
        self.assertEqual(self.crawler.round_trip_diagnostics["offer_rows_seen"], 1)
        reply.json.assert_called_once_with()
        reply.text.assert_not_called()

    def test_detail_expansion_gets_later_rows_from_the_same_collector(self):
        self.crawler.config.update(tongcheng_round_trip_fare_details=True, tongcheng_keep_list_quotes=True)
        initial = response(offer())
        later = response({"data": {"res": [
            {"lng": "NH976_20270215|NH973_20270222", "tp": 2400},
            {"lng": "NH976_20270215|MM079_20270222", "tp": 2700},
        ], "done": 1}})
        page = Page([initial])

        def inspect(active_page, itinerary, fallback, access, diagnostics, *, preferred_pair, current_quotes):
            self.assertIs(active_page, page)
            self.assertEqual([(quote.return_flight_no, quote.price) for quote in fallback], [("NH973", 2450)])
            page.emit_response(later)
            self.assertEqual([(quote.return_flight_no, quote.price) for quote in current_quotes()],
                             [("NH973", 2400), ("MM079", 2700)])
            return []

        with patch("crawlers.tongcheng_fare_details.read_fare_inventory", side_effect=inspect) as read:
            rows = self.run_page(page)
        read.assert_called_once()
        self.assertEqual([quote.price for quote in rows], [2400, 2700])
        self.assertEqual(self.crawler.round_trip_diagnostics["matched_offer_rows"], 3)
        for reply in (initial, later):
            reply.json.assert_called_once_with()
            reply.text.assert_not_called()

    def test_simple_challenge_reaches_original_solver_and_reload(self):
        page = Page([response(offer())], "请完成拼图验证", '<div class="slide-verify"></div>')
        def solved(*args, **kwargs):
            page.body = "同程旅行"
            return {"ok": True}
        with patch.object(captcha, "attempt", side_effect=solved) as attempt:
            self.assertEqual(len(self.run_page(page)), 1)
        attempt.assert_called_once()
        self.assertEqual(page.reloads, 1)
        self.assertEqual(self.crawler.round_trip_searches, 2)

    def test_strong_challenge_stops_without_solver(self):
        with patch.object(captcha, "attempt") as attempt, self.assertRaises(FetchFailure) as err:
            self.run_page(Page(body="安全验证 whaleguard", html="whaleguard"))
        self.assertEqual(err.exception.status, "blocked")
        attempt.assert_not_called()
        self.assertEqual(self.crawler.round_trip_searches, 1)

    def test_plain_login_prompt_is_classified(self):
        with self.assertRaises(FetchFailure) as err:
            self.run_page(Page(body="请登录"))
        self.assertEqual(err.exception.status, "login_required")

    def test_business_444_is_not_empty(self):
        with self.assertRaises(FetchFailure) as err:
            self.run_page(Page([response({"code": 444, "data": None})]))
        self.assertEqual(str(err.exception), "tongcheng_list_request_rejected_444")
        self.assertEqual(self.crawler.round_trip_diagnostics["list_business_codes"], [444])

    def test_list_406_does_not_attempt_fare_details_or_classify_as_empty(self):
        self.crawler.config["tongcheng_round_trip_fare_details"] = True
        page = Page([response({}, status=406)], "同程旅行")
        with patch("crawlers.tongcheng_fare_details.read_fare_inventory") as read, \
                self.assertRaises(FetchFailure) as err:
            self.run_page(page)
        read.assert_not_called()
        self.assertEqual((err.exception.status, str(err.exception)), ("error", "tongcheng_list_http_406"))
        self.assertEqual(self.crawler.round_trip_searches, 1)

    def test_page_http_rejection_is_blocked(self):
        r = Mock(url="https://www.ly.com/iflight/book1.html", status=403)
        with self.assertRaises(FetchFailure) as err:
            self.run_page(Page([r]))
        self.assertEqual(err.exception.status, "blocked")
        self.assertEqual(self.crawler.round_trip_diagnostics["page_http_status"], 403)

    def test_no_response_and_invalid_pair_remain_distinct(self):
        for replies, reason in (([], "tongcheng_no_list_response"),
                ([response({"data": {"res": [{"lng": "NH976_20270215"}], "done": 1}})],
                 "tongcheng_offer_rows_failed_price_or_date_validation")):
            with self.subTest(reason=reason), self.assertRaises(FetchFailure) as err:
                self.run_page(Page(replies))
            self.assertEqual(str(err.exception), reason)

    def test_navigation_timeout_keeps_timeout_status(self):
        page = Page()
        page.goto = Mock(side_effect=TimeoutError("synthetic offline timeout"))
        with self.assertRaises(FetchFailure) as err:
            self.run_page(page)
        self.assertEqual(err.exception.status, "timeout")

    def test_login_and_query_use_same_upstream_browser_profile(self):
        worker = TongchengDesktop(self.crawler)
        self.assertEqual(Path(worker.user_data_dir).resolve(), (Path(self.root.name) / "tongcheng-pc").resolve())
        with patch.object(TongchengDesktop, "browser", return_value=nullcontext(Mock())) as launch:
            with desktop(self.crawler, headed=True, login=True): pass
        launch.assert_called_once_with(headless=False, save_session="login")

    def test_original_initialization_and_snapshot_restore(self):
        worker = TongchengDesktop(self.crawler)
        state = {"saved_at": 123, "cookies": [{"name": "memberIdNew", "domain": ".ly.com",
            "path": "/", "value": "synthetic-offline-only", "expires": -1}], "origins": []}
        Path(worker.user_data_dir).mkdir(exist_ok=True)
        Path(worker.session_state_path).write_text(json.dumps(state), encoding="utf-8")
        ctx = Mock()
        ctx.cookies.return_value = []
        ctx.storage_state.return_value = state
        with patch.object(browser, "sync_playwright") as pw, patch.object(browser, "kernel_major", return_value=151):
            engine = pw.return_value.__enter__.return_value.chromium
            engine.launch_persistent_context.return_value = ctx
            with worker.browser(headless=False): pass
        launch = engine.launch_persistent_context.call_args.kwargs
        self.assertEqual(launch["channel"], "msedge")
        self.assertFalse(launch["is_mobile"])
        self.assertIn("Chrome/151", launch["user_agent"])
        ctx.add_init_script.assert_called_once()
        self.assertNotIn("__MAXTOUCH__", ctx.add_init_script.call_args.args[0])
        ctx.add_cookies.assert_called_once()
        ctx.close.assert_called_once()

    def test_login_missing_auth_does_not_overwrite_previous_snapshot(self):
        worker = TongchengDesktop(self.crawler)
        Path(worker.user_data_dir).mkdir(exist_ok=True)
        old = '{"saved_at":123,"cookies":[],"origins":[]}'
        Path(worker.session_state_path).write_text(old, encoding="utf-8")
        ctx = Mock()
        ctx.cookies.return_value = []
        ctx.pages = []
        ctx.storage_state.return_value = {"cookies": [], "origins": []}
        with patch.object(browser, "sync_playwright") as pw, patch.object(browser, "kernel_major", return_value=151):
            pw.return_value.__enter__.return_value.chromium.launch_persistent_context.return_value = ctx
            with self.assertRaises(RuntimeError):
                with worker.browser(save_session="login"): pass
        self.assertEqual(Path(worker.session_state_path).read_text(encoding="utf-8"), old)
