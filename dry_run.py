"""Dry-run the Capitol Radar pipeline without sending Telegram alerts.

Shows every trade candidate with its structured score breakdown so you can
judge whether the scoring logic is calibrated correctly before going live.

Usage:
  python dry_run.py                  # scrape live, show all candidates
  python dry_run.py --top 10        # show only top 10 by score
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

from storage.db import Database
from scraper.capitol_trades import fetch_trades
from filters.screener import filter_trades
from scorer.signal import score_trade


def _load_config(path: Path) -> dict:
    if not path.exists():
        print(f"Config not found: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _bar(score: int, width: int = 20) -> str:
    filled = int(score / 100 * width)
    return "[" + "█" * filled + "·" * (width - filled) + f"] {score:3d}/100"


def _signal_label(strength: str) -> str:
    return {"strong": "[STRONG]", "moderate": "[MODERATE]", "weak": "[WEAK]"}.get(strength, "[UNKNOWN]")


def _print_trade(trade: dict, rank: int) -> None:
    t = trade
    print(f"\n{'-'*68}")
    print(f"  #{rank}  {'BUY ' if t.get('trade_type')=='Buy' else 'SELL'} "
          f"{t.get('ticker','?'):<6}  {t.get('politician_name','?')}")
    print(f"       {t.get('company_name','')}  |  {t.get('trade_size','?')}")
    print(f"       Owner: {t.get('owner_type','?')}  |  "
          f"Filed: {t.get('filing_date','?')}  |  Traded: {t.get('trade_date','?')}")
    print(f"       {_bar(t.get('_structured_score', 0))}  {_signal_label(t.get('signal_strength','?'))}")
    print(f"       Score breakdown: {t.get('_score_breakdown','')}")
    print(f"       Committee: {t.get('_committee_note','none')}")
    print(f"       Basket score: {t.get('_basket_score','?')} (0=concentrated, 3=basket)  |  "
          f"Rel size: {t.get('_rel_size_pct','?')}th pct")

    if t.get("_entry_quality"):
        eq = t["_entry_quality"]
        emoji = {"fresh":"[OK]","caution":"[CAUTION]","discount":"[DISCOUNT]","blocked":"[BLOCKED]"}.get(eq,"[?]")
        print(f"       Entry ({emoji} {eq.upper()}): {t.get('_entry_note','')}")
        if t.get("_price_at_disclosure"):
            print(f"       Politician cost: ${t.get('_price_at_trade') or '?'}  |  "
                  f"At disclosure: ${t.get('_price_at_disclosure')}  |  "
                  f"Now: ${t.get('_current_price','?')}  "
                  f"({t.get('_move_pct_since_disclosure',0):+.1f}% since disclosure, "
                  f"{t.get('_days_since_disclosure','?')}d ago)")

    if t.get("reasoning"):
        print(f"       Claude: {t.get('reasoning')}")
    if t.get("watch_out"):
        print(f"       Risk:   {t.get('watch_out')}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--top", type=int, default=0, help="Show only top N by score (0=all)")
    parser.add_argument("--skip-score", action="store_true",
                        help="Skip Claude narrative calls (faster, shows structured scores only)")
    parser.add_argument("--ignore-seen", action="store_true",
                        help="Re-evaluate all scraped trades regardless of seen status (dry run only — nothing is stored)")
    args = parser.parse_args()

    config = _load_config(Path(args.config))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    db_path = config.get("db_path", "./data/capitol_radar.db")
    db = Database(db_path)
    db.init_db()

    print("\n" + "="*68)
    print("  CAPITOL RADAR -- DRY RUN  (no alerts will be sent)")
    print("="*68)

    # Scrape
    print("\n[1/3] Scraping capitoltrades.com ...")
    trades = fetch_trades(config)
    print(f"      -> {len(trades)} trades fetched")

    if args.ignore_seen:
        print("      [--ignore-seen] Bypassing seen_trades table for this dry run")

    # Filter + structured score
    print("\n[2/3] Filtering and scoring ...")

    if args.ignore_seen:
        # Wrap db so is_seen always returns False — nothing is written
        class _NoSeenDB:
            def is_seen(self, _): return False
            def get_politician_trade_history(self, name): return db.get_politician_trade_history(name)
            def get_politician_stats(self, name): return db.get_politician_stats(name)
        scoring_db = _NoSeenDB()
    else:
        scoring_db = db

    buy_candidates, sell_candidates, store_only = filter_trades(trades, config, scoring_db)
    all_candidates = buy_candidates + sell_candidates
    print(f"      → {len(buy_candidates)} buy candidates, "
          f"{len(sell_candidates)} sell candidates, "
          f"{len(store_only)} stored-only")

    if not all_candidates:
        print("\nNo candidates to show. All trades were either already seen, "
              "no valid ticker, too old, or entry blocked.")
        return

    # Sort by structured score
    all_candidates.sort(key=lambda t: t.get("_structured_score", 0), reverse=True)
    if args.top:
        all_candidates = all_candidates[:args.top]

    # Claude narrative (optional)
    if not args.skip_score:
        print(f"\n[3/3] Generating Claude narratives for {len(all_candidates)} candidates …")
        for i, trade in enumerate(all_candidates, 1):
            print(f"      {i}/{len(all_candidates)} {trade.get('ticker')} {trade.get('politician_name')}")
            stats = db.get_politician_stats(trade.get("politician_name", ""))
            scored = score_trade(trade, stats, config)
            all_candidates[i-1] = scored
    else:
        print("\n[3/3] Skipping Claude (--skip-score)")

    # Print results
    print(f"\n{'='*68}")
    print(f"  RESULTS: {len(all_candidates)} candidates ranked by structured score")
    print(f"{'='*68}")

    for rank, trade in enumerate(all_candidates, 1):
        _print_trade(trade, rank)

    # Summary table
    strong   = sum(1 for t in all_candidates if t.get("signal_strength") == "strong")
    moderate = sum(1 for t in all_candidates if t.get("signal_strength") == "moderate")
    weak     = sum(1 for t in all_candidates if t.get("signal_strength") == "weak")
    buys     = sum(1 for t in all_candidates if t.get("trade_type") == "Buy")
    sells    = sum(1 for t in all_candidates if t.get("trade_type") == "Sell")

    print(f"\n{'='*68}")
    print(f"  SUMMARY")
    print(f"  Total scraped: {len(trades)}   Buys: {buys}   Sells: {sells}")
    print(f"  Stored-only:   {len(store_only)}")
    print(f"  Signal mix:    [STRONG] {strong}   [MODERATE] {moderate}   [WEAK] {weak}")
    print(f"{'='*68}\n")


if __name__ == "__main__":
    main()
