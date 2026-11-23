"""低价提醒 + 微信推送（带去抖）"""
import logging
from typing import List, Optional

from .airlines import round_trip_flights_label
from .baggage import baggage_total, checked_baggage_labels, lowest_round_trip_options
from .models import FlightPrice, Route


PLATFORM_NAMES = {"ctrip": "携程", "tongcheng": "同程", "fliggy": "飞猪",
                  "qunar": "去哪儿", "tuniu": "途牛"}
COLLECTION_STATUS = {"ok": "已返回报价", "empty": "明确无票", "timeout": "采集超时",
                     "login_required": "需登录", "blocked": "查询受阻",
                     "parse_error": "详情未核实", "unsupported": "暂不支持"}


def _checked_baggage_line(quote):
    labels = checked_baggage_labels(quote)
    line = f"去程托运 {labels['outbound']}；回程托运 {labels['inbound']}"
    fare_rule = (quote.baggage_offer or {}).get("fare_rule")
    if fare_rule and "待确认" in labels.values():
        line = f"票面标注 {fare_rule} · {line}"
    return line


class Alerter:
    def report_joint_round_trip(self, route, report):
        itinerary = report["itinerary"]
        ranked = report["ranked_verified"]
        best = ranked[0] if ranked else None
        title = f"{route.from_name}↔{route.to_name} 联合比价"
        if best and best["within_budget"]:
            title += f" · 预算内 ¥{best['total_with_baggage_cny']:,.0f}"
        else:
            title += " · 本轮未确认预算内方案"

        lines = [
            f"去程 {itinerary['depart']} / 回程 {itinerary['return']}，预算 ¥{itinerary['budget_cny']:,.0f}。",
            "", "**当前已核实方案 · 按含两程托运总价排序**",
        ]
        if not ranked:
            lines.append("本轮暂无两程托运均已核实的报价。")
        cutoff = ranked[min(len(ranked), 5) - 1]["total_with_baggage_cny"] if ranked else None
        previous_total = None
        for index, row in enumerate(ranked, 1):
            total = row["total_with_baggage_cny"]
            if total > cutoff:
                break
            if total != previous_total:
                rank = index
            previous_total = total
            platform = PLATFORM_NAMES.get(row["platform"], row["platform"])
            outbound = f"{row['outbound_airline'] or '航司待确认'} {row['flight_no']}"
            inbound = f"{row['return_airline'] or '航司待确认'} {row['return_flight_no']}"
            legs = row["checked_baggage"]
            view = f" · [查看报价]({row['source_url']})" if row["source_url"] else ""
            lines.append(f"- 第 {rank} 名 · {platform} · {outbound} / {inbound} · ¥{total:,.2f}"
                         f" · 去程托运 {legs['outbound']} / 回程托运 {legs['inbound']}{view}")

        pending = [row for row in report["pending_baggage"]
                   if len(ranked) < 5 or row["ticket_price_cny"] <= cutoff]
        if pending:
            lowest = pending[0]
            platform = PLATFORM_NAMES.get(lowest["platform"], lowest["platform"])
            lines.extend(["", f"另有 {len(pending)} 条票价可能影响榜单，尚待核实。"
                           f"最低已读票价为 {platform} ¥{lowest['ticket_price_cny']:,.2f}。"])
            for row in [item for item in pending if item["fare_baggage_rule"]][:3]:
                platform = PLATFORM_NAMES.get(row["platform"], row["platform"])
                legs = row["checked_baggage"]
                lines.append(f"- {platform} · {row['flight_no']} / {row['return_flight_no']}"
                             f" · ¥{row['ticket_price_cny']:,.2f} · 票面标注 {row['fare_baggage_rule']}"
                             f" · 去程托运 {legs['outbound']} / 回程托运 {legs['inbound']}")
        lines.extend(["", "**各平台采集情况**"])
        for name, status in report["platforms"].items():
            label = PLATFORM_NAMES.get(name, name)
            outcome = COLLECTION_STATUS.get(status["status"], status["status"])
            lines.append(f"- {label} · {outcome}，已核实 {status['verified_quotes']} 条，"
                         f"待核实 {status['pending_quotes']} 条。")
        description = "\n".join(lines)
        self.logger.info("[联合比价] %s\n%s", title, description)
        if self.notifier:
            self.notifier.send(title, description)

    def report_round_trip_options(self, route, depart, ret, result):
        ranked = lowest_round_trip_options(result.prices)
        if not ranked:
            self.logger.info("[往返报价] %s/%s 本轮无可报告报价（%s）", depart, ret, result.status)
            return
        best, total = ranked[0]
        if total is not None:
            self.check_round_trip(route, best)
            if total <= route.alert_threshold:
                return

        lines = []
        for index, (quote, total) in enumerate(ranked, 1):
            flights = round_trip_flights_label(quote)
            if total is not None:
                label = f"含两程托运 ¥{total:,.2f}"
            else:
                kind = "往返含税票价" if quote.price_basis == "selected_round_trip_tax" else "往返含税起价"
                label = f"{kind} ¥{quote.price:,.2f}，托运费用待确认"
            lines.append(f"{index}. {flights} · {label} · {_checked_baggage_line(quote)}")
        title = f"{route.from_name}↔{route.to_name} 本轮未确认预算内方案"
        description = (f"去程 {depart} / 回程 {ret}，预算 ¥{route.alert_threshold:,.0f}。\n\n"
                       "本轮已查最低方案，已核实的含托运总价优先。\n\n"
                       + "\n".join(lines)
                       + (f"\n\n详情采集状态：{result.status}" if result.status != "ok" else "")
                       + f"\n\n[查看往返]({best.source_url})")
        self.logger.info("[往返报价] %s\n%s", title, description)
        if self.notifier:
            self.notifier.send(title, description)

    def check_round_trip(self, route, quote):
        if not route.alert_threshold or not self.notifier:
            return
        if route.checked_baggage_required:
            from dataclasses import replace
            total = baggage_total(quote)
            if total is None:
                self.logger.info("[托运待确认] %s->%s %s/%s：两程托运与实际组合费用未核实，暂不触发低价提醒",
                                 route.from_code, route.to_code, quote.depart_date, quote.return_date)
                return
            quote = replace(quote, price=total)
        key = (f"RT:{route.from_code}:{route.to_code}:{quote.depart_date}:"
               f"{quote.return_date}:{quote.currency}:{quote.price_basis}")
        if route.checked_baggage_required:
            key += ":checked-baggage-both-legs"
        if quote.price > route.alert_threshold:
            self.storage.clear_alert_state(key)
            return
        last = self.storage.get_alert_state(key)
        should_push, _ = self._should_push(quote.price, last)
        label = "往返含税含两程托运合计" if route.checked_baggage_required else "往返含税起价"
        note = (_checked_baggage_line(quote) if route.checked_baggage_required else
                "返程具体航班尚未选定，支付价格请到平台核实。")
        if should_push and self.notifier.send(
                f"{label} {route.from_name}↔{route.to_name} ¥{quote.price:.0f}",
                f"去程 {quote.depart_date} / 回程 {quote.return_date}\n\n"
                f"{round_trip_flights_label(quote)}\n\n"
                f"{label}：CNY {quote.price:.0f}（1成人经济舱）。\n"
                f"{note}\n\n[查看往返]({quote.source_url})"):
            self.storage.set_alert_state(key, quote.price)

    def __init__(self, logger: logging.Logger, notifier=None, storage=None,
                 push_drop_min: float = 30, push_rise_min: float = 50):
        """
        notifier:        推送器（None 表示不推送）
        storage:         用于读写 alert_state（去抖）
        push_drop_min:   触发"再次推送"的最小降幅(¥)
        push_rise_min:   触发"再次推送"的最小涨幅(¥)
        """
        self.logger = logger
        self.notifier = notifier
        self.storage = storage
        self.push_drop_min = push_drop_min
        self.push_rise_min = push_rise_min

    def check_and_alert(self, route: Route, prices: List[FlightPrice]):
        if not prices:
            return
        # 控制台/日志：三平台对比 + 多日期对比
        self._compare_platforms(route, prices)
        self._compare_dates(route, prices)

        # 低价阈值提醒
        if route.alert_threshold and route.alert_threshold > 0:
            self._handle_threshold(route, prices)

    # ---------- 控制台对比 ----------
    def _compare_platforms(self, route: Route, prices: List[FlightPrice]):
        by_date = {}
        for p in prices:
            by_date.setdefault(p.depart_date, []).append(p)
        for date, lst in sorted(by_date.items()):
            lst = sorted(lst, key=lambda x: x.price)
            best = lst[0]
            line = " | ".join(f"{p.platform}: ¥{p.price:.0f}" for p in lst)
            self.logger.info(
                "[对比] %s->%s %s 最低=%s ¥%.0f  (%s)",
                route.from_name, route.to_name, date,
                best.platform, best.price, line,
            )

    def _compare_dates(self, route: Route, prices: List[FlightPrice]):
        if len(route.dates) <= 1:
            return
        best_by_date = {}
        for p in prices:
            cur = best_by_date.get(p.depart_date)
            if cur is None or p.price < cur.price:
                best_by_date[p.depart_date] = p
        ordered = sorted(best_by_date.items(), key=lambda kv: kv[1].price)
        if not ordered:
            return
        cheapest_date, cheapest_p = ordered[0]
        self.logger.info(
            "[多日期] %s->%s 最便宜日期=%s ¥%.0f (%s)",
            route.from_name, route.to_name, cheapest_date,
            cheapest_p.price, cheapest_p.platform,
        )

    # ---------- 阈值提醒 + 推送 ----------
    def _handle_threshold(self, route: Route, prices: List[FlightPrice]):
        # 按日期取最低
        best_by_date = {}
        for p in prices:
            cur = best_by_date.get(p.depart_date)
            if cur is None or p.price < cur.price:
                best_by_date[p.depart_date] = p

        for date, bp in sorted(best_by_date.items()):
            route_key = f"{route.from_code}-{route.to_code}-{date}"

            if bp.price > route.alert_threshold:
                # 涨出阈值：清除去抖状态，下次跌破按"首次"重新推
                if self.storage:
                    self.storage.clear_alert_state(route_key)
                continue

            last = self.storage.get_alert_state(route_key) if self.storage else None
            should_push, reason = self._should_push(bp.price, last)

            # 控制台始终打印当前命中低价的状态
            self.logger.warning(
                "[低价] %s->%s %s ¥%.0f (阈值¥%.0f, 上次推送¥%s) -> %s",
                route.from_name, route.to_name, date,
                bp.price, route.alert_threshold,
                f"{last:.0f}" if last else "-",
                "推送" if should_push else f"跳过({reason})",
            )

            if should_push and self.notifier:
                ok = self._push(route, date, bp, last)
                if ok and self.storage:
                    self.storage.set_alert_state(route_key, bp.price)

    def _should_push(self, cur: float, last: Optional[float]):
        if last is None:
            return True, "首次"
        diff = cur - last
        if diff <= -self.push_drop_min:
            return True, f"降¥{-diff:.0f}"
        if diff >= self.push_rise_min:
            return True, f"涨¥{diff:.0f}"
        return False, f"波动¥{diff:+.0f}(<阈值)"

    def _push(self, route: Route, date: str, p: FlightPrice,
              last: Optional[float]) -> bool:
        diff_txt = ""
        emoji = "✈️"
        if last is not None:
            diff = p.price - last
            if diff <= 0:
                emoji = "📉"
                diff_txt = f"（较上次降 ¥{-diff:.0f}）"
            else:
                emoji = "📈"
                diff_txt = f"（较上次涨 ¥{diff:.0f}）"

        title = (f"{emoji} {route.from_name}→{route.to_name} {date} "
                 f"¥{p.price:.0f}{diff_txt}")

        # markdown 详细
        view_url = self._build_view_url(route, date, p.platform)
        desp = (
            f"## 机票低价提醒\n\n"
            f"- **航线**：{route.from_name}（{route.from_code}） → "
            f"{route.to_name}（{route.to_code}）\n"
            f"- **日期**：{date}\n"
            f"- **当前最低价**：**¥{p.price:.0f}**\n"
            f"- **设定阈值**：¥{route.alert_threshold:.0f}\n"
            f"- **来源**：{p.platform}\n"
            f"- **抓取时间**：{p.fetched_at}\n"
        )
        if last is not None:
            desp += f"- **上次推送价**：¥{last:.0f}\n"
        desp += f"\n[👉 在 {p.platform} 查看详情]({view_url})\n"
        return self.notifier.send(title, desp)

    def _build_view_url(self, route: Route, date: str, platform: str) -> str:
        if platform == "ctrip":
            return (
                "https://m.ctrip.com/html5/flight/taro/first?from=inner"
                "&tripType=ONE_WAY"
                f"&dcity={route.from_code}&acity={route.to_code}&ddate={date}"
            )
        if platform == "tongcheng":
            return (
                "https://m.ly.com/ft/touch/book1"
                f"?date={date}&an=1&cn=0&baby=0"
                f"&fromcitycode={route.from_code}&fromCode={route.from_code}"
                f"&tocitycode={route.to_code}&toCode={route.to_code}"
                "&cabin=0&platcode=518&frompage=HOME"
            )
        if platform == "qunar":
            return (
                "https://touch.qunar.com/ncs/page/flightlist"
                f"?depCity={route.from_name}&arrCity={route.to_name}"
                f"&goDate={date}&from=touch_index_search"
                "&child=0&baby=0&cabinType=0"
            )
        if platform == "tuniu":
            return (
                "https://m.tuniu.com/flight/domestic/new/"
                f"{route.from_code}_{route.to_code}_OW_1_0_0"
                f"?deptDate={date}&isGo=0"
            )
        # fliggy 默认走飞猪 H5
        return (
            "https://outfliggys.m.taobao.com/app/trip/rx-flight-eco/pages/listing"
            f"?depCityCode={route.from_code}&arrCityCode={route.to_code}"
            f"&leaveDate={date}&adultPassengerNum=1&searchType=1"
        )
