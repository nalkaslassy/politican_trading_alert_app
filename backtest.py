"""Backtest Capitol Radar signal criteria against stored historical trades.

For every historical buy that would have passed the current live criteria,
computes:
  - Return at 7 / 30 / 60 / 90 days after disclosure date
  - SPY return over the same window (alpha = trade return - SPY return)
  - Win rate: % of trades that beat SPY at the 30-day mark

Uses the ACTUAL live scoring logic (filters/screener.py, power_score.py,
committee_overlap.py) so results reflect what the system would genuinely
have alerted. Contractor scores are skipped (set to 0) for speed — this
slightly underestimates scores but does not change the gate outcome for
high-power/committee trades.

Usage:
  python backtest.py               # full analysis, all filter combos
  python backtest.py --combo current_live   # just the live criteria
  python backtest.py --min-date 2026-04-01  # restrict date range
"""

import argparse
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import yaml

# ── optional rich output ─────────────────────────────────────────────────────
try:
    from rich.console import Console
    from rich.table import Table
    _console = Console()
    def _print_table(title, headers, rows):
        t = Table(title=title, show_lines=True)
        for h in headers: t.add_column(h, justify="right" if h not in ("Filter","Politician","Ticker") else "left")
        for r in rows: t.add_row(*[str(x) for x in r])
        _console.print(t)
except ImportError:
    def _print_table(title, headers, rows):
        print(f"\n{'='*72}\n  {title}\n{'='*72}")
        print("  " + "  ".join(f"{h:<18}" for h in headers))
        print("  " + "-"*68)
        for r in rows:
            print("  " + "  ".join(f"{str(x):<18}" for x in r))


# ── constants ────────────────────────────────────────────────────────────────

_HOLD_DAYS = [7, 30, 60, 90]
_BENCHMARK  = "SPY"


# ── date parsing ─────────────────────────────────────────────────────────────

def _parse_date(s: str) -> Optional[date]:
    if not s:
        return None
    s = s.strip().replace("\n", " ")
    if "today" in s.lower() or (len(s) <= 5 and ":" in s):
        return date.today()
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


# ── price fetching ────────────────────────────────────────────────────────────

_price_cache: dict[str, object] = {}

def _get_prices(ticker: str, start: date, days: list[int]) -> dict[int, Optional[float]]:
    """Return {hold_days: closing_price} for a ticker starting from `start`."""
    end = start + timedelta(days=max(days) + 10)
    key = f"{ticker}_{start}_{end}"
    if key not in _price_cache:
        try:
            import yfinance as yf
            df = yf.download(ticker, start=str(start), end=str(end), progress=False, auto_adjust=True)
            _price_cache[key] = df
        except Exception:
            _price_cache[key] = None

    df = _price_cache[key]
    result: dict[int, Optional[float]] = {0: None}
    if df is None or df.empty:
        return {d: None for d in [0] + days}

    closes = df["Close"]
    idx = closes.index

    def _price_on_or_after(target: date) -> Optional[float]:
        target_ts = str(target)
        matches = [i for i in idx if str(i.date()) >= target_ts]
        if not matches:
            return None
        val = closes.loc[matches[0]]
        # Handle MultiIndex columns (ticker-level wrapping)
        if hasattr(val, '__iter__') and not isinstance(val, float):
            try:
                val = float(val.iloc[0])
            except Exception:
                return None
        return float(val)

    result[0] = _price_on_or_after(start)
    for d in days:
        result[d] = _price_on_or_after(start + timedelta(days=d))
    return result


# ── sector lookup ─────────────────────────────────────────────────────────────

_sector_cache: dict[str, Optional[str]] = {}

def _get_sector(ticker: str) -> Optional[str]:
    if ticker not in _sector_cache:
        try:
            import yfinance as yf
            info = yf.Ticker(ticker).info
            raw = info.get("sector") or ""
            _sector_cache[ticker] = _SECTOR_ALIASES.get(raw.lower(), raw) or None
        except Exception:
            _sector_cache[ticker] = None
    return _sector_cache[ticker]


# ── scoring — uses live filter modules ───────────────────────────────────────
# Contractor score is set to 0 to avoid API calls; this slightly underestimates
# scores but does not change gate outcomes for high-power/committee trades.

def _get_trade_score(trade: dict, basket_score: int) -> tuple[int, int, int]:
    """Return (structured_score, power_pts, committee_pts) using live modules."""
    from filters.power_score import get_power_score
    from filters.committee_overlap import get_committee_overlap_score
    from filters.screener import _compute_structured_score

    pol  = trade.get("politician_name", "")
    tick = trade.get("ticker", "")

    power_score, _     = get_power_score(pol)
    committee_overlap, _ = get_committee_overlap_score(pol, tick)

    power_pts     = min(28, power_score)
    committee_pts = min(15, committee_overlap * 5)

    score, _ = _compute_structured_score(
        trade, basket_score, committee_overlap, power_score,
        prior_buys=0, prior_sells=0,
        freshness_pts=20,    # assume day-0 detection for historical trades
        contractor_pts=0,    # skip API calls; slightly underestimates scores
    )
    return score, power_pts, committee_pts


def _signal_strength(score: int, basket_score: int,
                     power_pts: int, committee_pts: int) -> str:
    """Current live gate logic."""
    from filters.screener import _score_to_strength
    return _score_to_strength(score, basket_score, power_pts, committee_pts, freshness_pts=20)


# ── filter definitions ───────────────────────────────────────────────────────
# Tests different sub-criteria to show which components drive performance.
# "current_live" is exactly what the live system alerts on.

FILTER_COMBOS = {
    "current_live": {
        "desc":       "Current live criteria  (score≥35, power≥5 OR committee≥5pts)",
        "owner_types": {"Spouse", "Self", "Dependent", "Unknown"},
        "min_signal":  "moderate",
        "max_basket":  2,
        "min_power":   0,
    },
    "high_power": {
        "desc":       "Power ≥16 only (committee chairs & leadership)",
        "owner_types": {"Spouse", "Self", "Dependent", "Unknown"},
        "min_signal":  "moderate",
        "max_basket":  2,
        "min_power":   16,
    },
    "concentrated": {
        "desc":       "Isolated buys only (basket=0), current criteria",
        "owner_types": {"Spouse", "Self", "Dependent", "Unknown"},
        "min_signal":  "moderate",
        "max_basket":  0,
        "min_power":   0,
    },
    "spouse_self": {
        "desc":       "Spouse/Self accounts only, current criteria",
        "owner_types": {"Spouse", "Self"},
        "min_signal":  "moderate",
        "max_basket":  2,
        "min_power":   0,
    },
    "strong_only": {
        "desc":       "Strong signals only  (score≥65, power≥22)",
        "owner_types": {"Spouse", "Self", "Dependent", "Unknown"},
        "min_signal":  "strong",
        "max_basket":  2,
        "min_power":   0,
    },
}


# ── main backtest logic ───────────────────────────────────────────────────────

def load_buys(db_path: str, min_date: Optional[date]) -> list[dict]:
    """Load historical buys using trade_date as the entry anchor.

    Note: in live trading we enter at disclosure (filing_date), which is
    0-45 days after trade_date. Using trade_date here is slightly optimistic
    but lets us measure signal quality across 12 months of history.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT politician_name, ticker, trade_type, trade_size,
               owner_type, filing_date, trade_date, company_name
        FROM all_trades
        WHERE trade_type = 'Buy' AND ticker IS NOT NULL AND ticker != ''
        ORDER BY trade_date
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    result = []
    for r in rows:
        td = _parse_date(r.get("trade_date", ""))
        if td is None:
            continue
        if min_date and td < min_date:
            continue
        # Need at least 30 days of forward price data
        if td >= date.today() - timedelta(days=30):
            continue
        r["_filing_date_parsed"] = td  # use trade_date as entry anchor
        result.append(r)
    return result


def compute_basket_scores(trades: list[dict]) -> dict[str, int]:
    """Count same-politician same-trade-date buys to get basket score."""
    groups: dict[tuple, int] = defaultdict(int)
    for t in trades:
        key = (t["politician_name"], str(t["_filing_date_parsed"]))
        groups[key] += 1

    scores: dict[str, int] = {}
    for t in trades:
        key = (t["politician_name"], str(t["_filing_date_parsed"]))
        n = groups[key]
        scores[f"{t['politician_name']}_{t['ticker']}_{t['_filing_date_parsed']}"] = (
            0 if n <= 2 else 1 if n <= 4 else 2 if n <= 8 else 3
        )
    return scores


def run_backtest(trades: list[dict], basket_scores: dict, spy_prices: dict,
                 combo: dict, label: str) -> dict:
    """Run one filter combination. Returns result dict."""
    results = []
    skipped_price = 0

    _rank = {"strong": 3, "high_moderate": 2, "moderate": 2, "weak": 1, "unknown": 0}
    min_rank = _rank.get(combo["min_signal"], 2)

    for t in trades:
        owner = t.get("owner_type", "Unknown")
        if owner not in combo["owner_types"]:
            continue

        ticker = t["ticker"]
        pol    = t["politician_name"]
        fd     = t["_filing_date_parsed"]
        bkey   = f"{pol}_{ticker}_{fd}"
        basket = basket_scores.get(bkey, 0)

        if basket > combo["max_basket"]:
            continue

        score, power_pts, committee_pts = _get_trade_score(t, basket)
        strength = _signal_strength(score, basket, power_pts, committee_pts)

        if _rank.get(strength, 0) < min_rank:
            continue

        # Optional minimum power filter
        if power_pts < combo.get("min_power", 0):
            continue

        # Fetch prices
        prices = _get_prices(ticker, fd, _HOLD_DAYS)
        if prices[0] is None:
            skipped_price += 1
            continue

        entry = prices[0]
        row = {
            "politician":   pol,
            "ticker":       ticker,
            "owner":        owner,
            "filing_date":  str(fd),
            "score":        score,
            "strength":     strength,
            "basket":       basket,
            "power_pts":    power_pts,
            "committee_pts":committee_pts,
            "entry_px":     entry,
        }

        for d in _HOLD_DAYS:
            px        = prices.get(d)
            spy_px    = spy_prices.get(d, {}).get(str(fd))
            spy_entry = spy_prices.get(0, {}).get(str(fd))

            ret     = (px - entry) / entry * 100 if (px is not None and entry > 0) else None
            spy_ret = (spy_px - spy_entry) / spy_entry * 100 if (spy_px and spy_entry and spy_entry > 0) else None

            row[f"ret_{d}d"]   = ret
            row[f"spy_{d}d"]   = spy_ret
            row[f"alpha_{d}d"] = (ret - spy_ret) if (ret is not None and spy_ret is not None) else None

        results.append(row)

    return {"label": label, "combo": combo, "trades": results, "skipped_price": skipped_price}


def _stats(values: list[Optional[float]]) -> tuple[float, float, float, int]:
    """Return (mean, win_rate_vs_zero, median, count) for a list of returns."""
    valid = [v for v in values if v is not None]
    if not valid:
        return 0.0, 0.0, 0.0, 0
    mean     = sum(valid) / len(valid)
    wins     = sum(1 for v in valid if v > 0) / len(valid) * 100
    sorted_v = sorted(valid)
    mid      = len(sorted_v) // 2
    median   = sorted_v[mid] if len(sorted_v) % 2 else (sorted_v[mid-1]+sorted_v[mid])/2
    return mean, wins, median, len(valid)


def print_summary(all_results: list[dict]) -> None:
    headers = ["Filter", "N", "7d ret%", "30d ret%", "30d alpha%", "30d win%", "60d ret%", "90d ret%"]
    rows = []
    for res in all_results:
        trades = res["trades"]
        if not trades:
            rows.append([res["combo"]["desc"], 0, "-","-","-","-","-","-"])
            continue
        mean7,  _, _, n7  = _stats([t.get("ret_7d")   for t in trades])
        mean30, w30, _, _ = _stats([t.get("ret_30d")  for t in trades])
        alpha30,_,  _, _  = _stats([t.get("alpha_30d")for t in trades])
        mean60, _, _, _   = _stats([t.get("ret_60d")  for t in trades])
        mean90, _, _, _   = _stats([t.get("ret_90d")  for t in trades])
        rows.append([
            res["combo"]["desc"],
            len(trades),
            f"{mean7:+.1f}%",
            f"{mean30:+.1f}%",
            f"{alpha30:+.1f}%",
            f"{w30:.0f}%",
            f"{mean60:+.1f}%",
            f"{mean90:+.1f}%",
        ])
    _print_table("BACKTEST RESULTS — all filter combos", headers, rows)


def print_top_trades(res: dict, n: int = 15) -> None:
    trades = sorted(res["trades"], key=lambda t: t.get("alpha_30d") or -999, reverse=True)
    headers = ["Politician","Ticker","Owner","Filed","Score","Power","Comm","30d ret%","30d alpha%","90d ret%"]
    rows = []
    for t in trades[:n]:
        rows.append([
            t["politician"][:22],
            t["ticker"],
            t["owner"],
            t["filing_date"],
            t["score"],
            t.get("power_pts", 0),
            t.get("committee_pts", 0),
            f"{t.get('ret_30d',0) or 0:+.1f}%",
            f"{t.get('alpha_30d',0) or 0:+.1f}%",
            f"{t.get('ret_90d',0) or 0:+.1f}%",
        ])
    _print_table(f"TOP {n} TRADES — {res['combo']['desc']}", headers, rows)


def print_worst_trades(res: dict, n: int = 10) -> None:
    trades = sorted(res["trades"], key=lambda t: t.get("alpha_30d") or 999)
    headers = ["Politician","Ticker","Owner","Filed","Score","30d ret%","30d alpha%"]
    rows = []
    for t in trades[:n]:
        rows.append([
            t["politician"][:22],
            t["ticker"],
            t["owner"],
            t["filing_date"],
            t["score"],
            f"{t.get('ret_30d',0) or 0:+.1f}%",
            f"{t.get('alpha_30d',0) or 0:+.1f}%",
        ])
    _print_table(f"WORST {n} TRADES — {res['combo']['desc']}", headers, rows)


# ── SPY bulk prefetch ─────────────────────────────────────────────────────────

def prefetch_spy(filing_dates: list[date]) -> dict:
    """Download SPY once for the entire date range and index by (hold_days, date_str)."""
    if not filing_dates:
        return {}
    min_d = min(filing_dates)
    max_d = min(max(filing_dates) + timedelta(days=100), date.today())
    print(f"  Fetching SPY from {min_d} to {max_d}...")
    try:
        import yfinance as yf
        df = yf.download(_BENCHMARK, start=str(min_d), end=str(max_d),
                         progress=False, auto_adjust=True)
        closes = df["Close"]
        idx    = [str(i.date()) for i in closes.index]
        prices = list(closes.values.flatten())
        date_to_px = dict(zip(idx, prices))
    except Exception as e:
        print(f"  Warning: SPY fetch failed ({e})")
        return {}

    result: dict = {0: {}, **{d: {} for d in _HOLD_DAYS}}
    for fd in filing_dates:
        fds = str(fd)
        def _nearest(target_date):
            for shift in range(6):
                k = str(target_date + timedelta(days=shift))
                if k in date_to_px:
                    return date_to_px[k]
            return None

        result[0][fds]  = _nearest(fd)
        for d in _HOLD_DAYS:
            result[d][fds] = _nearest(fd + timedelta(days=d))
    return result


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config",   default="config.yaml")
    parser.add_argument("--combo",    default=None, help="Run only this combo key")
    parser.add_argument("--min-date", default=None, help="YYYY-MM-DD earliest filing date")
    parser.add_argument("--top",      type=int, default=15, help="Show top N trades for best combo")
    args = parser.parse_args()

    with Path(args.config).open() as f:
        config = yaml.safe_load(f) or {}

    db_path  = config.get("db_path", "./data/capitol_radar.db")
    min_date = date.fromisoformat(args.min_date) if args.min_date else None

    print("\n" + "="*72)
    print("  CAPITOL RADAR — BACKTEST")
    print("="*72)

    print("\n[1/4] Loading historical buys from DB...")
    buys = load_buys(db_path, min_date)
    print(f"      {len(buys)} qualifying buy records")

    if not buys:
        print("\nNo historical data found. Run historical_scrape.py first.")
        sys.exit(1)

    print("\n[2/4] Computing basket scores...")
    basket_scores = compute_basket_scores(buys)

    filing_dates = [t["_filing_date_parsed"] for t in buys]
    print("\n[3/4] Pre-fetching SPY benchmark prices...")
    spy_prices = prefetch_spy(filing_dates)

    combos_to_run = {k: v for k, v in FILTER_COMBOS.items()
                     if args.combo is None or k == args.combo}

    print(f"\n[4/4] Running {len(combos_to_run)} filter combination(s)...")
    print("      (fetching stock prices — this may take a few minutes)\n")

    all_results = []
    for label, combo in combos_to_run.items():
        print(f"  >> {combo['desc']} ...")
        res = run_backtest(buys, basket_scores, spy_prices, combo, label)
        all_results.append(res)
        n = len(res["trades"])
        skipped = res["skipped_price"]
        print(f"     {n} qualifying trades ({skipped} skipped — no price data)")

    # Summary table
    print_summary(all_results)

    # Deep dive on "best" combo (or the only one run)
    focus = next((r for r in all_results if r["label"] == "best"), all_results[-1])
    if focus["trades"]:
        print_top_trades(focus, n=args.top)
        print_worst_trades(focus, n=10)

    # Spouse-only breakdown
    spouse_res = next((r for r in all_results if r["label"] == "spouse_committee"), None)
    if spouse_res and spouse_res["trades"]:
        print(f"\n--- Spouse vs Self breakdown ({spouse_res['combo']['desc']}) ---")
        for owner_type in ["Spouse", "Self"]:
            subset = [t for t in spouse_res["trades"] if t["owner"] == owner_type]
            if subset:
                mean30, w30, _, n = _stats([t.get("ret_30d") for t in subset])
                alpha30, _, _, _  = _stats([t.get("alpha_30d") for t in subset])
                print(f"  {owner_type:10s}  n={n:<4}  30d avg={mean30:+.1f}%  alpha={alpha30:+.1f}%  win={w30:.0f}%")

    print("\nDone.\n")


if __name__ == "__main__":
    main()
