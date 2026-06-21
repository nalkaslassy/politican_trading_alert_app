"""APScheduler job definitions for Capitol Radar.

Three jobs:
  1. Daily pipeline        Mon–Fri 09:00 ET  — scrape, filter, score, alert
  2. Outcome updater       Daily   08:00 ET  — update win/loss records
  3. Weekly leaderboard    Monday  09:05 ET  — post leaderboard to Telegram
"""

import logging
from collections import defaultdict
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from scraper.capitol_trades import fetch_trades
from filters.screener import filter_trades
from scorer.signal import score_trade
from alerts.telegram import send_trade_alert
from performance.tracker import update_outcomes
from performance.leaderboard import post_leaderboard

logger = logging.getLogger(__name__)

_TZ = "America/New_York"

_SIGNAL_RANK = {"strong": 3, "moderate": 2, "weak": 1, "unknown": 0}

# Larger size = higher priority when capping per-politician alerts
_SIZE_RANK = {
    "1m":    7, "500k": 6, "250k": 5, "100k": 4,
    "50k":   3, "15k":  2, "1k":   1,
}


def _size_score(trade_size: str) -> int:
    """Return a numeric rank for a trade size string (higher = larger trade)."""
    s = (trade_size or "").lower().replace("–", "-").replace(",", "")
    for key, rank in _SIZE_RANK.items():
        if key in s:
            return rank
    return 0


def _flag_bulk_filers(candidates: list[dict], bulk_threshold: int) -> None:
    """Mark trades as bulk_filing=True when a politician files many trades on one date.

    A politician filing 10+ trades on the same date is almost certainly doing
    routine portfolio rebalancing, not making a concentrated conviction bet.
    Those trades are scored and stored but deprioritised in the alert queue.
    """
    counts: dict[tuple, int] = defaultdict(int)
    for t in candidates:
        key = (t.get("politician_name", ""), t.get("trade_date", ""))
        counts[key] += 1

    for t in candidates:
        key = (t.get("politician_name", ""), t.get("trade_date", ""))
        t["_bulk_filing"] = counts[key] >= bulk_threshold
        if t["_bulk_filing"]:
            logger.debug(
                "%s filed %d trades on %s — flagged as bulk/rebalancing",
                t.get("politician_name"), counts[key], t.get("trade_date"),
            )


def run_outcome_updater(db) -> None:
    """Job 2: Update trade outcomes and politician stats."""
    logger.info("[Outcome Updater] Started at %s", datetime.now().isoformat())
    update_outcomes(db)
    logger.info("[Outcome Updater] Finished at %s", datetime.now().isoformat())


def run_daily_pipeline(db, config) -> None:
    """Job 1: Scrape → Filter → Score → Alert pipeline.

    Alert selection logic (in order):
      1. Scrape all pages within the age window.
      2. Filter: Buy, valid ticker, size ≥ $15K, not seen, entry point OK.
      3. Flag bulk-filing days (politician filed ≥ bulk_filing_threshold trades
         on the same date — likely routine rebalancing, not conviction bets).
      4. Score every candidate with Claude Sonnet.
      5. Sort by (signal_rank DESC, is_bulk_filing ASC, size_rank DESC).
      6. Apply per-politician cap (max_alerts_per_politician) — ensures diversity.
      7. Apply minimum signal strength gate — suppress weak/unknown scores.
      8. Send surviving alerts to Telegram.
    """
    logger.info("[Daily Pipeline] Started at %s", datetime.now().isoformat())

    trades = fetch_trades(config)
    logger.info("[Daily Pipeline] Scraped %d trades", len(trades))

    trades_to_alert, trades_to_store_only = filter_trades(trades, config, db)

    # Store and mark all qualifying Buy trades before scoring.
    # alerted=True here means "passed the screener" — the signal gate below
    # may still suppress the Telegram send, but the trade is tracked either way.
    for trade in trades_to_alert:
        db.insert_trade(trade, alerted=True)
        db.mark_seen(trade["trade_id"])
    for trade in trades_to_store_only:
        db.insert_trade(trade, alerted=False)
        db.mark_seen(trade["trade_id"])

    if not trades_to_alert:
        logger.info(
            "[Daily Pipeline] Finished — %d scraped, 0 alerted, %d stored silently",
            len(trades), len(trades_to_store_only),
        )
        return

    # Read config knobs
    min_signal       = config.get("min_signal_strength", "moderate")
    min_rank         = _SIGNAL_RANK.get(min_signal, 2)
    max_per_pol      = int(config.get("max_alerts_per_politician", 3))
    bulk_threshold   = int(config.get("bulk_filing_threshold", 6))

    # Step 3 — flag bulk filers before scoring (cheaper than API calls)
    _flag_bulk_filers(trades_to_alert, bulk_threshold)

    bulk_count   = sum(1 for t in trades_to_alert if t.get("_bulk_filing"))
    single_count = len(trades_to_alert) - bulk_count
    logger.info(
        "Candidates: %d single-conviction trades, %d bulk-filing trades",
        single_count, bulk_count,
    )

    # Step 4 — score all candidates with Claude
    scored: list[dict] = []
    for trade in trades_to_alert:
        stats        = db.get_politician_stats(trade.get("politician_name", ""))
        scored_trade = score_trade(trade, stats, config)
        scored.append(scored_trade)

    # Step 5 — sort: signal quality DESC, bulk penalty, size DESC
    scored.sort(
        key=lambda t: (
            _SIGNAL_RANK.get(t.get("signal_strength", "unknown"), 0),
            0 if t.get("_bulk_filing") else 1,   # non-bulk trades first
            _size_score(t.get("trade_size", "")),
        ),
        reverse=True,
    )

    # Steps 6 & 7 — per-politician cap + signal gate
    pol_counts:    dict[str, int] = defaultdict(int)
    alerts_to_send: list[dict]    = []
    suppressed_weak   = 0
    suppressed_cap    = 0
    suppressed_bulk   = 0

    for trade in scored:
        pol    = trade.get("politician_name", "Unknown")
        signal = trade.get("signal_strength", "unknown")
        rank   = _SIGNAL_RANK.get(signal, 0)

        if rank < min_rank:
            suppressed_weak += 1
            logger.info(
                "SUPPRESSED (weak signal '%s')  %s  %s",
                signal, trade.get("ticker"), pol,
            )
            continue

        if pol_counts[pol] >= max_per_pol:
            suppressed_cap += 1
            logger.info(
                "SUPPRESSED (per-politician cap %d)  %s  %s",
                max_per_pol, trade.get("ticker"), pol,
            )
            continue

        if trade.get("_bulk_filing") and rank < _SIGNAL_RANK.get("strong", 3):
            suppressed_bulk += 1
            logger.info(
                "SUPPRESSED (bulk-filing day, signal='%s')  %s  %s",
                signal, trade.get("ticker"), pol,
            )
            continue

        pol_counts[pol] += 1
        alerts_to_send.append(trade)

    logger.info(
        "Alert selection: %d to send | suppressed: %d weak, %d cap, %d bulk-rebalancing",
        len(alerts_to_send), suppressed_weak, suppressed_cap, suppressed_bulk,
    )

    # Step 8 — send and update DB alerted flag
    alerted_count = 0
    for trade in alerts_to_send:
        stats = db.get_politician_stats(trade.get("politician_name", ""))
        send_trade_alert(trade, stats, config)
        alerted_count += 1
        logger.info(
            "ALERT SENT  %-6s  %-28s  signal=%-8s  entry=%s  move=%+.1f%%",
            trade.get("ticker", "?"),
            trade.get("politician_name", "?"),
            trade.get("signal_strength", "?"),
            trade.get("_entry_quality", "?"),
            trade.get("_move_pct_since_trade", 0.0),
        )

    logger.info(
        "[Daily Pipeline] Finished — %d scraped, %d alerted, %d stored silently",
        len(trades), alerted_count, len(trades_to_store_only),
    )


def run_weekly_leaderboard(db, config) -> None:
    """Job 3: Post the weekly performance leaderboard to Telegram."""
    logger.info("[Weekly Leaderboard] Started at %s", datetime.now().isoformat())
    post_leaderboard(db, config)
    logger.info("[Weekly Leaderboard] Finished at %s", datetime.now().isoformat())


def start_scheduler(db, config) -> None:
    """Configure and start the blocking APScheduler with all three jobs."""
    scheduler = BlockingScheduler(timezone=_TZ)

    scheduler.add_job(
        run_outcome_updater,
        CronTrigger(hour=8, minute=0, timezone=_TZ),
        args=[db],
        name="outcome_updater",
        id="outcome_updater",
    )

    scheduler.add_job(
        run_daily_pipeline,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=_TZ),
        args=[db, config],
        name="daily_pipeline",
        id="daily_pipeline",
    )

    scheduler.add_job(
        run_weekly_leaderboard,
        CronTrigger(day_of_week="mon", hour=9, minute=5, timezone=_TZ),
        args=[db, config],
        name="weekly_leaderboard",
        id="weekly_leaderboard",
    )

    logger.info(
        "Scheduler starting — outcome_updater (daily 08:00), "
        "daily_pipeline (Mon–Fri 09:00), weekly_leaderboard (Mon 09:05) [all ET]"
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped by user")
