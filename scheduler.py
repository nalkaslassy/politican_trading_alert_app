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

_SIZE_RANK = {
    "1m":   7, "500k": 6, "250k": 5, "100k": 4,
    "50k":  3, "15k":  2, "1k":   1,
}


def _size_score(trade_size: str) -> int:
    s = (trade_size or "").lower().replace("–", "-").replace(",", "")
    for key, rank in _SIZE_RANK.items():
        if key in s:
            return rank
    return 0


def _select_alerts(
    candidates: list[dict],
    min_rank: int,
    max_per_pol: int,
    label: str,
) -> tuple[list[dict], int, int]:
    """Apply per-politician cap and signal gate.

    Returns (alerts_to_send, suppressed_weak, suppressed_cap).
    Candidates must be pre-sorted (structured_score DESC, then size DESC).
    """
    pol_counts: dict[str, int] = defaultdict(int)
    alerts:     list[dict]     = []
    suppressed_weak = 0
    suppressed_cap  = 0

    for trade in candidates:
        pol    = trade.get("politician_name", "Unknown")
        signal = trade.get("signal_strength", "unknown")
        rank   = _SIGNAL_RANK.get(signal, 0)

        if rank < min_rank:
            suppressed_weak += 1
            logger.info(
                "[%s] SUPPRESSED (weak '%s')  %s  %s",
                label, signal, trade.get("ticker"), pol,
            )
            continue

        if pol_counts[pol] >= max_per_pol:
            suppressed_cap += 1
            logger.info(
                "[%s] SUPPRESSED (per-politician cap %d)  %s  %s",
                label, max_per_pol, trade.get("ticker"), pol,
            )
            continue

        pol_counts[pol] += 1
        alerts.append(trade)

    return alerts, suppressed_weak, suppressed_cap


def run_outcome_updater(db) -> None:
    """Job 2: Update trade outcomes and politician stats."""
    logger.info("[Outcome Updater] Started at %s", datetime.now().isoformat())
    update_outcomes(db)
    logger.info("[Outcome Updater] Finished at %s", datetime.now().isoformat())


def run_daily_pipeline(db, config) -> None:
    """Job 1: Scrape → Filter → Score → Alert pipeline.

    Pipeline (research-informed):
      1. Scrape all pages within max_trade_age_days window.
      2. Filter: separate buy_alerts, sell_alerts, store_only paths.
         - Both buys and sells are scored (Molk & Partnoy: sells carry signal).
         - Structured score from tabular features drives signal_strength.
         - Basket likelihood (not binary bulk flag) handles rebalancing.
         - Disclosure-date entry assessment (Lazzaretto 2024) for buys.
         - ATR-normalised entry block instead of fixed percentage.
      3. Score: Claude writes narrative only — does NOT determine signal_strength.
      4. Sort by structured_score DESC, then size DESC.
      5. Per-politician cap + signal gate.
      6. Send buy alerts then sell alerts to Telegram.
    """
    logger.info("[Daily Pipeline] Started at %s", datetime.now().isoformat())

    trades = fetch_trades(config)
    logger.info("[Daily Pipeline] Scraped %d trades", len(trades))

    buy_candidates, sell_candidates, store_only = filter_trades(trades, config, db)

    # Store all (seen-marking happens after so we don't re-alert next run)
    for trade in buy_candidates + sell_candidates:
        db.insert_trade(trade, alerted=True)
        db.mark_seen(trade["trade_id"])
    for trade in store_only:
        db.insert_trade(trade, alerted=False)
        db.mark_seen(trade["trade_id"])

    min_signal     = config.get("min_signal_strength", "moderate")
    min_rank       = _SIGNAL_RANK.get(min_signal, 2)
    max_per_pol    = int(config.get("max_alerts_per_politician", 3))

    total_alerted = 0

    # ── Buy alerts ─────────────────────────────────────────────────────
    if buy_candidates:
        # Sort by score first, then apply gates — Claude only called on survivors
        buy_candidates.sort(
            key=lambda t: (t.get("_structured_score", 0), _size_score(t.get("trade_size", ""))),
            reverse=True,
        )
        buy_to_score, sup_weak, sup_cap = _select_alerts(
            buy_candidates, min_rank, max_per_pol, "BUY"
        )
        logger.info(
            "[BUY] %d candidates → %d pass gate (suppressed: %d weak, %d cap) → calling Claude",
            len(buy_candidates), len(buy_to_score), sup_weak, sup_cap,
        )

        buy_alerts = []
        for trade in buy_to_score:
            stats = db.get_politician_stats(trade.get("politician_name", ""))
            buy_alerts.append(score_trade(trade, stats, config))

        for trade in buy_alerts:
            stats = db.get_politician_stats(trade.get("politician_name", ""))
            send_trade_alert(trade, stats, config)
            total_alerted += 1
            logger.info(
                "BUY ALERT  %-6s  %-28s  score=%-3d  signal=%-8s  entry=%s",
                trade.get("ticker", "?"), trade.get("politician_name", "?"),
                trade.get("_structured_score", 0), trade.get("signal_strength", "?"),
                trade.get("_entry_quality", "?"),
            )

    # ── Sell alerts ────────────────────────────────────────────────────
    if sell_candidates:
        sell_candidates.sort(
            key=lambda t: (t.get("_structured_score", 0), _size_score(t.get("trade_size", ""))),
            reverse=True,
        )
        sell_to_score, sup_weak, sup_cap = _select_alerts(
            sell_candidates, min_rank, max_per_pol, "SELL"
        )
        logger.info(
            "[SELL] %d candidates → %d pass gate (suppressed: %d weak, %d cap) → calling Claude",
            len(sell_candidates), len(sell_to_score), sup_weak, sup_cap,
        )

        sell_alerts = []
        for trade in sell_to_score:
            stats = db.get_politician_stats(trade.get("politician_name", ""))
            sell_alerts.append(score_trade(trade, stats, config))

        for trade in sell_alerts:
            stats = db.get_politician_stats(trade.get("politician_name", ""))
            send_trade_alert(trade, stats, config)
            total_alerted += 1
            logger.info(
                "SELL ALERT %-6s  %-28s  score=%-3d  signal=%-8s",
                trade.get("ticker", "?"), trade.get("politician_name", "?"),
                trade.get("_structured_score", 0), trade.get("signal_strength", "?"),
            )

    logger.info(
        "[Daily Pipeline] Finished — %d scraped, %d alerted (%d buys, %d sells), %d stored silently",
        len(trades), total_alerted,
        len(buy_candidates), len(sell_candidates),
        len(store_only),
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
