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
from core.collection import FetchResult, collect
from core.baggage import baggage_total, lowest_round_trip_options


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--authenticated", action="store_true")
    args = parser.parse_args()
    from core.cloud_session import SECRET_NAME
    has_state = bool(os.environ.get(SECRET_NAME))
    cfg = load_config("config.monitor.yaml")
    route = build_routes(cfg)[0]
    logger = logging.getLogger("cloud_acceptance")
    logging.basicConfig(level=logging.INFO)
    crawler = None
    if args.authenticated != has_state:
        result = FetchResult("login_required", evidence="login_state_mode_mismatch_no_request")
    else:
        crawler = CtripCrawler(cfg["crawler"], logger)
        result = collect(crawler, route.from_code, route.to_code,
                         route.dates[0], route.return_dates[0])
    rows = [{"ticket_from_price": q.price, "baggage_inclusive_total": total,
             "depart_date": q.depart_date, "return_date": q.return_date,
             "flight_no": q.flight_no, "return_flight_no": q.return_flight_no,
             "price_basis": q.price_basis, "baggage_offer": q.baggage_offer,
             "source_url": q.source_url} for q, total in lowest_round_trip_options(result.prices)]
    ready = any(r["baggage_inclusive_total"] is not None for r in rows)
    # Check every quote, including any omitted from the three-row report.
    totals = [(q.price, baggage_total(q)) for q in result.prices]
    within_budget = any(total is not None and total <= route.alert_threshold
                        for _, total in totals)
    over_budget = bool(totals) and all(
        (total if total is not None else price) > route.alert_threshold
        for price, total in totals)
    budget_result = ("within_budget" if within_budget else
                     "over_budget" if over_budget else "unverified" if rows else "no_quotes")
    navigation = getattr(crawler, "round_trip_diagnostics", {})
    report = {"status": result.status, "evidence": result.evidence, "attempts": 1,
              "queries": navigation.get("search_visits", 0),
              "navigation": navigation,
              "browser": getattr(crawler, "cloud_browser_diagnostics", {}),
              "account_used": has_state, "notifications_sent": False,
              "baggage_verified": ready,
              "budget_cny": route.alert_threshold,
              "within_budget": within_budget,
              "budget_result": budget_result,
              "inspected_quote_count": len(totals),
              "quotes": rows}
    out = Path("data/cloud-acceptance.json")
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if result.status == "ok" and over_budget:
        logger.info("本轮已查方案均超过预算，已输出最多三项报价。")
    elif result.status == "ok" and budget_result == "unverified":
        logger.warning("已有往返报价，但仍有预算内票价的托运费用待查，无法确定是否达标。")
    print(json.dumps(report, ensure_ascii=False))
    # This workflow verifies a functional query and report. Budget and baggage
    # have independent fields, including when the amount is still unknown.
    return 0 if (result.status == "ok" and rows) or result.status == "empty" else 2


if __name__ == "__main__":
    raise SystemExit(main())
