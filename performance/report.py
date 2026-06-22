"""Performance report for Capitol Radar alert history.

Shows P&L for every alerted buy: entry price, 7/30/60/90-day returns,
and alpha vs SPY. Run anytime to see how past alerts are performing.

Usage:
  python -m performance.report
  python -m performance.report --days 30   # only show last N days of alerts
"""

import argparse
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml


def _load_config(path: Path) -> dict:
    if not path.exists():
        print(f"Config not found: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open() as f:
        return yaml.safe_load(f) or {}


def _ret(entry: Optional[float], exit_: Optional[float]) -> Optional[float]:
    if entry and exit_ and entry > 0:
        return (exit_ - entry) / entry * 100
    return None


def _alpha(stock_ret: Optional[float], spy_ret: Optional[float]) -> Optional[float]:
    if stock_ret is not None and spy_ret is not None:
        return stock_ret - spy_ret
    return None


def _fmt(v: Optional[float], suffix: str = "%") -> str:
    if v is None:
        return "pending"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.1f}{suffix}"


def _col(v: Optional[float]) -> str:
    """Return colored indicator for terminal output."""
    if v is None:
        return "·"
    return "+" if v > 0 else "-"


def run_report(db, days_limit: Optional[int] = None) -> None:
    alerts = db.get_all_alert_performances()

    if not alerts:
        print("\nNo alerts tracked yet. Alerts appear after the first pipeline run.")
        return

    if days_limit:
        cutoff = str(date.today() - timedelta(days=days_limit))
        alerts = [a for a in alerts if a.get("alert_date", "") >= cutoff]

    print(f"\n{'='*80}")
    print(f"  CAPITOL RADAR — ALERT PERFORMANCE REPORT")
    print(f"  {len(alerts)} alerts tracked | as of {date.today()}")
    print(f"{'='*80}")
    print(f"  {'Date':<12} {'Ticker':<7} {'Politician':<24} {'Sig':>6} {'Score':>5} "
          f"{'7d':>7} {'30d':>7} {'30d-α':>7} {'60d':>7} {'90d':>7}")
    print(f"  {'-'*77}")

    summary_30d_rets   = []
    summary_30d_alphas = []

    for a in sorted(alerts, key=lambda x: x.get("alert_date", "")):
        entry    = a.get("entry_price")
        spy_e    = a.get("spy_entry")
        r7       = _ret(entry, a.get("price_7d"))
        r30      = _ret(entry, a.get("price_30d"))
        r60      = _ret(entry, a.get("price_60d"))
        r90      = _ret(entry, a.get("price_90d"))
        spy30    = _ret(spy_e, a.get("spy_30d"))
        spy60    = _ret(spy_e, a.get("spy_60d"))
        spy90    = _ret(spy_e, a.get("spy_90d"))
        a30      = _alpha(r30, spy30)

        if r30 is not None:
            summary_30d_rets.append(r30)
        if a30 is not None:
            summary_30d_alphas.append(a30)

        sig   = a.get("signal_strength", "?")[:3].upper()
        score = a.get("structured_score", 0)
        pol   = (a.get("politician_name") or "?")[:24]
        ticker = a.get("ticker", "?")
        dt    = a.get("alert_date", "?")

        print(f"  {dt:<12} {ticker:<7} {pol:<24} {sig:>6} {score:>5} "
              f"{_fmt(r7):>7} {_fmt(r30):>7} {_fmt(a30):>7} {_fmt(r60):>7} {_fmt(r90):>7}")

    print(f"\n{'='*80}")
    print(f"  SUMMARY")

    if summary_30d_rets:
        avg_30d   = sum(summary_30d_rets) / len(summary_30d_rets)
        wins_30d  = sum(1 for r in summary_30d_rets if r > 0)
        win_rate  = wins_30d / len(summary_30d_rets) * 100
        avg_alpha = sum(summary_30d_alphas) / len(summary_30d_alphas) if summary_30d_alphas else None
        print(f"  Settled 30d trades : {len(summary_30d_rets)}")
        print(f"  Avg 30d return     : {_fmt(avg_30d)}")
        print(f"  Avg 30d alpha      : {_fmt(avg_alpha)}")
        print(f"  Win rate (30d > 0) : {win_rate:.0f}%")
    else:
        print("  No settled 30-day trades yet — check back in 30 days.")

    pending = sum(1 for a in alerts if a.get("price_30d") is None)
    print(f"  Still pending 30d  : {pending}")
    print(f"{'='*80}\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--days",   type=int, default=None,
                        help="Only show alerts from the last N days")
    args = parser.parse_args()

    config = _load_config(Path(args.config))

    from storage.db import Database
    db = Database(config.get("db_path", "./data/capitol_radar.db"))
    db.init_db()

    run_report(db, days_limit=args.days)


if __name__ == "__main__":
    main()
