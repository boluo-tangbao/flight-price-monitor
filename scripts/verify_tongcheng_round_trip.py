"""Run an isolated local Tongcheng pilot or prepare its separate desktop login."""
import argparse
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.collection import collect
from core.baggage import baggage_total
from crawlers.tongcheng import TongchengCrawler
from crawlers.tongcheng_roundtrip import desktop
from main import build_routes, load_config


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--details", action="store_true", help="Inspect one selected fare and both baggage directions without booking")
    ap.add_argument("--outbound-flight", help="Keep the detail check on this outbound flight")
    ap.add_argument("--return-flight", help="Keep the detail check on this return flight")
    ap.add_argument("--browser-channel", choices=("msedge", "chrome"))
    ap.add_argument("--user-data-dir", default="user_data")
    args = ap.parse_args()
    if bool(args.outbound_flight) != bool(args.return_flight):
        ap.error('Specify both --outbound-flight and --return-flight')
    logging.basicConfig(level=logging.INFO)
    crawler = TongchengCrawler({"headless": False, "timeout_seconds": 35,
                              "user_data_dir": args.user_data_dir,
                              "tongcheng_pc_browser_channel": args.browser_channel,
                              "tongcheng_round_trip_fare_details": args.details,
                              "tongcheng_detail_pair": [args.outbound_flight, args.return_flight]
                                  if args.outbound_flight else None},
                              logging.getLogger("tongcheng_acceptance"))
    if args.login:
        with desktop(crawler, headed=True, login=True) as ctx:
            crawler.tongcheng_pc_session.new_page(ctx)
            print("请在同一窗口手动打开 https://www.ly.com/ 并登录同程。完成后回这里按回车。", flush=True)
            input()
        return 0
    route = build_routes(load_config("config.monitor.yaml"))[0]
    result = collect(crawler, route.from_code, route.to_code, route.dates[0], route.return_dates[0])
    print(json.dumps({"platform": "tongcheng", "status": result.status, "evidence": result.evidence,
                      "queries": getattr(crawler, "round_trip_searches", 0), "notifications_sent": False,
                      "diagnostics": getattr(crawler, "round_trip_diagnostics", {}),
                      "quotes": [dict(quote.to_dict(), checked_baggage_total=baggage_total(quote))
                                 for quote in result.prices]}, ensure_ascii=False))
    return 0 if result.status in ("ok", "empty") else 2


if __name__ == "__main__":
    raise SystemExit(main())
