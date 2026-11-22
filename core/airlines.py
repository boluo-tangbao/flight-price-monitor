"""Airline labels for the flight numbers shown on Shanghai–Osaka quotes."""
import re


# IATA flight designators, with MM also confirmed by Peach Aviation.
AIRLINE_NAMES = {
    "9C": "春秋航空",
    "CA": "中国国际航空",
    "CZ": "中国南方航空",
    "FM": "上海航空",
    "HO": "吉祥航空",
    "JL": "日本航空",
    "MM": "乐桃航空",
    "MU": "中国东方航空",
    "NH": "全日空",
}


def airline_name(flight_no, reported_name=""):
    match = re.fullmatch(r"([A-Z0-9]{2})\d{1,4}[A-Z]?", flight_no.strip().upper())
    name = reported_name.strip()
    if name:
        return name
    return AIRLINE_NAMES.get(match[1], "") if match else ""


def round_trip_airlines(quote):
    return {
        "outbound_airline": airline_name(quote.flight_no, quote.airline),
        "return_airline": airline_name(quote.return_flight_no),
    }


def round_trip_flights_label(quote):
    names = round_trip_airlines(quote)
    outbound = " ".join(filter(None, (names["outbound_airline"] or "航司待确认", quote.flight_no)))
    if not quote.return_flight_no:
        return f"去程 {outbound} / 回程航班待确认"
    inbound = f"{names['return_airline'] or '航司待确认'} {quote.return_flight_no}"
    return f"去程 {outbound} / 回程 {inbound}"
