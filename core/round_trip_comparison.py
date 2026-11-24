"""Compare offers from one collection round for one round-trip itinerary."""
import math

from .airlines import round_trip_airlines
from .baggage import baggage_total, checked_baggage_labels, requires_baggage_purchase
from .models import RoundTripQuote


def comparison_settings(config, registry, override_platforms=None):
    settings = config.get("comparison") or {}
    selected = (override_platforms if override_platforms is not None
                else settings.get("platforms", config.get("platforms")))
    if not isinstance(selected, (list, tuple)) or not selected:
        raise ValueError("comparison.platforms must be a non-empty list")
    for name in selected:
        if not isinstance(name, str) or name not in registry:
            raise ValueError(f"Unknown platform in the comparison list: {name!r}")
    crawler = dict(config.get("crawler") or {})
    crawler.update(settings.get("crawler") or {})
    crawler.update(round_trip_fare_details=True, ctrip_keep_list_quotes=True,
                   tongcheng_round_trip_fare_details=True,
                   tongcheng_keep_list_quotes=True)
    return list(dict.fromkeys(selected)), crawler


def validate_comparison_route(routes):
    if len(routes) != 1 or len(routes[0].dates) != 1 or len(routes[0].return_dates) != 1:
        raise ValueError("Specify one round-trip itinerary with one departure date and one return date")


def compare_round_trips(route, depart, back, results):
    """Accept the same FetchResult contract from any round-trip provider.

    Each provider is queried for one adult in economy. An adapter must only
    mark a fare verified after confirming that scope and both baggage legs.
    The ticket-price ranking retains all valid rows. The strict ranking only
    includes fares with checked baggage already included on both legs.
    """
    ticket_prices, ranked, pending, platforms = [], [], [], {}
    for platform, result in results.items():
        ticket_count = 0
        verified_count = pending_count = rejected_count = purchase_required_count = 0
        for quote in result.prices:
            if (not isinstance(quote, RoundTripQuote) or quote.platform != platform
                    or (quote.from_city, quote.to_city, quote.depart_date, quote.return_date)
                    != (route.from_code, route.to_code, depart, back)
                    or quote.currency != "CNY" or not math.isfinite(quote.price) or quote.price <= 0):
                rejected_count += 1
                continue
            offer = quote.baggage_offer or {}
            cabin = offer.get("cabin")
            if cabin and cabin != "economy":
                rejected_count += 1
                continue
            purchase_required = requires_baggage_purchase(quote)
            total = (baggage_total(quote)
                     if not purchase_required and cabin == "economy"
                     and quote.flight_no and quote.return_flight_no else None)
            row = {
                "platform": platform, "flight_no": quote.flight_no,
                "return_flight_no": quote.return_flight_no, **round_trip_airlines(quote),
                "ticket_price_cny": quote.price, "price_basis": quote.price_basis,
                "checked_baggage": checked_baggage_labels(quote),
                "checked_baggage_status": ("purchase_required" if purchase_required else
                                            "included" if total is not None else "unverified"),
                "fare_baggage_rule": offer.get("fare_rule"),
                "cabin_verified": cabin == "economy",
                "total_with_baggage_cny": total,
                "within_budget": total <= route.alert_threshold if total is not None and route.alert_threshold else None,
                "fetched_at": quote.fetched_at, "source_url": quote.source_url,
            }
            ticket_prices.append(row)
            ticket_count += 1
            if purchase_required:
                purchase_required_count += 1
                continue
            if total is None:
                pending.append(row)
                pending_count += 1
            else:
                ranked.append(row)
                verified_count += 1
        platforms[platform] = {"status": result.status, "evidence": result.evidence,
                               "ticket_status": "available" if ticket_count else "unavailable",
                               "ticket_quotes": ticket_count,
                               "verified_quotes": verified_count, "pending_quotes": pending_count,
                               "rejected_quotes": rejected_count,
                               "excluded_baggage_purchase_required": purchase_required_count}

    ranked.sort(key=lambda row: (row["total_with_baggage_cny"], row["platform"],
                                 row["flight_no"], row["return_flight_no"]))
    ticket_prices.sort(key=lambda row: (row["ticket_price_cny"], row["platform"],
                                        row["flight_no"], row["return_flight_no"]))
    pending.sort(key=lambda row: (row["ticket_price_cny"], row["platform"]))
    best_total = ranked[0]["total_with_baggage_cny"] if ranked else None
    best = [row for row in ranked if row["total_with_baggage_cny"] == best_total]
    unresolved = [row for row in pending
                  if best_total is None or row["ticket_price_cny"] <= best_total]
    incomplete = [name for name, details in platforms.items()
                  if details["status"] not in ("ok", "empty") or not details["verified_quotes"]]
    return {
        "itinerary": {"origin": route.from_code, "destination": route.to_code,
                      "depart": depart, "return": back, "adults": 1, "cabin": "economy",
                      "budget_cny": route.alert_threshold, "checked_baggage": "both_directions",
                      "checked_baggage_included_in_fare": True},
        "platforms": platforms,
        "best_verified": best,
        "ranked_verified": ranked,
        "ranked_ticket": ticket_prices,
        "pending_baggage": pending,
        "platforms_without_complete_comparison": incomplete,
        "pending_at_or_below_best_total": unresolved,
        "comparison_status": ("no_verified_quote" if not best else
                              "verified_leader_with_gaps" if incomplete or unresolved else
                              "best_of_checked_quotes"),
    }
