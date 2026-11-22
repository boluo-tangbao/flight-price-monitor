"""Only verified two-leg baggage offers can satisfy a baggage-inclusive budget."""
import math
import re


def _confirmed_allowance(item):
    if item.get("confirmed") is not True:
        return None
    try:
        kg = float(item.get("weight_kg", 0))
        pieces = float(item.get("pieces", 0))
        fee = float(item["additional_fee_cny"])
    except (KeyError, ValueError, TypeError, OverflowError):
        return None
    if (not all(math.isfinite(value) for value in (kg, pieces, fee))
            or min(kg, pieces, fee) < 0 or max(kg, pieces) <= 0):
        return None
    return pieces, kg, fee


def checked_baggage_labels(quote):
    """Explain the checked allowance for each leg of this specific fare."""
    offer = quote.baggage_offer or {}
    labels = {}
    for leg in ("outbound", "inbound"):
        item = offer.get(leg) or {}
        matched = offer.get("itinerary_confirmed") is True
        allowance = _confirmed_allowance(item) if matched else None
        if allowance is None:
            rule = str(item.get("rule") or "").strip()
            no_free = matched and re.fullmatch(
                r"(?:无免费|不含免费)(?:托运(?:行李(?:额)?)?|行李额)[。.]?", rule)
            labels[leg] = "无免费托运" if no_free else "待确认"
            continue
        pieces, kg, fee = allowance
        units = []
        if pieces:
            units.append(f"{pieces:g} 件")
        if kg:
            # The existing weight field can represent total or per-piece weight.
            # Prefer the source rule when both are present to preserve its units.
            units.append(f"{kg:g} kg")
        rule = item.get("rule") or "、".join(units)
        price_label = "票价已含" if fee == 0 else f"加购 ¥{fee:,.2f}"
        labels[leg] = f"{price_label}（{rule}）"
    return labels


def baggage_total(quote):
    offer = quote.baggage_offer
    if (not offer or offer.get("itinerary_confirmed") is not True
            or offer.get("tax_inclusive_ticket_confirmed") is not True):
        return None
    total = float(quote.price)
    if not math.isfinite(total) or total <= 0:
        return None
    for leg in ("outbound", "inbound"):
        allowance = _confirmed_allowance(offer.get(leg) or {})
        if allowance is None:
            return None
        total += allowance[2]
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
