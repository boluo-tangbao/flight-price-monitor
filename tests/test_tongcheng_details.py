import json
import logging
from contextlib import nullcontext
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.baggage import baggage_total, lowest_round_trip_options
from core.models import RoundTripQuote
from crawlers.tongcheng_fare_details import adult_checked_rule, fare_price, flight_pattern, select_pair
from core.collection import FetchFailure
from crawlers.tongcheng_pc import TongchengDesktop
from crawlers.tongcheng_upstream.browser import BaseCrawler
from crawlers.tongcheng_upstream import challenge


class TongchengDetailTests(unittest.TestCase):
    def test_target_pair_does_not_follow_new_cheapest_unrelated_offer(self):
        cheaper = RoundTripQuote('tongcheng','SHA','OSA','2027-02-15',2515,
            flight_no='9C6575',return_flight_no='MM0079',return_date='2027-02-22')
        target = RoundTripQuote('tongcheng','SHA','OSA','2027-02-15',2633,
            flight_no='NH0976',return_flight_no='NH0975',return_date='2027-02-22')
        self.assertIs(select_pair([cheaper,target], ['NH0976','NH0975']), target)
        with self.assertRaises(FetchFailure): select_pair([cheaper], ['NH0976','NH0975'])

    def test_plain_tax_fare_requires_economy_and_no_ambiguous_addons(self):
        plain = '经济舱\n托运行李2*23KG\n不得退改\n提供行程单\n¥2755\n含税总价\n预订'
        self.assertEqual(fare_price(plain), 2755)
        for extra in ('\n优享礼包+¥140', '\n低价优品', '\n另付服务费', '\n¥100'):
            self.assertIsNone(fare_price(plain + extra))
        self.assertIsNone(fare_price(plain.replace('经济舱', '公务舱')))

    def test_adult_checked_is_independent_of_hand_and_child(self):
        text = '行李规定\n上海 - 大阪\n成人\n手提\n每人1件, 每件10KG\n托运\n每人2件, 每件23KG\n儿童\n托运\n每人1件, 每件10KG'
        rule = adult_checked_rule(text)
        self.assertEqual((rule['pieces'], rule['weight_kg'], rule['additional_fee_cny']), (2, 23, 0))
        self.assertEqual(adult_checked_rule(text.replace('每人2件, 每件23KG', '不含免费托运')), {})
        self.assertEqual(adult_checked_rule('儿童\n托运\n每人2件, 每件23KG'), {})

    def test_displayed_flight_number_padding_does_not_match_codeshare_suffix(self):
        pattern = flight_pattern('NH0975')
        self.assertIsNotNone(pattern.search('NH975 空客A320'))
        self.assertIsNone(pattern.search('NH9756 波音787'))

    def test_both_directions_required_for_budget(self):
        q = RoundTripQuote('tongcheng', 'SHA', 'OSA', '2027-02-15', 2755,
                          return_date='2027-02-22')
        rule = adult_checked_rule('成人\n托运\n每人2件, 每件23KG')
        q.baggage_offer = {'itinerary_confirmed': True, 'tax_inclusive_ticket_confirmed': True,
                           'outbound': rule}
        self.assertIsNone(baggage_total(q))
        q.baggage_offer['inbound'] = rule
        self.assertEqual(baggage_total(q), 2755)
        self.assertEqual(lowest_round_trip_options([q])[0][1], 2755)

    def test_reader_runs_before_original_context_closes_only_on_success(self):
        with tempfile.TemporaryDirectory() as root:
            challenge.unregister_handler()
            self.addCleanup(challenge.unregister_handler)
            crawler = SimpleNamespace(config={}, user_data_root=root, logger=logging.getLogger('tc-details'))
            worker = TongchengDesktop(crawler)
            ctx, page = Mock(), Mock()
            worker._last_page = page
            worker.detail_reader = Mock(return_value=['verified'])
            with patch.object(BaseCrawler, 'browser', return_value=nullcontext(ctx)):
                with worker.browser(): worker.last_outcome = 'blocked'
                worker.detail_reader.assert_not_called()
                with worker.browser(): worker.last_outcome = 'ok'
            worker.detail_reader.assert_called_once_with(page)
            self.assertEqual(worker.detail_quotes, ['verified'])
