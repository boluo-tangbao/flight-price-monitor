"""Ctrip round-trip product parser adapted from dengmeiluan/ticket-monitoring.
Apache-2.0; upstream commit 658ae9c9d50f8af656208fd972580752e349d844.
Modifications: standalone parser; require a flight number and labelled total.
"""
import re
import json
import time
from urllib.parse import urlsplit

from core.collection import FetchFailure
from core.models import RoundTripQuote


def check_access(body, url, http_status, stage):
    """Report only fixed marker names, HTTP status and hostname, never page text/tokens."""
    host = urlsplit(str(url)).hostname or "unknown"
    context = f"stage={stage};http={http_status};host={host}"
    if http_status in (403, 429):
        raise FetchFailure("blocked", "http_rejected;" + context)
    if re.search(r"whaleguard", body + " " + str(url), re.I):
        raise FetchFailure("blocked", "verification_page;marker=whaleguard;" + context)
    # A normal SMS-code login form is not evidence of WAF rejection.
    if (host == "passport.ctrip.com" or host.endswith(".passport.ctrip.com")
            or re.search(r"请先登录|请登录后(?:查看|查询)|登录后.{0,6}(?:才|方可).{0,6}(?:查看|查询)", body)):
        raise FetchFailure("login_required", "login_page;" + context)
    for marker, pattern in (("security_verification", r"(?:请|需要|须|进行|完成).{0,12}(?:安全验证|访问验证)|^\s*(?:安全验证|访问验证)\s*$"),
                            ("slider", r"滑动验证|拖动.{0,8}滑块|滑块验证"),
                            ("captcha_prompt", r"请.{0,12}(完成|输入).{0,6}验证码")):
        if re.search(pattern, body, re.I | re.M):
            raise FetchFailure("blocked", "verification_marker;marker=" + marker + ";" + context)


def build_url(fc, tc, date, ret):
    # PC search is city based. Do not silently widen an airport-only request.
    if fc in {"PVG", "PEK", "PKX", "NRT", "HND", "KIX", "ITM"} or tc in {
            "PVG", "PEK", "PKX", "NRT", "HND", "KIX", "ITM"}:
        raise FetchFailure("unsupported", "airport_scope_not_verified_use_city_code")
    if not re.fullmatch(r"[A-Z]{3}", fc) or not re.fullmatch(r"[A-Z]{3}", tc):
        raise ValueError("Expected uppercase city codes")
    return (f"https://flights.ctrip.com/online/list/round-{fc.lower()}-{tc.lower()}"
            f"?depdate={date}_{ret}&cabin=y_s&adult=1&child=0&infant=0&containstax=1")


RISK_COOKIES = ("cticket", "_bfa", "MKT_CKID")


def prepare_homepage(crawler, ctx):
    """One homepage visit; wait for cookie readiness without refreshing or solving.

    Cookie presence follows upstream's readiness signal, not proof of login or
    server acceptance. Stop on a challenge, including while waiting for cookies.
    """
    page = crawler.new_page(ctx)
    try:
        crawler.round_trip_diagnostics["homepage_visits"] += 1
        response = page.goto("https://flights.ctrip.com/", wait_until="domcontentloaded",
                             timeout=crawler.timeout_ms)
        status = response.status if response else None
        check_access("", page.url, status, "homepage")
        check_access(page.inner_text("body", timeout=crawler.timeout_ms), page.url,
                     status, "homepage")
        page.wait_for_timeout(2500)
        deadline = time.monotonic() + min(12, crawler.timeout_ms / 1000)
        while True:
            check_access(page.inner_text("body", timeout=crawler.timeout_ms), page.url,
                         status, "homepage_ready")
            # Scope the check to cookies the browser can send to this entry.
            names = {c.get("name") for c in ctx.cookies(["https://flights.ctrip.com/"])}
            ready = bool(names.intersection(RISK_COOKIES))
            crawler.round_trip_diagnostics["warmup_cookie_present"] = ready
            if ready:
                crawler.logger.info("[ctrip] 首页预热完成；http=%s; risk_cookie_present=true", status)
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchFailure("error", "warmup_missing_risk_cookie;stage=homepage_ready;"
                                   f"http={status};host=flights.ctrip.com")
            # Pump browser events so delayed homepage scripts can finish writing.
            page.wait_for_timeout(min(800, max(1, int(remaining * 1000))))
    finally:
        try:
            page.close()
        except Exception:
            pass


def fetch_round_trip(crawler, fc, tc, date, ret):
    url = build_url(fc, tc, date, ret)
    crawler.round_trip_diagnostics = {"homepage_visits": 0, "search_visits": 0,
                                      "warmup_cookie_present": None}
    mobile = crawler.use_mobile
    crawler.use_mobile = False
    try:
        with crawler.browser() as ctx:
            # Upstream uses separate warmup/result tabs in the same context.
            prepare_homepage(crawler, ctx)
            page = crawler.new_page(ctx)
            crawler.round_trip_diagnostics["search_visits"] += 1
            response = page.goto(url, wait_until="domcontentloaded", timeout=crawler.timeout_ms)
            http_status = response.status if response else None
            check_access("", page.url, http_status, "roundtrip")
            deadline = time.monotonic() + crawler.timeout_ms / 1000
            while True:
                body = page.inner_text("body", timeout=crawler.timeout_ms)
                check_access(body, page.url, http_status, "roundtrip")
                if f"round-{fc.lower()}-{tc.lower()}" not in page.url or f"{date}_{ret}" not in page.url:
                    raise FetchFailure("error", "redirected_or_wrong_search_context")
                rows = ProductParser._parse_rt_body(body, date, ret)
                # First release only accepts an explicit tax-inclusive product total.
                rows = [r for r in rows if r["basis"] == "product_round_trip_tax"
                        and 100 <= r["price"] <= 200000]
                if rows:
                    best = min(rows, key=lambda r: r["price"])
                    return [RoundTripQuote(
                        platform=crawler.name, from_city=fc, to_city=tc, depart_date=date,
                        return_date=ret, price=best["price"], airline=best["airline"],
                        flight_no=best["flight"], depart_time=best["dep"], arrive_time=best["arr"],
                        source_url=url, extra=json.dumps(rows, ensure_ascii=False))]
                if re.search(r"暂无.{0,8}(航班|机票)|没有.{0,8}(航班|机票)|未找到.{0,8}航班", body):
                    raise FetchFailure("empty", "explicit_no_available_round_trip")
                if time.monotonic() >= deadline:
                    raise FetchFailure("parse_error", "no_tax_inclusive_round_trip_product_card")
                page.wait_for_timeout(1000)
    finally:
        crawler.use_mobile = mobile


class ProductParser:
    _RE_RT_PRICE = re.compile(r"^¥\s*([0-9][0-9,]*)\s*起$")
    _RE_RT_TIME = re.compile(r"^\d{2}:\d{2}$")
    _RE_RT_FLIGHT = re.compile(r"^([A-Z0-9]{2}\d{1,4})\s*(.*)$")
    _RT_LABELS = ("往返含税价", "往返总价")

    @classmethod
    def _parse_rt_body(cls, body: str, date: str, ret_date: str) -> list:
        """PC 往返列表正文 → 统一行（价格=渠道往返含税总价）。

        买点只在「¥N起 + 产品总价标签」这一对（国际「往返含税价」/
        国内「往返总价」）：价格是产品自己给出的往返组合总价，不把
        两段单程相加。卡片缺时刻/航班号时不臆造，整卡跳过（残缺价
        会被上游当头条价，绕过明细守卫）。
        """
        lines = [ln.strip() for ln in (body or "").splitlines()]
        rows, seen = [], set()
        boundary = 0  # 上一张卡「选为去程」之后，防止跨卡取时刻
        for i, ln in enumerate(lines):
            m = cls._RE_RT_PRICE.match(ln)
            if not m:
                continue
            ahead = [x for x in lines[i + 1:i + 4] if x]
            label = next((x for x in ahead
                          if any(k in x for k in cls._RT_LABELS)), "")
            if not label:
                continue
            price = float(m.group(1).replace(",", ""))
            window = lines[max(boundary, i - 14):i]
            times = [(j, t) for j, t in enumerate(window)
                     if cls._RE_RT_TIME.match(t)]
            if len(times) < 2:
                continue
            dep_j, dep_t = times[0]
            arr_j, arr_t = times[1]
            fl_idx, flight, aircraft = None, "", ""
            for j, txt in enumerate(window):
                fm = cls._RE_RT_FLIGHT.match(txt)
                if fm and j < dep_j:
                    fl_idx, flight = j, fm.group(1)
                    aircraft = fm.group(2).strip()
                    break
            if fl_idx is not None and fl_idx > 0:
                airline = window[fl_idx - 1]
            elif dep_j > 0:
                airline = window[dep_j - 1]
            else:
                airline = ""
            if not flight:
                continue
            row = {
                "airline": airline,
                "flight": flight,
                "aircraft": aircraft,
                "dep": dep_t,
                "arr": arr_t,
                "depAirport": (window[dep_j + 1]
                               if dep_j + 1 < len(window) else ""),
                "arrAirport": (window[arr_j + 1]
                               if arr_j + 1 < len(window) else ""),
                "price": price,
                "priceLabel": label,
                "basis": ("product_round_trip_tax" if "含税" in label
                          else "product_round_trip_total"),
                "tripType": "RT",
                "depDate": date,
                "backDate": ret_date,
            }
            key = (flight, dep_t, arr_t, price)
            if key not in seen:
                seen.add(key)
                rows.append(row)
            for k in range(i + 1, min(i + 5, len(lines))):
                if lines[k] == "选为去程":
                    boundary = k + 1
                    break
            else:
                boundary = i + 2
        return rows
