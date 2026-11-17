"""Query outcomes: an unexplained empty legacy result is never evidence of no seats."""
from dataclasses import dataclass, field
import logging
import time


@dataclass
class FetchResult:
    status: str
    prices: list = field(default_factory=list)
    evidence: str = ""


class FetchFailure(Exception):
    def __init__(self, status, evidence):
        super().__init__(evidence)
        self.status = status


class _Signals(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.statuses = []

    def emit(self, record):
        text = record.getMessage().lower()
        if any(x in text for x in ("timeout", "timed out", "超时")):
            self.statuses.append("timeout")
        elif any(x in text for x in ("验证码", "风控", "1999", "179991", "captcha")):
            self.statuses.append("blocked")
        elif record.exc_info or record.levelno >= logging.ERROR:
            self.statuses.append("error")


def collect(crawler, fc, tc, date, return_date=""):
    start = time.monotonic()
    signals = _Signals()
    crawler.logger.addHandler(signals)
    try:
        if return_date:
            method = getattr(crawler, "fetch_round_trip", None)
            if method is None:
                return FetchResult("unsupported", evidence="round_trip_not_implemented")
            prices = method(fc, tc, date, return_date)
        else:
            prices = crawler.fetch(fc, tc, [date])
        if prices:
            return FetchResult("ok", prices)
        status = next((s for s in ("timeout", "blocked", "error")
                       if s in signals.statuses), "unknown_empty")
        return FetchResult(status, evidence="legacy_log_signal" if signals.statuses
                           else "no_explicit_empty_response")
    except FetchFailure as e:
        return FetchResult(e.status, evidence=str(e))
    except Exception as e:
        status = "timeout" if "timeout" in type(e).__name__.lower() else "error"
        return FetchResult(status, evidence=type(e).__name__)
    finally:
        crawler.logger.removeHandler(signals)
        crawler.last_elapsed_ms = int((time.monotonic() - start) * 1000)
