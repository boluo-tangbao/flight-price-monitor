"""One cloud acceptance attempt; optional login state, no retries or notifications."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import logging
import argparse
import os
from main import load_config, build_routes
from crawlers.ctrip import CtripCrawler
from core.collection import collect
from core.baggage import baggage_total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--authenticated", action="store_true")
    args = parser.parse_args()
    from core.cloud_session import SECRET_NAME
    has_state = bool(os.environ.get(SECRET_NAME))
    if args.authenticated != has_state:
        print("Login-state mode mismatch; no airline request sent.")
        return 2
    cfg = load_config("config.monitor.yaml")
    route = build_routes(cfg)[0]
    logger = logging.getLogger("cloud_acceptance")
    logging.basicConfig(level=logging.INFO)
    crawler = CtripCrawler(cfg["crawler"], logger)
    result = collect(crawler, route.from_code, route.to_code,
                     route.dates[0], route.return_dates[0])
    rows = [{"ticket_from_price": q.price, "baggage_inclusive_total": baggage_total(q),
             "depart_date": q.depart_date, "return_date": q.return_date,
             "flight_no": q.flight_no, "source_url": q.source_url} for q in result.prices]
    ready = any(r["baggage_inclusive_total"] is not None for r in rows)
    navigation = getattr(crawler, "round_trip_diagnostics", {})
    report = {"status": result.status, "evidence": result.evidence, "attempts": 1,
              "queries": navigation.get("search_visits", 0),
              "navigation": navigation,
              "browser": getattr(crawler, "cloud_browser_diagnostics", {}),
              "account_used": has_state, "notifications_sent": False,
              "baggage_verified": ready, "quotes": rows}
    out = Path("data/cloud-acceptance.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0 if result.status == "ok" and ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
