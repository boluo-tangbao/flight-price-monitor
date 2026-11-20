import logging
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime
from zoneinfo import ZoneInfo

from core.collection import collect, FetchFailure, FetchResult
from core.health import HealthMonitor
from core.models import Route, FlightPrice, RoundTripQuote
from core.storage import PriceStorage
from core.alerter import Alerter
from crawlers.ctrip_roundtrip import ProductParser, build_url, fetch_round_trip
from main import build_routes, make_job

CARD = "东方航空\nMU523 A320\n09:00\n浦东机场\n13:00\n成田机场\n¥1,299起\n往返含税价\n选为去程"
DEP, RET = "2026-12-01", "2026-12-08"


class ParserTests(unittest.TestCase):
    def test_total_and_return_evidence(self):
        row = ProductParser._parse_rt_body("更多日期\n¥299低\n" + CARD, DEP, RET)[0]
        self.assertEqual(row["price"], 1299)
        self.assertEqual(row["backDate"], RET)
        self.assertEqual(row["basis"], "product_round_trip_tax")

    def test_oneway_price_is_not_roundtrip(self):
        self.assertEqual(ProductParser._parse_rt_body(CARD.replace("往返含税价", "单程"), DEP, RET), [])

    def test_missing_flight_is_rejected(self):
        self.assertEqual(ProductParser._parse_rt_body(CARD.replace("MU523 A320\n", ""), DEP, RET), [])

    def test_missing_time_is_rejected(self):
        self.assertEqual(ProductParser._parse_rt_body(CARD.replace("13:00\n", ""), DEP, RET), [])

    def test_duplicate_card_dedup(self):
        self.assertEqual(len(ProductParser._parse_rt_body(CARD + "\n" + CARD, DEP, RET)), 1)

    def test_airport_scope_not_silently_widened(self):
        with self.assertRaises(FetchFailure):
            build_url("PVG", "NRT", DEP, RET)

    def test_city_url_has_both_dates_and_tax(self):
        url = build_url("SHA", "TYO", DEP, RET)
        self.assertIn(DEP + "_" + RET, url)
        self.assertIn("containstax=1", url)

    def crawler(self, body=CARD):
        c = MagicMock(name="crawler", use_mobile=True, timeout_ms=35000)
        c.name = "ctrip"
        page = c.new_page.return_value
        page.url = build_url("SHA", "TYO", DEP, RET)
        page.inner_text.return_value = body
        page.goto.return_value.status = 200
        c.browser.return_value.__enter__.return_value.cookies.return_value = [
            {"name": "cticket", "value": "synthetic"}]
        return c

    def test_adapter_uses_total_and_restores_browser_mode(self):
        c = self.crawler()
        quote = fetch_round_trip(c, "SHA", "TYO", DEP, RET)[0]
        self.assertEqual((quote.price, quote.return_date), (1299, RET))
        self.assertTrue(c.use_mobile)
        self.assertEqual(c.new_page.return_value.goto.call_count, 2)
        self.assertEqual(c.new_page.return_value.goto.call_args_list[0].args[0], "https://flights.ctrip.com/")
        c.new_page.return_value.wait_for_timeout.assert_any_call(2500)

    def test_captcha_is_not_empty(self):
        c = self.crawler("请完成安全验证")
        with self.assertRaises(FetchFailure) as ctx:
            fetch_round_trip(c, "SHA", "TYO", DEP, RET)
        self.assertEqual(ctx.exception.status, "blocked")
        self.assertEqual(c.new_page.return_value.goto.call_count, 1)
        self.assertIn("stage=homepage", str(ctx.exception))
        self.assertTrue(c.use_mobile)

    def test_explicit_no_seats(self):
        c = self.crawler("暂无可售航班")
        with self.assertRaises(FetchFailure) as ctx:
            fetch_round_trip(c, "SHA", "TYO", DEP, RET)
        self.assertEqual(ctx.exception.status, "empty")

    def test_redirect_rejected(self):
        c = self.crawler()
        c.new_page.return_value.url = "https://flights.ctrip.com/"
        with self.assertRaises(FetchFailure) as ctx:
            fetch_round_trip(c, "SHA", "TYO", DEP, RET)
        self.assertEqual(ctx.exception.status, "error")

    def test_ambiguous_tax_basis_is_rejected(self):
        c = self.crawler(CARD.replace("往返含税价", "往返总价"))
        c.timeout_ms = 0
        with self.assertRaises(FetchFailure) as ctx:
            fetch_round_trip(c, "SHA", "TYO", DEP, RET)
        self.assertEqual(ctx.exception.status, "parse_error")


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = PriceStorage(str(Path(self.tmp.name) / "prices.db"))
        self.notifier = Mock()
        self.notifier.send.return_value = True
        self.monitor = HealthMonitor(self.storage, self.notifier,
                                     {"enabled": True, "failure_threshold": 2})
        self.route = Route("SHA", "上海", "TYO", "东京", [DEP])
        self.now = datetime(2026, 11, 17, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    def record(self, status, ret="", now=None):
        self.monitor.record("ctrip", self.route, DEP, ret, FetchResult(status),
                            20, self.now if now is None else now)

    def test_restart_persists_streak_and_last_success(self):
        self.record("ok")
        self.record("blocked", now=self.now + 1)
        m = HealthMonitor(self.storage)
        row = m.view()[0]
        self.assertEqual(row["consecutive_failures"], 1)
        self.assertEqual(row["last_success"], self.now)

    def test_return_dates_have_separate_state(self):
        self.record("blocked", RET)
        self.record("ok", "2026-12-09")
        self.assertEqual(len(self.monitor.view()), 2)

    def test_empty_is_healthy_unknown_empty_is_failure(self):
        self.record("unknown_empty")
        self.assertEqual(self.monitor.view()[0]["consecutive_failures"], 1)
        self.record("empty")
        self.assertEqual(self.monitor.view()[0]["consecutive_failures"], 0)

    def test_unsupported_does_not_increment_or_notify(self):
        self.record("unsupported")
        self.monitor.notify(self.now)
        self.assertEqual(self.monitor.view()[0]["consecutive_failures"], 0)
        self.notifier.send.assert_not_called()

    def test_failure_cooldown_and_recovery(self):
        self.record("blocked")
        self.record("blocked")
        self.monitor.notify(self.now)
        self.monitor.notify(self.now + 60)
        self.assertEqual(self.notifier.send.call_count, 1)
        self.record("ok")
        self.monitor.notify(self.now + 120)
        self.assertEqual(self.notifier.send.call_count, 2)
        self.assertEqual(self.monitor.view()[0]["incident_open"], 0)

    def test_failed_send_is_retried(self):
        self.record("error")
        self.record("error")
        self.notifier.send.return_value = False
        self.monitor.notify(self.now)
        self.assertIsNone(self.monitor.view()[0]["last_alert"])

    def test_inactive_old_route_does_not_notify(self):
        self.record("error")
        self.record("error")
        fresh = HealthMonitor(self.storage, self.notifier, {"enabled": True, "failure_threshold": 2})
        fresh.notify(self.now)
        self.notifier.send.assert_not_called()

    def test_quiet_hours_defer(self):
        self.record("error")
        self.record("error")
        self.monitor.config.update(quiet_start="11:00", quiet_end="13:00")
        self.monitor.notify(self.now)
        self.notifier.send.assert_not_called()
        self.monitor.notify(self.now + 7200)
        self.notifier.send.assert_called_once()

    def test_roundtrip_separate_from_existing_price_table(self):
        self.storage.save(FlightPrice("ctrip", "SHA", "TYO", DEP, 500))
        self.monitor.save_round_trip(RoundTripQuote("ctrip", "SHA", "TYO", DEP, 1299, return_date=RET))
        self.assertEqual(self.storage.last_lowest("SHA", "TYO", DEP)["price"], 500)
        with self.storage._conn() as c:
            self.assertEqual(c.execute("SELECT price FROM round_trip_quotes").fetchone()[0], 1299)

    def test_rt_alert_dedup_includes_return_date(self):
        a = Alerter(logging.getLogger("test"), self.notifier, self.storage)
        self.route.alert_threshold = 2000
        p = RoundTripQuote("ctrip", "SHA", "TYO", DEP, 1299, return_date=RET)
        a.check_round_trip(self.route, p)
        a.check_round_trip(self.route, p)
        p.return_date = "2026-12-09"
        a.check_round_trip(self.route, p)
        self.assertEqual(self.notifier.send.call_count, 2)


class IntegrationTests(unittest.TestCase):
    def crawler(self, fetch):
        c = type("Legacy", (), {})()
        c.logger = logging.getLogger("legacy-test")
        c.fetch = fetch
        return c

    def test_legacy_empty_is_unknown(self):
        self.assertEqual(collect(self.crawler(lambda *a: []), "SHA", "TYO", DEP).status, "unknown_empty")

    def test_swallowed_timeout_is_visible(self):
        def fetch(*args):
            logging.getLogger("legacy-test").error("Timeout 35000 exceeded")
            return []
        self.assertEqual(collect(self.crawler(fetch), "SHA", "TYO", DEP).status, "timeout")

    def test_exception_is_failure(self):
        def fetch(*args):
            raise RuntimeError("do not store raw secret exception text")
        result = collect(self.crawler(fetch), "SHA", "TYO", DEP)
        self.assertEqual((result.status, result.evidence), ("error", "RuntimeError"))

    def test_unsupported_rt_does_not_call_oneway(self):
        fetch = Mock()
        self.assertEqual(collect(self.crawler(fetch), "SHA", "TYO", DEP, RET).status, "unsupported")
        fetch.assert_not_called()

    def test_route_validation(self):
        with self.assertRaises(ValueError):
            build_routes({"routes": [{"from": "SHA", "to": "TYO", "dates": [DEP], "return_dates": [RET]}]})
        r = build_routes({"routes": [{"from": "SHA", "to": "TYO", "dates": [DEP],
                                     "trip_type": "round_trip", "return_dates": [RET]}]})[0]
        self.assertEqual(r.return_dates, [RET])

    def test_generator_preserves_existing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "config.yaml"
            out.write_text("user config", encoding="utf-8")
            subprocess.run([sys.executable, "scripts/build_config.py", "--from-code", "SHA",
                            "--from-name", "上海", "--to-code", "TYO", "--to-name", "东京",
                            "--dates", DEP, "--output", str(out)], check=True, capture_output=True)
            self.assertEqual(out.read_text(encoding="utf-8"), "user config")

    def test_generator_builds_true_rt_route(self):
        import yaml
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "config.yaml"
            subprocess.run([sys.executable, "scripts/build_config.py", "--from-code", "SHA",
                            "--from-name", "上海", "--to-code", "TYO", "--to-name", "东京",
                            "--dates", DEP, "--return-dates", RET, "--trip-type", "round_trip",
                            "--output", str(out)], check=True, capture_output=True)
            cfg = yaml.safe_load(out.read_text(encoding="utf-8"))
            self.assertEqual(len(cfg["routes"]), 1)
            self.assertEqual(build_routes(cfg)[0].return_dates, [RET])

    def test_readonly_status_export(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            db, out = Path(tmp) / "db.sqlite", Path(tmp) / "status.json"
            HealthMonitor(PriceStorage(str(db)))
            subprocess.run([sys.executable, "scripts/collection_status.py", "--db", str(db),
                            "--out", str(out)], check=True, capture_output=True)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")),
                             {"health": [], "round_trip_quotes": []})

    def test_job_persists_both_types_without_mixing(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = PriceStorage(str(Path(tmp) / "db.sqlite"))
            logger = logging.getLogger("job-test")
            a = Alerter(logger, storage=storage)
            cfg = {"platforms": ["ctrip"], "routes": [
                {"from": "SHA", "to": "TYO", "dates": [DEP]},
                {"from": "SHA", "to": "TYO", "dates": [DEP], "trip_type": "round_trip", "return_dates": [RET]}]}
            class Fake:
                name = "ctrip"
                def __init__(self, cfg, log): self.logger = log
                def fetch(self, *args): return [FlightPrice("ctrip", "SHA", "TYO", DEP, 500)]
                def fetch_round_trip(self, *args):
                    return [RoundTripQuote("ctrip", "SHA", "TYO", DEP, 1299, return_date=RET)]
            with patch.dict("main.REGISTRY", {"ctrip": Fake}):
                make_job(cfg, logger, storage, a)()
            with storage._conn() as c:
                self.assertEqual(c.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0], 2)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM round_trip_quotes").fetchone()[0], 1)
                self.assertEqual(c.execute("SELECT COUNT(*) FROM flight_prices").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
