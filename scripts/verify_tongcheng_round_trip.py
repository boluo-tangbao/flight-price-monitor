"""Run an isolated local Tongcheng pilot or prepare its separate desktop login."""
import argparse
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.collection import collect
from core.airlines import round_trip_airlines
from core.baggage import baggage_total, checked_baggage_labels
from crawlers.tongcheng import TongchengCrawler
from crawlers.tongcheng_roundtrip import desktop
from main import build_routes, load_config


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--details", action="store_true",
                    help="Read fare details and baggage for up to eight round-trip pairs (default)")
    ap.add_argument("--list-only", action="store_true", help="Read only listed round-trip prices")
    ap.add_argument("--diagnose", action="store_true",
                    help="Read fare details with stage counters and stop at any verification prompt")
    ap.add_argument("--outbound-flight", help="Keep the detail check on this outbound flight")
    ap.add_argument("--return-flight", help="Keep the detail check on this return flight")
    ap.add_argument("--browser-channel", choices=("msedge", "chrome"))
    ap.add_argument("--user-data-dir", default="user_data")
    args = ap.parse_args()
    if bool(args.outbound_flight) != bool(args.return_flight):
        ap.error('Specify both --outbound-flight and --return-flight')
    if args.login and args.diagnose:
        ap.error('Run --login and --diagnose separately')
    if args.list_only and (args.details or args.diagnose or args.outbound_flight):
        ap.error('--list-only cannot be combined with fare-detail options')
    read_details = not (args.login or args.list_only)
    logging.basicConfig(level=logging.INFO)
    settings = {
        "headless": False, "timeout_seconds": 35, "user_data_dir": args.user_data_dir,
        "tongcheng_pc_browser_channel": args.browser_channel,
        "tongcheng_round_trip_fare_details": read_details,
        "tongcheng_keep_list_quotes": read_details and not args.outbound_flight,
        "slider": {"auto": False},
        "tongcheng_detail_pair": [args.outbound_flight, args.return_flight] if args.outbound_flight else None,
    }
    crawler = TongchengCrawler(settings, logging.getLogger("tongcheng_acceptance"))
    if args.login:
        with desktop(crawler, headed=True, login=True) as ctx:
            crawler.tongcheng_pc_session.new_page(ctx)
            print("请在同一窗口手动打开 https://www.ly.com/ 并登录同程。完成后回这里按回车。", flush=True)
            input()
        return 0
    route = build_routes(load_config("config.monitor.yaml"))[0]
    result = collect(crawler, route.from_code, route.to_code, route.dates[0], route.return_dates[0])
    print(json.dumps({"platform": "tongcheng", "status": result.status, "evidence": result.evidence,
                      "query_mode": "fare_details" if read_details else "list_only",
                      "queries": getattr(crawler, "round_trip_searches", 0), "notifications_sent": False,
                      "diagnostics": getattr(crawler, "round_trip_diagnostics", {}),
                      "quotes": [dict(quote.to_dict(), checked_baggage_total=baggage_total(quote),
                                      checked_baggage=checked_baggage_labels(quote),
                                      **round_trip_airlines(quote))
                                 for quote in result.prices]}, ensure_ascii=False))
    return 0 if result.status in ("ok", "empty") else 2


if __name__ == "__main__":
    raise SystemExit(main())
