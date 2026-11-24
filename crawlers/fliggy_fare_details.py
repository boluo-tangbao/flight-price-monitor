"""Read public Fliggy desktop fare cards and their adult baggage tables."""

from collections import Counter
import math
import re
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import TimeoutError as BrowserTimeout

from core.collection import FetchFailure
from core.models import RoundTripQuote
from .fare_tables import visible_table_rows


RULE_LINK = re.compile(r"^退改签规则\s*及\s*行李额$")
FLIGHT_NUMBER = re.compile(r"(?<![A-Z0-9])(?:[A-Z][A-Z0-9]|[0-9][A-Z])\d{1,4}(?![A-Z0-9])")
CALCULATED_TAX_PRICE = re.compile(r"往返\s*含税(?:价(?:格)?)?\s*¥\s*(\d[\d,]*(?:\.\d{1,2})?)")
AMOUNT = re.compile(r"¥\s*(\d[\d,]*(?:\.\d{1,2})?)")
BLOCKED_PATH = re.compile(r"_____tmd_____|/punish(?:/|$)|/captcha(?:/|$)", re.I)
VERIFY_TEXT = re.compile(r"请完成.{0,12}验证|滑动验证|安全验证|访问受限")
LOGIN_TEXT = re.compile(r"请先登录|扫码登录|登录后(?:查看|查询)(?:航班|机票)")
LOGIN_HOSTS = {"login.taobao.com", "login.fliggy.com", "passport.alitrip.com"}


def _normalize(value):
    return unicodedata.normalize("NFKC", value).strip()


def _number(value):
    price = float(value.replace(",", ""))
    return price if math.isfinite(price) and price > 0 else None


def _allowance(value):
    value = _normalize(value)
    if re.search(r"(?:无|不含)免费托运(?:行李(?:额度|额)?)?", value):
        return {"rule": "无免费托运", "no_free": True}
    if re.search(r"另购|另付|需(?:加购|付费)|不包含|不免费|收费托运", value):
        return {}
    pieces = re.search(r"(?:每人可)?免费托运(?:行李)?件数\s*:\s*(\d+)\s*件", value)
    weight = re.search(r"(?:可)?免费托运(?:的)?重量\s*:\s*(?:总重\s*)?(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)(\s*/\s*件)?",
                       value, re.I)
    count = int(pieces[1]) if pieces else 0
    kg = float(weight[1]) if weight else 0
    if (pieces and count <= 0) or (weight and (not math.isfinite(kg) or kg <= 0)) or not (count or kg):
        return {}
    if weight and weight[2] and not count:
        return {}
    text = f"{count}件" if count else ""
    if kg:
        text += (f"，每件 {kg:g} kg" if weight[2] else f"{'，总重 ' if count else ''}{kg:g} kg")
    return {"confirmed": True, "pieces": count, "weight_kg": kg, "additional_fee_cny": 0,
            "weight_basis": "per_piece" if weight and weight[2] else "total",
            "rule": text, "source": "fliggy_adult_checked_table"}


def _adult_checked_rules(tables, city_from, city_to):
    route = {f"{city_from}-{city_to}": "outbound", f"{city_to}-{city_from}": "inbound"}
    rules, conflicts = {}, set()
    for table in tables:
        for row in table:
            if len(row) != 4:
                continue
            direction, passenger, baggage, text = map(_normalize, row)
            direction = re.sub(r"\s+", "", direction)
            direction = re.sub(r"[—–→至]", "-", direction)
            leg = route.get(direction)
            if leg and passenger == "成人" and baggage == "托运行李":
                rule = _allowance(text)
                if rule:
                    if leg in rules and rule != rules[leg]:
                        conflicts.add(leg)
                    rules[leg] = rule
    return {leg: rule for leg, rule in rules.items() if leg not in conflicts}


def _text_baggage_rows(text, city_from, city_to):
    """Expand a rendered baggage grid, including its route/passenger groups."""
    routes = {f"{city_from}-{city_to}", f"{city_to}-{city_from}"}
    rows = []
    direction = passenger = baggage = ""
    values = []

    def flush():
        if direction and passenger and baggage and values:
            rows.append((direction, passenger, baggage, "\n".join(values)))
        values.clear()

    for line in text.splitlines():
        line = _normalize(line)
        if not line:
            continue
        route = re.sub(r"[—–→至]", "-", re.sub(r"\s+", "", line))
        if route in routes:
            flush()
            direction, passenger, baggage = route, "", ""
        elif line in ("成人", "儿童", "婴儿"):
            flush()
            passenger, baggage = line, ""
        elif line in ("个人物品", "手提行李", "托运行李"):
            flush()
            baggage = line
        elif baggage:
            values.append(line)
    flush()
    return tuple(rows)


def _visible_baggage_rows(page, city_from, city_to):
    tables = visible_table_rows(page)
    # Current Fliggy tooltips render the visual table with Rax div/span nodes.
    # Read only the active rules popup; never pool text across fare cards.
    blocks = page.get_by_text("行李额说明", exact=True).and_(page.locator(":visible")).evaluate_all(r"""headings => headings.map(heading => {
        let result = '';
        for (let node = heading.parentElement; node && node !== document.body; node = node.parentElement) {
            const text = node.innerText || '';
            if (/退改签规则\s*及\s*行李额/.test(text)) break;
            if (text.includes('乘客类型') && text.includes('托运行李')) result = text;
        }
        return result;
    }).filter(Boolean)""")
    if len(blocks) == 1:
        rows = _text_baggage_rows(blocks[0], city_from, city_to)
        if rows:
            tables.append(rows)
    return tables


def check_access(page, expected_url, status=None):
    actual, expected = map(urlsplit, (page.url, expected_url))
    if status == 401 or actual.hostname in LOGIN_HOSTS:
        raise FetchFailure("login_required", "fliggy_desktop_login_required")
    if status in (403, 429) or _visible_verification(page):
        raise FetchFailure("blocked", "fliggy_desktop_verification_required")
    if status is not None and status >= 400:
        raise FetchFailure("error", "fliggy_desktop_http_error")
    required = ("tripType", "depDate", "arrDate")
    actual_params, expected_params = map(parse_qs, (actual.query, expected.query))

    def city(params, prefix):
        values = [params[key] for key in (prefix + "City", prefix + "CityCode") if key in params]
        return values[0] if values and all(value == values[0] for value in values) else None

    if ((actual.scheme, actual.hostname, actual.path) != (expected.scheme, expected.hostname, expected.path)
            or any(actual_params.get(key) != expected_params.get(key) for key in required)
            or any(city(actual_params, prefix) is None or city(actual_params, prefix) != city(expected_params, prefix)
                   for prefix in ("dep", "arr"))):
        raise FetchFailure("unverified", "fliggy_desktop_round_trip_page_not_matched")
    body = page.locator("body").inner_text(timeout=3500)
    if VERIFY_TEXT.search(body):
        raise FetchFailure("blocked", "fliggy_desktop_verification_required")
    if LOGIN_TEXT.search(body):
        raise FetchFailure("login_required", "fliggy_desktop_login_required")


def _visible_verification(page):
    """A completed challenge can leave its hidden iframe attached."""
    for frame in page.frames:
        if not BLOCKED_PATH.search(urlsplit(frame.url).path):
            continue
        while frame.parent_frame is not None:
            if not frame.frame_element().is_visible():
                break
            frame = frame.parent_frame
        else:
            return True
    return False


def _flight_options(page, action):
    options = {}
    duplicate = set()
    for card in page.locator(".flightItem:visible").filter(has=action).all():
        heading = " ".join(card.locator('[aria-label^="航班号"]:visible').all_inner_texts())
        numbers = set(FLIGHT_NUMBER.findall(heading))
        if len(numbers) != 1:
            continue
        number = numbers.pop()
        if number in options:
            duplicate.add(number)
        match = CALCULATED_TAX_PRICE.search(_normalize(card.inner_text(timeout=3500)))
        options[number] = (card, _number(match[1]) if match else None)
    return {number: option for number, option in options.items() if number not in duplicate}


def _selected_direction(control, flight, depart, other_control):
    if control.count() != 1:
        return False
    banner = control.evaluate("""(node, other) => {
        for (let parent = node.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
            const text = parent.innerText || '';
            if (text.includes(other)) break;
            if (text.includes('已选定') && text.includes('出发')) return text;
        }
        return '';
    }""", other_control, timeout=3500)
    return (flight in FLIGHT_NUMBER.findall(banner)
            and re.search(rf"出发\s*[:：]?\s*{re.escape(depart)}(?:\s|$)", banner) is not None)


def _fare_text(link):
    return link.evaluate(r"""node => {
        for (let parent = node.parentElement; parent && parent !== document.body; parent = parent.parentElement) {
            const text = parent.innerText || '';
            if ((text.match(/退改签规则\s*及\s*行李额/g) || []).length > 1) break;
            if (text.includes('往返总价') || text.includes('往返含税总价')) return text;
        }
        return '';
    }""", timeout=3500)


def _fare_price(text, tax_display):
    text = _normalize(text)
    if not (re.search(r"去程\s*经济舱", text) and re.search(r"返程\s*经济舱", text)):
        return None
    if not ("往返含税总价" in text or (tax_display and "往返总价" in text)):
        return None
    if re.search(r"另付|另收|礼包|返现|优惠券|券后", text):
        return None
    amounts = AMOUNT.findall(text)
    return _number(amounts[0]) if len(amounts) == 1 else None


def _inspect_pair(page, itinerary, flights, tax_display, links, wait, diagnostics, deadline):
    origin, destination, depart, back, city_from, city_to, url = itinerary
    outward, inward = flights
    candidates = [(link, _fare_text(link)) for link in links.all()]
    diagnostics["fare_cards_seen"] += len(candidates)
    for link, text in candidates:
        price = _fare_price(text, tax_display)
        if price is None:
            diagnostics["fare_cards_price_unverified"] += 1
            continue
        legs = {}
        if time.monotonic() < deadline:
            page.mouse.move(0, 0)
            wait(lambda: not _adult_checked_rules(_visible_baggage_rows(page, city_from, city_to), city_from, city_to), 1)
            baseline = Counter(_visible_baggage_rows(page, city_from, city_to))

            def read_table():
                nonlocal legs
                fresh = (Counter(_visible_baggage_rows(page, city_from, city_to)) - baseline).elements()
                legs = _adult_checked_rules(fresh, city_from, city_to)
                return len(legs) == 2

            diagnostics["baggage_hovers"] += 1
            try:
                link.hover(timeout=3500)
                wait(read_table, 3)
            except BrowserTimeout:
                pass
        diagnostic = {"flight_no": outward, "return_flight_no": inward, "price_cny": price,
                      "baggage_directions_read": sorted(legs)}
        diagnostics["fare_cards"].append(diagnostic)
        yield RoundTripQuote(
            "fliggy", origin, destination, depart, price, flight_no=outward,
            return_date=back, return_flight_no=inward, source_url=url,
            price_basis="fliggy_selected_round_trip_tax",
            baggage_offer={"cabin": "economy", "itinerary_confirmed": True,
                           "tax_inclusive_ticket_confirmed": True, **legs},
        )


def read_fare_details(page, itinerary, diagnostics):
    """Inspect up to eight flight pairs by navigating visible flight controls."""
    _, _, depart, back, _, _, url = itinerary
    deadline = time.monotonic() + 120
    diagnostics.update(fare_cards_seen=0, fare_cards_price_unverified=0,
                       baggage_hovers=0, fare_cards=[], pairs_scanned=[])

    def wait(read, seconds=12):
        end = min(deadline, time.monotonic() + seconds)
        while True:
            check_access(page, url)
            if value := read():
                return value
            if time.monotonic() >= end:
                return None
            page.wait_for_timeout(200)

    visible = page.locator(":visible")
    choose_out = page.get_by_role("button", name="选为去程", exact=True).and_(visible)
    choose_in = page.get_by_role("button", name="选为返程", exact=True).and_(visible)
    reselect_out = page.get_by_text("重选去程航班", exact=True).and_(visible)
    reselect_in = page.get_by_text("重选返程航班", exact=True).and_(visible)
    tax = page.get_by_text("含税价格", exact=True).and_(visible)
    rules = page.get_by_text(RULE_LINK).and_(visible)
    if wait(lambda: choose_out.count()):
        if tax.count() == 1:
            tax.click(timeout=3500)
    outbounds = wait(lambda: _flight_options(page, choose_out))
    if not outbounds:
        raise FetchFailure("unverified", "fliggy_outbound_cards_unavailable")

    def ordered(options):
        return sorted(options, key=lambda number: (options[number][1] is None, options[number][1] or 0))

    detailed = []
    for index, outward in enumerate(ordered(outbounds)[:4]):
        if time.monotonic() >= deadline:
            break
        try:
            if index:
                if reselect_out.count() != 1:
                    break
                reselect_out.click(timeout=3500)
            out = wait(lambda: _flight_options(page, choose_out).get(outward))
            if out is None:
                continue
            out[0].get_by_role("button", name="选为去程", exact=True).click(timeout=3500)
            if not wait(lambda: _selected_direction(reselect_out, outward, depart, "重选返程航班")):
                break
            inbounds = wait(lambda: _flight_options(page, choose_in))
            if not inbounds:
                continue
            for position, inward in enumerate(ordered(inbounds)[:2]):
                if time.monotonic() >= deadline:
                    break
                if position:
                    if reselect_in.count() != 1:
                        break
                    reselect_in.click(timeout=3500)
                    if not wait(lambda: rules.count() == 0):
                        break
                match = wait(lambda: _flight_options(page, choose_in).get(inward))
                if match is None:
                    continue
                match[0].get_by_role("button", name="选为返程", exact=True).click(timeout=3500)
                if not wait(lambda: _selected_direction(reselect_in, inward, back, "重选去程航班")
                            and rules.count()):
                    continue
                diagnostics["pairs_scanned"].append([outward, inward])
                detailed.extend(_inspect_pair(page, itinerary, (outward, inward), match[1] is not None,
                                               rules, wait, diagnostics, deadline))
        except BrowserTimeout:
            diagnostics["pair_timeouts"] = diagnostics.get("pair_timeouts", 0) + 1
        except FetchFailure as failure:
            failure.prices = detailed
            raise
    if not detailed:
        raise FetchFailure("unverified", "fliggy_round_trip_fare_cards_unavailable")
    return detailed
