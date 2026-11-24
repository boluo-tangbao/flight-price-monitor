"""Query selected providers once and print a joint round-trip comparison as JSON."""
import argparse
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.collection import collect, FetchFailure, FetchResult
from core.round_trip_comparison import compare_round_trips, comparison_settings, validate_comparison_route
from crawlers import REGISTRY
from main import build_routes, load_config


def run_comparison(route, crawler_config, providers, logger):
    depart, back = route.dates[0], route.return_dates[0]
    results = {}
    diagnostics = {}
    for provider in providers:
        crawler_class = REGISTRY[provider]
        if not callable(getattr(crawler_class, "fetch_round_trip", None)):
            results[provider] = FetchResult("unsupported", evidence="round_trip_not_implemented")
            continue
        crawler = crawler_class(dict(crawler_config), logger)
        results[provider] = collect(crawler, route.from_code, route.to_code, depart, back)
        details = getattr(crawler, "round_trip_diagnostics", None)
        if isinstance(details, dict):
            diagnostics[provider] = details
    report = compare_round_trips(route, depart, back, results)
    for provider, details in diagnostics.items():
        report["platforms"][provider]["diagnostics"] = details
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default=str(ROOT / "config.monitor.yaml"))
    parser.add_argument("--platforms", nargs="+", choices=sorted(REGISTRY),
                        help="Override comparison.platforms for just this run")
    parser.add_argument("--ctrip-channel", choices=["chromium", "chrome", "msedge"])
    parser.add_argument("--tongcheng-channel", choices=["chrome", "msedge"])
    parser.add_argument("--fliggy-channel", choices=["chromium", "chrome", "msedge"])
    fliggy_mode = parser.add_mutually_exclusive_group()
    fliggy_mode.add_argument("--fliggy-calendar-only", action="store_true",
                             help="Query Fliggy's date-level calendar for diagnosis")
    fliggy_mode.add_argument("--fliggy-login", action="store_true",
                             help="Open the Fliggy desktop profile for manual sign-in")
    parser.add_argument("--diagnostics", action="store_true",
                        help="Show pending-baggage subsets and detailed fare collection records")
    args = parser.parse_args()
    cfg = load_config(args.config)
    routes = build_routes(cfg)
    try:
        if not args.fliggy_login:
            validate_comparison_route(routes)
        providers, config = comparison_settings(cfg, REGISTRY, ["fliggy"] if args.fliggy_login else args.platforms)
    except ValueError as error:
        parser.error(str(error))
    if args.tongcheng_channel:
        config["tongcheng_pc_browser_channel"] = args.tongcheng_channel
    if args.fliggy_channel:
        config["fliggy_pc_browser_channel"] = args.fliggy_channel
    if args.fliggy_calendar_only:
        config["fliggy_calendar_only"] = True
    profile_root = Path(config.get("user_data_dir", "user_data"))
    if not profile_root.is_absolute():
        config["user_data_dir"] = str(ROOT / profile_root)
    if args.ctrip_channel:
        config["pc_browser_channel"] = args.ctrip_channel
    logging.basicConfig(level=logging.CRITICAL)
    if args.fliggy_login:
        from crawlers.fliggy_roundtrip import interactive_login

        try:
            interactive_login(REGISTRY["fliggy"](config, logging.getLogger("fliggy_login")))
        except FetchFailure as failure:
            print(f"[{failure.status}] {failure}", file=sys.stderr)
            return 2
        return 0
    report = run_comparison(routes[0], config, providers,
                            logging.getLogger("round_trip_comparison"))
    if not args.diagnostics:
        report.pop("pending_baggage", None)
        report.pop("pending_at_or_below_best_total", None)
        for status in report.get("platforms", {}).values():
            status.pop("diagnostics", None)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["best_verified"] or report.get("ranked_ticket") else 2


if __name__ == "__main__":
    raise SystemExit(main())
