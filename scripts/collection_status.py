"""Read-only private status/RT quote export; never publish sessions or logs."""
import argparse
import json
import sqlite3
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/prices.db")
    ap.add_argument("--out", default="data/collection-status.json")
    ap.add_argument("--stale-hours", type=float, default=24)
    args = ap.parse_args()
    uri = Path(args.db).resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as c:
        c.row_factory = sqlite3.Row
        health = [dict(r) for r in c.execute("SELECT * FROM collection_health ORDER BY query_key")]
        for row in health:
            row["stale"] = time.time() - row["checked_at"] > args.stale_hours * 3600
        quotes = [dict(r) for r in c.execute("SELECT * FROM round_trip_quotes ORDER BY id DESC LIMIT 100")]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"health": health, "round_trip_quotes": quotes},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Status written to {out}")


if __name__ == "__main__":
    main()
