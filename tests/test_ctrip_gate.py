import unittest
import logging
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
from crawlers.ctrip_roundtrip import check_access, build_url, fetch_round_trip
from crawlers.ctrip import CtripCrawler
from core.collection import FetchFailure
from core.models import RoundTripQuote


class GateTests(unittest.TestCase):
    def test_login_code_form_is_login_required(self):
        with self.assertRaises(FetchFailure) as caught:
            check_access("手机号 验证码 登录", "https://passport.ctrip.com/login?token=sensitive", 200, "homepage")
        self.assertEqual(caught.exception.status, "login_required")
        self.assertNotIn("sensitive", str(caught.exception))

    def test_generic_code_word_is_not_waf_evidence(self):
        check_access("登录方式支持验证码", "https://flights.ctrip.com/", 200, "homepage")

    def test_security_help_text_is_not_a_challenge(self):
        check_access("验证码登录\n登录后查看我的订单\n了解我们的安全验证服务", "https://flights.ctrip.com/", 200, "homepage")

    def test_explicit_login_requirement_has_its_own_status(self):
        with self.assertRaises(FetchFailure) as caught:
            check_access("请先登录，再查询机票", "https://flights.ctrip.com/", 200, "roundtrip")
        self.assertEqual(caught.exception.status, "login_required")

    def test_verification_title_on_its_own_line_is_a_marker(self):
        with self.assertRaises(FetchFailure) as caught:
            check_access("安全验证\n请完成以下操作", "https://flights.ctrip.com/", 200, "homepage")
        self.assertIn("marker=security_verification", str(caught.exception))

    def test_whaleguard_has_specific_safe_evidence(self):
        with self.assertRaises(FetchFailure) as caught:
            check_access("WhaleGuard Block", "https://flights.ctrip.com/?token=sensitive", 200, "roundtrip")
        self.assertEqual(caught.exception.status, "blocked")
        self.assertIn("marker=whaleguard", str(caught.exception))
        self.assertIn("http=200", str(caught.exception))
        self.assertNotIn("sensitive", str(caught.exception))

    def test_http_429_is_rejection_without_page_marker(self):
        with self.assertRaises(FetchFailure) as caught:
            check_access("", "https://flights.ctrip.com/", 429, "homepage")
        self.assertEqual(caught.exception.status, "blocked")
        self.assertIn("http=429", str(caught.exception))

    def test_security_verification_marked_as_marker_not_proven_cause(self):
        with self.assertRaises(FetchFailure) as caught:
            check_access("请完成安全验证", "https://flights.ctrip.com/", 200, "homepage_ready")
        self.assertIn("verification_marker", str(caught.exception))


HOME = "https://flights.ctrip.com/"
SEARCH = build_url("SHA", "OSA", "2027-02-15", "2027-02-22")
CARD = "测试航空\nMU123 A320\n09:00\n浦东机场\n12:00\n关西机场\n¥2,799起\n往返含税价\n选为去程"


class LocalPage:
    """A browser model for page transitions; no real browser or network."""
    def __init__(self, home_body="携程首页\n验证码登录", search_body=CARD,
                 home_status=200, search_status=200, late_home_body=None):
        self.url = "about:blank"
        self.home_body, self.search_body = home_body, search_body
        self.home_status, self.search_status = home_status, search_status
        self.late_home_body = late_home_body
        self.requests, self.waits, self.body_reads = [], [], 0
        self.closed = False

    def goto(self, url, **kwargs):
        self.requests.append(url)
        self.url = url
        return SimpleNamespace(status=self.home_status if url == HOME else self.search_status)

    def inner_text(self, selector, **kwargs):
        self.body_reads += 1
        if self.url == HOME:
            if self.waits and self.late_home_body is not None:
                return self.late_home_body
            return self.home_body
        return self.search_body

    def wait_for_timeout(self, ms):
        self.waits.append(ms)

    def close(self):
        self.closed = True


class WarmupTests(unittest.TestCase):
    def run_query(self, page, cookie_batches=None, timeout=35, config=None):
        crawler = CtripCrawler({"timeout_seconds": timeout, **(config or {})}, logging.getLogger("warmup-test"))
        self.crawler = crawler
        batches = iter(cookie_batches or [[{"name": "cticket", "value": "synthetic"}]])
        last = []
        def cookies(urls):
            nonlocal last
            self.assertEqual(urls, [HOME])
            last = next(batches, last)
            return last
        context = SimpleNamespace(cookies=cookies)
        @contextmanager
        def browser(**kwargs):
            yield context
        with patch.object(crawler, "browser", browser), patch.object(crawler, "new_page", return_value=page):
            try:
                return fetch_round_trip(crawler, "SHA", "OSA", "2027-02-15", "2027-02-22")
            finally:
                self.assertTrue(crawler.use_mobile)

    def test_homepage_then_single_search_with_ordinary_login_links(self):
        page = LocalPage()
        quotes = self.run_query(page)
        self.assertEqual(page.requests, [HOME, SEARCH])
        self.assertEqual(page.waits, [2500])
        self.assertEqual(quotes[0].price, 2799)
        self.assertEqual(self.crawler.round_trip_diagnostics,
                         {"homepage_visits": 1, "search_visits": 1,
                          "warmup_cookie_present": True})

    def test_comparison_keeps_initial_list_price_if_detail_fails(self):
        page = LocalPage()
        detail_quote = RoundTripQuote("ctrip", "SHA", "OSA", "2027-02-15", 3100,
                                      flight_no="MU123", return_date="2027-02-22",
                                      return_flight_no="MU456", price_basis="selected_round_trip_tax")
        detail_error = FetchFailure("timeout", "detail_timeout", prices=[detail_quote])
        with patch("crawlers.ctrip_fare_details.read_details", side_effect=detail_error), \
                self.assertRaises(FetchFailure) as caught:
            self.run_query(page, config={"round_trip_fare_details": True, "ctrip_keep_list_quotes": True})
        self.assertEqual([quote.price for quote in caught.exception.prices], [3100, 2799])
        self.assertEqual(page.requests, [HOME, SEARCH])

    def test_delayed_cookie_is_waited_for_without_reloading_home(self):
        page = LocalPage()
        self.assertEqual(self.run_query(page, cookie_batches=[[], [{"name": "_bfa"}]])[0].price, 2799)
        self.assertEqual(page.requests, [HOME, SEARCH])
        self.assertEqual(page.waits, [2500, 800])

    def test_cookie_never_arrives_stops_without_search(self):
        page = LocalPage()
        with self.assertRaises(FetchFailure) as caught:
            self.run_query(page, cookie_batches=[[]], timeout=0)
        self.assertEqual(page.requests, [HOME])
        self.assertEqual(caught.exception.status, "error")
        self.assertIn("warmup_missing_risk_cookie", str(caught.exception))
        self.assertEqual(self.crawler.round_trip_diagnostics["search_visits"], 0)
        self.assertTrue(page.closed)

    def test_unrelated_cookie_is_not_warmup_readiness(self):
        page = LocalPage()
        with self.assertRaises(FetchFailure):
            self.run_query(page, cookie_batches=[[{"name": "unrelated"}]], timeout=0)
        self.assertEqual(page.requests, [HOME])

    def test_challenge_while_waiting_for_cookie_stops_without_search(self):
        page = LocalPage()
        original_wait = page.wait_for_timeout
        def wait(ms):
            original_wait(ms)
            if len(page.waits) > 1:
                page.late_home_body = "WhaleGuard Block"
        page.wait_for_timeout = wait
        with self.assertRaises(FetchFailure) as caught:
            self.run_query(page, cookie_batches=[[]])
        self.assertIn("marker=whaleguard", str(caught.exception))
        self.assertEqual(page.requests, [HOME])

    def test_result_uses_new_tab_in_same_context_after_warmup_closes(self):
        crawler = CtripCrawler({}, logging.getLogger("tabs-test"))
        home, result = LocalPage(), LocalPage()
        context = SimpleNamespace(cookies=lambda urls: [{"name": "cticket"}])
        @contextmanager
        def browser(**kwargs):
            yield context
        def new_page(ctx):
            self.assertIs(ctx, context)
            if home.requests:
                self.assertTrue(home.closed)
                return result
            return home
        with patch.object(crawler, "browser", browser), patch.object(crawler, "new_page", new_page):
            self.assertEqual(fetch_round_trip(crawler, "SHA", "OSA", "2027-02-15", "2027-02-22")[0].price, 2799)
        self.assertEqual(home.requests, [HOME])
        self.assertEqual(result.requests, [SEARCH])

    def test_homepage_rejection_never_reaches_search_or_body_read(self):
        page = LocalPage(home_status=403)
        with self.assertRaises(FetchFailure) as caught:
            self.run_query(page)
        self.assertEqual(page.requests, [HOME])
        self.assertEqual(page.body_reads, 0)
        self.assertEqual(page.waits, [])
        self.assertIn("stage=homepage;http=403", str(caught.exception))

    def test_homepage_whaleguard_never_reaches_search(self):
        page = LocalPage(home_body="WhaleGuard Block")
        with self.assertRaises(FetchFailure) as caught:
            self.run_query(page)
        self.assertEqual(page.requests, [HOME])
        self.assertIn("marker=whaleguard", str(caught.exception))

    def test_challenge_appearing_after_homepage_load_stops_before_search(self):
        page = LocalPage(late_home_body="请完成安全验证")
        with self.assertRaises(FetchFailure) as caught:
            self.run_query(page)
        self.assertEqual(page.requests, [HOME])
        self.assertIn("stage=homepage_ready", str(caught.exception))

    def test_homepage_login_requirement_is_not_blocked(self):
        page = LocalPage(home_body="请先登录")
        with self.assertRaises(FetchFailure) as caught:
            self.run_query(page)
        self.assertEqual(page.requests, [HOME])
        self.assertEqual(caught.exception.status, "login_required")

    def test_result_page_rejection_does_not_retry_search(self):
        page = LocalPage(search_status=429)
        with self.assertRaises(FetchFailure) as caught:
            self.run_query(page)
        self.assertEqual(page.requests, [HOME, SEARCH])
        self.assertIn("stage=roundtrip;http=429", str(caught.exception))

    def test_result_page_whaleguard_records_correct_stage(self):
        page = LocalPage(search_body="WhaleGuard Block")
        with self.assertRaises(FetchFailure) as caught:
            self.run_query(page)
        self.assertEqual(page.requests, [HOME, SEARCH])
        self.assertIn("stage=roundtrip", str(caught.exception))

    def test_normal_verification_help_text_does_not_hide_valid_prices(self):
        page = LocalPage(search_body=CARD + "\n了解我们的安全验证服务\n验证码登录")
        self.assertEqual(self.run_query(page)[0].price, 2799)

    def test_result_page_login_requirement_is_recorded_without_retry(self):
        page = LocalPage(search_body="请先登录，再查询机票")
        with self.assertRaises(FetchFailure) as caught:
            self.run_query(page)
        self.assertEqual(page.requests, [HOME, SEARCH])
        self.assertEqual(caught.exception.status, "login_required")
