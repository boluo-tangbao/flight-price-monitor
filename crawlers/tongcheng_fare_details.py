"""Inspect selected Tongcheng fare cards and both direction tabs; never book."""
from dataclasses import replace
import json
import re
import time

from playwright.sync_api import TimeoutError as BrowserTimeout
from core.baggage import baggage_total
from core.collection import FetchFailure


def flight_pattern(number):
    match = re.fullmatch(r'([A-Z0-9]{2})(\d{1,4})', number)
    if not match:
        raise ValueError('Invalid flight number')
    return re.compile(r'(?<![A-Z0-9])' + re.escape(match[1]) + r'0*' + str(int(match[2])) + r'(?![A-Z0-9])')


def fare_price(text):
    """Only a single explicit tax-inclusive economy fare without add-on ambiguity."""
    if '经济舱' not in text or '含税总价' not in text:
        return None
    if re.search(r'礼包|套餐|组合险|损失险|低价优品|另付|服务费|优惠券|返现', text):
        return None
    prices = re.findall(r'^¥\s*([\d,]+(?:\.\d{1,2})?)\s*$', text, re.M)
    if len(prices) != 1:
        return None
    value = float(prices[0].replace(',', ''))
    return value if 100 <= value <= 200000 else None


def adult_checked_rule(text):
    """Read the adult checked allowance, excluding hand baggage and child rules."""
    adult = re.search(r'^成人\s*\n(.*?)(?=^儿童\s*$|^婴儿\s*$|\Z)', text, re.M | re.S)
    if not adult:
        return {}
    checked = re.search(r'^托运\s*\n([^\n]+)', adult[1], re.M)
    if not checked or re.search(r'不含|无免费|另购|收费|自费|付费|待确认', checked[1]):
        return {}
    pieces = re.search(r'每人\s*(\d+)\s*件', checked[1])
    weight = re.search(r'每件\s*(\d+(?:\.\d+)?)\s*(?:KG|公斤)', checked[1], re.I)
    if not pieces or not weight or int(pieces[1]) <= 0 or float(weight[1]) <= 0:
        return {}
    return {'confirmed': True, 'pieces': int(pieces[1]), 'weight_kg': float(weight[1]),
            'additional_fee_cny': 0, 'rule': checked[1].strip()}


def select_pair(fallback, preferred_pair=None):
    if preferred_pair:
        fallback = [q for q in fallback if (q.flight_no, q.return_flight_no) == tuple(preferred_pair)]
    if not fallback:
        raise FetchFailure('parse_error', 'tongcheng_requested_pair_not_in_verified_quotes')
    return min(fallback, key=lambda q: q.price)


def read_details(page, fallback, check_access, diagnostics, preferred_pair=None):
    """Select one observed flight pair, inspect at most three plain economy fares."""
    if not fallback:
        raise FetchFailure('parse_error', 'tongcheng_no_pair_for_details')
    deadline = time.monotonic() + 90
    original = select_pair(fallback, preferred_pair)
    diagnostics['detail_selected_pair'] = [original.flight_no, original.return_flight_no]
    diagnostics['detail_stage'] = 'outbound_card'

    def check():
        check_access(page.url, page.inner_text('body', timeout=35000))

    def wait(read, seconds=12):
        until = min(deadline, time.monotonic() + seconds)
        while True:
            check()
            value = read()
            if value:
                return value
            if time.monotonic() >= until:
                return None
            page.wait_for_timeout(250)

    def card(number):
        # has_text also includes hidden popover text. Match rendered card text
        # and a visible choice action so a selected summary/hidden tooltip
        # cannot be mistaken for the return-flight card during transition.
        pattern = flight_pattern(number)
        model = page.locator('.airways-model:visible').filter(has_text=pattern)
        candidate = page.locator('.flight-item-main:visible').filter(has=model)
        if candidate.count() != 1:
            return None
        choice = candidate.get_by_text('选择', exact=True)
        return candidate if choice.count() == 1 and choice.is_visible() else None

    check()
    outbound = wait(lambda: card(original.flight_no))
    if outbound is None:
        raise FetchFailure('parse_error', 'tongcheng_outbound_card_not_matched', prices=fallback)
    outbound.get_by_text('选择', exact=True).click(timeout=35000)
    diagnostics['detail_stage'] = 'return_card'
    inbound = wait(lambda: card(original.return_flight_no))
    if inbound is None:
        raise FetchFailure('parse_error', 'tongcheng_return_card_not_matched', prices=fallback)
    inbound.get_by_text('选择', exact=True).click(timeout=35000)
    diagnostics['detail_stage'] = 'fare_cards'
    fares = inbound.locator('..').locator('.pro-item-box:visible')
    if not wait(lambda: fares.count()):
        raise FetchFailure('parse_error', 'tongcheng_fare_cards_missing', prices=fallback)
    candidates = []
    for fare in fares.all():
        text = fare.inner_text(timeout=35000)
        price = fare_price(text)
        if price is not None:
            candidates.append((price, fare, text))
    verified = []
    diagnostics['detail_fares_seen'] = fares.count()
    diagnostics['detail_plain_fares'] = len(candidates)
    for price, fare, text in sorted(candidates, key=lambda row: row[0])[:3]:
        if time.monotonic() >= deadline:
            break
        check()
        diagnostics['detail_stage'] = 'baggage_popup'
        fare.hover(timeout=35000)
        bag = fare.get_by_text(re.compile(r'^托运行李'))
        if bag.count() != 1:
            continue
        bag.hover(timeout=35000)
        popup = page.locator('[role="tooltip"]:visible').filter(has_text='行李规定')
        if not wait(lambda: popup.count() == 1, 4):
            continue
        legs = {}
        for key, direction in [('outbound', '上海 - 大阪'), ('inbound', '大阪 - 上海')]:
            diagnostics['detail_stage'] = key + '_baggage'
            popup.get_by_text(direction, exact=True).click(timeout=35000)
            def active_rule():
                active = popup.locator('.crumb-item.active')
                if active.count() != 1 or active.inner_text(timeout=35000).strip() != direction:
                    return None
                rule = adult_checked_rule(popup.inner_text(timeout=35000))
                return rule or None
            rule = wait(active_rule, 4)
            if rule:
                legs[key] = dict(rule, direction=direction)
        if len(legs) != 2:
            continue
        quote = replace(original, price=price, price_basis='selected_round_trip_tax',
            baggage_offer={'itinerary_confirmed': True, 'tax_inclusive_ticket_confirmed': True, **legs},
            extra=json.dumps({'cabin': '经济舱', 'cabin_verified': True,
                'selected_fare_verified': True, 'final_fare_verified': False,
                'refund_rule': '不得退改' if '不得退改' in text else '以页面规则为准'}, ensure_ascii=False))
        verified.append(quote)
        # One verified offer is sufficient for the current bounded acceptance.
        if baggage_total(quote) is not None:
            break
    diagnostics['detail_verified_fares'] = len(verified)
    diagnostics['detail_status'] = 'ok' if verified else 'unverified'
    diagnostics['detail_stage'] = 'complete'
    if not verified:
        raise FetchFailure('parse_error', 'tongcheng_two_direction_baggage_not_verified', prices=fallback)
    return verified
