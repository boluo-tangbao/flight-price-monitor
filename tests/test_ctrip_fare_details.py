import logging
import os
import unittest
from unittest.mock import Mock

from core.alerter import Alerter
from core.baggage import baggage_total
from core.collection import FetchResult
from core.models import RoundTripQuote, Route
from crawlers.ctrip_fare_details import included_baggage, read_details
from crawlers.ctrip_roundtrip import check_access


RULES = ("行李规定        上海 — 大阪\n手提：每人1件,每件7公斤\n"
         "托运：总重不超过20公斤\n大阪 — 上海\n手提：每人1件\n托运：每人1件,每件20公斤")


def quote(price, inbound="", rules=""):
    return RoundTripQuote("ctrip", "SHA", "OSA", "2027-02-15", price, flight_no="9C6575",
                          return_date="2027-02-22", return_flight_no=inbound,
                          baggage_offer={"itinerary_confirmed": bool(inbound),
                                         "tax_inclusive_ticket_confirmed": True,
                                         **included_baggage(rules)})


class FareTests(unittest.TestCase):
    def test_two_directions_with_checked_allowance_match_same_fare(self):
        self.assertEqual(baggage_total(quote(2997, "MM079", RULES)), 2997)

    def test_hand_baggage_and_missing_fee_never_count(self):
        for body in (RULES.replace("总重不超过20公斤", "无免费行李额"),
                     RULES.replace("托运：总重不超过20公斤", ""),
                     RULES.replace("大阪 — 上海", "上海 — 大阪"),
                     RULES.replace("总重不超过20公斤", "收费20公斤")):
            with self.subTest(body=body):
                self.assertIsNone(baggage_total(quote(2542, "MM079", body)))

    def test_over_budget_reports_three_and_labels_unknown_as_lower_bound(self):
        route = Route("SHA", "上海", "OSA", "大阪", [], 2800, checked_baggage_required=True)
        notifier, storage = Mock(), Mock()
        alerter = Alerter(logging.getLogger("test"), notifier, storage)
        alerter.report_round_trip_options(route, "2027-02-15", "2027-02-22", FetchResult("ok", [
            quote(2997, "MM079", RULES), quote(2542), quote(3009, "9C6576", RULES),
            quote(3600, "9C6582", RULES)]))
        notifier.send.assert_called_once()
        message = notifier.send.call_args.args[1]
        self.assertIn("含两程托运 ¥2,997.00", message)
        self.assertIn("含两程托运 ¥3,009.00", message)
        self.assertIn("¥3,600.00", message)
        self.assertNotIn("¥2,542.00", message)
        notifier.reset_mock()
        alerter.report_round_trip_options(route, "2027-02-15", "2027-02-22", FetchResult("ok", [
            quote(2997, "MM079", RULES), quote(2542)]))
        self.assertIn("¥2,542.00，托运费用待确认", notifier.send.call_args.args[1])

    def test_in_budget_sends_one_price_alert_instead_of_summary(self):
        route = Route("SHA", "上海", "OSA", "大阪", [], 2800, checked_baggage_required=True)
        notifier, storage = Mock(), Mock()
        storage.get_alert_state.return_value = None
        Alerter(logging.getLogger("test"), notifier, storage).report_round_trip_options(
            route, "2027-02-15", "2027-02-22", FetchResult("ok", [
                quote(3100, "9C6582", RULES), quote(2700, "MM079", RULES)]))
        notifier.send.assert_called_once()
        self.assertIn("¥2700", notifier.send.call_args.args[0])

    @unittest.skipUnless(os.environ.get("RUN_OFFLINE_BROWSER_CHECK") == "1", "opt-in local Chromium fixture")
    def test_expanding_first_return_does_not_reindex_later_cards(self):
        from playwright.sync_api import sync_playwright

        html = '''<div id="out"><div class="flight-box"><div>9C6575 A320</div>
          <div>¥2500起</div><div>往返含税价</div><button id="go">选为去程</button></div></div>
          <div id="returns"></div><div id="popup" style="white-space:pre-line"></div>'''
        script = '''document.querySelector('#go').onclick = () => {
          document.querySelector('#out').remove();
          const target = document.querySelector('#returns');
          for (const [flight, price] of [['MM079', 2542], ['9C6576', 2600], ['NH975', 2770]]) {
            const row = document.createElement('div');
            row.innerHTML = `<div class="flight-box"><div>${flight} A320</div><div>¥${price}起</div>
                  <div>往返含税价</div><button>订票</button></div>`;
            target.append(row);
            row.querySelector('button').onclick = e => {
              e.target.innerText = '收起';
              row.insertAdjacentHTML('beforeend', `<div class="flight-seats"><div class="seat-row">
                  <span>行李规定</span><div>¥${price}</div><button>预订</button></div></div>`);
              const link = row.querySelector('span');
              const popup = document.querySelector('#popup');
              link.onmouseenter = () => popup.innerText = window.rules;
              link.onmouseleave = () => popup.innerText = '';
              row.querySelector('.seat-row button').onclick = () => { throw new Error('order button clicked'); };
            };
          }
        };'''
        with sync_playwright() as pw:
            browser = pw.chromium.launch(proxy={"server": "http://127.0.0.1:9"},
                                         args=["--disable-background-networking"])
            try:
                page = browser.new_page()
                page.route("**/*", lambda route: route.abort())
                page.set_content(html)
                page.evaluate("rules => window.rules = rules", RULES)
                page.add_script_tag(content=script)
                diagnostics = {"expanded_return_options": 0}
                rows = read_details(page, [quote(2500)], check_access, diagnostics)
                self.assertEqual([(q.return_flight_no, baggage_total(q)) for q in rows],
                                 [("MM079", 2542), ("9C6576", 2600), ("NH975", 2770)])
                self.assertEqual(diagnostics["expanded_return_options"], 3)
            finally:
                browser.close()
