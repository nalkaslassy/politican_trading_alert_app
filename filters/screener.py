"""Trade filtering and alpha-politician logic for Capitol Radar."""

import logging
import re

logger = logging.getLogger(__name__)

_SMALL_SIZE_PHRASES = frozenset(
    [
        "under $1,000",
        "$1,001 - $15,000",
        "$1,001-$15,000",
    ]
)

_VALID_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


def _is_valid_ticker(ticker: str | None) -> bool:
    """Return True if ticker is a plausible US equity symbol (1–5 uppercase letters)."""
    if not ticker:
        return False
    return bool(_VALID_TICKER_RE.match(ticker))


def _is_small_trade(trade_size: str) -> bool:
    """Return True if the trade size falls below the $15,001 alert threshold."""
    if not trade_size:
        return False
    normalised = trade_size.strip().lower()
    return any(phrase.lower() in normalised for phrase in _SMALL_SIZE_PHRASES)


def is_alpha_politician(politician_name: str, config: dict, db) -> bool:
    """Determine whether a politician qualifies for trade alerts.

    Evaluation order:
      1. Strict watchlist (config watchlist_mode == "strict")
      2. Dynamic leaderboard stats (watchlist_mode == "dynamic")
      3. Catch-all (watchlist_mode == "all")
    """
    mode = config.get("watchlist_mode", "strict")

    if mode == "strict":
        watchlist = [p.strip() for p in config.get("watchlist_politicians", [])]
        return politician_name.strip() in watchlist

    if mode == "dynamic":
        min_trades = int(config.get("min_trades_for_dynamic", 5))
        min_win_rate = float(config.get("min_win_rate", 0.60))
        stats = db.get_politician_stats(politician_name)
        if stats is None:
            return False
        return (
            stats.get("total_buys", 0) >= min_trades
            and stats.get("win_rate_30d", 0.0) >= min_win_rate
        )

    # "all" or any unrecognised mode — alert on everything
    return True


def filter_trades(
    trades: list[dict], config: dict, db
) -> tuple[list[dict], list[dict]]:
    """Split trades into (trades_to_alert, trades_to_store_only).

    Every Buy trade is stored regardless.  Only trades passing ALL
    alert criteria go into trades_to_alert.
    """
    trades_to_alert: list[dict] = []
    trades_to_store_only: list[dict] = []

    for trade in trades:
        trade_id = trade.get("trade_id", "")
        trade_type = trade.get("trade_type", "")
        ticker = trade.get("ticker")
        trade_size = trade.get("trade_size", "")

        # Only track Buy trades at all
        if trade_type != "Buy":
            logger.debug("Skipping non-Buy trade %s (%s)", trade_id, trade_type)
            continue

        # Deduplication — already processed
        if db.is_seen(trade_id):
            logger.debug("Already seen trade %s; skipping", trade_id)
            continue

        # Evaluate alert eligibility
        valid_ticker = _is_valid_ticker(ticker)
        small_size = _is_small_trade(trade_size)
        alpha = is_alpha_politician(trade.get("politician_name", ""), config, db)

        if valid_ticker and not small_size and alpha:
            trades_to_alert.append(trade)
            logger.debug("Trade %s → alert queue", trade_id)
        else:
            reason = []
            if not valid_ticker:
                reason.append(f"invalid ticker '{ticker}'")
            if small_size:
                reason.append(f"small size '{trade_size}'")
            if not alpha:
                reason.append("not alpha politician")
            trades_to_store_only.append(trade)
            logger.debug("Trade %s → store-only (%s)", trade_id, "; ".join(reason))

    logger.info(
        "Filter result: %d to alert, %d to store only",
        len(trades_to_alert),
        len(trades_to_store_only),
    )
    return trades_to_alert, trades_to_store_only
