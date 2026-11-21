# Extracted from dengmeiluan/ticket-monitoring, Apache-2.0.
# Pinned commit: 5cfa6723f072dffb603724a23b7a2e13cf82c5d1.
# Browser/session and international RT methods retained from upstream.
# Unrelated scheduling, notification, channel policy and diagnostics omitted.


import json, re, time, urllib.parse
from typing import List
from .core.models import PRICE_MIN, PRICE_MAX, FlightPrice
from .challenge import classify_text

# Pilot accepts city codes SHA and OSA only; no airport-code routing is exposed.
def airport_city_code(code):
    if code not in ('SHA', 'OSA'):
        raise ValueError('Tongcheng pilot only supports SHA/OSA city codes')
    return code

def city_short_of(code):
    return {'SHA': '上海', 'OSA': '大阪'}.get(code, code)

class InternationalRoundTrip:
    CITY_NAME = {'SHA': '上海', 'OSA': '大阪'}


    def _intl_rt_url(self, fc: str, tc: str, date: str,
                     ret_date: str) -> str:
        """book1 国际往返结果页：para 用城市码，展示名用中文短名。"""
        def _name(city: str) -> str:
            return self.CITY_NAME.get(city) or city_short_of(city) or city
        para = "%s*%s*%s*%s*RT*1_0_0*Y|S|C|F" % (
            fc, tc, date, ret_date)
        return ("https://www.ly.com/iflight/book1.html?para="
                + urllib.parse.quote(para, safe="*|")
                + "&departureCity=" + urllib.parse.quote(_name(fc))
                + "&departureCityIsInter=0&departAirport&departAirportCode"
                + "&arrivalCity=" + urllib.parse.quote(_name(tc))
                + "&arrivalCityIsInter=1&arriveAirport&arriveAirportCode"
                + "&advanced=false")

    @staticmethod
    def _parse_intl_rt_entry(e: dict, date: str, ret_date: str):
        """一条 ts/list res → 往返行；只认带两段航班且 tp 为正的条目。"""
        lng = str(e.get("lng") or "")
        legs = lng.split("|")
        if len(legs) != 2:
            return None
        try:
            ret_flight, ret_day = legs[1].rsplit("_", 1)
        except ValueError:
            return None
        if ret_day != str(ret_date).replace("-", ""):
            return None
        try:
            price = float(e.get("tp") or e.get("avg") or 0)
        except (TypeError, ValueError):
            return None
        if not (PRICE_MIN <= price <= PRICE_MAX):
            return None
        acs = e.get("acs") or []
        if not acs:
            return None
        ac = acs[0] or {}
        deps = e.get("dants") or [{}]
        arrs = e.get("aants") or [{}]
        dep = str(e.get("dt") or "")
        arr = str(e.get("at") or "")
        flight = str(ac.get("an") or "")
        airline = str(ac.get("acn") or "")
        return {
            "airline": airline,
            "flight": flight,
            "dep": dep,
            "arr": arr,
            # 兼容报表/明细消费端的标准键（与其它渠道 extra 行同构）
            "depTime": dep,
            "arrTime": arr,
            "code": flight,
            "name": airline,
            "date": date,
            "intl": True,
            "flightKey": str(e.get("flightKey") or ""),
            "depAirport": str((deps[0] or {}).get("an") or ""),
            "arrAirport": str((arrs[0] or {}).get("an") or ""),
            "price": price,
            "priceLabel": "往返含税总价",
            "basis": "product_round_trip_total",
            "totalBasis": "product_round_trip_total",
            "tripType": "RT",
            "depDate": date,
            "backDate": ret_date,
            "returnFlight": ret_flight,
            "returnDate": ret_day,
            "rType": str(e.get("rType") or ""),
            "pair": lng,
            "source": "miflightapi/ts/list",
        }

    def _fetch_intl_rt(self, from_city: str, to_city: str,
                       dates: List[str], ret_date: str) -> List[FlightPrice]:
        """国际往返：登录档案下 book1 RT 页 → ts/list 的 tp 含税总价。

        每条 res 的 `lng` 是 `去程航班_日期|返程航班_日期`；`tp` 是页面
        卡片的「含税总价」，与同一条目的去/返两段一起出现。只落这个
        总价，绝不把两段单程相加。页面未登录会跳 passport，如实记 login。
        """
        fc, tc = (airport_city_code(from_city.upper()),
                  airport_city_code(to_city.upper()))
        results: List[FlightPrice] = []
        # 链路闸门（r1063 T8）：国际往返这条通路被策略停用时如实返回空
        # （不启浏览器；往返与单程各自独立记账，互不连坐）。
        if not self.path_gate("往返", "intl"):
            return results
        bak = self.use_mobile
        self.use_mobile = False
        try:
            with self.browser(headless=(False if self.probe_mode else None)) as ctx:
                page = self.new_page(ctx)
                payloads: list = []

                def _on_resp(r):
                    if "miflightapi/ts/list" not in r.url:
                        return
                    try:
                        payloads.append(json.loads(r.text()))
                    except Exception:  # noqa: BLE001
                        pass

                page.on("response", _on_resp)
                for date in dates:
                    t0 = time.time()
                    url = self._intl_rt_url(fc, tc, date, ret_date)
                    nav_err = None
                    payloads.clear()
                    try:
                        page.goto(url, wait_until="domcontentloaded",
                                  timeout=45000)
                    except Exception as e:  # noqa: BLE001
                        nav_err = (self._exc_status(e),
                                   "往返页导航异常: %s" % e)
                        self.logger.warning(
                            "[tongcheng][intl-rt] 导航异常: %s", e)
                    rows = {}
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        try:
                            page.wait_for_timeout(1000)
                        except Exception:  # noqa: BLE001
                            break
                        for pl in list(payloads):
                            if not isinstance(pl, dict):
                                continue
                            data = pl.get("data") or {}
                            for e in (data.get("res") or []):
                                row = self._parse_intl_rt_entry(
                                    e, date, ret_date)
                                if not row:
                                    continue
                                key = (row["flight"], row["returnFlight"])
                                old = rows.get(key)
                                if old is None or row["price"] < old["price"]:
                                    rows[key] = row
                        done = any(
                            isinstance(pl, dict)
                            and (pl.get("data") or {}).get("done") == 1
                            for pl in list(payloads))
                        if rows and (done or time.monotonic() > deadline - 7):
                            break
                    url_now = str(getattr(page, "url", "") or "")
                    if rows:
                        best = min(rows.values(),
                                   key=lambda r: r["price"])
                        type(self).login_suspect = False
                        results.append(FlightPrice(
                            platform=self.name,
                            from_city=from_city, to_city=to_city,
                            depart_date=date,
                            price=float(best["price"]),
                            airline=str(best.get("airline") or ""),
                            flight_no=str(best.get("flight") or ""),
                            depart_time=str(best.get("dep") or ""),
                            arrive_time=str(best.get("arr") or ""),
                            extra=json.dumps(list(rows.values()),
                                             ensure_ascii=False)))
                        self.path_note(
                            "往返", "ok", rows=len(rows), date=date,
                            url=url_now,
                            detail="同程 PC 国际往返列表（含税总价）",
                            ms=int((time.time() - t0) * 1000))
                        self.logger.info(
                            "[tongcheng][intl-rt] %s→%s 返 %s：%d 条，"
                            "最低往返含税总价 ¥%.0f（%s + %s）",
                            from_city, to_city, ret_date, len(rows),
                            best["price"], best.get("flight"),
                            best.get("returnFlight"))
                    else:
                        try:
                            body = page.inner_text("body")[:2500]
                        except Exception:  # noqa: BLE001
                            body = ""
                        sig = classify_text(body, url_now)
                        login = bool(
                            "passport.ly.com" in url_now
                            or (sig and sig.status == "login")
                            or "请登录" in body)
                        blocked = bool(
                            re.search(r"验证码|安全验证|滑块|captcha", body)
                            or (sig and sig.status == "blocked"))
                        if nav_err:
                            status, detail = nav_err
                        elif login:
                            type(self).login_suspect = True
                            status, detail = "login", "同程国际往返需登录"
                        elif blocked:
                            status, detail = "blocked", "同程国际往返人机验证"
                        elif re.search(r"没有符合|暂无.{0,8}航班|"
                                       r"未找到.{0,8}航班", body):
                            status, detail = "empty", "站点明确无可售往返"
                        else:
                            status, detail = (
                                "error", "往返列表未解析到含税总价")
                        self.path_note("往返", status, date=date,
                                       detail=detail, url=url_now,
                                       ms=int((time.time() - t0) * 1000))
                        self.logger.warning(
                            "[tongcheng][intl-rt] %s→%s 未出往返总价（%s）",
                            from_city, to_city, detail)
                    self._sleep()
        finally:
            self.use_mobile = bak
        return results
