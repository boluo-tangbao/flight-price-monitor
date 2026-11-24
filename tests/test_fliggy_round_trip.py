import json
import logging
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import httpx

from core.collection import FetchFailure, FetchResult, collect
from core.models import Route, RoundTripQuote
from core.round_trip_comparison import compare_round_trips
from crawlers import fliggy_roundtrip
from crawlers.fliggy import FliggyCrawler
from crawlers.fliggy_fare_details import _adult_checked_rules, _fare_price, _text_baggage_rows, check_access, read_fare_details
from crawlers.fliggy_roundtrip import _calendar_price, _decode_calendar, _search_url
from scripts.compare_round_trip import main as comparison_main, run_comparison


class FliggyRoundTripTests(unittest.TestCase):
    depart = "2027-02-15"
    back = "2027-02-22"

    def test_completed_hidden_challenge_does_not_block_flight_results(self):
        url = _search_url("SHA", "OSA", self.depart, self.back)
        page = MagicMock(url=url)
        main = MagicMock(url=url, parent_frame=None)
        challenge = MagicMock(url="https://sijipiao.fliggy.com/_____tmd_____/punish", parent_frame=main)
        page.frames = [main, challenge]
        page.locator.return_value.inner_text.return_value = "选择去程 春秋航空 9C6575"
        challenge.frame_element.return_value.is_visible.return_value = False
        check_access(page, url)
        challenge.frame_element.return_value.is_visible.return_value = True
        with self.assertRaises(FetchFailure) as caught:
            check_access(page, url)
        self.assertEqual(caught.exception.status, "blocked")

    def test_nested_challenge_inside_hidden_frame_is_inactive(self):
        url = _search_url("SHA", "OSA", self.depart, self.back)
        main = MagicMock(url=url, parent_frame=None)
        container = MagicMock(url="about:blank", parent_frame=main)
        challenge = MagicMock(url="https://sijipiao.fliggy.com/captcha/", parent_frame=container)
        container.frame_element.return_value.is_visible.return_value = False
        challenge.frame_element.return_value.is_visible.return_value = True
        page = MagicMock(url=url, frames=[main, container, challenge])
        page.locator.return_value.inner_text.return_value = "选择去程"
        check_access(page, url)

    def test_original_calendar_link_city_code_aliases_are_checked(self):
        expected = _search_url("SHA", "OSA", self.depart, self.back)
        actual = expected.replace("depCity=", "depCityCode=").replace("arrCity=", "arrCityCode=")
        page = MagicMock(url=actual, frames=[])
        page.locator.return_value.inner_text.return_value = "选择去程"
        check_access(page, expected)
        check_access(page, actual)
        page.url = actual + "&depCity=PEK"
        with self.assertRaises(FetchFailure):
            check_access(page, expected)

    def test_jsonp_requires_both_dates_and_explicit_tax(self):
        rows = [
            {"leaveDate": self.depart, "backDate": "2027-02-23", "price": 70, "tax": 0},
            {"leaveDate": "2027-02-16", "backDate": self.back, "price": 80, "tax": 0},
            {"leaveDate": self.depart, "price": 90, "tax": 0},
            {"leaveDate": self.depart, "backDate": self.back, "price": 100},
            {"leaveDate": self.depart, "backDate": self.back, "price": 1200, "tax": 0, "currency": "JPY"},
            {"leaveDate": self.depart, "backDate": self.back, "price": 1100, "tax": -1},
            {"leaveDate": self.depart, "backDate": self.back, "price": 2100, "tax": 410},
        ]
        payload = _decode_calendar("cb(" + json.dumps({"result": rows}) + ");")
        self.assertEqual(_calendar_price(payload["result"], self.depart, self.back), 2510)

    def test_calendar_is_pending_even_when_below_budget(self):
        route = Route("SHA", "上海", "OSA", "大阪", [self.depart], 2800, "round_trip", [self.back], True)
        response = httpx.Response(200, json={"result": [
            {"leaveDate": self.depart, "backDate": self.back, "price": 2100, "tax": 410},
        ]})
        with patch("crawlers.fliggy_roundtrip.httpx.Client.send", return_value=response) as send:
            report = run_comparison(route, {"fliggy_calendar_only": True}, ["fliggy"], logging.getLogger(__name__))
        self.assertEqual(report["platforms"]["fliggy"]["status"], "unverified")
        self.assertEqual(report["platforms"]["fliggy"]["diagnostics"]["calendar_rows_seen"], 1)
        quote = report["pending_baggage"][0]
        self.assertEqual(quote["ticket_price_cny"], 2510)
        self.assertEqual(quote["flight_no"], "")
        request = send.call_args.args[0]
        self.assertEqual(request.headers["Referer"], "https://sijipiao.fliggy.com/")
        self.assertEqual(request.url.params["tripType"], "1")
        self.assertEqual(request.url.params["backDate"], self.back)
        search = parse_qs(urlsplit(quote["source_url"]).query)
        self.assertEqual((search["tripType"], search["depDate"], search["arrDate"]),
                         (["1"], [self.depart], [self.back]))
        self.assertEqual(report["ranked_verified"], [])
        self.assertIsNone(quote["total_with_baggage_cny"])

    def test_calendar_access_refusal_is_reported_without_retry(self):
        crawler = FliggyCrawler({"fliggy_calendar_only": True}, logging.getLogger(__name__))
        with patch("crawlers.fliggy_roundtrip.httpx.Client.get", return_value=httpx.Response(
                200, json={"success": False, "errorCode": "REFUSED", "errorMessage": "Referer is not safe",
                           "trackLinkedId": "internal-id"})) as get:
            result = collect(crawler, "SHA", "OSA", self.depart, self.back)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.prices, [])
        self.assertEqual(crawler.round_trip_diagnostics["calendar_error_code"], "REFUSED")
        self.assertEqual(crawler.round_trip_diagnostics["calendar_error_message"], "Referer is not safe")
        self.assertNotIn("internal-id", json.dumps(crawler.round_trip_diagnostics))
        get.assert_called_once()

    def test_matching_date_with_zero_price_is_distinguished_from_missing_dates(self):
        crawler = FliggyCrawler({"fliggy_calendar_only": True}, logging.getLogger(__name__))
        rows = [
            {"leaveDate": self.depart, "backDate": self.back, "price": 0, "tax": 0},
            {"leaveDate": "2027-02-18", "backDate": "2027-02-25", "price": 1394, "tax": 924},
        ]
        with patch("crawlers.fliggy_roundtrip.httpx.Client.get", return_value=httpx.Response(
                200, json={"result": rows})):
            result = collect(crawler, "SHA", "OSA", self.depart, self.back)
        self.assertEqual(result.status, "unverified")
        self.assertEqual(result.evidence, "fliggy_calendar_requested_dates_no_quoted_price")
        self.assertEqual(result.prices, [])
        self.assertEqual(crawler.round_trip_diagnostics["calendar_matching_date_rows"], 1)
        self.assertEqual(crawler.round_trip_diagnostics["calendar_requested_date_prices"], [rows[0]])

    def test_http_error_diagnostics_redact_verification_links_and_credentials(self):
        crawler = FliggyCrawler({"fliggy_calendar_only": True}, logging.getLogger(__name__))
        message = "Access denied https://example.test/verify?token=private-token; Authorization: Bearer secret"
        with patch("crawlers.fliggy_roundtrip.httpx.Client.get", return_value=httpx.Response(
                403, json={"errorCode": 403, "errorMessage": message})):
            result = collect(crawler, "SHA", "OSA", self.depart, self.back)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(crawler.round_trip_diagnostics["calendar_error_code"], 403)
        self.assertEqual(crawler.round_trip_diagnostics["calendar_error_message"],
                         "Access denied [URL] [redacted]")

    def test_unsupported_route_does_not_make_a_single_trip_request(self):
        crawler = FliggyCrawler({}, logging.getLogger(__name__))
        with patch("crawlers.fliggy_roundtrip.httpx.Client.get") as get:
            result = collect(crawler, "SHA", "PEK", self.depart, self.back)
        self.assertEqual(result.status, "unsupported")
        get.assert_not_called()

    def test_calendar_ticket_is_retained_when_desktop_verification_stops_baggage_check(self):
        crawler = FliggyCrawler({}, logging.getLogger(__name__))
        calendar_quote = RoundTripQuote("fliggy", "SHA", "OSA", self.depart, 2510,
                                       return_date=self.back, price_basis="fliggy_calendar_round_trip_tax")
        with patch("crawlers.fliggy_roundtrip._fetch_desktop", side_effect=FetchFailure(
                "blocked", "fliggy_desktop_verification_required")), patch(
                "crawlers.fliggy_roundtrip._fetch_calendar", return_value=calendar_quote) as calendar:
            result = collect(crawler, "SHA", "OSA", self.depart, self.back)
        self.assertEqual(result.status, "blocked")
        calendar.assert_called_once()
        self.assertEqual(result.prices, [calendar_quote])
        route = Route("SHA", "上海", "OSA", "大阪", [self.depart], 2800, "round_trip", [self.back], True)
        report = compare_round_trips(route, self.depart, self.back, {"fliggy": result})
        self.assertEqual([row["ticket_price_cny"] for row in report["ranked_ticket"]], [2510])
        self.assertEqual(report["ranked_ticket"][0]["checked_baggage_status"], "unverified")
        self.assertEqual(report["platforms"]["fliggy"]["ticket_status"], "available")
        self.assertEqual(report["ranked_verified"], [])

    def test_calendar_and_selected_fare_keep_separate_prices_and_baggage(self):
        crawler = FliggyCrawler({}, logging.getLogger(__name__))
        calendar = RoundTripQuote("fliggy", "SHA", "OSA", self.depart, 2510, return_date=self.back,
                                  price_basis="fliggy_calendar_round_trip_tax")
        allowance = {"confirmed": True, "pieces": 1, "weight_kg": 20, "additional_fee_cny": 0}
        fare = RoundTripQuote("fliggy", "SHA", "OSA", self.depart, 2949, return_date=self.back,
                              flight_no="NH976", return_flight_no="MM079",
                              baggage_offer={"cabin": "economy", "itinerary_confirmed": True,
                                  "tax_inclusive_ticket_confirmed": True,
                                  "outbound": allowance, "inbound": allowance})
        with patch("crawlers.fliggy_roundtrip._fetch_calendar", return_value=calendar), patch(
                "crawlers.fliggy_roundtrip._fetch_desktop", return_value=[fare]):
            result = collect(crawler, "SHA", "OSA", self.depart, self.back)
        route = Route("SHA", "上海", "OSA", "大阪", [self.depart], 2800, "round_trip", [self.back], True)
        report = compare_round_trips(route, self.depart, self.back, {"fliggy": result})
        self.assertEqual([row["ticket_price_cny"] for row in report["ranked_ticket"]], [2510, 2949])
        self.assertIsNone(report["ranked_ticket"][0]["total_with_baggage_cny"])
        self.assertEqual([row["ticket_price_cny"] for row in report["ranked_verified"]], [2949])

    def test_manual_login_and_query_open_the_same_desktop_profile(self):
        crawler = FliggyCrawler({"user_data_dir": "fixture_profiles", "fliggy_pc_browser_channel": "chrome"},
                               logging.getLogger(__name__))
        url = _search_url("SHA", "OSA", self.depart, self.back)
        with patch.object(fliggy_roundtrip, "sync_playwright") as playwright, patch(
                "builtins.input", return_value=""), patch.object(
                fliggy_roundtrip, "check_access"), patch.object(fliggy_roundtrip, "read_fare_details"):
            launch = playwright.return_value.__enter__.return_value.chromium.launch_persistent_context
            context = launch.return_value.__enter__.return_value
            context.pages = []
            page = context.new_page.return_value
            fliggy_roundtrip.interactive_login(crawler)
            fliggy_roundtrip._fetch_desktop(crawler, "SHA", "OSA", self.depart, self.back,
                                            {"search_url": url})
        self.assertEqual([call.args[0] for call in launch.call_args_list],
                         [str(Path("fixture_profiles") / "fliggy-pc")] * 2)
        self.assertEqual([call.kwargs["channel"] for call in launch.call_args_list], ["chrome", "chrome"])
        self.assertEqual([call.kwargs["headless"] for call in launch.call_args_list], [False, True])
        self.assertEqual([call.args[0] for call in page.goto.call_args_list],
                         [fliggy_roundtrip.LOGIN_HOME, url])

    def test_manual_login_command_does_not_run_comparison(self):
        with patch("sys.argv", ["compare_round_trip.py", "--fliggy-login", "--fliggy-channel", "chrome"]), patch(
                "crawlers.fliggy_roundtrip.interactive_login") as login, patch(
                "scripts.compare_round_trip.run_comparison") as query:
            self.assertEqual(comparison_main(), 0)
        self.assertEqual(login.call_args.args[0].config["fliggy_pc_browser_channel"], "chrome")
        query.assert_not_called()

    def test_adult_checked_rows_use_each_direction(self):
        rules = _adult_checked_rules([(
            ("上海-大阪", "成人", "托运行李", "每人可免费托运件数：\n2件\n可免费托运的重量：\n23KG/件"),
            ("大阪-上海", "成人", "手提行李", "每人可携带件数：\n1件"),
            ("大阪-上海", "成人", "托运行李", "航司规定，无免费托运行李额"),
            ("大阪-上海", "儿童", "托运行李", "每人可免费托运件数：\n1件"),
        )], "上海", "大阪")
        self.assertEqual((rules["outbound"]["pieces"], rules["outbound"]["weight_kg"]), (2, 23))
        self.assertEqual(rules["outbound"]["weight_basis"], "per_piece")
        self.assertTrue(rules["inbound"]["no_free"])
        self.assertIsNone(_fare_price("去程 经济舱 返程 经济舱 ¥2865 往返总价", False))

    def test_rendered_baggage_grid_keeps_direction_passenger_and_baggage_type(self):
        rows = _text_baggage_rows("""行李额说明
上海-大阪
乘客类型
行李类型
行李规则
成人
个人物品
每人可携带件数：1件
手提行李
可带上飞机的重量：8KG/件
托运行李
每人可免费托运件数：
2件
可免费托运的重量：
23KG/件
儿童
托运行李
每人可免费托运件数：99件
大阪-上海
成人
手提行李
可带上飞机的重量：7KG/件
托运行李
航司规定，无免费托运行李额
儿童
托运行李
每人可免费托运件数：2件
可免费托运的重量：23KG/件""", "上海", "大阪")
        rules = _adult_checked_rules([rows], "上海", "大阪")
        self.assertEqual(rules["outbound"]["pieces"], 2)
        self.assertEqual(rules["outbound"]["weight_kg"], 23)
        self.assertTrue(rules["inbound"]["no_free"])

    def test_grid_does_not_borrow_passenger_from_previous_direction(self):
        rows = _text_baggage_rows("上海-大阪\n成人\n托运行李\n每人可免费托运件数：2件\n"
                                  "大阪-上海\n托运行李\n每人可免费托运件数：2件", "上海", "大阪")
        self.assertEqual(set(_adult_checked_rules([rows], "上海", "大阪")), {"outbound"})

    def test_grid_reads_spring_total_weight_and_peach_per_piece_weight(self):
        rows = _text_baggage_rows("上海-大阪\n成人\n托运行李\n可免费托运的重量：\n总重20KG\n"
                                  "大阪-上海\n成人\n托运行李\n每人可免费托运件数：\n1件\n"
                                  "可免费托运的重量：\n20KG/件", "上海", "大阪")
        rules = _adult_checked_rules([rows], "上海", "大阪")
        self.assertEqual(rules["outbound"]["weight_kg"], 20)
        self.assertEqual(rules["outbound"]["weight_basis"], "total")
        self.assertEqual(rules["inbound"]["pieces"], 1)
        self.assertEqual(rules["inbound"]["weight_kg"], 20)
        self.assertEqual(rules["inbound"]["weight_basis"], "per_piece")

    @unittest.skipUnless(os.environ.get("RUN_FLIGGY_OFFLINE_BROWSER_CHECK") == "1",
                         "opt-in local Edge fixture")
    def test_desktop_hovers_each_fare_and_uses_its_own_baggage(self, rax=False):
        from playwright.sync_api import sync_playwright

        route = Route("SHA", "上海", "OSA", "大阪", [self.depart], 2800, "round_trip", [self.back], True)
        url = _search_url("SHA", "OSA", self.depart, self.back)
        html = """
          <button>含税价格</button>
          <div class="flightItem" id="out">
            <span aria-label="航班号 NH976">全日空航空 NH976</span>往返 含税￥2553
            <button aria-label="选为去程" onclick="chooseOut()"><span>2张</span>选为去程</button>
          </div>
          <section id="selected-out" hidden>
            <span>去程已选定</span><span>全日空航空NH976</span><span>出发: 2027-02-15 19:05</span>
            <button>重选去程航班</button>
          </section>
          <section id="selected-in" hidden>
            <span>返程已选定</span><span>乐桃航空MM079</span><span>出发: 2027-02-22 22:25</span>
            <button>重选返程航班</button>
          </section>
          <div class="flightItem" id="back" hidden>
            <span aria-label="航班号 MM079">乐桃航空 MM079</span>往返 含税￥2865
            <button aria-label="选为返程" onclick="chooseBack()"><span>2张</span>选为返程</button>
            <section id="fares"></section>
          </div>
          <section id="rules" hidden></section>
          <script>
            const free = '每人可免费托运件数：<br>2件<br>可免费托运的重量：<br>23KG/件';
            const none = '航司规定，无免费托运行李额';
            function chooseOut() {
              document.querySelector('#out').hidden = true;
              document.querySelector('#selected-out').hidden = false;
              document.querySelector('#back').hidden = false;
            }
            function chooseBack() {
              document.querySelector('#selected-in').hidden = false;
              for (const price of [2865, 3097, 3533, 3858]) {
                document.querySelector('#fares').insertAdjacentHTML('beforeend',
                  `<article><div>去程 经济舱</div><div>返程 经济舱</div>
                    <span onmouseenter="showRules(${price})" onmouseleave="hideRules()">退改签规则及行李额</span>
                    <div>此为组合特价，机票将分开出票，航变及退改需分开处理</div>
                    <div>￥${price}</div><div>往返总价</div>
                    <button onclick="document.body.dataset.booked='true'">订</button></article>`);
              }
            }
            function baggageTable(city, checked) {
              return `<table><tr><th></th><th>乘客类型</th><th>行李类型</th><th>行李规则</th></tr>
                <tr><th rowspan="4">${city}</th><td rowspan="3">成人</td><td>个人物品</td><td>1件</td></tr>
                <tr><td>手提行李</td><td>1件</td></tr>
                <tr><td>托运行李</td><td>${checked}</td></tr>
                <tr><td>儿童</td><td>托运行李</td><td>每人可免费托运件数：99件</td></tr></table>`;
            }
            function showRules(price) {
              const popup = document.querySelector('#rules');
              popup.hidden = false;
              popup.innerHTML = '<div>退改签规则</div><div>行李额说明</div>' +
                baggageTable('上海-大阪', free) +
                (price === 3858 ? '' : baggageTable('大阪-上海', price === 3097 ? free : none));
            }
            function hideRules() { document.querySelector('#rules').hidden = true; }
          </script>"""

        if rax:
            html = html.replace("<table><tr><th></th><th>乘客类型</th><th>行李类型</th><th>行李规则</th></tr>",
                                "<div class='rax-view'><div>${city}</div><div>乘客类型</div><div>行李类型</div><div>行李规则</div>")
            html = html.replace('<tr><th rowspan="4">${city}</th><td rowspan="3">成人</td><td>个人物品</td><td>1件</td></tr>',
                                '<div>成人</div><div>个人物品</div><div>1件</div>')
            html = html.replace('<tr><td>手提行李</td><td>1件</td></tr>', '<div>手提行李</div><div>1件</div>')
            html = html.replace('<tr><td>托运行李</td><td>${checked}</td></tr>', '<div>托运行李</div><div>${checked}</div>')
            html = html.replace('<tr><td>儿童</td><td>托运行李</td><td>每人可免费托运件数：99件</td></tr></table>',
                                '<div>儿童</div><div>托运行李</div><div>每人可免费托运件数：99件</div></div>')

        with sync_playwright() as pw:
            with pw.chromium.launch(channel="msedge", headless=True,
                    proxy={"server": "http://127.0.0.1:9"}, args=["--disable-background-networking"]) as browser:
                page = browser.new_page()
                page.route("**/*", lambda req: req.fulfill(body=html, content_type="text/html; charset=utf-8")
                           if req.request.url == url else req.abort())
                page.goto(url)
                diagnostics = {}
                quotes = read_fare_details(page, ("SHA", "OSA", self.depart, self.back, "上海", "大阪", url),
                                           diagnostics)
                self.assertIsNone(page.locator("body").get_attribute("data-booked"))

        report = compare_round_trips(route, self.depart, self.back, {"fliggy": FetchResult("ok", quotes)})
        self.assertEqual(diagnostics["pairs_scanned"], [["NH976", "MM079"]])
        self.assertEqual(diagnostics["baggage_hovers"], 4)
        self.assertEqual([quote.price for quote in quotes], [2865, 3097, 3533, 3858])
        self.assertEqual([item["ticket_price_cny"] for item in report["ranked_verified"]], [3097])
        self.assertEqual([item["ticket_price_cny"] for item in report["pending_baggage"]], [3858])
        self.assertEqual(report["platforms"]["fliggy"]["excluded_baggage_purchase_required"], 2)

    @unittest.skipUnless(os.environ.get("RUN_FLIGGY_OFFLINE_BROWSER_CHECK") == "1",
                         "opt-in local Edge fixture")
    def test_desktop_reads_div_based_popup_for_each_fare(self):
        self.test_desktop_hovers_each_fare_and_uses_its_own_baggage(rax=True)
