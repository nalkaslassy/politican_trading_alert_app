"""Scrape 12 months of congressional trades into the DB for backtesting.

Bypasses the seen-gate and signal filter — stores every trade found.
Run this once before backtest.py.
"""

import logging
import sys
from pathlib import Path

import yaml

from storage.db import Database
from scraper.capitol_trades import fetch_trades


def main() -> None:
    config_path = Path("config.yaml")
    if not config_path.exists():
        print("config.yaml not found", file=sys.stderr)
        sys.exit(1)

    with config_path.open("r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}

    # Override scraper settings for deep historical pull
    config["max_trade_age_days"]    = 365
    config["scrape_pages_hard_cap"] = 120

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    db = Database(config.get("db_path", "./data/capitol_radar.db"))
    db.init_db()

    print("\nScraping 12 months of historical congressional trades...")
    print("This will take 30-60 minutes. Leave it running.\n")

    trades = fetch_trades(config)
    print(f"\nScrape complete — {len(trades)} total trades found")

    stored = skipped = 0
    for trade in trades:
        if not db.is_seen(trade["trade_id"]):
            db.insert_trade(trade, alerted=False)
            db.mark_seen(trade["trade_id"])
            stored += 1
        else:
            skipped += 1

    print(f"Stored {stored} new trades | {skipped} already in DB")
    print("\nRun backtest.py next.")


if __name__ == "__main__":
    main()
