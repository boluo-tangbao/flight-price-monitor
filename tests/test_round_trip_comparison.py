import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from core.collection import FetchResult
from core.cloud_session import SESSION_SECRETS, encode_state
from core.alerter import Alerter
from core.models import Route, RoundTripQuote
from core.round_trip_comparison import compare_round_trips
from core.storage import PriceStorage
from main import load_config, make_job
from scripts.compare_round_trip import main as comparison_main, run_comparison


class ComparisonTest(unittest.TestCase):
    def test_cloud_round_skips_missing_session_and_still_exports_other_platforms(self):
        cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.monitor.yaml"))
        calls = []

        class Crawler:
            def __init__(self, config, logger):
                self.config, self.logger = config, logger

            def fetch_round_trip(self, origin, destination, depart, back):
                calls.append(self.name)
                self_test.assertTrue(self.config["headless"])
                self_test.assertEqual(self.config[f"{self.name}_pc_browser_channel"]
                                      if self.name != "ctrip" else self.config["pc_browser_channel"], "chromium")
                self_test.assertTrue(all(secret not in os.environ for secret in SESSION_SECRETS.values()))
                self_test.assertEqual(self.cloud_storage_state["cookies"][0]["name"], self.name)
                return [RoundTripQuote(self.name, origin, destination, depart, 2800,
                                       flight_no="NH976", return_date=back, return_flight_no="NH975")]

        self_test = self
        providers = cfg["comparison"]["platforms"]
        self.assertEqual(providers, ["ctrip", "tongcheng", "fliggy"])
        registry = {name: type(name, (Crawler,), {"name": name}) for name in providers}
        env = {secret: "" for secret in SESSION_SECRETS.values()}
        for name, domain in (("ctrip", "ctrip.com"), ("fliggy", "fliggy.com")):
            env[SESSION_SECRETS[name]] = encode_state(
                {"cookies": [{"name": name, "domain": domain, "value": "synthetic-secret"}]}, platform=name)
        logger = logging.getLogger("cloud-comparison-test")
        with TemporaryDirectory() as temp, patch("main.REGISTRY", registry), patch.dict(os.environ, env):
            cfg["output"]["db_path"] = str(Path(temp) / "prices.db")
            storage = PriceStorage(cfg["output"]["db_path"])
            make_job(cfg, logger, storage, Alerter(logger, storage=storage), comparison=True, cloud=True)()
            raw = (Path(temp) / "comparison.json").read_text(encoding="utf-8")
        report = json.loads(raw)
        self.assertEqual(calls, ["ctrip", "fliggy"])
        self.assertEqual(report["platforms"]["tongcheng"]["status"], "login_required")
        self.assertEqual(len(report["ranked_ticket"]), 2)
        self.assertNotIn("synthetic-secret", raw)

    def test_command_lists_ticket_prices_when_no_baggage_total_is_verified(self):
        cfg = load_config(str(Path(__file__).resolve().parents[1] / "config.monitor.yaml"))
        for args in ([], ["--diagnostics"]):
            with self.subTest(args=args), patch("sys.argv", ["compare_round_trip.py", *args]), patch(
                    "scripts.compare_round_trip.load_config", return_value=cfg
            ), patch("scripts.compare_round_trip.run_comparison", return_value={
                "best_verified": [],
                "ranked_verified": [],
                "ranked_ticket": [{"ticket_price_cny": 2688}],
                "pending_baggage": [{"ticket_price_cny": 2688}],
                "pending_at_or_below_best_total": [{"ticket_price_cny": 2688}],
                "platforms": {"tongcheng": {"pending_quotes": 1, "diagnostics": {"detail_stage": "return_card"}}},
            }), patch("builtins.print") as printed:
                self.assertEqual(comparison_main(), 0)
            result = json.loads(printed.call_args.args[0])
            self.assertEqual(result["ranked_ticket"][0]["ticket_price_cny"], 2688)
            self.assertEqual("pending_baggage" in result, bool(args))
            self.assertEqual("pending_at_or_below_best_total" in result, bool(args))
            self.assertEqual("diagnostics" in result["platforms"]["tongcheng"], bool(args))
            self.assertEqual(result["platforms"]["tongcheng"]["pending_quotes"], 1)

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
                verified = RoundTripQuote(self.name, origin, destination, depart, self.price,
                                          flight_no="NH976", return_date=back, return_flight_no="NH975",
                                          baggage_offer=offer)
                if self.name == "tongcheng":
                    return [verified, replace(verified, price=2300, baggage_offer={
                        **offer, "inbound": {"confirmed": True, "rule": "无免费托运"}})]
                return [verified]

        class Ctrip(Crawler):
            name = "ctrip"
            price = 2755

        class Tongcheng(Crawler):
            name = "tongcheng"
            price = 2553

        calls = []
        cfg["routes"][0]["checked_baggage_required"] = False
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
        strict, raw = body.split("**原始票价", 1)
        self.assertNotIn("2,300", strict)
        self.assertIn("2,300", raw)
        self.assertIn("回程托运 无免费托运", raw)

    def test_notification_explains_partial_results_and_list_only_prices(self):
        route = Route("SHA", "上海", "OSA", "大阪", ["2027-02-15"], 2800,
                      "round_trip", ["2027-02-22"], True)
        depart, back = route.dates[0], route.return_dates[0]
        allowance = {"confirmed": True, "pieces": 2, "weight_kg": 23, "additional_fee_cny": 0}
        offer = {"itinerary_confirmed": True, "tax_inclusive_ticket_confirmed": True,
                 "cabin": "economy", "outbound": allowance, "inbound": allowance}
        verified = RoundTripQuote("ctrip", "SHA", "OSA", depart, 3034, flight_no="9C6575",
                                  return_date=back, return_flight_no="9C6576", baggage_offer=offer)
        no_free = replace(verified, baggage_offer={**offer, "inbound": {"rule": "无免费托运"}})
        unknown = replace(verified, price=2724, price_basis="selected_round_trip_tax",
                          baggage_offer={"fare_rule": "无免费托运行李"})
        list_only = replace(verified, price=2555, flight_no="NH976", return_flight_no="",
                            price_basis="product_round_trip_tax", baggage_offer={})
        results = {
            "ctrip": FetchResult("parse_error", [verified] * 3 + [no_free] * 2 + [unknown] + [list_only] * 3,
                                 "fare_baggage_rules_incomplete"),
            "tongcheng": FetchResult("error", evidence="tongcheng_list_http_406"),
            "fliggy": FetchResult("blocked", [replace(verified, platform="fliggy", price=2757)] * 4
                                  + [replace(no_free, platform="fliggy")] * 4,
                                  "fliggy_desktop_verification_required"),
        }
        notifier = Mock()
        report = compare_round_trips(route, depart, back, results)
        Alerter(logging.getLogger("notification-test"), notifier).report_joint_round_trip(route, report)
        _, body = notifier.send.call_args.args
        self.assertIn("NH976 · 列表往返起价 ¥2,555.00 · 往返航班未配对", body)
        self.assertIn("3 条是未配对航班的参考价，1 条托运待核实", body)
        self.assertIn("票面标注 无免费托运行李 · 两程托运范围待核实", body)
        self.assertIn("携程 · 已返回 9 条（部分托运规则未读全），"
                      "含两程托运已核实 3 条、托运待核实 1 条、仅参考价 3 条、需另购托运 2 条", body)
        self.assertIn("同程 · 采集失败（报价接口返回 HTTP 406），本轮无报价", body)
        self.assertIn("飞猪 · 已返回 8 条（本轮遇到验证），含两程托运已核实 4 条、需另购托运 4 条", body)

    def test_one_round_compares_verified_totals_and_preserves_incomplete_platforms(self):
        route = Route("SHA", "上海", "OSA", "大阪", ["2027-02-15"], 2800,
                      "round_trip", ["2027-02-22"], True)
        allowance = {"confirmed": True, "pieces": 2, "weight_kg": 23, "additional_fee_cny": 0}
        offer = {"itinerary_confirmed": True, "tax_inclusive_ticket_confirmed": True,
                 "cabin": "economy", "outbound": allowance, "inbound": allowance}
        ctrip = RoundTripQuote("ctrip", "SHA", "OSA", route.dates[0], 2553, flight_no="NH976",
                              return_date=route.return_dates[0], return_flight_no="NH416", baggage_offer=offer)
        tongcheng = replace(ctrip, platform="tongcheng", price=2949)
        paid_baggage = replace(tongcheng, price=2500, baggage_offer={
            **offer, "inbound": {**allowance, "additional_fee_cny": 25}})
        third = replace(tongcheng, platform="third", price=2553, baggage_offer=offer)
        no_free = {"confirmed": True, "rule": "无免费托运"}
        bare_fare = replace(tongcheng, price=2450, baggage_offer={
            **offer, "outbound": no_free, "inbound": {}})
        results = {
            "ctrip": FetchResult("ok", [ctrip]),
            "tongcheng": FetchResult("timeout", [
                tongcheng, paid_baggage, bare_fare, replace(tongcheng, price=2400, baggage_offer={})]),
            "third": FetchResult("ok", [third, replace(third, price=1, return_date="2027-02-23")]),
        }
        class Supported:
            def __init__(self, config, logger):
                self.round_trip_diagnostics = {"detail_fares_seen": 3}

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
        self.assertEqual(report["ranked_verified"][-1]["total_with_baggage_cny"], 2949)
        self.assertFalse(report["ranked_verified"][-1]["within_budget"])
        self.assertEqual(len(report["ranked_verified"]), 3)
        self.assertEqual([row["ticket_price_cny"] for row in report["pending_baggage"]], [2400])
        self.assertIsNone(report["pending_baggage"][0]["within_budget"])
        self.assertEqual(report["platforms"]["tongcheng"]["excluded_baggage_purchase_required"], 2)
        self.assertEqual([(row["ticket_price_cny"], row["checked_baggage_status"])
                          for row in report["ranked_ticket"][:3]],
                         [(2400, "unverified"), (2450, "purchase_required"),
                          (2500, "purchase_required")])
        self.assertTrue(report["itinerary"]["checked_baggage_included_in_fare"])
        self.assertEqual(report["pending_at_or_below_best_total"][0]["ticket_price_cny"], 2400)
        self.assertEqual(report["platforms"]["tongcheng"]["status"], "timeout")
        self.assertEqual(report["platforms"]["tongcheng"]["diagnostics"], {"detail_fares_seen": 3})
        self.assertEqual(report["platforms"]["third"]["rejected_quotes"], 1)
        self.assertEqual(report["platforms"]["future"]["status"], "unsupported")
