import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core.cloud_session import SECRET_NAME
from core.collection import FetchResult
from core.models import RoundTripQuote
from scripts import cloud_acceptance


class CloudAcceptanceTests(unittest.TestCase):
    def run_acceptance(self, result, search_visits, supplied_state="synthetic-not-decoded"):
        route = SimpleNamespace(from_code="SHA", to_code="OSA", dates=["2027-02-15"],
                                return_dates=["2027-02-22"], alert_threshold=2800)
        crawler = SimpleNamespace(
            round_trip_diagnostics={"homepage_visits": 1, "search_visits": search_visits,
                                    "warmup_cookie_present": None if not search_visits else True},
            cloud_browser_diagnostics={"mode": "upstream", "initialization_installed": True,
                                       "kernel_major": 159})
        with tempfile.TemporaryDirectory() as root:
            previous = Path.cwd()
            try:
                os.chdir(root)
                with patch.dict(os.environ, {SECRET_NAME: supplied_state}), \
                        patch("sys.argv", ["cloud_acceptance.py", "--authenticated"]), \
                        patch.object(cloud_acceptance, "load_config", return_value={"crawler": {}}), \
                        patch.object(cloud_acceptance, "build_routes", return_value=[route]), \
                        patch.object(cloud_acceptance, "CtripCrawler", return_value=crawler), \
                        patch.object(cloud_acceptance, "collect", return_value=result), \
                        contextlib.redirect_stdout(io.StringIO()):
                    code = cloud_acceptance.main()
                report = json.loads(Path("data/cloud-acceptance.json").read_text(encoding="utf-8"))
            finally:
                os.chdir(previous)
        self.assertNotIn("synthetic-not-decoded", json.dumps(report))
        return code, report

    def test_homepage_block_counts_zero_searches_and_one_attempt(self):
        code, report = self.run_acceptance(
            FetchResult("blocked", evidence="verification_page;marker=whaleguard;stage=homepage"), 0)
        self.assertEqual(code, 2)
        self.assertEqual((report["attempts"], report["queries"]), (1, 0))
        self.assertTrue(report["browser"]["initialization_installed"])
        self.assertFalse(report["notifications_sent"])

    def test_ticket_success_and_unverified_baggage_remain_distinct(self):
        quote = RoundTripQuote("ctrip", "SHA", "OSA", "2027-02-15", 2500,
                               return_date="2027-02-22")
        code, report = self.run_acceptance(FetchResult("ok", [quote]), 1)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["queries"], 1)
        self.assertEqual(report["quotes"][0]["ticket_from_price"], 2500)
        self.assertFalse(report["baggage_verified"])
        self.assertEqual(report["budget_result"], "unverified")

    def test_over_budget_ticket_prices_are_conclusive_without_baggage_fees(self):
        quotes = [RoundTripQuote("ctrip", "SHA", "OSA", "2027-02-15", price,
                                 return_date="2027-02-22", flight_no="NH976",
                                 return_flight_no="NH973", price_basis="selected_round_trip_tax")
                  for price in (2805, 2820, 2927, 3000)]
        code, report = self.run_acceptance(FetchResult("ok", quotes), 1)
        self.assertEqual(code, 0)
        self.assertEqual(report["budget_result"], "over_budget")
        self.assertFalse(report["baggage_verified"])
        self.assertFalse(report["within_budget"])
        self.assertEqual(report["inspected_quote_count"], 4)
        self.assertEqual([q["ticket_from_price"] for q in report["quotes"]], [2805, 2820, 2927])

    def test_unknown_price_at_budget_behind_verified_totals_still_prevents_conclusion(self):
        allowance = {"confirmed": True, "pieces": 1, "additional_fee_cny": 0}
        quotes = [RoundTripQuote("ctrip", "SHA", "OSA", "2027-02-15", price,
                                 return_date="2027-02-22", baggage_offer={
                                     "itinerary_confirmed": True,
                                     "tax_inclusive_ticket_confirmed": True,
                                     "outbound": allowance,
                                     "inbound": allowance})
                  for price in (2900, 3000, 3100)]
        quotes.append(RoundTripQuote("ctrip", "SHA", "OSA", "2027-02-15", 2800,
                                     return_date="2027-02-22"))
        code, report = self.run_acceptance(FetchResult("ok", quotes), 1)
        self.assertEqual(code, 0)
        self.assertEqual(report["budget_result"], "unverified")
        self.assertTrue(report["baggage_verified"])
        self.assertEqual(len(report["quotes"]), 3)

    def test_missing_login_secret_writes_failure_report_without_search(self):
        code, report = self.run_acceptance(FetchResult("ok"), 1, supplied_state="")
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "login_required")
        self.assertEqual(report["queries"], 0)
        self.assertEqual(report["evidence"], "login_state_mode_mismatch_no_request")

    def test_explicit_empty_result_returns_a_report_successfully(self):
        code, report = self.run_acceptance(FetchResult("empty", evidence="explicit_no_available_round_trip"), 1)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "empty")
        self.assertEqual(report["budget_result"], "no_quotes")
