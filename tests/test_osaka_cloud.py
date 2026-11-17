import logging
from pathlib import Path
import unittest
from unittest.mock import Mock
import yaml
from core.models import RoundTripQuote, Route
from core.baggage import baggage_total
from core.alerter import Alerter
from main import build_routes


class OsakaCloudTests(unittest.TestCase):
    def quote(self, ticket=2500, fee=100):
        return RoundTripQuote("ctrip", "SHA", "OSA", "2027-02-15", ticket,
                              return_date="2027-02-22", baggage_offer={
            "itinerary_confirmed": True, "tax_inclusive_ticket_confirmed": True,
            "outbound": {"confirmed": True, "weight_kg": 10, "additional_fee_cny": fee},
            "inbound": {"confirmed": True, "pieces": 1, "additional_fee_cny": fee}})

    def test_paid_baggage_is_added_for_both_legs(self):
        self.assertEqual(baggage_total(self.quote()), 2700)

    def test_unknown_baggage_is_never_zero_fee(self):
        q = self.quote()
        del q.baggage_offer["inbound"]["additional_fee_cny"]
        self.assertIsNone(baggage_total(q))

    def test_no_checked_allowance_is_rejected(self):
        q = self.quote()
        q.baggage_offer["inbound"]["pieces"] = 0
        self.assertIsNone(baggage_total(q))

    def test_unselected_combination_is_rejected(self):
        q = self.quote()
        q.baggage_offer["itinerary_confirmed"] = False
        self.assertIsNone(baggage_total(q))

    def test_baggage_unknown_does_not_send_price_alert(self):
        notifier = Mock()
        route = Route("SHA", "上海", "OSA", "大阪", ["2027-02-15"],
                      2800, "round_trip", ["2027-02-22"], True)
        q = self.quote()
        q.baggage_offer = {}
        Alerter(logging.getLogger("test"), notifier, Mock()).check_round_trip(route, q)
        notifier.send.assert_not_called()

    def test_paid_baggage_total_over_budget_does_not_alert(self):
        notifier, storage = Mock(), Mock()
        route = Route("SHA", "上海", "OSA", "大阪", ["2027-02-15"],
                      2800, "round_trip", ["2027-02-22"], True)
        Alerter(logging.getLogger("test"), notifier, storage).check_round_trip(route, self.quote(2700, 100))
        notifier.send.assert_not_called()

    def test_exact_budget_alert_is_baggage_inclusive(self):
        notifier, storage = Mock(), Mock()
        storage.get_alert_state.return_value = None
        route = Route("SHA", "上海", "OSA", "大阪", ["2027-02-15"],
                      2800, "round_trip", ["2027-02-22"], True)
        Alerter(logging.getLogger("test"), notifier, storage).check_round_trip(route, self.quote(2600, 100))
        self.assertIn("2800", notifier.send.call_args.args[0])
        self.assertIn("两程托运", notifier.send.call_args.args[0])

    def test_formal_route_and_dates(self):
        cfg = yaml.safe_load(Path("config.monitor.yaml").read_text(encoding="utf-8"))
        r = build_routes(cfg)[0]
        self.assertEqual((r.from_code, r.to_code, r.dates, r.return_dates, r.alert_threshold),
                         ("SHA", "OSA", ["2027-02-15"], ["2027-02-22"], 2800))
        self.assertTrue(r.checked_baggage_required)

    def test_cloud_schedule_is_preserved(self):
        workflow = Path(".github/workflows/monitor.yml").read_text(encoding="utf-8")
        self.assertIn("25 8,13,19,0 * * *", workflow)
        self.assertIn("Asia/Shanghai", workflow)
        self.assertIn("FLIGHT_MONITOR_ENABLED", workflow)
        self.assertNotIn("2026-12-01", workflow)
