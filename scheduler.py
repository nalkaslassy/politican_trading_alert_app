"""APScheduler job definitions for Capitol Radar.

Three jobs:
  1. Daily pipeline        Mon–Fri 09:00 ET  — scrape, filter, score, alert
  2. Outcome updater       Daily   08:00 ET  — update win/loss records
  3. Weekly leaderboard    Monday  09:05 ET  — post leaderboard to Telegram
"""

import logging
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


def run_outcome_updater(db) -> None:
    """Job 2: Update trade outcomes and politician stats."""
    logger.info("[Outcome Updater] Started at %s", datetime.now().isoformat())
    update_outcomes(db)
    logger.info("[Outcome Updater] Finished at %s", datetime.now().isoformat())


def run_daily_pipeline(db, config) -> None:
    """Job 1: Scrape → Filter → Score → Alert pipeline."""
    logger.info("[Daily Pipeline] Started at %s", datetime.now().isoformat())

    trades = fetch_trades(config)
    logger.info("[Daily Pipeline] Scraped %d trades", len(trades))

    trades_to_alert, trades_to_store_only = filter_trades(trades, config, db)

    # Store and mark all qualifying Buy trades
    all_buys = trades_to_alert + trades_to_store_only
    for trade in all_buys:
        is_alerting = trade in trades_to_alert
        db.insert_trade(trade, alerted=is_alerting)
        db.mark_seen(trade["trade_id"])

    # Score qualifying trades; gate on minimum signal strength before alerting
    min_signal = config.get("min_signal_strength", "weak")   # "strong" | "moderate" | "weak"
    _signal_rank = {"strong": 3, "moderate": 2, "weak": 1, "unknown": 0}
    min_rank = _signal_rank.get(min_signal, 1)

    alerted_count = 0
    for trade in trades_to_alert:
        stats = db.get_politician_stats(trade.get("politician_name", ""))
        scored_trade = score_trade(trade, stats, config)

        signal = scored_trade.get("signal_strength", "unknown")
        if _signal_rank.get(signal, 0) >= min_rank:
            send_trade_alert(scored_trade, stats, config)
            alerted_count += 1
        else:
            logger.info(
                "Trade %s scored '%s' — below min_signal_strength '%s'; suppressed",
                trade.get("trade_id"), signal, min_signal,
            )

    logger.info(
        "[Daily Pipeline] Finished — %d total scraped, %d alerted, %d stored silently",
        len(trades),
        alerted_count,
        len(trades_to_store_only),
    )


def run_weekly_leaderboard(db, config) -> None:
    """Job 3: Post the weekly performance leaderboard to Telegram."""
    logger.info("[Weekly Leaderboard] Started at %s", datetime.now().isoformat())
    post_leaderboard(db, config)
    logger.info("[Weekly Leaderboard] Finished at %s", datetime.now().isoformat())


def start_scheduler(db, config) -> None:
    """Configure and start the blocking APScheduler with all three jobs."""
    scheduler = BlockingScheduler(timezone=_TZ)

    # Job 2 runs at 8:00am ET daily — before the pipeline
    scheduler.add_job(
        run_outcome_updater,
        CronTrigger(hour=8, minute=0, timezone=_TZ),
        args=[db],
        name="outcome_updater",
        id="outcome_updater",
    )

    # Job 1 runs Mon–Fri at 9:00am ET
    scheduler.add_job(
        run_daily_pipeline,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=_TZ),
        args=[db, config],
        name="daily_pipeline",
        id="daily_pipeline",
    )

    # Job 3 runs every Monday at 9:05am ET
    scheduler.add_job(
        run_weekly_leaderboard,
        CronTrigger(day_of_week="mon", hour=9, minute=5, timezone=_TZ),
        args=[db, config],
        name="weekly_leaderboard",
        id="weekly_leaderboard",
    )

    logger.info("Scheduler starting — jobs: outcome_updater (daily 08:00), "
                "daily_pipeline (Mon–Fri 09:00), weekly_leaderboard (Mon 09:05) "
                "[all times ET]")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped by user")
