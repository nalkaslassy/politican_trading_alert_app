"""Trade filtering and alpha-politician logic for Capitol Radar."""

import logging
import re
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

_SMALL_SIZE_PHRASES = frozenset(
    [
        # Old capitoltrades.com format
        "under $1,000",
        "$1,001 - $15,000",
        "$1,001-$15,000",
        # New abbreviated format used by the site (e.g. "1K–15K")
        "<1k",
        "1k",
        "1k–15k",
        "1k-15k",
    ]
)

_VALID_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")

# Date formats emitted by the scraper and stored in the DB
_DATE_FORMATS = [
    "%d %b %Y",   # "13 May 2026"  ← scraper output
    "%Y-%m-%d",   # "2026-05-13"   ← ISO
    "%m/%d/%Y",   # "05/13/2026"
    "%B %d, %Y",  # "May 13, 2026"
]


def _parse_trade_date(date_str: str) -> date | None:
    """Parse a trade_date string into a date object; return None on failure."""
    if not date_str:
        return None
    cleaned = date_str.strip().replace("\n", " ")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    logger.debug("Could not parse trade date: %r", date_str)
    return None


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


def _is_trade_recent(trade: dict, config: dict) -> tuple[bool, str]:
    """Return (passes, reason) based on how old the trade_date is.

    The STOCK Act allows up to 45 days to disclose, so a trade filed today
    could be 45 days old.  We allow up to max_trade_age_days (default 90)
    to catch late filers while still excluding truly stale opportunities.
    """
    max_days = int(config.get("max_trade_age_days", 90))
    trade_date = _parse_trade_date(trade.get("trade_date", ""))
    if trade_date is None:
        return True, ""  # can't determine — allow through

    age = (date.today() - trade_date).days
    if age > max_days:
        return False, f"trade is {age} days old (max {max_days})"
    return True, ""


def _assess_entry_point(trade: dict, config: dict) -> tuple[bool, str]:
    """Assess whether there is still a reasonable entry for this trade.

    Returns (blocked, reason).  Attaches entry metadata to the trade dict so
    the Telegram alert can surface useful context to the user:

      _price_at_trade        closing price on (or nearest to) the trade date
      _current_price         latest close
      _move_pct_since_trade  % change from trade date to now (negative = discount)
      _entry_quality         "fresh" | "caution" | "discount" | "blocked"
      _entry_note            one-line comment included in the alert

    Three configurable thresholds:
      max_price_move_block (default 40%) — hard block; opportunity clearly passed.
      max_price_move_warn  (default 15%) — still alert, flag the move as a caution.
      max_price_move_disc  (default -10%) — stock is CHEAPER than when the politician
                                            bought; highlight as a discount entry.
    """
    block_pct = float(config.get("max_price_move_block", 40.0))
    warn_pct  = float(config.get("max_price_move_warn",  15.0))
    disc_pct  = float(config.get("max_price_move_disc", -10.0))

    ticker = trade.get("ticker")
    trade_date_str = trade.get("trade_date", "")

    if not ticker or not trade_date_str:
        return False, ""

    trade_date = _parse_trade_date(trade_date_str)
    if trade_date is None:
        return False, ""

    try:
        import yfinance as yf

        tk = yf.Ticker(ticker)

        # Price on (or near) the trade date
        start = trade_date - timedelta(days=4)
        end = trade_date + timedelta(days=2)
        hist = tk.history(start=str(start), end=str(end))
        if hist.empty:
            logger.debug("No price history for %s around %s; skipping entry check", ticker, trade_date)
            return False, ""
        price_at_trade = float(hist["Close"].iloc[-1])

        # Current price
        today_hist = tk.history(period="2d")
        if today_hist.empty:
            return False, ""
        current_price = float(today_hist["Close"].iloc[-1])

        if price_at_trade <= 0:
            return False, ""

        move_pct = ((current_price - price_at_trade) / price_at_trade) * 100

        trade["_price_at_trade"]       = round(price_at_trade, 2)
        trade["_current_price"]        = round(current_price, 2)
        trade["_move_pct_since_trade"] = round(move_pct, 1)

        # Hard block — stock already ran hard; train has left the station
        if move_pct >= block_pct:
            trade["_entry_quality"] = "blocked"
            trade["_entry_note"] = (
                f"Already up {move_pct:.1f}% since trade — entry window has closed"
            )
            return True, f"{ticker} already up {move_pct:.1f}% (hard block >{block_pct}%)"

        # Discount — stock is cheaper than when the politician bought it (better entry)
        if move_pct <= disc_pct:
            trade["_entry_quality"] = "discount"
            trade["_entry_note"] = (
                f"Discount entry — stock is {abs(move_pct):.1f}% below politician's "
                f"cost basis (${price_at_trade:.2f}). Thesis still intact."
            )
            return False, ""

        # Caution — meaningful move but catalyst may still be ahead
        if move_pct >= warn_pct:
            trade["_entry_quality"] = "caution"
            trade["_entry_note"] = (
                f"Up {move_pct:.1f}% since trade date — confirm the catalyst "
                f"is still pending before entering"
            )
            return False, ""

        # Fresh — minimal price change, clean entry window
        trade["_entry_quality"] = "fresh"
        trade["_entry_note"] = (
            f"Clean entry — only {move_pct:+.1f}% from politician's price (${price_at_trade:.2f})"
        )
        return False, ""

    except Exception as exc:
        logger.debug("Entry-point check failed for %s: %s", ticker, exc)
        return False, ""  # on error, allow the trade through


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

    Every Buy trade is stored regardless of outcome.  A trade reaches
    trades_to_alert only when it passes ALL of:
      1. Is a Buy
      2. Valid US equity ticker (1–5 chars)
      3. Size ≥ $15,001
      4. Not already seen
      5. Politician is on the alpha watchlist
      6. Trade is recent enough (within max_trade_age_days)
      7. Stock has NOT already blown past the hard-block threshold
         (caution and discount entries still go through with context tags)
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

        # --- Evaluate all alert criteria ---
        reasons_blocked: list[str] = []

        valid_ticker = _is_valid_ticker(ticker)
        if not valid_ticker:
            company = trade.get("company_name", "")
            label   = f"'{company}'" if company else f"ticker='{ticker}'"
            reasons_blocked.append(f"non-equity instrument {label} (no stock ticker)")

        small_size = _is_small_trade(trade_size)
        if small_size:
            reasons_blocked.append(f"small size '{trade_size}'")

        alpha = is_alpha_politician(trade.get("politician_name", ""), config, db)
        if not alpha:
            reasons_blocked.append("not alpha politician")

        # Recency gate — don't alert on stale trades
        recent, age_reason = _is_trade_recent(trade, config)
        if not recent:
            reasons_blocked.append(age_reason)

        # Entry-point assessment — only run yfinance if trade passed the cheaper gates
        if not reasons_blocked and valid_ticker:
            entry_blocked, entry_reason = _assess_entry_point(trade, config)
            if entry_blocked:
                reasons_blocked.append(entry_reason)

        if not reasons_blocked:
            trades_to_alert.append(trade)
            quality = trade.get("_entry_quality", "unknown")
            move    = trade.get("_move_pct_since_trade", 0.0)
            logger.info(
                "Trade %s → ALERT  ticker=%-6s  entry=%s  move=%+.1f%%",
                trade_id, ticker, quality, move,
            )
        else:
            trades_to_store_only.append(trade)
            logger.debug("Trade %s → store-only (%s)", trade_id, "; ".join(reasons_blocked))

    logger.info(
        "Filter result: %d to alert, %d to store only",
        len(trades_to_alert),
        len(trades_to_store_only),
    )
    return trades_to_alert, trades_to_store_only
