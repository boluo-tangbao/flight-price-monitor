import json
import logging
import os
from contextlib import nullcontext
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core.baggage import baggage_total, lowest_round_trip_options
from core.models import RoundTripQuote
from crawlers.tongcheng_fare_details import (
    adult_checked_rule, fare_checked_baggage, fare_price, flight_pattern, read_details, select_pair,
)
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
        self.assertIs(select_pair([cheaper,target], ['NH976','NH975']), target)
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
        self.assertTrue(adult_checked_rule(text.replace('每人2件, 每件23KG', '不含免费托运'))['no_free'])
        self.assertEqual(adult_checked_rule('儿童\n托运\n每人2件, 每件23KG'), {})
        self.assertEqual(adult_checked_rule('成人\n托运行李额：总重不超过20KG')['weight_basis'], 'total')

    def test_inline_quota_requires_direction_before_it_counts(self):
        self.assertEqual(
            fare_checked_baggage('经济舱\n免费托运行李额20KG\n手提行李5KG'),
            ('免费托运行李额20KG', {}),
        )
        note, legs = fare_checked_baggage('往返两程各托运行李2×23KG')
        self.assertIn('23KG', note)
        self.assertEqual({key: (leg['pieces'], leg['weight_kg'], leg['weight_basis'])
                          for key, leg in legs.items()},
                         {'outbound': (2, 23, 'per_piece'), 'inbound': (2, 23, 'per_piece')})
        _, legs = fare_checked_baggage('去程：免费托运行李额度20kg\n返程：无免费托运行李')
        self.assertEqual(legs['outbound']['weight_kg'], 20)
        self.assertTrue(legs['inbound']['no_free'])
        self.assertEqual(fare_checked_baggage('随身行李7kg\n往返托运行李额20KG')[1], {})

    def test_card_badges_bind_later_baggage_lines_to_their_own_legs(self):
        _, legs = fare_checked_baggage(
            '经济舱\n去退改¥150起\n提供发票\n免费托运行李额20KG\n'
            '返\n退改¥240起\n提供行程单\n免费 托运 行李 额度 15公斤\n手提行李额7KG'
        )
        self.assertEqual({key: rule['weight_kg'] for key, rule in legs.items()},
                         {'outbound': 20, 'inbound': 15})
        _, legs = fare_checked_baggage(
            '去\n免费托运行李额20KG\n返：提供行程单\n手提行李额7KG'
        )
        self.assertEqual(set(legs), {'outbound'})

    @unittest.skipUnless(os.environ.get("RUN_TONGCHENG_OFFLINE_BROWSER_CHECK") == "1",
                         "opt-in local Edge fixture")
    def test_expanded_fares_preserve_inline_and_read_separate_direction_tabs(self):
        from playwright.sync_api import sync_playwright

        def card(number, stage):
            return (f'<div class="flight-item"><div class="flight-item-main"><span>{number}</span>'
                    f' <button onclick="openStage({stage})">选择</button></div><div class="offers"></div></div>')

        html = f'''<div id="out">{card("NH976", 1)}</div>
            <div id="in" hidden>{card("NH975", 2)}</div>
            <div class="v-popover_box" hidden style="position:fixed;right:30px;top:50px">
              <div>行李规定</div>
              <button class="crumb-item active" onclick="selectLeg(this, 0)">上海 - 大阪</button>
              <button class="crumb-item" onclick="selectLeg(this, 1)">大阪 - 上海</button>
              <div>成人</div><div>手提：每人1件, 每件7公斤</div>
              <div id="rule">托运：每人2件, 每件23公斤</div>
              <div>儿童</div><div>托运：每人1件, 每件10公斤</div>
            </div>'''
        script = '''const panel = document.querySelector('.v-popover_box');
            function openStage(stage) {
              if (stage === 1) {
                document.querySelector('#out').hidden = true;
                document.querySelector('#in').hidden = false;
              } else {
                document.querySelector('#in .offers').innerHTML = `
                  <div class="pro-item-box"><div>经济舱</div><span>免费托运行李额20KG</span>
                    <div>¥2650</div><div>含税总价</div><button>预订</button></div>
                  <div class="pro-item-box"><div>经济舱</div>
                    <span onmouseenter="panel.hidden = false">托运行李2*23KG</span>
                    <div>¥2755</div><div>含税总价</div><button>预订</button></div>`;
                document.querySelectorAll('.pro-item-box button').forEach(button => {
                  button.onclick = () => { document.body.dataset.booked = 'true'; };
                });
              }
            }
            function selectLeg(tab, inbound) {
              panel.querySelector('.active').classList.remove('active');
              tab.classList.add('active');
              document.querySelector('#rule').innerText = inbound
                ? '托运行李额：总重不超过20公斤'
                : '托运：每人2件, 每件23公斤';
            }'''
        fallback = [
            RoundTripQuote('tongcheng', 'SHA', 'OSA', '2027-02-15', price,
                           flight_no=outbound, return_flight_no=inbound, return_date='2027-02-22')
            for outbound, inbound, price in [('9C6575', '9C6576', 2480),
                                              ('NH0976', 'NH0974', 2610),
                                              ('NH0976', 'NH0975', 2633)]
        ]
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True,
                proxy={'server': 'http://127.0.0.1:9'}, args=['--disable-background-networking'])
            try:
                page = browser.new_page()
                page.route('**/*', lambda route: route.abort())
                page.set_content(html)
                page.add_script_tag(content=script)
                diagnostics = {}
                pending, verified = read_details(page, fallback, lambda *_: None, diagnostics)
                self.assertIsNone(baggage_total(pending))
                self.assertEqual(pending.baggage_offer['fare_rule'], '免费托运行李额20KG')
                self.assertEqual(verified.return_flight_no, 'NH0975')
                self.assertEqual(baggage_total(verified), 2755)
                self.assertEqual(verified.baggage_offer['outbound']['pieces'], 2)
                self.assertEqual(verified.baggage_offer['inbound']['weight_kg'], 20)
                self.assertEqual(verified.baggage_offer['inbound']['weight_basis'], 'total')
                self.assertEqual(diagnostics['detail_fares_with_card_rule'], 2)
                self.assertIsNone(page.locator('body').get_attribute('data-booked'))

                # The screenshot shows directional badges beside the refunds,
                # then the baggage amounts on later lines of the same fare.
                page = browser.new_page()
                page.route('**/*', lambda route: route.abort())
                page.set_content(html)
                page.add_script_tag(content=script.replace(
                    '<span onmouseenter="panel.hidden = false">托运行李2*23KG</span>',
                    '<div><span>去</span><span>退改¥150起</span></div><div>提供发票</div>'
                    '<div>免费托运行李额20KG</div>'
                    '<div><span>返</span><span>退改¥240起</span></div><div>提供行程单</div>'
                    '<div>免费托运行李额15KG</div>'))
                pending, verified = read_details(page, fallback, lambda *_: None, {})
                self.assertEqual(baggage_total(verified), 2755)
                self.assertEqual(verified.baggage_offer['outbound']['weight_kg'], 20)
                self.assertEqual(verified.baggage_offer['inbound']['weight_kg'], 15)
                self.assertEqual(verified.baggage_offer['inbound']['source'], 'fare')
                self.assertFalse(page.locator('.v-popover_box').is_visible())
            finally:
                browser.close()

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
