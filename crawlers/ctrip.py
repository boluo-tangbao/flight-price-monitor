"""携程机票：m.ctrip.com H5 (taro) + 手机UA + XHR 拦截

数据源接口 flightListSearchForH5 返回 JSON。
每个航班(fltitem)的 policyinfo 内含多个销售政策，
每个政策有 tprice(含税价) 与 quantity(余票)。quantity 为 null/0 表示
无票(诱饵价)，必须剔除，否则会拿到买不到的超低价。
"""
import os
import re
import json
import urllib.parse
from typing import List
from playwright.sync_api import Error as PlaywrightError

from core.models import FlightPrice
from .base import BaseCrawler


class CtripCrawler(BaseCrawler):
    name = "ctrip"
    use_mobile = True  # 移动端 H5

    @property
    def user_data_dir(self):
        channel = self.config.get("pc_browser_channel", "chromium")
        if not self.use_mobile and channel in ("chrome", "msedge"):
            # Do not open a newer bundled Chromium profile in an older branded browser.
            path = os.path.join(self.user_data_root, f"ctrip-{channel}")
            os.makedirs(path, exist_ok=True)
            return path
        return super().user_data_dir

    def _check_pc_block(self, page):
        from core.collection import FetchFailure
        body = page.inner_text("body", timeout=self.timeout_ms)
        if re.search(r"whaleguard|访问被拒绝|access denied", str(body) + " " + str(page.url), re.I):
            raise FetchFailure("blocked", "携程风控拦截，登录未完成；停止重试，需人工确认")

    def fetch_round_trip(self, from_city, to_city, date, return_date):
        from .ctrip_roundtrip import fetch_round_trip
        return fetch_round_trip(self, from_city, to_city, date, return_date)

    # 携程 H5 机票单程列表（taro 版，用户提供的真实入口）
    URL_TPL = ("https://m.ctrip.com/html5/flight/taro/first?from=inner"
               "&tripType=ONE_WAY"
               "&dcity={from_city}&dcityName={from_name}"
               "&acity={to_city}&acityName={to_name}"
               "&ddate={date}")

    # 仅认航班列表接口；LowestPriceSearch 是跨日期低价日历，会混入其他日期价格
    XHR_KEYS = [
        "flightListSearchForH5",
    ]

    CITY_NAME = {
        "SZX": "深圳", "KMG": "昆明", "PEK": "北京", "BJS": "北京",
        "SHA": "上海", "PVG": "上海", "CAN": "广州", "HGH": "杭州",
        "CTU": "成都", "SIA": "西安", "CKG": "重庆", "NKG": "南京",
        "WUH": "武汉", "CSX": "长沙", "XMN": "厦门", "SYX": "三亚",
        "HAK": "海口", "LJG": "丽江", "DLU": "大理", "JHG": "西双版纳",
        "DIG": "香格里拉", "TCZ": "腾冲",
    }

    def login_url(self) -> str:
        return "https://m.ctrip.com/webapp/passenger/login"

    def interactive_login_pc(self):
        """Manual PC login, using the same persistent profile as round-trip fetch."""
        mobile = self.use_mobile
        self.use_mobile = False
        try:
            with self.browser(headless=False) as ctx:
                page = self.new_page(ctx)
                # Leave the new page blank: the user reported that address-bar
                # navigation works in this browser while automatic goto is blocked.
                print("请在弹出的空白页地址栏中手动粘贴并打开：https://flights.ctrip.com/")
                print("随后点击登录，自行完成扫码或验证码。")
                print("确认页面显示已登录后，回到此终端按 Enter 关闭窗口并保留会话。")
                print(f"会话目录：{os.path.abspath(self.user_data_dir)}")
                try:
                    input(">>> 完成登录后按 Enter：")
                except EOFError as e:
                    raise RuntimeError("PC 登录需要交互终端，请在 PowerShell 中运行该命令") from e
                from core.collection import FetchFailure
                # input() blocks the synchronous Playwright event dispatcher.
                # Re-enter the API before inspecting cached page URLs/new tabs.
                try:
                    page.wait_for_timeout(100)
                except PlaywrightError:
                    # Closing the original tab may be intentional; inspect remaining tabs.
                    pass
                open_pages = [p for p in ctx.pages if not p.is_closed()]
                hosts = [(urllib.parse.urlparse(str(p.url)).hostname or "").lower().rstrip(".")
                         for p in open_pages]
                pages = [p for p, host in zip(open_pages, hosts)
                         if host == "ctrip.com" or host.endswith(".ctrip.com")]
                if not pages:
                    domains = ", ".join(sorted(set(hosts))) or "空白页或窗口已关闭"
                    raise FetchFailure("login_incomplete", f"页面检查未通过（当前域名：{domains}）；"
                                       "已有会话目录已保留，不能据此判断登录失败。请重开同一浏览器会话确认")
                self._check_pc_block(pages[-1])
            self.logger.info("携程 PC 浏览器已关闭，会话已保留；登录是否可用于报价仍需实机验收")
        finally:
            self.use_mobile = mobile

    def fetch(self, from_city: str, to_city: str, dates: List[str]) -> List[FlightPrice]:
        results: List[FlightPrice] = []
        with self.browser() as ctx:
            page = self.new_page(ctx)
            for date in dates:
                url = self.URL_TPL.format(
                    from_city=from_city.upper(),
                    to_city=to_city.upper(),
                    from_name=urllib.parse.quote(self.CITY_NAME.get(from_city.upper(), from_city)),
                    to_name=urllib.parse.quote(self.CITY_NAME.get(to_city.upper(), to_city)),
                    date=date,
                )
                self.logger.info("[ctrip] GET %s", url)
                captured = self.attach_xhr_collector(page, self.XHR_KEYS)
                try:
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(8000)
                    for _ in range(4):
                        try:
                            page.mouse.wheel(0, 2000)
                        except Exception:
                            pass
                        page.wait_for_timeout(1200)

                    price = self._pick_lowest(captured)
                    self._dump_xhr(captured, f"ctrip_xhr_{date}")
                    if price is not None:
                        results.append(FlightPrice(
                            platform=self.name,
                            from_city=from_city, to_city=to_city,
                            depart_date=date, price=price,
                        ))
                        self.logger.info("[ctrip] %s 最低价 ¥%.0f", date, price)
                    else:
                        self.logger.warning("[ctrip] %s 未解析到价格", date)
                        self._debug_snapshot(page, f"nopx_{date}")
                except Exception as e:
                    self.logger.exception("[ctrip] %s 抓取异常: %s", date, e)
                self._sleep()
        return results

    def _pick_lowest(self, captured: list) -> float | None:
        prices: list = []
        for item in captured:
            prices.extend(self._extract_ctrip_prices(item.get("text", "")))
        prices = [p for p in prices if 100 <= p <= 50000]
        return float(min(prices)) if prices else None

    @staticmethod
    def _extract_ctrip_prices(text: str) -> list:
        """解析携程 flightListSearchForH5 JSON，返回所有"有票"政策的含税价。

        逐航班递归遍历 policyinfo，配对 (tprice, quantity)，
        quantity 为 null/0 的政策剔除（无票诱饵价）。
        """
        if not text:
            return []
        try:
            obj = json.loads(text)
        except Exception:
            # 解析失败则退回正则（但仍要求 tprice 与 quantity 在同一对象，尽量配对）
            return CtripCrawler._regex_fallback(text)

        flts = obj.get("fltitem")
        if not isinstance(flts, list):
            return CtripCrawler._regex_fallback(text)

        prices: list = []

        def scan(node):
            """递归找 tprice，并取同级 quantity 判断是否有票"""
            if isinstance(node, dict):
                if "tprice" in node:
                    tp = node.get("tprice")
                    qty = node.get("quantity", None)
                    try:
                        tpv = int(float(tp))
                    except Exception:
                        tpv = None
                    has_ticket = qty is not None
                    if has_ticket:
                        try:
                            has_ticket = int(qty) > 0
                        except Exception:
                            has_ticket = True  # 非数字但非 null，视为有票
                    if tpv is not None and has_ticket:
                        prices.append(tpv)
                for v in node.values():
                    scan(v)
            elif isinstance(node, list):
                for v in node:
                    scan(v)

        for f in flts:
            scan(f)
        return prices

    @staticmethod
    def _regex_fallback(text: str) -> list:
        """JSON 解析失败时的兜底：仅取 tprice 紧跟 quantity 非 null 的。"""
        prices: list = []
        for m in re.finditer(
            r'"tprice"\s*:\s*(\d+(?:\.\d+)?)\s*,\s*"quantity"\s*:\s*(null|"?\d+"?)',
            text,
        ):
            qty = m.group(2)
            if qty == "null":
                continue
            try:
                if int(qty.strip('"')) <= 0:
                    continue
            except Exception:
                pass
            prices.append(int(float(m.group(1))))
        return prices

    def _dump_xhr(self, captured: list, tag: str):
        if not self.debug or not captured:
            return
        os.makedirs(self.debug_dir, exist_ok=True)
        path = os.path.join(self.debug_dir, tag + ".txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                for i, item in enumerate(captured):
                    f.write(f"\n----- [{i}] {item['url']} -----\n")
                    f.write((item.get("text") or "")[:200000])
                    f.write("\n")
            self.logger.info("[ctrip] 已保存 XHR: %s", path)
        except Exception:
            pass
