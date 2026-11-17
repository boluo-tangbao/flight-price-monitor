"""Health acceptance against a local ServerChan-compatible gateway; zero airline requests."""
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs
from unittest.mock import patch
from zoneinfo import ZoneInfo

from core.alerter import Alerter
from core.collection import FetchFailure, FetchResult
from core.health import HealthMonitor
from core.models import Route, RoundTripQuote
from core.notifier import ServerChanNotifier
from core.storage import PriceStorage
from main import make_job


class Gateway(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8")
        self.server.messages.append(parse_qs(body, keep_blank_values=True))
        response = self.server.responses.popleft() if self.server.responses else {"code": 0}
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def log_message(self, *args):
        pass


class HealthAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        cls.thread = threading.Thread(target=cls.gateway.serve_forever,
                                      kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.gateway.shutdown()
        cls.gateway.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.gateway.messages = []
        self.gateway.responses = deque()
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.storage = PriceStorage(str(Path(temp.name) / "health.sqlite"))
        self.logger = logging.getLogger("health-acceptance")
        self.notifier = ServerChanNotifier("SCT_LOCAL_TEST_NOT_A_REAL_KEY", self.logger)
        self.notifier.ENDPOINT_TPL = f"http://127.0.0.1:{self.gateway.server_port}/{{key}}.send"
        self.config = {"enabled": True, "failure_threshold": 3, "cooldown_hours": 6,
                       "quiet_start": "23:00", "quiet_end": "07:00"}
        self.health = HealthMonitor(self.storage, self.notifier, self.config)
        self.route = Route("SHA", "上海", "TYO", "东京", ["2026-12-01"],
                           trip_type="round_trip", return_dates=["2026-12-10"])
        self.now = self.at("2026-11-17 12:00")
        # Any accidental platform browser access is a hard failure.
        guard = patch("crawlers.base.sync_playwright", side_effect=AssertionError("Airline access forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    @staticmethod
    def at(value):
        return datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()

    def record(self, status, ret="2026-12-10", now=None):
        self.health.record("ctrip", self.route, "2026-12-01", ret,
                           FetchResult(status, evidence="synthetic_test"), 10,
                           self.now if now is None else now)

    def fail_three(self, now=None):
        for status in ("timeout", "blocked", "login_required"):
            self.record(status, now=now)

    def test_failure_threshold_three_and_payload(self):
        for status in ("timeout", "blocked"):
            self.record(status)
            self.health.notify(self.now)
        self.assertEqual(len(self.gateway.messages), 0)
        self.record("login_required")
        self.health.notify(self.now)
        self.assertEqual(len(self.gateway.messages), 1)
        message = self.gateway.messages[0]
        self.assertEqual(message["title"], ["机票采集状态提醒"])
        self.assertIn("连续失败 3", message["desp"][0])
        self.assertIn("2026-12-10", message["desp"][0])

    def test_repeated_failure_suppressed_then_cooldown_expires(self):
        self.fail_three()
        self.health.notify(self.now)
        self.record("blocked", now=self.now + 60)
        self.health.notify(self.now + 60)
        self.assertEqual(len(self.gateway.messages), 1)
        self.health.notify(self.now + 6 * 3600)
        self.assertEqual(len(self.gateway.messages), 2)

    def test_recovery_notified_once_and_state_cleared(self):
        self.fail_three()
        self.health.notify(self.now)
        self.record("ok")
        self.health.notify(self.now + 60)
        self.health.notify(self.now + 120)
        self.assertEqual(len(self.gateway.messages), 2)
        self.assertIn("恢复", self.gateway.messages[-1]["desp"][0])
        row = self.health.view()[0]
        self.assertEqual(row["consecutive_failures"], 0)
        self.assertEqual(row["incident_open"], 0)

    def test_midnight_quiet_window_and_exact_end_boundary(self):
        evening = self.at("2026-11-17 23:00")
        self.fail_three(now=evening)
        for clock in (evening, self.at("2026-11-18 00:01"), self.at("2026-11-18 06:59")):
            self.health.notify(clock)
        self.assertEqual(len(self.gateway.messages), 0)
        self.assertIsNone(self.health.view()[0]["last_alert"])
        self.health.notify(self.at("2026-11-18 07:00"))
        self.assertEqual(len(self.gateway.messages), 1)

    def test_recovery_during_quiet_hours_deferred_until_morning(self):
        self.fail_three()
        self.health.notify(self.now)
        evening = self.at("2026-11-17 23:30")
        self.record("ok", now=evening)
        self.health.notify(evening)
        self.assertEqual(len(self.gateway.messages), 1)
        self.assertEqual(self.health.view()[0]["incident_open"], 1)
        self.health.notify(self.at("2026-11-18 07:00"))
        self.assertIn("恢复", self.gateway.messages[-1]["desp"][0])

    def test_gateway_rejection_does_not_mark_delivered_and_can_retry(self):
        self.fail_three()
        self.gateway.responses.append({"code": 1, "message": "synthetic rejection"})
        self.health.notify(self.now)
        self.assertIsNone(self.health.view()[0]["last_alert"])
        self.assertEqual(self.health.view()[0]["incident_open"], 0)
        self.health.notify(self.now + 60)
        self.assertEqual(len(self.gateway.messages), 2)
        self.assertEqual(self.health.view()[0]["incident_open"], 1)

    def test_delivery_state_and_streak_survive_process_restart(self):
        self.fail_three()
        self.health.notify(self.now)
        restarted = HealthMonitor(self.storage, self.notifier, self.config)
        restarted.record("ctrip", self.route, "2026-12-01", "2026-12-10",
                         FetchResult("blocked"), now=self.now + 60)
        restarted.notify(self.now + 60)
        self.assertEqual(len(self.gateway.messages), 1)
        self.assertEqual(restarted.view()[0]["consecutive_failures"], 4)
        restarted.record("ctrip", self.route, "2026-12-01", "2026-12-10",
                         FetchResult("ok"), now=self.now + 120)
        restarted.notify(self.now + 120)
        self.assertEqual(len(self.gateway.messages), 2)

    def test_explicit_no_seats_is_not_a_failure(self):
        for _ in range(4):
            self.record("empty")
            self.health.notify(self.now)
        self.assertEqual(len(self.gateway.messages), 0)
        self.assertEqual(self.health.view()[0]["consecutive_failures"], 0)

    def test_unknown_empty_is_reported_honestly(self):
        for _ in range(3):
            self.record("unknown_empty")
        self.health.notify(self.now)
        self.assertIn("unknown_empty", self.gateway.messages[0]["desp"][0])

    def test_unsupported_and_skipped_do_not_alert(self):
        for status in ("unsupported", "skipped") * 3:
            self.record(status)
            self.health.notify(self.now)
        self.assertEqual(len(self.gateway.messages), 0)

    def test_dates_isolated_and_alarms_batched(self):
        for ret in ("2026-12-10", "2026-12-11"):
            for _ in range(3):
                self.record("blocked", ret=ret)
        self.health.notify(self.now)
        self.assertEqual(len(self.gateway.messages), 1)
        self.assertEqual(len(self.health.view()), 2)
        for ret in ("2026-12-10", "2026-12-11"):
            self.assertIn(ret, self.gateway.messages[0]["desp"][0])

    def test_health_disabled_does_not_send(self):
        self.health.config["enabled"] = False
        self.fail_three()
        self.health.notify(self.now)
        self.assertEqual(len(self.gateway.messages), 0)

    def test_main_job_pipeline_fault_and_recovery_without_platform_access(self):
        outcomes = deque(["timeout", "blocked", "login_required", "ok"])
        class FakeCrawler:
            name = "ctrip"
            def __init__(self, cfg, logger):
                self.logger = logger
            def fetch_round_trip(self, fc, tc, date, ret):
                status = outcomes.popleft()
                if status != "ok":
                    raise FetchFailure(status, "synthetic_test")
                return [RoundTripQuote("ctrip", fc, tc, date, 2143, return_date=ret)]
        cfg = {"platforms": ["ctrip"], "health": self.config,
               "routes": [{"from": "SHA", "to": "TYO", "dates": ["2026-12-01"],
                           "trip_type": "round_trip", "return_dates": ["2026-12-10"]}]}
        with patch.dict("main.REGISTRY", {"ctrip": FakeCrawler}), patch("core.health.time.time", return_value=self.now):
            job = make_job(cfg, self.logger, self.storage,
                           Alerter(self.logger, self.notifier, self.storage))
            for _ in range(4):
                job()
        self.assertEqual(len(self.gateway.messages), 2)
        self.assertIn("恢复", self.gateway.messages[-1]["desp"][0])
        with self.storage._conn() as conn:
            rows = conn.execute("SELECT status FROM collection_runs ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], ["timeout", "blocked", "login_required", "ok"])


if __name__ == "__main__":
    unittest.main()
