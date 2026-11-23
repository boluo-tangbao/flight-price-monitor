import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from core.collection import FetchResult
from core.alerter import Alerter
from core.models import Route, RoundTripQuote
from core.storage import PriceStorage
from main import load_config, make_job
from scripts.compare_round_trip import main as comparison_main, run_comparison


class ComparisonTest(unittest.TestCase):
    def test_fixed_command_uses_configured_list_and_accepts_override(self):
        cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.monitor.yaml"))
        cfg["comparison"]["platforms"] = ["tongcheng", "ctrip", "tongcheng"]
        for args, expected in (([], ["tongcheng", "ctrip"]), (["--platforms", "ctrip"], ["ctrip"])):
            with self.subTest(args=args), patch("sys.argv", ["compare_round_trip.py", *args]), patch(
                    "scripts.compare_round_trip.load_config", return_value=cfg
            ), patch(
                    "scripts.compare_round_trip.run_comparison", return_value={"best_verified": [True]}
            ) as comparison, patch("builtins.print"):
                self.assertEqual(comparison_main(), 0)
                self.assertEqual(comparison.call_args.args[2], expected)

    def test_scheduled_round_combines_prices_and_sends_only_one_board(self):
        cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.monitor.yaml"))
        allowance = {"confirmed": True, "pieces": 2, "weight_kg": 23, "additional_fee_cny": 0}
        offer = {"itinerary_confirmed": True, "tax_inclusive_ticket_confirmed": True,
                 "cabin": "economy", "outbound": allowance, "inbound": allowance}

        class Crawler:
            def __init__(self, config, logger):
                self.config = config
                self.logger = logger

            def fetch_round_trip(self, origin, destination, depart, back):
                self.config["call_log"].append((self.name, origin, destination, depart, back))
                return [RoundTripQuote(self.name, origin, destination, depart, self.price,
                                       flight_no="NH976", return_date=back, return_flight_no="NH975",
                                       baggage_offer=offer)]

        class Ctrip(Crawler):
            name = "ctrip"
            price = 2755

        class Tongcheng(Crawler):
            name = "tongcheng"
            price = 2553

        calls = []
        cfg["comparison"]["platforms"] = ["ctrip", "tongcheng"]
        cfg["comparison"]["crawler"]["call_log"] = calls
        logger = logging.getLogger("joint-monitor-test")
        notifier = Mock()
        with TemporaryDirectory() as temp, patch("main.REGISTRY", {"ctrip": Ctrip, "tongcheng": Tongcheng}):
            storage = PriceStorage(str(Path(temp) / "prices.db"))
            make_job(cfg, logger, storage, Alerter(logger, notifier, storage), comparison=True)()
            with storage._conn() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0], 2)
        self.assertEqual(calls, [(platform, "SHA", "OSA", "2027-02-15", "2027-02-22")
                                 for platform in ("ctrip", "tongcheng")])
        notifier.send.assert_called_once()
        title, body = notifier.send.call_args.args
        self.assertIn("预算内 ¥2,553", title)
        self.assertLess(body.index("第 1 名 · 同程"), body.index("第 2 名 · 携程"))
        self.assertIn("去程托运", body)

    def test_one_round_compares_verified_totals_and_preserves_incomplete_platforms(self):
        route = Route("SHA", "上海", "OSA", "大阪", ["2027-02-15"], 2800,
                      "round_trip", ["2027-02-22"], True)
        allowance = {"confirmed": True, "pieces": 2, "weight_kg": 23, "additional_fee_cny": 0}
        offer = {"itinerary_confirmed": True, "tax_inclusive_ticket_confirmed": True,
                 "cabin": "economy", "outbound": allowance, "inbound": allowance}
        ctrip = RoundTripQuote("ctrip", "SHA", "OSA", route.dates[0], 2553, flight_no="NH976",
                              return_date=route.return_dates[0], return_flight_no="NH416", baggage_offer=offer)
        tongcheng = replace(ctrip, platform="tongcheng", price=2500,
                           baggage_offer={**offer, "inbound": {**allowance, "additional_fee_cny": 255}})
        third = replace(tongcheng, platform="third", price=2553, baggage_offer=offer)
        results = {
            "ctrip": FetchResult("ok", [ctrip]),
            "tongcheng": FetchResult("timeout", [tongcheng, replace(tongcheng, price=2400, baggage_offer={})]),
            "third": FetchResult("ok", [third, replace(third, price=1, return_date="2027-02-23")]),
        }
        class Supported:
            def __init__(self, config, logger):
                pass

            def fetch_round_trip(self):
                pass

        registry = {key: Supported for key in results} | {"future": object}
        with patch("scripts.compare_round_trip.REGISTRY", registry), patch(
                "scripts.compare_round_trip.collect", side_effect=results.values()) as collect:
            report = run_comparison(route, {}, registry, logging.getLogger("comparison-test"))
        self.assertEqual(collect.call_count, 3)
        self.assertTrue(all(call.args[1:] == ("SHA", "OSA", "2027-02-15", "2027-02-22")
                            for call in collect.call_args_list))
        self.assertEqual([row["platform"] for row in report["best_verified"]], ["ctrip", "third"])
        self.assertEqual(report["ranked_verified"][-1]["total_with_baggage_cny"], 2755)
        self.assertEqual(report["pending_at_or_below_best_total"][0]["ticket_price_cny"], 2400)
        self.assertEqual(report["platforms"]["tongcheng"]["status"], "timeout")
        self.assertEqual(report["platforms"]["third"]["rejected_quotes"], 1)
        self.assertEqual(report["platforms"]["future"]["status"], "unsupported")
