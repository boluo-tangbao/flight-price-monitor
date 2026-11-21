"""Run an isolated local Tongcheng pilot or prepare its separate desktop login."""
import argparse
import json
import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.collection import collect
from crawlers.tongcheng import TongchengCrawler
from crawlers.tongcheng_roundtrip import desktop
from main import build_routes, load_config


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--login", action="store_true")
    ap.add_argument("--browser-channel", choices=("msedge", "chrome"))
    ap.add_argument("--user-data-dir", default="user_data")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    crawler = TongchengCrawler({"headless": False, "timeout_seconds": 35,
                              "user_data_dir": args.user_data_dir,
                              "tongcheng_pc_browser_channel": args.browser_channel},
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
                      "quotes": [quote.to_dict() for quote in result.prices]}, ensure_ascii=False))
    return 0 if result.status in ("ok", "empty") else 2


if __name__ == "__main__":
    raise SystemExit(main())
