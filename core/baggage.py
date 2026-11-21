"""Only verified two-leg baggage offers can satisfy a baggage-inclusive budget."""
import math


def baggage_total(quote):
    offer = quote.baggage_offer
    if (not offer or offer.get("itinerary_confirmed") is not True
            or offer.get("tax_inclusive_ticket_confirmed") is not True):
        return None
    total = float(quote.price)
    if not math.isfinite(total) or total <= 0:
        return None
    for leg in ("outbound", "inbound"):
        item = offer.get(leg) or {}
        if item.get("confirmed") is not True:
            return None
        try:
            kg, pieces = float(item.get("weight_kg", 0)), float(item.get("pieces", 0))
            fee = float(item["additional_fee_cny"])
        except (KeyError, ValueError, TypeError):
            return None
        if not all(math.isfinite(x) for x in (kg, pieces, fee)) or max(kg, pieces) <= 0 or fee < 0:
            return None
        total += fee
    return total


def lowest_round_trip_options(quotes, limit=3):
    """Compare verified totals. Preserve unknown fares as labelled lower bounds."""
    priced = [(q, baggage_total(q)) for q in quotes]
    ranked = sorted(priced, key=lambda row: (row[1] is None,
                                            row[1] if row[1] is not None else row[0].price))
    seen = set()
    options = []
    for quote, total in ranked:
        key = (quote.flight_no, quote.return_flight_no, quote.price, total, quote.price_basis)
        if key in seen:
            continue
        seen.add(key)
        options.append((quote, total))
        if len(options) == limit:
            break
    return options
