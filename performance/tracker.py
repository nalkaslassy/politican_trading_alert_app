"""Update forward prices for all alerted buy trades.

Runs as Job 3 in the daily scheduler. For each alert in alert_performance
that still has missing price data, fetches the price at 7/30/60/90 days
after the alert_date and computes alpha vs SPY.
"""

import logging
from datetime import date, datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

_HOLD_DAYS = [7, 30, 60, 90]


def _fetch_price_on_or_after(ticker: str, target: date) -> Optional[float]:
    """Return the closing price on or after target (up to 5 trading days later)."""
    try:
        import yfinance as yf
        end = target + timedelta(days=8)
        df  = yf.download(ticker, start=str(target), end=str(end),
                          progress=False, auto_adjust=True)
        if df.empty:
            return None
        val = df["Close"].iloc[0]
        try:
            return float(val.iloc[0]) if hasattr(val, "iloc") else float(val)
        except Exception:
            return float(val)
    except Exception as exc:
        logger.debug("Price fetch failed %s @ %s: %s", ticker, target, exc)
        return None


def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def update_alert_prices(db) -> int:
    """Fetch any missing forward prices for tracked alerts. Returns count updated."""
    open_alerts = db.get_open_alert_performances()
    if not open_alerts:
        logger.info("[Performance] No open alerts to update")
        return 0

    today   = date.today()
    updated = 0

    for alert in open_alerts:
        trade_id   = alert["trade_id"]
        ticker     = alert["ticker"]
        alert_date = _parse_date(alert.get("alert_date", ""))
        if alert_date is None:
            continue

        fields: dict = {}

        for days in _HOLD_DAYS:
            col     = f"price_{days}d"
            spy_col = f"spy_{days}d"
            target  = alert_date + timedelta(days=days)

            if alert.get(col) is not None:
                continue
            if target > today:
                continue

            px = _fetch_price_on_or_after(ticker, target)
            if px is not None:
                fields[col] = round(px, 4)

            spy_px = _fetch_price_on_or_after("SPY", target)
            if spy_px is not None:
                fields[spy_col] = round(spy_px, 4)

        if fields:
            db.update_alert_performance(trade_id, fields)
            updated += 1
            logger.info(
                "[Performance] Updated %s %s: %s",
                ticker, trade_id[:20], list(fields.keys()),
            )

    logger.info("[Performance] Updated %d / %d open alerts", updated, len(open_alerts))
    return updated


def get_spy_price_now() -> Optional[float]:
    """Return current SPY closing price for recording alert entry."""
    return _fetch_price_on_or_after("SPY", date.today() - timedelta(days=1))
