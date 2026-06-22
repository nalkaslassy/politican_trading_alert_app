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
from performance.tracker import update_alert_prices, get_spy_price_now

logger = logging.getLogger(__name__)

_TZ = "America/New_York"

_SIGNAL_RANK = {"strong": 3, "high_moderate": 2, "moderate": 2, "weak": 1, "unknown": 0}

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


def run_performance_updater(db) -> None:
    """Job 2: Update forward prices for all alerted trades."""
    logger.info("[Performance Updater] Started at %s", datetime.now().isoformat())
    updated = update_alert_prices(db)
    logger.info("[Performance Updater] Finished — %d alerts updated", updated)


def run_daily_pipeline(db, config) -> None:
    """Job 1: Scrape → Filter → Score → Alert pipeline.

    Pipeline (research-informed):
      1. Scrape all pages within max_trade_age_days window.
      2. Filter: score buys using power/influence + committee + owner type.
         - Sells stored to DB silently (no Telegram — evidence too ambiguous).
         - Structured score gates: strong ≥55, moderate ≥35.
         - Basket likelihood handles rebalancing noise.
         - Disclosure-date entry assessment (Lazzaretto 2024).
      3. Gate + cap BEFORE Claude — only call API on trades that will alert.
      4. Claude writes narrative only; does NOT set signal_strength.
      5. Log alert to alert_performance for P&L tracking.
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

        # Fetch SPY once for performance logging
        spy_entry = get_spy_price_now()

        for trade in buy_alerts:
            stats = db.get_politician_stats(trade.get("politician_name", ""))
            send_trade_alert(trade, stats, config)
            total_alerted += 1

            # Log to performance tracker with entry price
            entry_price = trade.get("_current_price")
            db.insert_alert_performance(trade, entry_price, spy_entry)

            logger.info(
                "BUY ALERT  %-6s  %-28s  score=%-3d  signal=%-8s  entry=$%s",
                trade.get("ticker", "?"), trade.get("politician_name", "?"),
                trade.get("_structured_score", 0), trade.get("signal_strength", "?"),
                entry_price or "?",
            )

    # ── Sells stored silently — evidence too ambiguous to alert ────────
    if sell_candidates:
        logger.info(
            "[SELL] %d sell candidates stored silently (no Telegram alerts)",
            len(sell_candidates),
        )

    logger.info(
        "[Daily Pipeline] Finished — %d scraped, %d buy alerts, %d sells stored, %d store-only",
        len(trades), total_alerted, len(sell_candidates), len(store_only),
    )


def start_scheduler(db, config) -> None:
    """Configure and start the blocking APScheduler with all jobs."""
    scheduler = BlockingScheduler(timezone=_TZ)

    scheduler.add_job(
        run_performance_updater,
        CronTrigger(hour=8, minute=0, timezone=_TZ),
        args=[db],
        name="performance_updater",
        id="performance_updater",
    )

    scheduler.add_job(
        run_daily_pipeline,
        CronTrigger(day_of_week="mon-fri", hour=9, minute=0, timezone=_TZ),
        args=[db, config],
        name="daily_pipeline",
        id="daily_pipeline",
    )

    logger.info(
        "Scheduler starting — outcome_updater (daily 08:00), "
        "daily_pipeline (Mon–Fri 09:00), weekly_leaderboard (Mon 09:05) [all ET]"
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped by user")
