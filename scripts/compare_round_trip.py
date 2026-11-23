"""Query selected providers once and print a joint round-trip comparison as JSON."""
import argparse
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.collection import collect, FetchResult
from core.round_trip_comparison import compare_round_trips, comparison_settings, validate_comparison_route
from crawlers import REGISTRY
from main import build_routes, load_config


def run_comparison(route, crawler_config, providers, logger):
    depart, back = route.dates[0], route.return_dates[0]
    results = {}
    for provider in providers:
        crawler_class = REGISTRY[provider]
        if not callable(getattr(crawler_class, "fetch_round_trip", None)):
            results[provider] = FetchResult("unsupported", evidence="round_trip_not_implemented")
            continue
        crawler = crawler_class(dict(crawler_config), logger)
        results[provider] = collect(crawler, route.from_code, route.to_code, depart, back)
    return compare_round_trips(route, depart, back, results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default=str(ROOT / "config.monitor.yaml"))
    parser.add_argument("--platforms", nargs="+", choices=sorted(REGISTRY),
                        help="Override comparison.platforms for just this run")
    parser.add_argument("--ctrip-channel", choices=["chromium", "chrome", "msedge"])
    parser.add_argument("--tongcheng-channel", choices=["chrome", "msedge"])
    args = parser.parse_args()
    cfg = load_config(args.config)
    routes = build_routes(cfg)
    try:
        validate_comparison_route(routes)
        providers, config = comparison_settings(cfg, REGISTRY, args.platforms)
    except ValueError as error:
        parser.error(str(error))
    if args.tongcheng_channel:
        config["tongcheng_pc_browser_channel"] = args.tongcheng_channel
    profile_root = Path(config.get("user_data_dir", "user_data"))
    if not profile_root.is_absolute():
        config["user_data_dir"] = str(ROOT / profile_root)
    if args.ctrip_channel:
        config["pc_browser_channel"] = args.ctrip_channel
    logging.basicConfig(level=logging.CRITICAL)
    report = run_comparison(routes[0], config, providers,
                            logging.getLogger("round_trip_comparison"))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["best_verified"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
