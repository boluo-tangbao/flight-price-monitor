"""Read the expanded fare panel for a bounded set of Ctrip return flights."""
import json
import re
import time
from dataclasses import replace

from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout

from core.baggage import baggage_total
from core.collection import FetchFailure


MONEY = re.compile(r"^¥\s*([\d,]+(?:\.\d{1,2})?)$", re.M)
FLIGHT = re.compile(r"^([A-Z0-9]{2}\d{1,4})(?=\s|$)", re.M)
START = re.compile(r"^¥\s*([\d,]+)\s*起$", re.M)
DIRECTION = re.compile(r"^(?:行李规定[ \t]*)?([^\s—–-]+)[ \t]*[—–-][ \t]*([^\s—–-]+)[ \t]*$", re.M)
CHECKED = re.compile(r"^[ \t]*托运(?:行李)?[ \t]*[:：][ \t]*(\S[^\n]*)$", re.M)
INLINE_CHECKED = re.compile(
    r"无免费托运行李(?:额)?|托运行李总重不超过\s*\d+(?:\.\d+)?\s*(?:公斤|kg)", re.I)


def included_baggage(text):
    """Require opposite directions and a specific positive allowance for each leg."""
    headings = list(DIRECTION.finditer(text))
    if len(headings) != 2 or headings[0].groups() != headings[1].groups()[::-1]:
        return {}
    result = {}
    for index, name in enumerate(("outbound", "inbound")):
        start = headings[index].end()
        end = headings[1].start() if index == 0 else len(text)
        lines = CHECKED.findall(text[start:end])
        if len(lines) != 1:
            continue
        result[name] = _checked_rule(lines[0])
    return result


def _checked_rule(text):
    rule = text.strip()
    leg = {"rule": rule}
    if not re.search(r"无免费|不含|另购|收费|自费|付费", rule):
        kg = re.search(r"(\d+(?:\.\d+)?)\s*(?:公斤|kg)", rule, re.I)
        pieces = re.search(r"(\d+)\s*件", rule)
        weight = float(kg[1]) if kg else 0
        count = int(pieces[1]) if pieces else 0
        if ((weight > 0 or count > 0) and (kg is None or weight > 0)
                and (pieces is None or count > 0)):
            leg.update(confirmed=True, weight_kg=weight, pieces=count, additional_fee_cny=0)
    return leg


def _table_baggage(page):
    # Use the outer rule row to determine which directions share the same
    # sub-table. The text of the nested allowance cell alone cannot tell us.
    blocks = page.evaluate("""() => {
        const result = [];
        for (const title of document.querySelectorAll('th')) {
            if (!title.getClientRects().length || title.innerText.trim() !== '行李规定') continue;
            let row = title.parentElement;
            for (let i = 0; row && i < title.rowSpan; i++, row = row.nextElementSibling) {
                const direction = row.querySelector(':scope > td.sub-tit');
                const content = row.querySelector(':scope > td.sub-table');
                if (!direction || !content) continue;
                const rules = [...content.querySelectorAll('.luggage-type')].filter(rule => {
                    if (!rule.getClientRects().length || !/^托运(?:行李)?[：:]/.test(rule.innerText.trim())) return false;
                    const current = rule.closest('tr');
                    for (let item = current; item; item = item.previousElementSibling) {
                        const caption = item.querySelector(':scope > td.caption');
                        if (caption) return caption.innerText.trim() === '成人' &&
                            current.rowIndex - item.rowIndex < caption.rowSpan;
                    }
                    return false;
                });
                if (rules.length === 1) result.push([direction.innerText, rules[0].innerText]);
            }
        }
        return result;
    }""")
    if not isinstance(blocks, list):
        return {}
    directions = []
    for heading, text in blocks:
        rule = CHECKED.fullmatch(text.strip())
        for match in DIRECTION.finditer(heading):
            if rule:
                directions.append((match.groups(), rule[1]))
    if len(directions) != 2 or directions[0][0] != directions[1][0][::-1]:
        return {}
    return {name: _checked_rule(entry[1])
            for name, entry in zip(("outbound", "inbound"), directions)}


def _tabbed_baggage(page, wait_for):
    group = page.locator(".tooltip-tab:visible").filter(
        has=page.get_by_text(DIRECTION))
    if group.count() != 1:
        return {}
    tabs = group.locator("a:visible").all()
    if len(tabs) != 2:
        return {}
    directions = [DIRECTION.fullmatch(tab.inner_text(timeout=2500).strip()) for tab in tabs]
    if not all(directions) or directions[0].groups() != directions[1].groups()[::-1]:
        return {}
    table = group.locator("xpath=ancestor::table[1]")
    rules = table.locator(".luggage-type:visible").filter(has_text=CHECKED)
    result = {}
    for name, tab in zip(("outbound", "inbound"), tabs):
        if "current" not in (tab.get_attribute("class", timeout=2500) or "").split():
            tab.click(timeout=2500)

        def active_rule(_):
            if ("current" not in (tab.get_attribute("class", timeout=2500) or "").split()
                    or rules.count() != 1):
                return None
            match = CHECKED.fullmatch(rules.inner_text(timeout=2500).strip())
            return _checked_rule(match[1]) if match else None

        rule = wait_for(active_rule, 2)
        if rule:
            result[name] = rule
    return result


def _baggage_popup(body):
    start = re.search(r"^行李规定[ \t]*(?:\n[ \t]*)?[^\s—–-]+[ \t]*[—–-][ \t]*[^\s—–-]+", body, re.M)
    if not start:
        return ""
    # A floating rule panel follows the fare cards. Its two direction headings
    # and their individual checked-baggage rows are required together.
    return body[start.start():start.start() + 1200]


def _flight_cards(page, button):
    cards = page.locator(".flight-box:visible").filter(has=page.get_by_text(button, exact=True))
    options = []
    for card in cards.all():
        text = card.inner_text(timeout=2500)
        number, price = FLIGHT.search(text), START.search(text)
        if number and price and "往返含税价" in text:
            # The action switches from 订票 to 收起 when expanded. Bind to the
            # flight number so expanding one card cannot reindex later cards.
            stable = page.locator(".flight-box:visible").filter(
                has_text=re.compile(rf"(?<![A-Z0-9]){re.escape(number[1])}(?=\s|$)"))
            if stable.count() == 1:
                options.append((float(price[1].replace(",", "")), number[1], stable))
    return sorted(options, key=lambda entry: entry[0])


def read_details(page, fallback, check_access, diagnostics):
    """Take the cheapest observed outbound and inspect up to three return cards."""
    confirmed = []
    return_estimates = []
    status = "ok"
    deadline = time.monotonic() + 60
    stage = "outbound_card"

    def record_failure(outcome, evidence):
        nonlocal status
        if status in ("ok", "parse_error") or outcome == "timeout":
            status = outcome
            diagnostics["detail_evidence"] = evidence

    def body():
        text = page.inner_text("body", timeout=2500)
        check_access(text, page.url, None, "fare_details")
        return text

    def wait_for(read, seconds):
        until = min(deadline, time.monotonic() + seconds)
        while True:
            text = body()
            value = read(text)
            if value or time.monotonic() >= until:
                return value
            page.wait_for_timeout(250)

    try:
        outbound_cards = _flight_cards(page, "选为去程")
        if not outbound_cards:
            raise FetchFailure("parse_error", "outbound_card_not_found")
        _, outbound, card = outbound_cards[0]
        original = next((q for q in fallback if q.flight_no == outbound), None)
        if original is None:
            raise FetchFailure("parse_error", "outbound_card_not_matched")
        card.get_by_text("选为去程", exact=True).click(timeout=2500)
        stage = "return_cards"
        returns = wait_for(lambda _: _flight_cards(page, "订票"), 12)
        if not returns:
            raise FetchFailure("parse_error", "return_cards_not_found")
        return_estimates = [replace(original, price=start_price, return_flight_no=inbound,
                                    extra=json.dumps({"return_flight_no": inbound}))
                            for start_price, inbound, _ in returns[:3]]
        for _, inbound, card in returns[:3]:
            if time.monotonic() >= deadline:
                record_failure("timeout", "fare_details_deadline")
                break
            body()
            # This button expands fare offers. The later 预订 button is not used.
            stage = "expand_return"
            try:
                card.get_by_text("订票", exact=True).click(timeout=2500)
                parent = card.locator("..").filter(has_text="往返含税价")
                fares = parent.locator(".flight-seats .seat-row:visible")
                if not wait_for(lambda _: fares.count(), 5):
                    record_failure("parse_error", "fare_rows_not_found")
                    continue
            except BrowserTimeout:
                record_failure("timeout", "fare_details_expand_return_timeout")
                continue
            diagnostics["expanded_return_options"] += 1
            stage = "fare_price"
            priced = []
            for fare in fares.all():
                try:
                    fare_text = fare.inner_text(timeout=2500)
                    prices = MONEY.findall(fare_text)
                except BrowserTimeout:
                    record_failure("timeout", "fare_details_price_timeout")
                    continue
                if len(prices) == 1:
                    priced.append((float(prices[0].replace(",", "")), fare, fare_text))
            if not priced:
                record_failure("parse_error", "fare_price_not_found")
            for price, fare, fare_text in sorted(priced, key=lambda item: item[0])[:2]:
                if time.monotonic() >= deadline:
                    record_failure("timeout", "fare_details_deadline")
                    break
                stage = "baggage_rules"
                body()
                inline = INLINE_CHECKED.search(fare_text)
                link = fare.get_by_text("行李规定", exact=True)
                if inline and link.count() == 0:
                    link = fare.get_by_text(re.compile(re.escape(inline[0])))
                legs = {}
                try:
                    if link.count() == 1:
                        card.hover(timeout=2500)
                        # Never reuse a prior fare's still-open floating panel.
                        if wait_for(lambda text: not _baggage_popup(text), 1.5):
                            link.hover(timeout=2500)
                            def both_rules(text):
                                nonlocal legs
                                popup = _baggage_popup(text)
                                rules = (_table_baggage(page) or included_baggage(popup)) if popup else {}
                                if rules:
                                    legs = rules
                                return len(rules) == 2
                            if not wait_for(both_rules, 3) and not _baggage_popup(body()):
                                link.click(timeout=2500)
                                wait_for(both_rules, 3)
                            if len(legs) != 2:
                                legs.update(_tabbed_baggage(page, wait_for))
                except BrowserTimeout:
                    record_failure("timeout", "fare_details_baggage_timeout")
                if len(legs) != 2:
                    record_failure("parse_error", "fare_baggage_rules_incomplete")
                offer = {"itinerary_confirmed": True, "tax_inclusive_ticket_confirmed": True, **legs}
                if fare_text.strip().startswith("经济舱"):
                    offer["cabin"] = "economy"
                if inline:
                    offer["fare_rule"] = inline[0]
                quote = replace(original, price=price, return_flight_no=inbound,
                                price_basis="selected_round_trip_tax", baggage_offer=offer)
                confirmed.append(replace(quote, extra=json.dumps({
                    "return_flight_no": inbound, "baggage_offer": offer,
                    "baggage_inclusive_total": baggage_total(quote)}, ensure_ascii=False)))
    except FetchFailure as error:
        status = error.status
        diagnostics["detail_evidence"] = str(error)
    except BrowserTimeout:
        status = "timeout"
        diagnostics["detail_evidence"] = f"fare_details_{stage}_timeout"
    except BrowserError:
        status = "error"
        diagnostics["detail_evidence"] = f"fare_details_{stage}_error"
    diagnostics["detail_status"] = status
    diagnostics["fare_options"] = len(confirmed)
    diagnostics["fare_options_missing_baggage_rules"] = sum(
        not {"outbound", "inbound"} <= q.baggage_offer.keys() for q in confirmed)
    detailed_returns = {q.return_flight_no for q in confirmed}
    quotes = confirmed + [q for q in return_estimates if q.return_flight_no not in detailed_returns]
    if len(quotes) < 3:
        quotes += [q for q in fallback if not quotes or q.flight_no != quotes[0].flight_no]
    if status != "ok":
        raise FetchFailure(status, diagnostics.get("detail_evidence", "fare_details_" + status), prices=quotes)
    return quotes
