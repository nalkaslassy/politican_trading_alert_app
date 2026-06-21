"""Fetches historical stock prices via yfinance and updates trade outcomes in the DB."""

import logging
from datetime import date, datetime, timedelta

import yfinance as yf

logger = logging.getLogger(__name__)


def _parse_trade_date(trade_date_str: str) -> date | None:
    """Parse a trade_date string into a date object; return None on failure."""
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y"):
        try:
            return datetime.strptime(trade_date_str.strip(), fmt).date()
        except ValueError:
            continue
    logger.warning("Cannot parse trade date: %s", trade_date_str)
    return None


def _fetch_closing_price(ticker: str, target_date: date) -> float | None:
    """Return the closing price of ticker on or near target_date.

    Looks in a 5-day window to handle weekends and holidays.
    Returns None if data is unavailable.
    """
    try:
        start = target_date - timedelta(days=4)
        end = target_date + timedelta(days=1)
        hist = yf.Ticker(ticker).history(start=str(start), end=str(end))
        if hist.empty:
            logger.warning("No price data for %s around %s", ticker, target_date)
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as exc:
        logger.warning("yfinance error for %s on %s: %s", ticker, target_date, exc)
        return None


def _recalculate_politician_stats(db, politician_name: str, party: str | None, chamber: str | None) -> None:
    """Recompute and persist politician_stats for a single politician."""
    outcomes = db.get_trade_outcomes_for_politician(politician_name)
    total_buys = len(outcomes)
    if total_buys == 0:
        return

    wins = [o for o in outcomes if o.get("outcome_30d") == "win"]
    losses = [o for o in outcomes if o.get("outcome_30d") == "loss"]
    wins_30d = len(wins)
    losses_30d = len(losses)
    settled = wins_30d + losses_30d
    win_rate_30d = (wins_30d / settled) if settled > 0 else 0.0

    returns = [o["return_30d"] for o in outcomes if o.get("return_30d") is not None]
    avg_return_30d = (sum(returns) / len(returns)) if returns else 0.0

    existing = db.get_politician_stats(politician_name)
    total_buys_alerted = existing.get("total_buys_alerted", 0) if existing else 0

    db.upsert_politician_stats(
        politician_name,
        {
            "party": party,
            "chamber": chamber,
            "total_buys": total_buys,
            "wins_30d": wins_30d,
            "losses_30d": losses_30d,
            "win_rate_30d": win_rate_30d,
            "avg_return_30d": avg_return_30d,
            "total_buys_alerted": total_buys_alerted,
        },
    )
    logger.debug(
        "Updated stats for %s: %d buys, %.1f%% win rate",
        politician_name,
        total_buys,
        win_rate_30d * 100,
    )


def update_outcomes(db) -> None:
    """Fetch missing price data for pending trades and update outcomes + politician stats.

    Processes all Buy trades whose 30-day or 60-day outcome is still open.
    """
    pending = db.get_pending_outcomes()
    logger.info("update_outcomes: %d trades to check", len(pending))

    updated_count = 0
    affected_politicians: dict[str, tuple[str | None, str | None]] = {}

    today = date.today()

    for trade in pending:
        trade_id = trade["trade_id"]
        ticker = trade.get("ticker")
        trade_date_str = trade.get("trade_date", "")
        politician_name = trade.get("politician_name", "")
        party = trade.get("party")
        chamber = trade.get("chamber")

        if not ticker or not trade_date_str:
            logger.debug("Skipping trade %s: missing ticker or date", trade_id)
            continue

        trade_date = _parse_trade_date(trade_date_str)
        if trade_date is None:
            continue

        fields: dict = {
            "ticker": ticker,
            "politician_name": politician_name,
            "trade_date": trade_date_str,
        }

        # Price on the day of the trade
        price_at_trade = trade.get("price_at_trade")
        if price_at_trade is None:
            price_at_trade = _fetch_closing_price(ticker, trade_date)
            if price_at_trade is not None:
                fields["price_at_trade"] = price_at_trade

        # 30-day outcome
        date_30d = trade_date + timedelta(days=30)
        outcome_30d = trade.get("outcome_30d")
        if (outcome_30d is None or outcome_30d == "pending") and today >= date_30d:
            price_30d = _fetch_closing_price(ticker, date_30d)
            if price_30d is not None and price_at_trade is not None:
                return_30d = ((price_30d - price_at_trade) / price_at_trade) * 100
                fields["price_30d"] = price_30d
                fields["return_30d"] = return_30d
                fields["outcome_30d"] = "win" if return_30d > 0 else "loss"
            else:
                fields["outcome_30d"] = "pending"
        else:
            fields["outcome_30d"] = outcome_30d or "pending"

        # 60-day outcome
        date_60d = trade_date + timedelta(days=60)
        outcome_60d = trade.get("outcome_60d")
        if (outcome_60d is None or outcome_60d == "pending") and today >= date_60d:
            price_60d = _fetch_closing_price(ticker, date_60d)
            if price_60d is not None and price_at_trade is not None:
                return_60d = ((price_60d - price_at_trade) / price_at_trade) * 100
                fields["price_60d"] = price_60d
                fields["return_60d"] = return_60d
                fields["outcome_60d"] = "win" if return_60d > 0 else "loss"
            else:
                fields["outcome_60d"] = "pending"
        else:
            fields["outcome_60d"] = outcome_60d or "pending"

        db.upsert_outcome(trade_id, fields)
        updated_count += 1
        affected_politicians[politician_name] = (party, chamber)

    logger.info("Updated %d trade outcomes", updated_count)

    # Recalculate stats for every politician whose outcomes changed
    for name, (party, chamber) in affected_politicians.items():
        _recalculate_politician_stats(db, name, party, chamber)

    logger.info("Refreshed stats for %d politicians", len(affected_politicians))
