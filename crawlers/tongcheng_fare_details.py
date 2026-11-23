"""Inspect selected Tongcheng fare cards and both direction tabs; never book."""
from dataclasses import replace
import json
import re
import time
import unicodedata

from playwright.sync_api import TimeoutError as BrowserTimeout
from core.baggage import baggage_total
from core.collection import FetchFailure


CHECKED_LABEL = r"(?:免费\s*)?托运(?:\s*行李)?(?:\s*(?:额度|额))?"
FARE_BAGGAGE_TEXT = re.compile(r"行李\s*额|托运")
FARE_CHECKED_LABEL = re.compile(rf"(?:无免费|不含(?:免费)?)(?:托运|行李额)|{CHECKED_LABEL}")
NO_FREE = re.compile(r"^(?:无免费|不含(?:免费)?)(?:托运(?:行李(?:额)?)?|行李额)")
WEIGHT = r"(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)"
PIECES_AND_WEIGHT = re.compile(rf"(\d+)\s*(?:件\s*)?[x×*]\s*{WEIGHT}", re.I)
DIRECTIONS = {
    "outbound": re.compile(r"^(?:去(?:程)?|上海\s*[-–—→至]\s*大阪)$"),
    "inbound": re.compile(r"^(?:返(?:程)?|回程|大阪\s*[-–—→至]\s*上海)$"),
}
EACH_LEG = re.compile(r"^(?:往返两程|往返|去返两程|去返程|去回程|两程)\s*(?:均|各)[\s:]*")
# The small 去/返 badge can be a line of its own or render as 去退改… .
LEG_PREFIX = re.compile(
    r"^(去程|回程|返程|[去返](?=$|[\s:：]|退改|提供|免费|无免费|不含|托运|行李额))\s*[:：]?\s*")
POPUP = '[role="tooltip"]:visible, .v-popover_box:visible, [role="dialog"]:visible'
ACTION_MS = 3500


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


def checked_allowance(text):
    """Parse a rule already identified as checked baggage."""
    source = unicodedata.normalize("NFKC", text).strip()
    if NO_FREE.match(source):
        return {"rule": "无免费托运", "no_free": True}
    if re.search(r"需付费|不含|无免费|另购|收费|自费|付费|待确认", source):
        return {}
    rule = re.sub(rf"^{CHECKED_LABEL}\s*[:：]?\s*", "", source)
    pair = PIECES_AND_WEIGHT.fullmatch(rule)
    pieces = re.search(r"(\d+)\s*件", rule)
    weight = re.search(WEIGHT, rule, re.I)
    if pair:
        count, kg = int(pair[1]), float(pair[2])
    else:
        count, kg = int(pieces[1]) if pieces else 0, float(weight[1]) if weight else 0
    per_piece = bool(pair or re.search(rf"每件\s*(?:不超过|最多)?\s*{WEIGHT}", rule, re.I))
    if ((pieces or pair) and count <= 0 or (weight or pair) and kg <= 0
            or (not count and (not kg or per_piece))):
        return {}
    # Reject unrelated numeric text such as dimensions and booking instructions.
    if not pair and not re.fullmatch(
            rf"(?:(?:每人\s*)?\d+\s*件(?:\s*[,、]?\s*(?:每件\s*)?(?:总重\s*)?"
            rf"(?:不超过|限重|最多)?\s*{WEIGHT})?|"
            rf"(?:每人\s*)?(?:总重\s*)?(?:不超过|限重|最多)?\s*{WEIGHT})[。.]?",
            rule, re.I):
        return {}
    result = {"confirmed": True, "pieces": count, "weight_kg": kg,
              "additional_fee_cny": 0, "rule": source}
    if kg:
        result["weight_basis"] = "per_piece" if per_piece else "total" if not count or "总重" in rule else "unspecified"
    return result


def adult_checked_rule(text):
    """Read only the checked rule in an adult block of the allowance panel."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    rules = []
    adult = False
    consumed = -1
    for index, line in enumerate(lines):
        if index == consumed:
            continue
        if re.fullmatch(r"成人|儿童|婴儿", line):
            adult = line == "成人"
        elif adult and NO_FREE.match(line):
            rules.append(checked_allowance(line))
        elif adult and re.match(rf"^{CHECKED_LABEL}", line):
            value = re.sub(rf"^{CHECKED_LABEL}\s*[:：]?\s*", "", line)
            rule = checked_allowance(value)
            if not value and index + 1 < len(lines) and (rule := checked_allowance(lines[index + 1])):
                consumed = index + 1
            if rule:
                rules.append(rule)
    return rules[0] if len(rules) == 1 else {}


def fare_checked_baggage(text):
    """Read the fare card in order, keeping each rule under its 去/返 badge."""
    notes, legs, ambiguous = set(), {}, set()
    current_leg = None
    for line in re.split(r"[\n；;|]", unicodedata.normalize("NFKC", text)):
        line = line.strip()
        both = EACH_LEG.match(line)
        direction = LEG_PREFIX.match(line)
        scope = both or direction
        value = line[scope.end():].strip() if scope else line
        if direction:
            current_leg = "outbound" if direction[1].startswith("去") else "inbound"
        if not FARE_BAGGAGE_TEXT.search(value):
            continue
        label = FARE_CHECKED_LABEL.search(value)
        if not label or re.search(r"手提|随身|儿童|婴儿|另购|付费|自费|收费", value[:label.start()]):
            continue
        rule = checked_allowance(value[label.start():])
        if not rule:
            continue
        notes.add(rule["rule"])
        keys = tuple(DIRECTIONS) if both else (current_leg,) if current_leg else ()
        for key in keys:
            if key in legs and legs[key] != rule:
                ambiguous.add(key)
            legs[key] = rule
    return (next(iter(notes)) if len(notes) == 1 else "",
            {key: dict(rule, source="fare") for key, rule in legs.items() if key not in ambiguous})


def _panel_baggage(page, fare, wait, diagnostics):
    popup = page.locator(POPUP).filter(has_text=re.compile(r"行李(?:规定|规则)"))
    visible = page.locator(":visible")
    link = fare.get_by_text(FARE_BAGGAGE_TEXT).and_(visible)
    if link.count() != 1:
        link = fare.get_by_text(re.compile(r"^行李(?:规定|规则)$")).and_(visible)
    if link.count() != 1:
        return {}
    # Reject a lingering or ambiguous panel instead of using another fare's rule.
    page.mouse.move(0, 0)
    if not wait(lambda: popup.count() == 0, 1.5):
        return {}
    link.hover(timeout=ACTION_MS)
    opened = wait(lambda: popup.count() == 1, 2)
    if not opened:
        control = link.and_(page.locator('a:not([href]), a[href="#"], .v-popover__reference, [role="button"]'))
        if popup.count() != 0 or control.count() != 1:
            return {}
        control.click(timeout=ACTION_MS)
        if not wait(lambda: popup.count() == 1, 2):
            return {}

    # An explicit all-directions or per-direction statement is usable even
    # when the popover has no direction controls.
    panel_text = popup.inner_text(timeout=ACTION_MS)
    adult = re.search(r"^成人[^\S\n]*\n(.*?)(?=^(?:儿童|婴儿)\s*$|\Z)",
                      panel_text, re.M | re.S)
    _, legs = fare_checked_baggage(adult[1]) if adult else ("", {})
    tabs = popup.locator('.crumb-item:visible, [role="tab"]:visible')
    active = popup.locator('.crumb-item.active:visible, [role="tab"][aria-selected="true"]:visible')
    for key, pattern in DIRECTIONS.items():
        diagnostics["detail_stage"] = key + "_baggage"
        tab = tabs.filter(has_text=pattern)
        if tab.count() != 1:
            continue

        def active_rule():
            if active.count() != 1 or not pattern.fullmatch(active.inner_text(timeout=ACTION_MS).strip()):
                return None
            return adult_checked_rule(popup.inner_text(timeout=ACTION_MS)) or None

        try:
            if not active.filter(has_text=pattern).count():
                tab.click(timeout=ACTION_MS)
            rule = wait(active_rule, 3)
        except BrowserTimeout:
            continue
        if rule:
            legs[key] = dict(rule, source="panel")
    return legs


def select_pair(fallback, preferred_pair=None):
    if preferred_pair:
        try:
            outbound, inbound = map(flight_pattern, preferred_pair)
        except ValueError:
            raise FetchFailure('parse_error', 'tongcheng_invalid_requested_pair') from None
        fallback = [q for q in fallback if outbound.fullmatch(q.flight_no)
                    and inbound.fullmatch(q.return_flight_no)]
    if not fallback:
        raise FetchFailure('parse_error', 'tongcheng_requested_pair_not_in_verified_quotes')
    return min(fallback, key=lambda q: q.price)


def read_details(page, fallback, check_access, diagnostics, preferred_pair=None):
    """Select one observed flight pair, inspect at most three plain economy fares."""
    if not fallback:
        raise FetchFailure('parse_error', 'tongcheng_no_pair_for_details')
    deadline = time.monotonic() + 90
    diagnostics['detail_stage'] = 'outbound_card'

    def check():
        check_access(page.url, page.inner_text('body', timeout=ACTION_MS))

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
        pattern = flight_pattern(number)
        choice = page.get_by_text('选择', exact=True).and_(page.locator(':visible'))
        candidates = page.locator('.flight-item-main:visible').filter(has=choice).filter(has_text=pattern)
        matches = [candidate for candidate in candidates.all()
                   if pattern.search(candidate.inner_text(timeout=ACTION_MS))]
        return matches[0] if len(matches) == 1 else None

    def visible_pair(options, attribute):
        for quote in options:
            if candidate := card(getattr(quote, attribute)):
                return quote, candidate
        return None

    check()
    options = [select_pair(fallback, preferred_pair)] if preferred_pair else sorted(fallback, key=lambda q: q.price)[:8]
    selection = wait(lambda: visible_pair(options, 'flight_no'))
    if selection is None:
        raise FetchFailure('parse_error', 'tongcheng_outbound_card_not_matched', prices=fallback)
    selected, outbound = selection
    visible = page.locator(':visible')
    outbound.get_by_text('选择', exact=True).and_(visible).click(timeout=ACTION_MS)
    diagnostics['detail_stage'] = 'return_card'
    same_outbound = [quote for quote in options if flight_pattern(selected.flight_no).fullmatch(quote.flight_no)]
    selection = wait(lambda: visible_pair(same_outbound, 'return_flight_no'))
    if selection is None:
        raise FetchFailure('parse_error', 'tongcheng_return_card_not_matched', prices=fallback)
    original, inbound = selection
    diagnostics['detail_selected_pair'] = [original.flight_no, original.return_flight_no]
    inbound.get_by_text('选择', exact=True).and_(visible).click(timeout=ACTION_MS)
    diagnostics['detail_stage'] = 'fare_cards'
    fares = inbound.locator('..').locator('.pro-item-box:visible')
    if not wait(lambda: fares.count()):
        raise FetchFailure('parse_error', 'tongcheng_fare_cards_missing', prices=fallback)
    candidates = []
    for fare in fares.all():
        text = fare.inner_text(timeout=ACTION_MS)
        price = fare_price(text)
        if price is not None:
            candidates.append((price, fare, text))
    detailed = []
    diagnostics['detail_fares_seen'] = fares.count()
    diagnostics['detail_plain_fares'] = len(candidates)
    for price, fare, text in sorted(candidates, key=lambda row: row[0])[:3]:
        if time.monotonic() >= deadline:
            break
        check()
        diagnostics['detail_stage'] = 'baggage_popup'
        note, legs = fare_checked_baggage(text)
        if len(legs) != 2:
            try:
                legs.update(_panel_baggage(page, fare, wait, diagnostics))
            except BrowserTimeout:
                diagnostics['detail_baggage_timeout'] = True
        offer = {'itinerary_confirmed': True, 'tax_inclusive_ticket_confirmed': True,
                 'cabin': 'economy', **legs}
        if note:
            offer['fare_rule'] = note
        quote = replace(original, price=price, price_basis='selected_round_trip_tax',
            baggage_offer=offer,
            extra=json.dumps({'cabin': '经济舱', 'cabin_verified': True,
                'selected_fare_verified': True, 'final_fare_verified': False,
                'baggage_offer': offer,
                'refund_rule': '不得退改' if '不得退改' in text else '以页面规则为准'}, ensure_ascii=False))
        detailed.append(quote)
        if baggage_total(quote) is not None:
            break
    complete = sum(DIRECTIONS.keys() <= quote.baggage_offer.keys() for quote in detailed)
    diagnostics['detail_verified_fares'] = sum(baggage_total(quote) is not None for quote in detailed)
    diagnostics['detail_fares_with_both_rules'] = complete
    diagnostics['detail_fares_with_card_rule'] = sum('fare_rule' in quote.baggage_offer for quote in detailed)
    diagnostics['detail_status'] = 'ok' if complete else 'unverified'
    diagnostics['detail_stage'] = 'complete'
    if not complete:
        detailed_keys = {(q.flight_no, q.return_flight_no, q.price) for q in detailed}
        listed = [q for q in sorted(fallback, key=lambda q: q.price)
                  if (q.flight_no, q.return_flight_no, q.price) not in detailed_keys]
        raise FetchFailure('parse_error', 'tongcheng_two_direction_baggage_not_verified',
                           prices=(detailed + listed)[:3])
    return detailed
