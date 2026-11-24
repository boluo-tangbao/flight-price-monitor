"""Fliggy's international round-trip results and calendar fallback.

Calendar request and response fields adapted from dengmeiluan/ticket-monitoring
(Apache-2.0, commit 658ae9c9d50f8af656208fd972580752e349d844).
"""

from contextlib import contextmanager
from datetime import date
import json
import math
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit

import httpx
from playwright.sync_api import Error as BrowserError, TimeoutError as BrowserTimeout, sync_playwright

from core.collection import FetchFailure
from core.models import RoundTripQuote
from .fliggy_fare_details import check_access, read_fare_details


CALENDAR_URL = "https://r.fliggy.com/cheapestCalendar/pc"
CALENDAR_REFERER = "https://sijipiao.fliggy.com/"
SEARCH_URL = "https://sijipiao.fliggy.com/ie/flight_search_result.htm"
LOGIN_HOME = "https://www.fliggy.com/?tab=flight"
CITY_NAMES = {"SHA": "上海", "OSA": "大阪"}
ACCESS_DENIED = re.compile(r"referer is not safe|access denied|访问受限|安全验证|滑动验证|captcha", re.I)
ERROR_URL = re.compile(r"(?:https?://|(?<!:)//)[^\s<>\"'，。；]+", re.I)
ERROR_SECRET = re.compile(
    r"\b(?:authorization|cookie|(?:access|refresh)[_-]?token|token|session[_-]?id|password|signature|sign)"
    r"\s*[:=]\s*(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;，。；]+)",
    re.I,
)


def _validate_itinerary(origin, destination, depart, back):
    if set((origin, destination)) != CITY_NAMES.keys():
        raise FetchFailure("unsupported", "fliggy_round_trip_only_supports_sha_osa")
    try:
        dates = tuple(date.fromisoformat(value) for value in (depart, back))
    except (TypeError, ValueError):
        raise FetchFailure("parse_error", "fliggy_invalid_round_trip_dates") from None
    if tuple(day.isoformat() for day in dates) != (depart, back) or dates[1] <= dates[0]:
        raise FetchFailure("parse_error", "fliggy_invalid_round_trip_dates")


def _calendar_params(origin, destination, depart, back):
    return {
        "bizType": 1, "depCityCode": origin, "arrCityCode": destination,
        "leaveDate": depart, "backDate": back, "calendarType": 0,
        "tripType": 1, "callback": "cb",
    }


def _search_url(origin, destination, depart, back):
    return SEARCH_URL + "?" + urlencode({
        "tripType": 1, "depCity": origin, "arrCity": destination,
        "depCityName": CITY_NAMES[origin], "arrCityName": CITY_NAMES[destination],
        "depDate": depart, "arrDate": back,
    })


def _decode_calendar(body):
    wrapped = re.fullmatch(r"\s*cb\s*\((.*)\)\s*;?\s*", body, re.S)
    try:
        payload = json.loads(wrapped[1] if wrapped else body)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _record_calendar_error(payload, diagnostics):
    if payload is None:
        return
    code = payload.get("errorCode")
    if type(code) is int or (isinstance(code, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,48}", code)):
        diagnostics["calendar_error_code"] = code
    message = payload.get("errorMessage")
    if isinstance(message, str) and message.strip():
        redacted = ERROR_URL.sub("[URL]", message)
        redacted = ERROR_SECRET.sub("[redacted]", redacted)
        diagnostics["calendar_error_message"] = " ".join(redacted.split())[:240]


def _calendar_price(rows, depart, back, diagnostics=None):
    prices = []
    matching_rows = 0
    quoted_rows = []
    for row in rows:
        if not isinstance(row, dict) or (row.get("leaveDate"), row.get("backDate")) != (depart, back):
            continue
        matching_rows += 1
        if row.get("currency", "CNY") != "CNY":
            continue
        if any(isinstance(row.get(key), bool) for key in ("price", "tax")):
            continue
        try:
            fare, tax = float(row["price"]), float(row["tax"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(fare) and math.isfinite(tax):
            quoted_rows.append({"leaveDate": depart, "backDate": back, "price": fare, "tax": tax})
        total = round(fare + tax, 2)
        if math.isfinite(total) and fare >= 0 and tax >= 0 and total > 0:
            prices.append(total)
    if diagnostics is not None:
        diagnostics["calendar_matching_date_rows"] = matching_rows
        diagnostics["calendar_requested_date_prices"] = quoted_rows
    return min(prices, default=None)


def _fetch_calendar(origin, destination, depart, back, diagnostics):
    diagnostics["calendar_request_referer"] = CALENDAR_REFERER
    try:
        with httpx.Client(timeout=20, follow_redirects=False,
                          headers={"Referer": CALENDAR_REFERER}) as client:
            response = client.get(CALENDAR_URL, params=_calendar_params(origin, destination, depart, back))
    except httpx.TimeoutException:
        raise FetchFailure("timeout", "fliggy_calendar_request_timeout") from None
    except httpx.HTTPError:
        raise FetchFailure("error", "fliggy_calendar_request_failed") from None
    diagnostics["calendar_http_status"] = response.status_code
    payload = _decode_calendar(response.text)
    _record_calendar_error(payload, diagnostics)
    if response.status_code == 401:
        raise FetchFailure("login_required", "fliggy_calendar_login_required")
    if response.status_code in (403, 429):
        raise FetchFailure("blocked", "fliggy_calendar_access_denied")
    if response.is_redirect:
        host = urlsplit(response.headers.get("location", "")).hostname
        if host in {"login.taobao.com", "login.fliggy.com"}:
            raise FetchFailure("login_required", "fliggy_calendar_login_required")
    if response.status_code != 200:
        raise FetchFailure("error", "fliggy_calendar_http_error")
    if isinstance(payload, dict):
        message = str(payload.get("errorMessage") or "")
        if ACCESS_DENIED.search(message):
            raise FetchFailure("blocked", "fliggy_calendar_access_denied")
    rows = payload.get("result") if payload is not None else None
    if not isinstance(rows, list):
        raise FetchFailure("parse_error", "fliggy_calendar_no_usable_result")
    diagnostics["calendar_rows_seen"] = len(rows)
    price = _calendar_price(rows, depart, back, diagnostics)
    if price is None:
        if diagnostics["calendar_matching_date_rows"]:
            raise FetchFailure("unverified", "fliggy_calendar_requested_dates_no_quoted_price")
        raise FetchFailure("unverified", "fliggy_calendar_no_matching_round_trip_price")
    return RoundTripQuote("fliggy", origin, destination, depart, price, return_date=back,
                          price_basis="fliggy_calendar_round_trip_tax",
                          source_url=_search_url(origin, destination, depart, back))


@contextmanager
def _desktop_browser(crawler, *, headless):
    channel = crawler.config.get("fliggy_pc_browser_channel", "msedge")
    profile = Path(crawler.user_data_root) / "fliggy-pc"
    options = {"channel": channel} if channel != "chromium" else {}
    try:
        state = getattr(crawler, "cloud_storage_state", None)
        if state is not None:
            from core.cloud_session import cloud_desktop
            with cloud_desktop(state, {"width": 1440, "height": 950}) as context:
                yield context
            return
        with sync_playwright() as pw:
            with pw.chromium.launch_persistent_context(
                    str(profile), locale="zh-CN", headless=headless,
                    viewport={"width": 1440, "height": 950}, **options) as context:
                yield context
    except BrowserTimeout:
        raise FetchFailure("timeout", "fliggy_desktop_timeout") from None
    except BrowserError:
        raise FetchFailure("error", "fliggy_desktop_browser_error") from None


def interactive_login(crawler):
    with _desktop_browser(crawler, headless=False) as context:
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_HOME, wait_until="domcontentloaded", timeout=crawler.timeout_ms)
        try:
            input("请在打开的飞猪窗口手动登录。完成后回到终端按回车，关闭并保留桌面档案。\n")
        except EOFError:
            raise FetchFailure("error", "fliggy_manual_login_requires_terminal") from None


def _fetch_desktop(crawler, origin, destination, depart, back, diagnostics):
    url = diagnostics["search_url"]
    with _desktop_browser(crawler, headless=crawler.headless) as context:
        page = context.new_page()
        response = page.goto(url, wait_until="domcontentloaded", timeout=crawler.timeout_ms)
        check_access(page, url, response.status if response is not None else None)
        return read_fare_details(page, (origin, destination, depart, back,
                                       CITY_NAMES[origin], CITY_NAMES[destination], url), diagnostics)


def fetch_round_trip(crawler, origin, destination, depart, back):
    origin, destination = origin.upper(), destination.upper()
    _validate_itinerary(origin, destination, depart, back)
    diagnostics = crawler.round_trip_diagnostics = {
        "requested_dates": [depart, back],
        "search_url": _search_url(origin, destination, depart, back),
    }
    calendar_only = crawler.config.get("fliggy_calendar_only")
    references = []
    try:
        calendar = _fetch_calendar(origin, destination, depart, back, diagnostics)
    except FetchFailure as failure:
        diagnostics["calendar_outcome"] = failure.status
        diagnostics["calendar_evidence"] = str(failure)
        if calendar_only:
            raise
    else:
        diagnostics["calendar_outcome"] = "ok"
        references.append(calendar)
        if calendar_only:
            raise FetchFailure("unverified", "fliggy_calendar_only_baggage_unknown", prices=references)
    try:
        detailed = _fetch_desktop(crawler, origin, destination, depart, back, diagnostics)
    except FetchFailure as failure:
        diagnostics["desktop_outcome"] = failure.status
        diagnostics["desktop_evidence"] = str(failure)
        failure.prices = references + failure.prices
        raise
    diagnostics["desktop_outcome"] = "ok"
    return references + detailed
