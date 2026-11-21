from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from core.baggage import baggage_total
from core.collection import FetchFailure
from crawlers.tongcheng_roundtrip import check_access, fetch_round_trip, payload_quotes, search_url


class TongchengRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.trip = ("SHA", "OSA", "2027-02-15", "2027-02-22")
        self.url = search_url(*self.trip)

    def test_url_contains_one_round_trip_and_both_dates(self):
        para = parse_qs(urlsplit(self.url).query)["para"][0]
        self.assertEqual(para.split("*")[:6], [*self.trip, "RT", "1_0_0"])

    def test_only_matching_pairs_with_explicit_tax_total_are_parsed(self):
        payload = {"data": {"res": [
            {"lng": "NH976_20270215|NH973_20270222", "tp": 2450, "avg": 900},
            {"lng": "NH976_20270216|NH973_20270222", "tp": 500},
            {"lng": "NH976_20270215|NH973_20270223", "tp": 500},
            {"lng": "NH976_20270215|NH973_20270222", "avg": 900},
            {"lng": "NH976_20270215|NH973_20270222", "tp": "NaN"},
            {"lng": "NH976_20270215", "tp": 600},
        ]}}
        rows = payload_quotes(payload, *self.trip, self.url)
        self.assertEqual([(q.flight_no, q.return_flight_no, q.price) for q in rows],
                         [("NH976", "NH973", 2450)])
        self.assertIsNone(baggage_total(rows[0]))

    def test_login_and_access_denial_have_distinct_statuses(self):
        check_access(self.url, "登录/注册", 200)
        for url, text, status, outcome in (
            ("https://passport.ly.com/login", "", 200, "login_required"),
            (self.url, "请先登录", 200, "login_required"),
            (self.url, "请拖动滑块", 200, "blocked"),
            (self.url, "", 429, "blocked"),
        ):
            with self.subTest(outcome=outcome), self.assertRaises(FetchFailure) as error:
                check_access(url, text, status)
            self.assertEqual(error.exception.status, outcome)

