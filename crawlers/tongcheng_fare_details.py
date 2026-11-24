"""Inspect selected Tongcheng fare cards and both direction tabs; never book."""
from collections import Counter
from dataclasses import replace
import json
import re
import time
import unicodedata
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.sync_api import TimeoutError as BrowserTimeout
from core.baggage import baggage_total, checked_baggage_labels
from core.collection import FetchFailure
from core.models import RoundTripQuote
from .fare_tables import visible_table_rows


CHECKED_LABEL = r"(?:每人\s*可\s*)?(?:免费\s*)?托运(?:\s*行李)?(?:\s*(?:额度|额))?"
FARE_BAGGAGE_TEXT = re.compile(r"行李\s*额|托运")
FARE_CHECKED_LABEL = re.compile(rf"(?:无(?:免费)?|不含(?:免费)?)(?:托运|行李额)|{CHECKED_LABEL}")
NO_FREE = re.compile(r"^(?:(?:无(?:免费)?|不含(?:免费)?)托运(?:行李(?:额)?)?|"
                     r"(?:无免费|不含免费)行李额)")
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
CHECKED_HEADING = re.compile(rf"^{CHECKED_LABEL}\s*[:：]?$")
BAGGAGE_CONTROL_ELEMENTS = 'a, button, [role="link"], [role="button"], .v-popover__reference'
BAGGAGE_CONTROLS = (
    'a:visible, button:visible, [role="link"]:visible, [role="button"]:visible, '
    '.v-popover__reference:visible:not(:has(a, button, [role="link"], [role="button"]))'
)
BAGGAGE_LINK = re.compile(r"行李(?:规定|规则|\s*额)|托运")
BAGGAGE_PURCHASE = re.compile(r"预订|加购|购买|支付")
ACTION_MS = 3500


def flight_pattern(number):
    match = re.fullmatch(r'([A-Z0-9]{2})(\d{1,4})', number)
    if not match:
        raise ValueError('Invalid flight number')
    return re.compile(r'(?<![A-Z0-9])' + re.escape(match[1]) + r'0*' + str(int(match[2])) + r'(?![A-Z0-9])')


def fare_price(text):
    """Only a single explicit tax-inclusive economy fare without add-on ambiguity."""
    return _fare_price_and_reason(text)[0]


def _fare_price_and_reason(text):
    if not re.search(r'(?<!高端)(?<!超级)经济舱', text):
        return None, "no_economy_marker"
    if '含税总价' not in text:
        return None, "no_tax_total_marker"
    if re.search(r'礼包|套餐|组合险|损失险|低价优品|另付|服务费|优惠券|返现', text):
        return None, "add_on_or_discount"
    price = _displayed_fare_price(text)
    if price is None:
        return None, "price_line_not_unique"
    return (price, None) if 100 <= price <= 200000 else (None, "price_out_of_range")


def _displayed_fare_price(text):
    text = unicodedata.normalize("NFKC", text)
    prices = re.findall(r'^¥[ \t]*(?:\n[ \t]*)?(\d[\d,]*(?:\.\d{1,2})?)[ \t]*$', text, re.M)
    return float(prices[0].replace(',', '')) if len(prices) == 1 else None


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
    # Refund terms can be rendered beside the allowance as an adjacent span.
    for line in re.split(r"[\n;|]|\s+(?=(?:部分行程)?(?:退票|退改|改期|不得退改))",
                         unicodedata.normalize("NFKC", text)):
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


def _direction(text):
    matches = {key for line in text.splitlines() for key, pattern in DIRECTIONS.items()
               if pattern.fullmatch(line.strip())}
    return matches.pop() if len(matches) == 1 else None


def _table_allowance(value):
    value = unicodedata.normalize("NFKC", value).strip()
    if re.fullmatch(r"(?:无|不含|0\s*(?:kg|公斤|件))", value, re.I):
        return {"rule": "无免费托运", "no_free": True}
    return checked_allowance(re.sub(r"^免费\s*|\s*/\s*人$", "", value))


def _table_baggage(rows, direction=None):
    """Read checked-baggage cells only where headings identify their scope."""
    legs, conflicts = {}, set()
    columns = {}
    adult_columns = None
    checked_column = None

    def add(key, value):
        rule = _table_allowance(value)
        if key and rule:
            if key in legs and legs[key] != rule:
                conflicts.add(key)
            legs[key] = rule

    for row in rows:
        labels = {i: key for i, value in enumerate(row) if (key := _direction(value))}
        checked = [i for i, value in enumerate(row) if CHECKED_HEADING.fullmatch(value)]
        if not checked and not labels and any(value in ("成人", "儿童", "婴儿") for value in row):
            adult_columns = {i for i, value in enumerate(row) if value == "成人"}
        if len(labels) >= 2 and len(set(labels.values())) == 2:
            columns = labels
        if len(checked) == 1:
            index = checked[0]
            if any(value in ("儿童", "婴儿") for value in row[:index]):
                continue
            if columns:
                for column, key in columns.items():
                    if index < column < len(row) and (adult_columns is None or column in adult_columns):
                        add(key, row[column])
            elif labels:
                if len(set(labels.values())) == 1 and len(row) == index + 2:
                    add(next(iter(labels.values())), row[index + 1])
            elif adult_columns is not None:
                for column in adult_columns:
                    if index < column < len(row):
                        add(direction, row[column])
            elif len(row) == index + 2:
                add(direction, row[index + 1])
            if not any(_table_allowance(value) for value in row[index + 1:]):
                checked_column = index
        elif (checked_column is not None and len(labels) == 1 and checked_column < len(row)
              and not any(value in ("儿童", "婴儿") for value in row)):
            add(next(iter(labels.values())), row[checked_column])
    return {key: dict(rule, source="table") for key, rule in legs.items() if key not in conflicts}


def _visible_checked_tables(page):
    return [table for table in visible_table_rows(page)
            if any(CHECKED_HEADING.fullmatch(cell) for row in table for cell in row)]


def _rules_from_tables(tables, direction=None):
    result, conflicts = {}, set()
    for rows in tables:
        for key, rule in _table_baggage(rows, direction).items():
            if key in result and result[key] != rule:
                conflicts.add(key)
            result[key] = rule
    return {key: rule for key, rule in result.items() if key not in conflicts}


def _baggage_controls(fare):
    # Match the clickable parent even when only a nested span contains 行李额.
    return fare.locator(BAGGAGE_CONTROLS).filter(
        has_text=BAGGAGE_LINK, has_not_text=BAGGAGE_PURCHASE)


def _link_direction(link):
    ancestry = link.evaluate("""el => {
        const texts = [];
        for (let node = el; node && !node.matches('.pro-item-box'); node = node.parentElement)
            texts.push(node.innerText || '');
        return texts;
    }""", timeout=ACTION_MS)
    for text in ancestry:
        directions = {match[1][0] for line in text.splitlines() if (match := LEG_PREFIX.match(line.strip()))}
        mapped = {"outbound" if token == "去" else "inbound" for token in directions}
        if len(mapped) == 1:
            return mapped.pop()
        if mapped:
            return None
    return None


def _linked_baggage_rules(page, link, direction, wait, diagnostics, check_access):
    href = link.get_attribute("href", timeout=ACTION_MS)
    if not href:
        return {}
    url = urljoin(page.url, href)
    target = urlsplit(url)
    host = target.hostname or ""
    if (target.scheme != "https" or not (host == "ly.com" or host.endswith(".ly.com"))
            or target.username is not None or target.password is not None):
        return {}
    diagnostics["detail_baggage_linked_pages"] = diagnostics.get("detail_baggage_linked_pages", 0) + 1
    detail = page.context.new_page()
    try:
        response = detail.goto(url, wait_until="domcontentloaded", timeout=ACTION_MS)

        def tables_ready():
            check_access(detail.url, detail.inner_text("body", timeout=ACTION_MS),
                         response.status if response else None)
            return _visible_checked_tables(detail)

        tables = wait(tables_ready, 3) or []
        diagnostics["detail_baggage_tables"] = diagnostics.get("detail_baggage_tables", 0) + len(tables)
        return _rules_from_tables(tables, direction)
    finally:
        detail.close()


def _read_baggage_surface(page, popup, direction, wait, diagnostics, activate, seconds):
    prior = Counter(_visible_checked_tables(page))
    prior_popups = set(popup.all_inner_texts())

    def new_tables():
        return list((Counter(_visible_checked_tables(page)) - prior).elements())

    def changed_popup():
        texts = popup.all_inner_texts()
        return len(texts) == 1 and texts[0] not in prior_popups

    activate(timeout=ACTION_MS)
    if not wait(lambda: new_tables() or changed_popup(), seconds):
        return {}

    tables = new_tables()
    diagnostics["detail_baggage_surfaces"] = diagnostics.get("detail_baggage_surfaces", 0) + 1
    if not tables and changed_popup():
        # Some dialogs insert their table after the title becomes visible.
        wait(lambda: new_tables() or (popup.count() == 1 and
             adult_checked_rule(popup.inner_text(timeout=ACTION_MS))), 2)
        tables = new_tables()
    diagnostics["detail_baggage_tables"] = diagnostics.get("detail_baggage_tables", 0) + len(tables)
    legs = _rules_from_tables(tables, direction)
    if len(legs) == 2 or not changed_popup():
        return legs

    # An explicit all-directions or per-direction statement is usable even
    # when the popover has no direction controls.
    panel_text = popup.inner_text(timeout=ACTION_MS)
    adult = re.search(r"^成人[^\S\n]*\n(.*?)(?=^(?:儿童|婴儿)\s*$|\Z)",
                      panel_text, re.M | re.S)
    if adult:
        legs.update(fare_checked_baggage(adult[1])[1])
    tabs = popup.locator('.crumb-item:visible, [role="tab"]:visible')
    if direction and not tabs.count() and (rule := adult_checked_rule(panel_text)):
        legs.setdefault(direction, {"source": "panel", **rule})
    active = popup.locator('.crumb-item.active:visible, [role="tab"][aria-selected="true"]:visible')
    for key, pattern in DIRECTIONS.items():
        if key in legs:
            continue
        diagnostics["detail_stage"] = key + "_baggage"
        tab = tabs.filter(has_text=pattern)
        if tab.count() != 1:
            continue

        def active_rule():
            if active.count() != 1 or not pattern.fullmatch(active.inner_text(timeout=ACTION_MS).strip()):
                return None
            return (_rules_from_tables(new_tables(), key).get(key)
                    or adult_checked_rule(popup.inner_text(timeout=ACTION_MS)) or None)

        try:
            if not active.filter(has_text=pattern).count():
                tab.click(timeout=ACTION_MS)
            rule = wait(active_rule, 3)
        except BrowserTimeout:
            continue
        if rule:
            legs[key] = {"source": "panel", **rule}
    return legs


def _open_baggage_rules(page, link, popup, wait, diagnostics, check_access, known_legs, deadline):
    if BAGGAGE_PURCHASE.search(link.inner_text(timeout=ACTION_MS)):
        return {}
    direction = _link_direction(link)
    page.mouse.move(0, 0)
    wait(lambda: popup.count() == 0, 1)

    def try_surface(action, seconds, counter):
        if time.monotonic() >= deadline:
            return {}
        diagnostics[counter] = diagnostics.get(counter, 0) + 1
        try:
            return _read_baggage_surface(page, popup, direction, wait, diagnostics, action, seconds)
        except BrowserTimeout:
            diagnostics["detail_baggage_timeout"] = True
            return {}

    legs = try_surface(link.hover, 2, "detail_baggage_hovers")
    if DIRECTIONS.keys() <= known_legs | legs.keys():
        return legs

    # Only activate a rule control on the current page. Regular links are
    # validated separately before opening a read-only rules page.
    control = link.and_(page.locator(
        'a:not([href]), a[href^="#"], a[href="javascript:void(0)"], a[href="javascript:void(0);"], '
        'a[href="javascript:;"], button, [role="button"], .v-popover__reference'))
    if control.count() == 1:
        legs.update(try_surface(control.click, 3, "detail_baggage_clicks"))
    if not DIRECTIONS.keys() <= known_legs | legs.keys() and time.monotonic() < deadline:
        try:
            legs.update(_linked_baggage_rules(page, link, direction, wait, diagnostics, check_access))
        except BrowserTimeout:
            diagnostics["detail_baggage_timeout"] = True
    return legs


def _panel_baggage(page, fare, wait, diagnostics, check_access, known_legs=(), deadline=float("inf")):
    popup = page.locator(POPUP).filter(has_text=re.compile(r"行李(?:规定|规则)|托运"))
    controls = _baggage_controls(fare)
    text_controls = fare.get_by_text(BAGGAGE_LINK).and_(page.locator(
        f':visible:not(:is({BAGGAGE_CONTROL_ELEMENTS}), :is({BAGGAGE_CONTROL_ELEMENTS}) *)'
    )).filter(has_not_text=BAGGAGE_PURCHASE)
    candidates = controls.all() + text_controls.all()
    diagnostics["detail_baggage_controls"] = diagnostics.get("detail_baggage_controls", 0) + len(candidates)
    legs = _rules_from_tables(_visible_checked_tables(fare))
    known_legs = set(known_legs)
    for link in candidates:
        if time.monotonic() >= deadline:
            break
        diagnostics["detail_baggage_controls_tried"] = diagnostics.get("detail_baggage_controls_tried", 0) + 1
        try:
            shape = link.evaluate("""element => ({
                kind: ['A', 'BUTTON', 'SPAN', 'DIV'].includes(element.tagName) ? element.tagName : 'other',
                has_href: element.hasAttribute('href'),
                has_controls: element.hasAttribute('aria-controls'),
            })""", timeout=ACTION_MS)
            diagnostics.setdefault("detail_baggage_control_shapes", []).append(shape)
            legs.update(_open_baggage_rules(
                page, link, popup, wait, diagnostics, check_access, known_legs | legs.keys(), deadline))
        except BrowserTimeout:
            diagnostics["detail_baggage_timeout"] = True
        if DIRECTIONS.keys() <= known_legs | legs.keys():
            break
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


CABIN_NAME = re.compile(r'高端经济舱|超级经济舱|经济舱|商务舱|公务舱|头等舱|尊享飞')


def _inspect_fare_rows(page, fares, original, check, wait, check_access, diagnostics, deadline,
                       inventory=None, fare_group=None):
    candidates = []
    rejections = Counter()
    for fare in fares.all():
        text = fare.inner_text(timeout=ACTION_MS)
        price, reason = _fare_price_and_reason(text)
        if fare_group and not fare_group.startswith('经济舱'):
            price, reason = None, 'non_standard_fare_tab'
        note, legs = fare_checked_baggage(text)
        row = None
        if inventory is not None:
            cabin = CABIN_NAME.search(text)
            row = {
                'flight_no': original.flight_no,
                'return_flight_no': original.return_flight_no,
                'cabin': cabin[0] if cabin else None,
                'fare_group': fare_group,
                'display_price_cny': _displayed_fare_price(text),
                'tax_total_label': '含税总价' in text,
                'price_review': reason,
                'fare_baggage_rule': note or None,
            }
            inventory.append(row)
        if price is None:
            rejections[reason] += 1
        else:
            candidates.append((price, fare, text, note, legs, row))

    diagnostics['detail_fares_seen'] = diagnostics.get('detail_fares_seen', 0) + fares.count()
    diagnostics['detail_plain_fares'] = diagnostics.get('detail_plain_fares', 0) + len(candidates)
    rejections.update(diagnostics.get('detail_fare_rejections', {}))
    diagnostics['detail_fare_rejections'] = dict(rejections)

    detailed = []
    best_total = None
    for price, fare, text, note, legs, row in sorted(candidates, key=lambda candidate: candidate[0]):
        timed_out = time.monotonic() >= deadline
        if inventory is None and (timed_out or (best_total is not None and price > best_total)):
            break
        if not timed_out and not DIRECTIONS.keys() <= legs.keys():
            check()
            diagnostics['detail_stage'] = 'baggage_popup'
            try:
                for leg, rule in _panel_baggage(page, fare, wait, diagnostics, check_access,
                                                known_legs=legs, deadline=deadline).items():
                    legs.setdefault(leg, rule)
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
        total = baggage_total(quote)
        if row is not None:
            row['checked_baggage'] = checked_baggage_labels(quote)
            row['total_with_baggage_cny'] = total
        if total is not None and (best_total is None or total < best_total):
            best_total = total
    return detailed


DISPLAY_FLIGHT = re.compile(r'(?<![A-Z0-9])((?:[A-Z][A-Z0-9]|[0-9][A-Z]))(\d{1,4})(?![A-Z0-9])')
AIRCRAFT_PREFIX = re.compile(r'(?:空客|波音|空中客车|中国商飞|商飞|Airbus|Boeing)\s*$', re.I)
RETURN_HEADING = re.compile(r'^选择\s*(?:返程|回程)')
STOCK_PREFIX = r'(?:余\s*\d+\s*张\s*)?'
SELECT_OUTBOUND = re.compile(rf'^{STOCK_PREFIX}选择(?:去程)?\s*[⌄∨﹀]?$')
OPEN_RETURN_FARES = re.compile(
    rf'^{STOCK_PREFIX}(?:选择(?:返程|回程)?|展开(?:报价)?|查看(?:报价|票价|价格))\s*[⌄∨﹀]?$'
)
RESELECT_OUTBOUND = re.compile(r'^(?:(?:返回|重新)选择|重选|更换|修改)去程$')
CABIN_TAB = re.compile(rf'^({CABIN_NAME.pattern})\s*[¥￥]\s*([\d,.]+)\s*起$')


def _flight_cards(cards):
    records = {}
    duplicates = set()
    for text in cards.all_inner_texts():
        numbers = {match[1] + str(int(match[2])) for match in DISPLAY_FLIGHT.finditer(text)
                   if not AIRCRAFT_PREFIX.search(text[:match.start()])}
        # A card with multiple sectors requires a model that preserves all of them.
        if len(numbers) != 1:
            continue
        number = numbers.pop()
        if number in records:
            duplicates.add(number)
        prices = re.findall(r'¥\s*(\d[\d,]*(?:\.\d{1,2})?)', unicodedata.normalize('NFKC', text))
        records[number] = float(prices[0].replace(',', '')) if len(prices) == 1 else float('inf')
    return {number: price for number, price in records.items() if number not in duplicates}


def _card_number(value):
    match = DISPLAY_FLIGHT.fullmatch(value)
    return match[1] + str(int(match[2])) if match else value


def _same_search(current, original):
    actual, expected = map(urlsplit, (current, original))
    return ((actual.scheme, actual.hostname, actual.path) == (expected.scheme, expected.hostname, expected.path)
            and parse_qs(actual.query).get('para') == parse_qs(expected.query).get('para'))


FLIGHT_HEADER_XPATH = 'contains(concat(" ", normalize-space(@class), " "), " flight-item-main ")'


def _flight_action(card, page, label):
    visible = page.locator(':visible')
    # The stock badge and selection label can share a single clickable div.
    item = 'contains(concat(" ", normalize-space(@class), " "), " flight-item ")'
    container = card.locator(f'xpath=ancestor::*[{item} and count(.//*[{FLIGHT_HEADER_XPATH}])=1][1]')
    for scope in (card, container):
        action = scope.locator('.flight-btn:visible').filter(has_text=label)
        if action.count():
            return action
        action = scope.get_by_text(label).and_(visible)
        if action.count():
            return action
    return action


def _visible_fare_container(card, page):
    # Locate the nearest fare panel belonging to this flight header. A parent
    # with other flight headers might contain an entirely different offer.
    offer = 'contains(concat(" ", normalize-space(@class), " "), " pro-item-box ")'
    return card.locator(f'xpath=ancestor::*[count(.//*[{FLIGHT_HEADER_XPATH}])=1 and .//*[{offer}]][1]').filter(
        has=page.locator('.pro-item-box:visible'))


def _current_fare_tab(labels, rows):
    # Some sites display their selected tab as an ordinary element. The
    # already-open cabin and its minimum fare can identify that tab.
    cabins = {match[0] if (match := CABIN_NAME.search(text)) else None for text in rows}
    if len(cabins) != 1 or None in cabins:
        return None
    lowest = min((price for text in rows if (price := _displayed_fare_price(text)) is not None),
                 default=None)
    candidates = [label for label in labels if (tab := CABIN_TAB.fullmatch(' '.join(label.split())))
                  and tab[1] in cabins and _displayed_fare_price('¥' + tab[2]) == lowest]
    return candidates[0] if len(candidates) == 1 else None


def read_fare_inventory(page, itinerary, fallback, check_access, diagnostics,
                        preferred_pair=None, current_quotes=None, pair_limit=8):
    """Inspect the available fare cards in one round-trip search document."""
    origin, destination, depart, back, source_url = itinerary
    deadline = time.monotonic() + 150
    diagnostics['detail_probe_version'] = 2
    diagnostics['detail_stage'] = 'outbound_card'
    diagnostics['detail_pair_limit'] = 1 if preferred_pair else pair_limit
    attempted = diagnostics['detail_pairs_attempted'] = []
    scanned = diagnostics['detail_pairs_scanned'] = []
    inventory = diagnostics['detail_fare_inventory'] = []
    transitions = diagnostics['detail_return_transitions'] = []
    detailed = []
    limit = diagnostics['detail_pair_limit']

    def check():
        check_access(page.url, page.inner_text('body', timeout=ACTION_MS))
        if not _same_search(page.url, source_url):
            raise FetchFailure('parse_error', 'tongcheng_detail_search_changed', prices=detailed)

    def wait(read, seconds=8):
        until = min(deadline, time.monotonic() + seconds)
        while True:
            check()
            if value := read():
                return value
            if time.monotonic() >= until:
                return None
            page.wait_for_timeout(200)

    visible = page.locator(':visible')
    flight_cards = page.locator('.flight-item-main:visible')
    collapse = re.compile(r'^收起\s*[⌃∧︿]?$')
    reselect = page.get_by_text(RESELECT_OUTBOUND).and_(visible)
    return_heading = page.get_by_text(RETURN_HEADING).and_(page.locator(
        ':visible:not(:is(a, button, [role="button"]), :is(a, button, [role="button"]) *)'))
    requested_outbound = _card_number(preferred_pair[0]) if preferred_pair else None
    if requested_outbound:
        diagnostics['detail_requested_outbound'] = requested_outbound

    def outbound_ready():
        if return_heading.count():
            return None
        cards = _flight_cards(flight_cards)
        diagnostics['detail_visible_flight_cards'] = flight_cards.count()
        diagnostics['detail_visible_outbound_cards'] = len(cards)
        if requested_outbound and requested_outbound not in cards:
            return None
        return cards

    def listed_pairs():
        listed = {}
        live = current_quotes() if current_quotes else ()
        for row in (*fallback, *live):
            key = (_card_number(row.flight_no), _card_number(row.return_flight_no))
            if key not in listed or row.price < listed[key].price:
                listed[key] = row
        return listed

    outbound_records = wait(outbound_ready)
    listed = listed_pairs()
    if preferred_pair:
        requested_pair = tuple(map(_card_number, preferred_pair))
        diagnostics['detail_requested_pair_in_list'] = requested_pair in listed
        diagnostics['detail_requested_outbound_in_list'] = any(pair[0] == requested_outbound for pair in listed)
    if not outbound_records:
        raise FetchFailure('parse_error', 'tongcheng_outbound_card_not_matched', prices=fallback)

    ranked = sorted(listed, key=lambda pair: (listed[pair].price, pair))
    diagnostics['detail_initial_list_leaders'] = [
        {'flight_no': pair[0], 'return_flight_no': pair[1], 'price_cny': listed[pair].price,
         'outbound_card_matched': pair[0] in outbound_records}
        for pair in ranked[:limit]
    ]
    targets = {}
    if preferred_pair:
        outward, inward = map(_card_number, preferred_pair)
        targets[outward] = [inward]
        diagnostics['detail_selection_basis'] = 'requested_pair'
    else:
        visible_ranked = [pair for pair in ranked if pair[0] in outbound_records]
        for outward, inward in visible_ranked[:limit]:
            targets.setdefault(outward, []).append(inward)
        diagnostics['detail_selection_basis'] = 'listed_price' if targets else 'visible_flight_cards'
        for outward in sorted(outbound_records, key=outbound_records.__getitem__):
            targets.setdefault(outward, [])
    diagnostics['detail_planned_pairs'] = [[outward, inward] for outward, group in targets.items()
                                           for inward in group]

    on_return_page = False
    for index, (outward, wanted) in enumerate(targets.items()):
        if len(attempted) >= limit or time.monotonic() >= deadline:
            break
        if on_return_page:
            if reselect.count() != 1:
                diagnostics['detail_coverage_limit'] = 'outbound_reselect_unavailable'
                break
            check()
            reselect.click(timeout=ACTION_MS)
            remaining_outbounds = set(list(targets)[index:])
            if not wait(lambda: not reselect.count() and not return_heading.count()
                        and not remaining_outbounds.isdisjoint(_flight_cards(flight_cards))):
                diagnostics['detail_coverage_limit'] = 'outbound_list_not_restored'
                break
            on_return_page = False
        outbound = flight_cards.filter(has_text=flight_pattern(outward))
        if outbound.count() != 1:
            continue
        action = _flight_action(outbound, page, SELECT_OUTBOUND)
        if action.count() != 1:
            diagnostics['detail_outbound_action_unmatched'] = diagnostics.get('detail_outbound_action_unmatched', 0) + 1
            if preferred_pair:
                raise FetchFailure('parse_error', 'tongcheng_outbound_selection_control_not_matched', prices=fallback)
            continue
        diagnostics['detail_stage'] = 'return_card'
        diagnostics['detail_selected_outbound'] = outward
        check()
        action.click(timeout=ACTION_MS)
        transition = {'outbound': outward}
        transitions.append(transition)

        def visible_returns():
            cards = _flight_cards(flight_cards)
            known_returns = set(wanted) | {pair[1] for pair in listed_pairs() if pair[0] == outward}
            has_heading = bool(return_heading.count())
            known_visible = known_returns.intersection(cards) - outbound_records.keys()
            transition.update({
                'return_heading_visible': has_heading,
                'visible_flight_cards': flight_cards.count(),
                'known_return_cards': len(known_visible),
            })
            if not (has_heading or known_visible):
                return None
            cards = {number: price for number, price in cards.items()
                     if number != outward and (number not in outbound_records or number in known_returns)}
            if preferred_pair and wanted[0] not in cards:
                return None
            return cards

        available = wait(visible_returns, 15)
        planned = set(wanted)
        if available and not planned.issubset(available):
            def planned_returns_ready():
                nonlocal available
                if cards := visible_returns():
                    available = cards
                return planned.issubset(available)

            wait(planned_returns_ready, 4)
        can_reselect = reselect.count() == 1
        transition['reselect_visible'] = can_reselect
        on_return_page = bool(available or can_reselect or transition.get('return_heading_visible')
                              or transition.get('known_return_cards'))
        if not available:
            transition['status'] = 'return_list_unavailable'
            diagnostics.setdefault('detail_coverage_limit', 'return_list_unavailable')
            continue
        transition['status'] = 'return_list_ready'
        diagnostics['detail_return_quote_candidates'] = len(available)
        shown = listed_pairs()
        priority = [number for number in wanted if number in available]
        if not preferred_pair:
            def price_for(number):
                listed_quote = shown.get((outward, number))
                return listed_quote.price if listed_quote is not None else available[number]

            others = sorted((number for number in available if number not in priority),
                            key=price_for)
            # Keep room for cheaper API-ranked combinations under later outbounds.
            reserved = sum(len(group) for group in list(targets.values())[index + 1:])
            if reserved and reselect.count() != 1:
                diagnostics['detail_coverage_limit'] = 'outbound_reselect_unavailable'
                reserved = 0
            priority.extend(others[:max(0, limit - len(attempted) - len(priority) - reserved)])

        for inward in priority:
            if len(attempted) >= limit or time.monotonic() >= deadline:
                break
            check()
            pair = [outward, inward]
            attempted.append(pair)
            inbound = flight_cards.filter(has_text=flight_pattern(inward))
            if inbound.count() != 1:
                continue
            diagnostics['detail_stage'] = 'fare_cards'
            try:
                container = _visible_fare_container(inbound, page)
                if not container.count() and not _flight_action(inbound, page, collapse).count():
                    action = _flight_action(inbound, page, OPEN_RETURN_FARES)
                    if action.count() != 1:
                        diagnostics['detail_return_action_unmatched'] = diagnostics.get(
                            'detail_return_action_unmatched', 0) + 1
                        continue
                    action.click(timeout=ACTION_MS)
                if not wait(lambda: container.count() == 1):
                    diagnostics['detail_fare_panel_missing'] = diagnostics.get(
                        'detail_fare_panel_missing', 0) + 1
                    continue
                original = shown.get((outward, inward))
                if original is None:
                    original = RoundTripQuote('tongcheng', origin, destination, depart, 0,
                        flight_no=outward, return_date=back, return_flight_no=inward, source_url=source_url)
                scanned.append(pair)
                scan_deadline = min(deadline, time.monotonic() + 12)
                fares = container.locator('.pro-item-box:visible')
                tabs = container.get_by_text(CABIN_TAB).and_(page.locator(':visible:not(.pro-item-box *)'))
                tab_labels = list(dict.fromkeys(tabs.all_inner_texts()))
                initial_rows = tuple(fares.all_inner_texts())
                active_labels = tabs.and_(page.locator('[aria-selected="true"]')).all_inner_texts()
                current_tab = active_labels[0] if len(active_labels) == 1 else _current_fare_tab(
                    tab_labels, initial_rows)
                seen = {initial_rows}
                detailed.extend(_inspect_fare_rows(page, fares, original, check, wait, check_access,
                                                   diagnostics, scan_deadline, inventory, current_tab))
                for label in tab_labels:
                    if time.monotonic() >= deadline:
                        break
                    tab = tabs.and_(page.get_by_text(label, exact=True))
                    if (label == current_tab or tab.count() != 1
                            or tab.get_attribute('aria-selected', timeout=ACTION_MS) == 'true'):
                        continue
                    check()
                    try:
                        tab.click(timeout=ACTION_MS)
                    except BrowserTimeout:
                        diagnostics['detail_fare_tab_timeouts'] = diagnostics.get('detail_fare_tab_timeouts', 0) + 1
                        continue

                    def new_tab_rows():
                        texts = tuple(fares.all_inner_texts())
                        return texts if texts and texts not in seen else None

                    current = wait(new_tab_rows, 2)
                    if current:
                        seen.add(current)
                        detailed.extend(_inspect_fare_rows(page, fares, original, check, wait, check_access,
                                                           diagnostics, scan_deadline, inventory, label))
            except BrowserTimeout:
                diagnostics['detail_pair_timeouts'] = diagnostics.get('detail_pair_timeouts', 0) + 1

    diagnostics['detail_lowest_display_fares'] = sorted(
        (row for row in inventory if row['display_price_cny'] is not None),
        key=lambda row: row['display_price_cny'])[:5]
    diagnostics['detail_verified_fares'] = sum(baggage_total(quote) is not None for quote in detailed)
    diagnostics['detail_final_list_leaders'] = [
        {'flight_no': row.flight_no, 'return_flight_no': row.return_flight_no, 'price_cny': row.price}
        for row in sorted(listed_pairs().values(), key=lambda row: row.price)[:limit]
    ]
    diagnostics['detail_status'] = 'ok' if inventory else 'unverified'
    if inventory:
        diagnostics['detail_stage'] = 'inventory_collected'
    if not inventory:
        if transitions and all(step['status'] == 'return_list_unavailable' for step in transitions):
            evidence = 'tongcheng_return_list_unavailable'
        elif diagnostics.get('detail_return_action_unmatched'):
            evidence = 'tongcheng_return_fare_control_not_matched'
        else:
            evidence = 'tongcheng_no_fare_cards_read'
        raise FetchFailure('parse_error', evidence, prices=fallback)
    if not detailed:
        raise FetchFailure('parse_error', 'tongcheng_fare_inventory_without_verified_economy_price', prices=fallback)
    return detailed


def read_details(page, fallback, check_access, diagnostics, preferred_pair=None, current_quotes=None):
    """Select one observed pair and look for its cheapest verified baggage total."""
    if not fallback:
        raise FetchFailure('parse_error', 'tongcheng_no_pair_for_details')
    deadline = time.monotonic() + 90
    diagnostics['detail_probe_version'] = 1
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

    visible_cards = page.locator('.flight-item-main:visible')
    visible = page.locator(':visible')
    selectable_cards = visible_cards.filter(has=page.get_by_text('选择', exact=True).and_(visible))
    expanded_cards = visible_cards.filter(has=page.get_by_text(re.compile(r'^收起\s*[⌃∧︿]?$')).and_(visible))

    def visible_pair(options, attribute, cards):
        texts = cards.all_inner_texts()
        for quote in options:
            pattern = flight_pattern(getattr(quote, attribute))
            if sum(bool(pattern.search(text)) for text in texts) != 1:
                continue
            candidate = cards.filter(has_text=pattern)
            if candidate.count() == 1:
                return quote, candidate
        return None

    check()
    options = [select_pair(fallback, preferred_pair)] if preferred_pair else sorted(fallback, key=lambda q: q.price)
    selection = wait(lambda: visible_pair(options, 'flight_no', selectable_cards))
    if selection is None:
        diagnostics['detail_visible_flight_cards'] = visible_cards.count()
        diagnostics['detail_selectable_flight_cards'] = selectable_cards.count()
        raise FetchFailure('parse_error', 'tongcheng_outbound_card_not_matched', prices=fallback)
    selected, outbound = selection
    diagnostics['detail_selected_outbound'] = selected.flight_no
    outbound.get_by_text('选择', exact=True).and_(visible).click(timeout=ACTION_MS)
    diagnostics['detail_stage'] = 'return_card'
    outbound_pattern = flight_pattern(selected.flight_no)

    def choose_return():
        # The outbound click may update the page's own list response. Keep the
        # verified dates/pair requirement and consider all eligible returns.
        live = current_quotes() if current_quotes and not preferred_pair else ()
        same_outbound = {quote.return_flight_no: quote for quote in (*options, *live)
                         if outbound_pattern.fullmatch(quote.flight_no)}
        diagnostics['detail_return_quote_candidates'] = len(same_outbound)
        diagnostics['detail_selectable_flight_cards'] = selectable_cards.count()
        diagnostics['detail_expanded_flight_cards'] = expanded_cards.count()
        returns = sorted(same_outbound.values(), key=lambda q: q.price)
        matches = []
        for cards, already_expanded in ((expanded_cards, True), (selectable_cards, False)):
            if matched := visible_pair(returns, 'return_flight_no', cards):
                matches.append((*matched, already_expanded))
        return min(matches, key=lambda match: match[0].price, default=None)

    selection = wait(choose_return)
    if selection is None:
        diagnostics['detail_visible_flight_cards'] = visible_cards.count()
        raise FetchFailure('parse_error', 'tongcheng_return_card_not_matched', prices=fallback)
    original, inbound, already_expanded = selection
    diagnostics['detail_selected_pair'] = [original.flight_no, original.return_flight_no]
    diagnostics['detail_return_already_expanded'] = already_expanded
    if not already_expanded:
        inbound.get_by_text('选择', exact=True).and_(visible).click(timeout=ACTION_MS)
    diagnostics['detail_stage'] = 'fare_cards'
    # The button changes from 选择 to 收起 after expanding. Re-bind by the
    # selected flight and the visible fare rows instead of using that button.
    return_pattern = flight_pattern(original.return_flight_no)
    containers = visible_cards.filter(has_text=return_pattern).locator('..').filter(
        has=page.locator('.pro-item-box:visible'))
    if not wait(lambda: containers.count() == 1):
        diagnostics['detail_fares_seen'] = 0
        raise FetchFailure('parse_error', 'tongcheng_fare_cards_missing', prices=fallback)
    fares = containers.locator('.pro-item-box:visible')
    detailed = _inspect_fare_rows(page, fares, original, check, wait, check_access, diagnostics, deadline)
    if not diagnostics['detail_plain_fares']:
        raise FetchFailure('parse_error', 'tongcheng_no_eligible_economy_fare', prices=fallback)
    complete = sum(DIRECTIONS.keys() <= quote.baggage_offer.keys() for quote in detailed)
    diagnostics['detail_verified_fares'] = sum(baggage_total(quote) is not None for quote in detailed)
    diagnostics['detail_fares_with_both_rules'] = complete
    diagnostics['detail_fares_with_card_rule'] = sum('fare_rule' in quote.baggage_offer for quote in detailed)
    diagnostics['detail_confirmed_directions'] = {
        key: sum(key in quote.baggage_offer for quote in detailed) for key in DIRECTIONS
    }
    diagnostics['detail_status'] = 'ok' if complete else 'unverified'
    diagnostics['detail_stage'] = 'complete' if complete else 'baggage_rules_incomplete'
    if not complete:
        detailed_keys = {(q.flight_no, q.return_flight_no, q.price) for q in detailed}
        listed = [q for q in sorted(fallback, key=lambda q: q.price)
                  if (q.flight_no, q.return_flight_no, q.price) not in detailed_keys]
        if not diagnostics.get('detail_baggage_controls'):
            evidence = 'tongcheng_baggage_control_not_found'
        elif not any(diagnostics.get(key) for key in (
                'detail_baggage_tables', 'detail_baggage_surfaces', 'detail_baggage_linked_pages')):
            evidence = 'tongcheng_baggage_control_did_not_open_rules'
        else:
            evidence = 'tongcheng_two_direction_baggage_not_verified'
        raise FetchFailure('parse_error', evidence, prices=(detailed + listed)[:3])
    return detailed
