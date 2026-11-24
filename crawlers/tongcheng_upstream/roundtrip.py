# Adapted from dengmeiluan/ticket-monitoring, Apache-2.0.
# Pinned commit: 5cfa6723f072dffb603724a23b7a2e13cf82c5d1.
# The desktop navigation and polling flow consumes one strict list collector.

import re
import time
import urllib.parse

from .challenge import classify_text


class InternationalRoundTrip:
    CITY_NAME = {"SHA": "上海", "OSA": "大阪"}

    def _intl_rt_url(self, fc: str, tc: str, date: str, ret_date: str) -> str:
        """Open the Book1 desktop search for one adult and both travel dates."""
        para = f"{fc}*{tc}*{date}*{ret_date}*RT*1_0_0*Y|S|C|F"
        return ("https://www.ly.com/iflight/book1.html?para="
                + urllib.parse.quote(para, safe="*|")
                + "&departureCity=" + urllib.parse.quote(self.CITY_NAME[fc])
                + "&departureCityIsInter=0&departAirport&departAirportCode"
                + "&arrivalCity=" + urllib.parse.quote(self.CITY_NAME[tc])
                + "&arrivalCityIsInter=1&arriveAirport&arriveAirportCode"
                + "&advanced=false")

    def _fetch_intl_rt(self, from_city, to_city, depart, back, *, capture, list_progress):
        """Reuse the upstream desktop search and wait on the shared list collector."""
        if not self.path_gate("往返", "intl"):
            return
        url = self._intl_rt_url(from_city, to_city, depart, back)
        previous_mobile = self.use_mobile
        self.use_mobile = False
        try:
            with self.browser(headless=False if self.probe_mode else None) as ctx:
                page = self.new_page(ctx)
                page.on("response", capture)
                started = time.monotonic()
                nav_error = None
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45000)
                except Exception as error:
                    nav_error = self._exc_status(error)
                    self.logger.warning("[tongcheng][intl-rt] 导航异常: %s", type(error).__name__)

                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    try:
                        page.wait_for_timeout(1000)
                    except Exception:
                        break
                    rows, done = list_progress()
                    if rows and (done or time.monotonic() > deadline - 7):
                        break

                url_now = page.url
                rows, _ = list_progress()
                if rows:
                    type(self).login_suspect = False
                    status = "ok"
                    detail = "同程 PC 国际往返列表（含税总价）"
                else:
                    try:
                        body = page.inner_text("body")[:2500]
                    except Exception:
                        body = ""
                    signal = classify_text(body, url_now)
                    if nav_error:
                        status, detail = nav_error, "往返页导航异常"
                    elif "passport.ly.com" in url_now or (signal and signal.status == "login") or "请登录" in body:
                        type(self).login_suspect = True
                        status, detail = "login", "同程国际往返需登录"
                    elif re.search(r"验证码|安全验证|滑块|captcha", body) or (signal and signal.status == "blocked"):
                        status, detail = "blocked", "同程国际往返人机验证"
                    elif re.search(r"没有符合|暂无.{0,8}航班|未找到.{0,8}航班", body):
                        status, detail = "empty", "站点明确无可售往返"
                    else:
                        status, detail = "error", "往返列表未解析到含税总价"

                self.path_note("往返", status, date=depart, url=url_now, rows=rows,
                               detail=detail, ms=int((time.monotonic() - started) * 1000))
                self.logger.info("[tongcheng][intl-rt] %s→%s 返 %s：%s，%d 条",
                                 from_city, to_city, back, detail, rows)
                self._sleep()
        finally:
            self.use_mobile = previous_mobile
