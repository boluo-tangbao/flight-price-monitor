"""Run the real scheduler against local counters only; no crawlers or network."""
import threading
import logging
import unittest
from unittest.mock import patch
from apscheduler.schedulers.background import BackgroundScheduler
from core.scheduler import run_scheduler


class SchedulerTests(unittest.TestCase):
    def assert_repeats(self, run_on_start):
        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        repeated = threading.Event()
        calls = []
        def job():
            calls.append(1)
            if len(calls) >= 2:
                repeated.set()
        try:
            with patch("core.scheduler.BlockingScheduler", return_value=scheduler):
                run_scheduler(job, interval_minutes=0.002, run_on_start=run_on_start)
            self.assertIsNotNone(scheduler.get_job("price_monitor").next_run_time)
            self.assertTrue(repeated.wait(3), "Periodic callbacks did not continue")
        finally:
            if scheduler.running:
                scheduler.shutdown()

    def test_periodic_runs_without_startup_query(self):
        self.assert_repeats(False)

    def test_periodic_runs_continue_after_startup_query(self):
        self.assert_repeats(True)

    def test_failed_callback_does_not_cancel_later_runs(self):
        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        recovered = threading.Event()
        calls = []
        def job():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("synthetic failure")
            recovered.set()
        try:
            with patch("core.scheduler.BlockingScheduler", return_value=scheduler), \
                    patch.object(logging.getLogger("jipiao"), "exception"):
                run_scheduler(job, interval_minutes=0.002, run_on_start=True)
            self.assertTrue(recovered.wait(3))
        finally:
            if scheduler.running:
                scheduler.shutdown()

    def test_jitter_enabled_without_startup_query_is_scheduled(self):
        scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
        try:
            with patch("core.scheduler.BlockingScheduler", return_value=scheduler):
                run_scheduler(lambda: None, interval_minutes=480, run_on_start=False, jitter_minutes=15)
            self.assertIsNotNone(scheduler.get_job("price_monitor").next_run_time)
        finally:
            if scheduler.running:
                scheduler.shutdown()
