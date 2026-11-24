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
    _panel_baggage, _table_baggage, adult_checked_rule, fare_checked_baggage,
    fare_price, flight_pattern, read_details, read_fare_inventory, select_pair,
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

    def test_checked_baggage_wording_from_screenshots(self):
        _, legs = fare_checked_baggage('去：每人可托运20KG\n返：无托运行李,手提行李1件每件7KG')
        self.assertEqual(legs['outbound']['weight_kg'], 20)
        self.assertTrue(legs['inbound']['no_free'])
        note, legs = fare_checked_baggage('托运行李2*23KG')
        self.assertEqual(note, '托运行李2*23KG')
        self.assertEqual(legs, {})
        self.assertEqual(
            fare_checked_baggage('托运行李1*20KG 部分行程退票¥1192起,部分行程改期¥1122起'),
            ('托运行李1*20KG', {}),
        )

    def test_baggage_table_uses_checked_cells_and_preserves_direction_scope(self):
        rows = [
            ['类别', '去程\n上海 - 大阪', '返程\n大阪 - 上海'],
            ['手提', '5KG', '7KG'],
            ['托运', '20KG/人', '免费2件, 每件23KG'],
        ]
        both = _table_baggage(rows)
        self.assertEqual(both['outbound']['weight_kg'], 20)
        self.assertEqual((both['inbound']['pieces'], both['inbound']['weight_kg']), (2, 23))
        vertical = _table_baggage([['方向', '手提', '托运'], ['去', '5kg', '20kg'], ['返', '7kg', '0kg']])
        self.assertEqual(vertical['outbound']['weight_kg'], 20)
        self.assertTrue(vertical['inbound']['no_free'])
        scoped = [['类别', '成人', '儿童'], ['托运', '20KG', '10KG']]
        self.assertEqual(_table_baggage(scoped), {})
        self.assertEqual(_table_baggage(scoped, 'inbound')['inbound']['weight_kg'], 20)
        self.assertEqual(_table_baggage([['类别', '儿童'], ['托运', '10KG']], 'outbound'), {})

    @unittest.skipUnless(os.environ.get("RUN_TONGCHENG_OFFLINE_BROWSER_CHECK") == "1",
                         "opt-in local Edge fixture")
    def test_clicking_baggage_link_reads_the_new_table_not_an_existing_one(self):
        from playwright.sync_api import sync_playwright
        import time

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True,
                proxy={'server': 'http://127.0.0.1:9'}, args=['--disable-background-networking'])
            try:
                page = browser.new_page()
                page.route('**/*', lambda route: route.abort())
                page.set_content('''
                    <table><tr><th>项目</th><th>去</th><th>返</th></tr>
                    <tr><td>托运</td><td>99kg</td><td>99kg</td></tr></table>
                    <div class="pro-item-box">
                      <button onclick="document.body.dataset.purchased='true'">加购托运行李额</button>
                      <a href="#rules" onclick="document.querySelector('#rules').hidden=false; return false">
                        <span>查看行李额</span></a>
                    </div>
                    <table id="rules" hidden>
                      <tr><th>项目</th><th>去</th><th>返</th></tr>
                      <tr><td>手提</td><td>5kg</td><td>7kg</td></tr>
                      <tr><td>托运</td><td>20kg</td><td>15kg</td></tr>
                    </table>
                ''')

                def wait(read, seconds):
                    deadline = time.monotonic() + seconds
                    while True:
                        result = read()
                        if result or time.monotonic() >= deadline:
                            return result
                        page.wait_for_timeout(50)

                diagnostics = {}
                legs = _panel_baggage(page, page.locator('.pro-item-box'), wait, diagnostics, lambda *_: None)
                self.assertEqual({key: value['weight_kg'] for key, value in legs.items()},
                                 {'outbound': 20, 'inbound': 15})
                self.assertEqual(legs['inbound']['source'], 'table')
                self.assertEqual(diagnostics['detail_baggage_tables'], 1)
                self.assertIsNone(page.locator('body').get_attribute('data-purchased'))

                page.set_content('''
                    <div class="pro-item-box">
                      <a href="#rules" onmouseenter="document.querySelector('#rules').hidden=false"
                         onclick="document.querySelector('#inbound').innerText='15kg'; return false">
                        行李额</a>
                    </div>
                    <div id="rules" class="v-popover_box" hidden>
                      <table><tr><th>行李规定</th><th>去</th><th>返</th></tr>
                        <tr><td>托运</td><td>20kg</td><td id="inbound">查看详情</td></tr>
                      </table>
                    </div>
                ''')
                diagnostics = {}
                legs = _panel_baggage(page, page.locator('.pro-item-box'), wait, diagnostics, lambda *_: None)
                self.assertEqual({key: rule['weight_kg'] for key, rule in legs.items()},
                                 {'outbound': 20, 'inbound': 15})
                self.assertEqual(diagnostics['detail_baggage_hovers'], 1)
                self.assertEqual(diagnostics['detail_baggage_clicks'], 1)
                self.assertEqual(diagnostics['detail_baggage_surfaces'], 2)

                page.set_content('''
                    <div class="pro-item-box">
                      <span role="link">行李规则</span>
                      <span role="link">查看行李额</span>
                      <span onmouseenter="document.querySelector('#rules').hidden=false"
                            onclick="document.body.dataset.textClicked='true'">托运行李1*20KG</span>
                    </div>
                    <table id="rules" hidden>
                      <tr><th>项目</th><th>去</th><th>返</th></tr>
                      <tr><td>托运</td><td>20kg</td><td>15kg</td></tr>
                    </table>
                ''')
                diagnostics = {}
                legs = _panel_baggage(page, page.locator('.pro-item-box'), wait, diagnostics, lambda *_: None)
                self.assertEqual({key: rule['weight_kg'] for key, rule in legs.items()},
                                 {'outbound': 20, 'inbound': 15})
                self.assertEqual(diagnostics['detail_baggage_hovers'], 3)
                self.assertIsNone(page.locator('body').get_attribute('data-text-clicked'))

                context = browser.new_context()
                def serve(route):
                    document = {
                        '/fixture': '<div class="pro-item-box"><a href="/rules">行李额</a></div>',
                        '/rules': ('<table><tr><th>类别</th><th>去程</th><th>回程</th></tr>'
                                   '<tr><td>托运</td><td colspan="2">23KG</td></tr></table>'),
                    }.get(route.request.url.removeprefix('https://www.ly.com'))
                    if document:
                        route.fulfill(body=document, content_type='text/html; charset=utf-8')
                    else:
                        route.abort()

                context.route('**/*', serve)
                page = context.new_page()
                page.goto('https://www.ly.com/fixture')
                legs = _panel_baggage(page, page.locator('.pro-item-box'), wait, {}, lambda *_: None)
                self.assertEqual({key: value['weight_kg'] for key, value in legs.items()},
                                 {'outbound': 23, 'inbound': 23})
                self.assertEqual(len(context.pages), 1)
            finally:
                browser.close()

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
                document.querySelector('#in button').innerText = '收起';
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
            for outbound, inbound, price in [
                ('9C6575', '9C6576', 2480),
                *(('NH0976', f'NH{970 + index}', 2600 + index)
                  for index in range(12) if index != 5),
                ('NH0976', 'NH0975', 2633),
            ]
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
                self.assertGreater(diagnostics['detail_return_quote_candidates'], 8)
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
                bare_fares = ''.join(
                    '<div class="pro-item-box"><div>经济舱</div>'
                    '<div>去：无托运行李</div><div>返：无托运行李</div>'
                    f'<div>¥{price}</div><div>含税总价</div><button>预订</button></div>'
                    for price in (2580, 2600, 2640)
                )
                page.add_script_tag(content=script.replace(
                    "document.querySelector('#in').hidden = false;",
                    "document.querySelector('#in').hidden = false; openStage(2);").replace(
                    '<div class="pro-item-box">', bare_fares + '<div class="pro-item-box">', 1).replace(
                    '<span onmouseenter="panel.hidden = false">托运行李2*23KG</span>',
                    '<div><span>去</span><span>退改¥150起</span></div><div>提供发票</div>'
                    '<div>免费托运行李额20KG</div>'
                    '<div><span>返</span><span>退改¥240起</span></div><div>提供行程单</div>'
                    '<div>免费托运行李额15KG</div>'))
                diagnostics = {}
                *pending, verified = read_details(page, fallback[:-1], lambda *_: None, diagnostics,
                                                  current_quotes=lambda: fallback[-1:])
                self.assertTrue(diagnostics['detail_return_already_expanded'])
                self.assertEqual(len(pending), 4)
                self.assertTrue(all(baggage_total(quote) is None for quote in pending))
                self.assertEqual(baggage_total(verified), 2755)
                self.assertEqual(verified.baggage_offer['outbound']['weight_kg'], 20)
                self.assertEqual(verified.baggage_offer['inbound']['weight_kg'], 15)
                self.assertEqual(verified.baggage_offer['inbound']['source'], 'fare')
                self.assertFalse(page.locator('.v-popover_box').is_visible())

                # A lower fare shows no checked baggage, while the higher fare
                # has its own allowance and a table opened by hovering.
                page = browser.new_page()
                page.route('**/*', lambda route: route.abort())
                page.set_content(
                    f'<div id="out">{card("9C6565", 1)}</div>'
                    f'<div id="in" hidden>{card("MM079", 2)}</div>'
                    '<div class="v-popover_box" hidden><table>'
                    '<tr><th>行李规定</th><th>去程</th><th>返程</th></tr>'
                    '<tr><td>托运</td><td>1*20KG</td><td>1*20KG</td></tr>'
                    '</table></div>')
                page.add_script_tag(content=script.replace('免费托运行李额20KG', '无免费托运行李额')
                    .replace('¥2650', '¥2495').replace('¥2755', '¥2949')
                    .replace('托运行李2*23KG</span>',
                             '托运行李1*20KG</span> <span>部分行程退票¥1192起</span>'))
                pair = RoundTripQuote('tongcheng', 'SHA', 'OSA', '2027-02-15', 2495,
                                      flight_no='9C6565', return_flight_no='MM0079',
                                      return_date='2027-02-22')
                bare, included = read_details(page, [pair], lambda *_: None, {})
                self.assertEqual(bare.price, 2495)
                self.assertIsNone(baggage_total(bare))
                self.assertEqual(baggage_total(included), 2949)
                self.assertEqual(included.baggage_offer['fare_rule'], '托运行李1*20KG')
                for leg in ('outbound', 'inbound'):
                    allowance = included.baggage_offer[leg]
                    self.assertEqual((allowance['pieces'], allowance['weight_kg']), (1, 20))

                # A real fare list with no usable economy price must report a
                # fare-selection problem rather than a failed baggage parse.
                page = browser.new_page()
                page.route('**/*', lambda route: route.abort())
                page.set_content(html)
                page.add_script_tag(content=script.replace('含税总价', '票价起'))
                diagnostics = {}
                with self.assertRaises(FetchFailure) as error:
                    read_details(page, fallback, lambda *_: None, diagnostics)
                self.assertEqual(str(error.exception), 'tongcheng_no_eligible_economy_fare')
                self.assertEqual(diagnostics['detail_fare_rejections'], {'no_tax_total_marker': 2})
                self.assertEqual(diagnostics['detail_stage'], 'fare_cards')
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

    @unittest.skipUnless(os.environ.get("RUN_TONGCHENG_OFFLINE_BROWSER_CHECK") == "1",
                         "opt-in local Edge fixture")
    def test_fare_inventory_can_use_page_without_api_and_keeps_non_ranking_fares(self):
        from playwright.sync_api import sync_playwright
        from crawlers.tongcheng_roundtrip import search_url

        route = ('SHA', 'OSA', '2027-02-15', '2027-02-22')
        url = search_url(*route)
        html = '''
            <div id="view"></div>
            <script>
              const view = document.querySelector('#view');
              const card = (flight, label, action, price, fares = '') =>
                `<div class="flight-item"><div class="flight-item-main">
                    <span>${flight}</span> <span>空客 A320</span> <span>¥${price}起</span>
                    <button onclick="${action}">${label}</button></div>${fares}</div>`;
              function outbound() {
                view.innerHTML = '<h1>选择去程：上海-大阪</h1>' +
                  card('9C6575', '选择', "showReturns('9C6575')", 2540) +
                  card('9C6565', '选择', "showReturns('9C6565')", 2480);
              }
              function fare(cabin, price) {
                return `<div class="pro-item-box"><div>${cabin}</div>
                  <div>¥</div><div>${price}</div><div>含税总价</div>
                  <button onclick="document.body.dataset.booked='yes'">预订</button></div>`;
              }
              function showReturns(out, chosen = 0) {
                view.innerHTML = '<h1>选择返程：大阪-上海</h1>' +
                  '<button onclick="outbound()">重选去程</button>';
                for (let n = 87; n >= 79; n--) {
                  const tabs = n === 79 ? `
                    <button onclick="document.body.dataset.economyClicked='yes'; document.querySelector('.offers').innerHTML = fare('经济舱', 2495)">经济舱 ¥2495起</button>
                    <button onclick="document.querySelector('.offers').innerHTML = fare('尊享飞', 3100)">尊享飞 ¥3100起</button>` : '';
                  const offers = chosen === n ? tabs + '<div class="offers">' +
                    fare('经济舱', n === 79 ? 2495 : 2440 + n) + '</div>' : '';
                  view.innerHTML += card('MM0' + n, chosen === n ? '收起' : '选择',
                    `showReturns('${out}', ${n})`, 2400 + n, offers);
                }
              }
              outbound();
            </script>
        '''

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True,
                proxy={'server': 'http://127.0.0.1:9'}, args=['--disable-background-networking'])
            try:
                page = browser.new_page()
                def serve(request):
                    if request.request.resource_type == 'document':
                        request.fulfill(body=html, content_type='text/html; charset=utf-8')
                    else:
                        request.abort()

                page.route('**/*', serve)
                page.goto(url)
                diagnostics = {}
                quotes = read_fare_inventory(page, (*route, url), [], lambda *_: None, diagnostics)
                self.assertEqual(diagnostics['detail_selection_basis'], 'visible_flight_cards')
                self.assertEqual(len(quotes), 8)
                self.assertEqual(diagnostics['detail_pairs_scanned'],
                                 [['9C6565', f'MM{number}'] for number in range(79, 87)])
                self.assertEqual(len(diagnostics['detail_fare_inventory']), 9)
                self.assertTrue(all(baggage_total(quote) is None for quote in quotes))
                self.assertEqual(diagnostics['detail_lowest_display_fares'][0]['display_price_cny'], 2495)
                premium = next(row for row in diagnostics['detail_fare_inventory'] if row['cabin'] == '尊享飞')
                self.assertEqual(premium['display_price_cny'], 3100)
                self.assertEqual(premium['price_review'], 'non_standard_fare_tab')
                self.assertIsNone(page.locator('body').get_attribute('data-booked'))
                self.assertIsNone(page.locator('body').get_attribute('data-economy-clicked'))

                page.goto(url)
                listed = [
                    RoundTripQuote('tongcheng', *route[:3], price, flight_no=out, return_date=route[3],
                                   return_flight_no=inward)
                    for price, out, inward in [
                        (2495, '9C6581', 'MM0079'), (2500, '9C6575', 'MM0080'), (2600, '9C6565', 'MM0081')]
                ]
                diagnostics = {}
                quotes = read_fare_inventory(page, (*route, url), listed, lambda *_: None, diagnostics, pair_limit=2)
                self.assertEqual(diagnostics['detail_pairs_scanned'], [['9C6575', 'MM80'], ['9C6565', 'MM81']])
                self.assertEqual([quote.return_flight_no for quote in quotes], ['MM0080', 'MM0081'])
                self.assertEqual(diagnostics['detail_initial_list_leaders'][0], {
                    'flight_no': '9C6581', 'return_flight_no': 'MM79',
                    'price_cny': 2495, 'outbound_card_matched': False})

                with self.subTest("delayed departure list after reselect"):
                    page.goto(url)
                    page.evaluate("""() => {
                        const renderOutbound = window.outbound;
                        window.outbound = () => {
                            document.querySelector('h1').remove();
                            document.querySelector('button[onclick="outbound()"]').remove();
                            setTimeout(renderOutbound, 1200);
                        };
                    }""")
                    diagnostics = {}
                    read_fare_inventory(page, (*route, url), listed, lambda *_: None, diagnostics, pair_limit=2)
                    self.assertEqual(diagnostics['detail_pairs_scanned'],
                                     [['9C6575', 'MM80'], ['9C6565', 'MM81']])

                with self.subTest("a planned return appears after the first card"):
                    page.goto(url)
                    page.evaluate("""() => {
                        const renderReturns = window.showReturns;
                        window.showReturns = (out, chosen) => {
                            renderReturns(out, chosen);
                            if (chosen) return;
                            const delayedCard = [...document.querySelectorAll('.flight-item')]
                                .find(card => card.innerText.includes('MM081'));
                            delayedCard.hidden = true;
                            setTimeout(() => { delayedCard.hidden = false; }, 1200);
                        };
                    }""")
                    delayed = RoundTripQuote('tongcheng', *route[:3], 2600, flight_no='9C6575',
                                             return_date=route[3], return_flight_no='MM0081')
                    diagnostics = {}
                    read_fare_inventory(page, (*route, url), [listed[1], delayed], lambda *_: None,
                                        diagnostics, pair_limit=2)
                    self.assertEqual(diagnostics['detail_pairs_scanned'],
                                     [['9C6575', 'MM80'], ['9C6575', 'MM81']])

                page.goto(url)
                page.add_style_tag(content='button[onclick="outbound()"] { display: none }')
                diagnostics = {}
                read_fare_inventory(page, (*route, url), listed, lambda *_: None, diagnostics, pair_limit=2)
                self.assertEqual(diagnostics['detail_pairs_scanned'], [['9C6575', 'MM80'], ['9C6575', 'MM79']])
                self.assertEqual(diagnostics['detail_coverage_limit'], 'outbound_reselect_unavailable')

                page.goto(url)
                page.evaluate("""() => {
                    const spring = [...document.querySelectorAll('.flight-item')]
                        .find(card => card.innerText.includes('9C6565'));
                    spring.hidden = true;
                    setTimeout(() => { spring.hidden = false; }, 1400);
                }""")
                diagnostics = {}
                quotes = read_fare_inventory(page, (*route, url), [], lambda *_: None, diagnostics,
                                             preferred_pair=('9C6565', 'MM0079'))
                self.assertEqual(diagnostics['detail_pairs_scanned'], [['9C6565', 'MM79']])
                self.assertEqual([quote.price for quote in quotes], [2495])

                page.goto(url)
                page.evaluate("""() => {
                    const spring = [...document.querySelectorAll('.flight-item')]
                        .find(card => card.innerText.includes('9C6565'));
                    const select = spring.querySelector('button');
                    const flightBtn = document.createElement('div');
                    flightBtn.className = 'flight-btn';
                    flightBtn.innerHTML = '<span>余4张</span> 选择';
                    flightBtn.setAttribute('onclick', select.getAttribute('onclick'));
                    select.replaceWith(flightBtn);
                    const render = window.showReturns;
                    window.showReturns = (out, chosen) => {
                        render(out, chosen);
                        document.querySelector('h1').textContent = '选择返程 大阪-上海';
                        for (const header of document.querySelectorAll('.flight-item-main')) {
                            const wrapper = document.createElement('div');
                            header.before(wrapper);
                            wrapper.append(header);
                            const next = header.querySelector('button');
                            if (next.textContent === '选择') {
                                next.classList.add('flight-btn');
                                next.innerHTML = '<span>余3张</span> 查看票价';
                            }
                            wrapper.after(next);
                        }
                        if (chosen === 79) {
                            const economyTab = [...document.querySelectorAll('button')]
                                .find(button => button.textContent.startsWith('经济舱 ¥'));
                            economyTab.textContent = '经济舱 ¥2688起';
                            document.querySelector('.offers').innerHTML =
                              window.fare('经济舱', 2688).replace('¥</div>',
                                '去：无免费托运行李额</div><div>返：无免费托运行李额</div><div>¥</div>') +
                              window.fare('经济舱', 2949).replace('¥</div>',
                                '去：托运行李1*20KG</div><div>返：托运行李1*20KG</div><div>¥</div>');
                        }
                    };
                }""")
                diagnostics = {}
                quotes = read_fare_inventory(page, (*route, url), [], lambda *_: None, diagnostics,
                                             preferred_pair=('9C6565', 'MM0079'))
                self.assertEqual(diagnostics['detail_return_transitions'][0]['status'], 'return_list_ready')
                self.assertEqual([(quote.price, baggage_total(quote)) for quote in quotes],
                                 [(2688, None), (2949, 2949)])
                self.assertEqual(quotes[-1].baggage_offer['outbound']['weight_kg'], 20)
                self.assertEqual(quotes[-1].baggage_offer['inbound']['weight_kg'], 20)
                self.assertIsNone(page.locator('body').get_attribute('data-booked'))
            finally:
                browser.close()

    def test_reader_can_continue_when_upstream_list_parsing_fails(self):
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
                with worker.browser(): worker.last_outcome = 'error'
            worker.detail_reader.assert_called_once_with(page)
            self.assertEqual(worker.detail_quotes, ['verified'])
