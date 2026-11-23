"""Tongcheng international round-trip validation via the normal desktop results page.

The entry URL and response field mapping are adapted from dengmeiluan/ticket-monitoring
(Apache-2.0, commit 5cfa6723f072dffb603724a23b7a2e13cf82c5d1).
"""
from contextlib import contextmanager
from datetime import date
import json
import re
from urllib.parse import urlencode, urlsplit

from playwright.sync_api import Error as BrowserError

from core.collection import FetchFailure
from core.models import RoundTripQuote


ROUTE = ("SHA", "OSA")
RESULT_HOSTS = {"www.ly.com", "tiflight.ly.com", "m.ly.com", "ly.com"}


def search_url(origin, destination, depart, back):
    if (origin, destination) != ROUTE:
        raise FetchFailure("unsupported", "tongcheng_pilot_only_sha_osa")
    if date.fromisoformat(back) <= date.fromisoformat(depart):
        raise ValueError("Return date must follow departure date")
    params = {"para": f"{origin}*{destination}*{depart}*{back}*RT*1_0_0*Y|S|C|F",
              "departureCity": "上海", "departureCityIsInter": 0,
              "departAirport": "", "departAirportCode": "", "arrivalCity": "大阪",
              "arrivalCityIsInter": 1, "arriveAirport": "", "arriveAirportCode": "", "advanced": "false"}
    return "https://www.ly.com/iflight/book1.html?" + urlencode(params, safe="*|")


def payload_quotes(payload, origin, destination, depart, back, url):
    data = payload.get("data") if isinstance(payload, dict) else None
    entries = data.get("res") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return []
    prices = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pair = str(entry.get("lng") or "").split("|")
        if len(pair) != 2:
            continue
        legs = [re.fullmatch(r"([A-Z0-9]{2}\d{1,4})_(\d{8})", leg) for leg in pair]
        if not all(legs) or (legs[0][2], legs[1][2]) != (depart.replace("-", ""), back.replace("-", "")):
            continue
        try:
            raw = entry["tp"]
            if isinstance(raw, bool):
                continue
            price = float(raw)
        except (KeyError, ValueError, TypeError, OverflowError):
            continue
        if not 100 <= price <= 200000:
            continue
        prices.append(RoundTripQuote("tongcheng", origin, destination, depart, price,
                      flight_no=legs[0][1], return_date=back, return_flight_no=legs[1][1],
                      price_basis="tongcheng_listed_round_trip_tax", source_url=url,
                      extra=json.dumps({"cabin_verified": False, "final_fare_verified": False})))
    return prices


def check_access(url, text, status=None):
    from .tongcheng_upstream.challenge import classify_text
    hostname = urlsplit(url).hostname or "unknown"
    if status in (401, 403, 429):
        raise FetchFailure("login_required" if status == 401 else "blocked", f"tongcheng_http_{status}")
    signal = classify_text(text, url)
    if signal and signal.status == "login":
        raise FetchFailure("login_required", "tongcheng_login_page")
    if signal and signal.status == "blocked":
        raise FetchFailure("blocked", "tongcheng_verification_page")
    if hostname == "passport.ly.com" or re.search(r"请先登录|请登录后.{0,10}(查询|查看)|登录后.{0,10}查看机票", text):
        raise FetchFailure("login_required", "tongcheng_login_page")
    if hostname not in RESULT_HOSTS:
        raise FetchFailure("error", "tongcheng_unexpected_host")


@contextmanager
def desktop(crawler, headed=None, login=False):
    from .tongcheng_pc import TongchengDesktop
    worker = TongchengDesktop(crawler)
    crawler.tongcheng_pc_session = worker
    with worker.browser(headless=crawler.headless if headed is None else not headed,
                        save_session="login" if login else None) as ctx:
        yield ctx


def fetch_round_trip(crawler, origin, destination, depart, back):
    from .tongcheng_pc import TongchengDesktop
    url = search_url(origin, destination, depart, back)
    crawler.round_trip_searches = 0
    diagnostics = crawler.round_trip_diagnostics = {
        "workflow": "ticket-monitoring-5cfa6723",
        "list_response_count": 0, "list_http_statuses": [],
        "unrecognized_list_host_count": 0, "list_payload_count": 0,
        "offer_rows_seen": 0, "matched_offer_rows": 0,
        "list_business_codes": [], "search_document_requests": 0,
    }
    quotes = {}
    api_status = None

    def capture(response):
        nonlocal api_status
        endpoint = urlsplit(response.url)
        if not endpoint.path.rstrip("/").endswith("/miflightapi/ts/list"):
            return
        host = endpoint.hostname or ""
        if endpoint.scheme != "https" or not (host == "ly.com" or host.endswith(".ly.com")):
            diagnostics["unrecognized_list_host_count"] += 1
            return
        diagnostics["list_response_count"] += 1
        if response.status not in diagnostics["list_http_statuses"]:
            diagnostics["list_http_statuses"].append(response.status)
        if response.status in (401, 403, 429, 444):
            api_status = response.status
            return
        if response.status != 200:
            return
        try:
            payload = response.json()
        except (BrowserError, ValueError):
            return
        diagnostics["list_payload_count"] += 1
        # Record only allowlisted numeric business states, never raw messages.
        code = payload.get("code") if isinstance(payload, dict) else None
        if str(code) in ("0", "200", "401", "403", "429", "444"):
            safe_code = int(code)
            if safe_code not in diagnostics["list_business_codes"]:
                diagnostics["list_business_codes"].append(safe_code)
            if safe_code in (401, 403, 429, 444):
                api_status = safe_code
                return
        data = payload.get("data") if isinstance(payload, dict) else None
        rows = data.get("res") if isinstance(data, dict) else None
        if isinstance(rows, list):
            diagnostics["offer_rows_seen"] += len(rows)
        parsed = payload_quotes(payload, origin, destination, depart, back, url)
        diagnostics["matched_offer_rows"] += len(parsed)
        for quote in parsed:
            key = (quote.flight_no, quote.return_flight_no)
            if key not in quotes or quote.price < quotes[key].price:
                quotes[key] = quote

    worker = TongchengDesktop(crawler, diagnostics, capture)
    if crawler.config.get('tongcheng_round_trip_fare_details') is True:
        from .tongcheng_fare_details import read_fare_inventory
        def detail_reader(page):
            fallback = list(quotes.values())

            def check_detail_access(current_url, text, status=None):
                check_access(current_url, text, status or diagnostics.get('page_http_status'))
                check_access(current_url, text, api_status)
                if api_status == 444:
                    raise FetchFailure('blocked', 'tongcheng_list_request_rejected_444')

            try:
                return read_fare_inventory(
                    page, (origin, destination, depart, back, url), fallback, check_detail_access, diagnostics,
                    preferred_pair=crawler.config.get('tongcheng_detail_pair'),
                    current_quotes=lambda: list(quotes.values()))
            except FetchFailure as error:
                diagnostics.setdefault('detail_status', error.status)
                error.prices = sorted(error.prices, key=lambda q: (
                    q.price_basis != "selected_round_trip_tax", q.price))[:3]
                raise
            except BrowserError as error:
                status = 'timeout' if 'timeout' in type(error).__name__.lower() else 'error'
                diagnostics['detail_status'] = status
                raise FetchFailure(status, 'tongcheng_detail_' + diagnostics.get('detail_stage', 'unknown'),
                                   prices=sorted(fallback, key=lambda q: q.price)[:3])
        worker.detail_reader = detail_reader
    try:
        # Execute the upstream international RT branch, including its desktop
        # browser, navigation challenge handler and response completion loop.
        worker._fetch_intl_rt(origin, destination, [depart], back)
    finally:
        crawler.round_trip_searches = diagnostics["search_document_requests"]
    if api_status in (401, 403, 429):
        check_access(url, "", api_status)
    check_access(url, "", diagnostics.get("page_http_status"))
    if api_status == 444:
        raise FetchFailure("error", "tongcheng_list_request_rejected_444")
    if worker.last_outcome == "login":
        raise FetchFailure("login_required", "tongcheng_login_page")
    if worker.last_outcome == "blocked":
        raise FetchFailure("blocked", "tongcheng_verification_page")
    if worker.last_outcome == "timeout":
        raise FetchFailure("timeout", "tongcheng_navigation_timeout")
    if hasattr(worker, 'detail_quotes'):
        if crawler.config.get("tongcheng_keep_list_quotes") is True:
            detailed = {(quote.flight_no, quote.return_flight_no, quote.price)
                        for quote in worker.detail_quotes}
            listed = sorted((quote for quote in quotes.values()
                             if (quote.flight_no, quote.return_flight_no, quote.price) not in detailed),
                            key=lambda quote: quote.price)[:3]
            return worker.detail_quotes + listed
        return worker.detail_quotes
    if quotes:
        return sorted(quotes.values(), key=lambda quote: quote.price)[:3]
    if worker.last_outcome == "empty":
        raise FetchFailure("empty", "tongcheng_explicit_no_flights")
    if not diagnostics["list_response_count"]:
        raise FetchFailure("parse_error", "tongcheng_no_list_response")
    if not diagnostics["list_payload_count"]:
        raise FetchFailure("parse_error", "tongcheng_list_response_not_usable")
    if not diagnostics["offer_rows_seen"]:
        raise FetchFailure("parse_error", "tongcheng_list_no_offer_rows")
    raise FetchFailure("parse_error", "tongcheng_offer_rows_failed_price_or_date_validation")
