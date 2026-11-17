"""Durable per-query health and batched, quiet-hours-aware ServerChan alerts."""
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_runs (
 id INTEGER PRIMARY KEY, query_key TEXT NOT NULL, status TEXT NOT NULL,
 checked_at REAL NOT NULL, elapsed_ms INTEGER NOT NULL, row_count INTEGER NOT NULL,
 evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS collection_health (
 query_key TEXT PRIMARY KEY, status TEXT NOT NULL, checked_at REAL NOT NULL,
 last_success REAL, consecutive_failures INTEGER NOT NULL DEFAULT 0,
 last_alert REAL, incident_open INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS round_trip_quotes (
 id INTEGER PRIMARY KEY, platform TEXT NOT NULL, from_city TEXT NOT NULL,
 to_city TEXT NOT NULL, depart_date TEXT NOT NULL, return_date TEXT NOT NULL,
 price REAL NOT NULL, currency TEXT NOT NULL, price_basis TEXT NOT NULL,
 source_url TEXT NOT NULL, fetched_at TEXT NOT NULL, extra TEXT NOT NULL);
"""
HEALTHY = {"ok", "empty"}
EXCLUDED = {"unsupported", "skipped"}


class HealthMonitor:
    def __init__(self, storage, notifier=None, config=None):
        self.storage, self.notifier = storage, notifier
        self.config = config or {}
        self.active_queries = set()
        with storage._conn() as c:
            c.executescript(SCHEMA)

    def record(self, platform, route, date, ret, result, elapsed_ms=0, now=None):
        now = time.time() if now is None else now
        key = json.dumps([platform, route.from_code, route.to_code, date, ret],
                         ensure_ascii=False, separators=(",", ":"))
        self.active_queries.add(key)
        with self.storage._conn() as c:
            old = c.execute("SELECT * FROM collection_health WHERE query_key=?", (key,)).fetchone()
            streak = old["consecutive_failures"] if old else 0
            last = old["last_success"] if old else None
            if result.status in HEALTHY:
                streak = 0
                last = now
            elif result.status not in EXCLUDED:
                streak += 1
            c.execute("INSERT INTO collection_runs VALUES(NULL,?,?,?,?,?,?)",
                      (key, result.status, now, elapsed_ms, len(result.prices), result.evidence))
            c.execute("INSERT INTO collection_health(query_key,status,checked_at,last_success,consecutive_failures) "
                      "VALUES(?,?,?,?,?) ON CONFLICT(query_key) DO UPDATE SET status=excluded.status, "
                      "checked_at=excluded.checked_at,last_success=excluded.last_success, "
                      "consecutive_failures=excluded.consecutive_failures",
                      (key, result.status, now, last, streak))
            # Bounded detailed history; keep current health across process restarts.
            c.execute("DELETE FROM collection_runs WHERE checked_at < ?", (now - 90 * 86400,))

    def notify(self, now=None):
        if not self.notifier or not self.config.get("enabled", False):
            return
        now = time.time() if now is None else now
        clock = datetime.fromtimestamp(now, ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
        start, end = self.config.get("quiet_start", "23:00"), self.config.get("quiet_end", "07:00")
        if start and end and ((start <= clock < end) if start < end else
                             (clock >= start or clock < end) if start > end else False):
            return
        due = []
        with self.storage._conn() as c:
            for row in c.execute("SELECT * FROM collection_health"):
                if row["query_key"] not in self.active_queries:
                    continue
                if row["status"] in EXCLUDED:
                    continue
                recovery = row["status"] in HEALTHY and row["incident_open"]
                failure = (row["consecutive_failures"] >= int(self.config.get("failure_threshold", 3))
                           and now - (row["last_alert"] or 0) >=
                           float(self.config.get("cooldown_hours", 6)) * 3600)
                if recovery or failure:
                    due.append((dict(row), bool(recovery)))
        if not due:
            return
        due = due[:30]
        lines = [f'- {r["query_key"]}: {"恢复" if recovered else r["status"]}；'
                 f'连续失败 {r["consecutive_failures"]}；最近成功 {r["last_success"] or "从未"}'
                 for r, recovered in due]
        if self.notifier.send("机票采集状态提醒", "\n".join(lines)):
            with self.storage._conn() as c:
                for row, recovered in due:
                    c.execute("UPDATE collection_health SET last_alert=?,incident_open=? WHERE query_key=?",
                              (now, 0 if recovered else 1, row["query_key"]))

    def save_round_trip(self, quote):
        with self.storage._conn() as c:
            c.execute("INSERT INTO round_trip_quotes VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?)",
                      (quote.platform, quote.from_city, quote.to_city, quote.depart_date,
                       quote.return_date, quote.price, quote.currency, quote.price_basis,
                       quote.source_url, quote.fetched_at, quote.extra))

    def view(self):
        with self.storage._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM collection_health ORDER BY query_key")]
