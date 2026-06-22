"""Trade filtering and structured pre-scoring for Capitol Radar.

Research-informed design (see RESEARCH_BRIEF.md):

1. BOTH buys and sells are tracked — the literature finds sell-side signal
   is at least as reliable as buy-side post-STOCK Act.

2. Size threshold is NOT a hard dollar cut-off. We use a relative-size
   percentile against the politician's own trade history. A $15K trade from
   a politician who normally files $1K buys is high-conviction; a $15K trade
   from someone who routinely files $100K+ is noise.

3. Basket likelihood replaces the binary bulk-filing flag. We score how
   concentrated a trade is (1-2 tickers that day = conviction) vs how broad
   (10+ tickers = rebalancing) using a 0–3 continuous score.

4. Entry assessment uses the DISCLOSURE DATE (filing_date) as the primary
   clock, not the execution date. Lazzaretto (2024) found alpha accrues after
   disclosure, not after the original transaction. Price move is normalised
   by ATR so that volatile stocks aren't unfairly blocked.

5. Structured score feeds Claude — Claude does NOT determine signal strength.
   It receives the pre-computed score and writes the human-readable narrative.
"""

import logging
import re
from datetime import date, datetime, timedelta
from collections import defaultdict

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")

# Date formats from the scraper
_DATE_FORMATS = [
    "%d %b %Y",   # "13 May 2026"
    "%Y-%m-%d",   # "2026-05-13"
    "%m/%d/%Y",   # "05/13/2026"
    "%B %d, %Y",  # "May 13, 2026"
]

# Absolute minimum size to bother storing at all (bonds/tiny speculative trades)
_ABS_MIN_SIZE_BAND = 2   # must be band 2+ ($1K–$15K still stored; <$1K dropped)

# Map size band strings to a numeric rank (higher = larger)
_SIZE_BANDS = {
    "1m": 7, "500k": 6, "250k": 5, "100k": 4,
    "50k": 3, "15k": 2, "1k": 1, "<1k": 0,
}

# Owner-type → conviction weight (Karadas: spouse portfolios > member)
_OWNER_WEIGHT = {"Spouse": 3, "Self": 2, "Dependent": 1, "Unknown": 1}

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _parse_trade_date(date_str: str) -> date | None:
    if not date_str:
        return None
    cleaned = date_str.strip().replace("\n", " ")
    # Site shows "HH:MM" or "Today" for same-day filings — treat as today
    if "today" in cleaned.lower() or (len(cleaned) <= 5 and ":" in cleaned):
        return date.today()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def _is_valid_ticker(ticker: str | None) -> bool:
    if not ticker:
        return False
    return bool(_VALID_TICKER_RE.match(ticker))


def _size_band(trade_size: str) -> int:
    """Return numeric band 0–7 for a trade_size string."""
    s = (trade_size or "").lower().replace("–", "-").replace(",", "")
    for key, rank in sorted(_SIZE_BANDS.items(), key=lambda x: -x[1]):
        if key in s:
            return rank
    return 0


def _size_band_to_dollars(band: int) -> int:
    """Return approximate midpoint dollar value for a size band."""
    mapping = {7: 1_500_000, 6: 750_000, 5: 375_000, 4: 175_000,
               3: 75_000,   2: 32_500,  1: 8_000,   0: 500}
    return mapping.get(band, 0)

# ---------------------------------------------------------------------------
# Structured feature computation
# ---------------------------------------------------------------------------

def _compute_basket_score(trade: dict, all_candidates: list[dict]) -> int:
    """Score how concentrated this trade is vs a broad basket (0=concentrated, 3=basket).

    Uses the NUMBER OF UNIQUE TICKERS traded by this politician on the same
    trade_date (not filing_date — avoids penalising late-filing batches).
    """
    pol        = trade.get("politician_name", "")
    trade_date = trade.get("trade_date", "")
    same_day   = [
        t for t in all_candidates
        if t.get("politician_name") == pol
        and t.get("trade_date") == trade_date
    ]
    n = len(same_day)
    if n <= 2:   return 0   # concentrated conviction bet
    if n <= 4:   return 1   # small cluster, some rebalancing possible
    if n <= 8:   return 2   # likely rebalancing
    return 3                # broad basket — routine rebalancing


def _compute_relative_size_score(trade: dict, history: list[dict]) -> float:
    """Return percentile (0.0–1.0) of this trade's size vs politician's history.

    Falls back to 0.5 if there is no history (unknown, treated as median).
    """
    if not history:
        return 0.5

    current_band = _size_band(trade.get("trade_size", ""))
    current_val  = _size_band_to_dollars(current_band)

    hist_vals = [_size_band_to_dollars(_size_band(h.get("trade_size", ""))) for h in history]
    hist_vals = [v for v in hist_vals if v > 0]
    if not hist_vals:
        return 0.5

    below = sum(1 for v in hist_vals if v < current_val)
    return below / len(hist_vals)


def _compute_disclosure_entry(trade: dict, config: dict) -> tuple[bool, str, dict]:
    """Assess entry using the DISCLOSURE DATE (filing_date) as the primary clock.

    Normalises the price move by ATR so that volatile stocks aren't blocked
    unfairly by a fixed percentage threshold.

    Returns (blocked: bool, reason: str, metadata: dict).
    Attaches _disclosure_* keys to metadata for the alert formatter.
    """
    ticker       = trade.get("ticker")
    filing_date  = _parse_trade_date(trade.get("filing_date", ""))
    trade_date   = _parse_trade_date(trade.get("trade_date", ""))

    meta: dict = {}

    if not ticker or not filing_date:
        return False, "", meta

    block_atr  = float(config.get("entry_block_atr_multiple",  3.0))
    caution_atr = float(config.get("entry_caution_atr_multiple", 1.5))
    disc_pct    = float(config.get("max_price_move_disc",       -10.0))

    try:
        import yfinance as yf
        tk = yf.Ticker(ticker)

        # Price at trade_date (politician's cost basis)
        if trade_date:
            hist_trade = tk.history(
                start=str(trade_date - timedelta(days=4)),
                end=str(trade_date + timedelta(days=2)),
            )
            price_at_trade = float(hist_trade["Close"].iloc[-1]) if not hist_trade.empty else None
        else:
            price_at_trade = None

        # Price at filing_date (disclosure date — when information becomes public)
        hist_disc = tk.history(
            start=str(filing_date - timedelta(days=2)),
            end=str(filing_date + timedelta(days=2)),
        )
        price_at_disclosure = float(hist_disc["Close"].iloc[-1]) if not hist_disc.empty else None

        # Current price
        today_hist  = tk.history(period="2d")
        current_price = float(today_hist["Close"].iloc[-1]) if not today_hist.empty else None

        if not current_price or not price_at_disclosure:
            return False, "", meta

        # ATR (14-day) for normalisation
        hist_atr = tk.history(period="30d")
        if len(hist_atr) >= 2:
            high_low  = hist_atr["High"] - hist_atr["Low"]
            atr       = float(high_low.tail(14).mean())
        else:
            atr = None

        move_since_disclosure_pct = ((current_price - price_at_disclosure) / price_at_disclosure) * 100
        days_since_disclosure     = (date.today() - filing_date).days

        meta.update({
            "_price_at_trade":           round(price_at_trade, 2) if price_at_trade else None,
            "_price_at_disclosure":      round(price_at_disclosure, 2),
            "_current_price":            round(current_price, 2),
            "_move_pct_since_disclosure":round(move_since_disclosure_pct, 1),
            "_days_since_disclosure":    days_since_disclosure,
            "_atr":                      round(atr, 2) if atr else None,
        })

        # ATR-normalised move since disclosure
        if atr and atr > 0:
            atr_units = abs(move_since_disclosure_pct / 100 * price_at_disclosure) / atr
            meta["_atr_units_moved"] = round(atr_units, 2)

            if move_since_disclosure_pct > 0 and atr_units >= block_atr:
                meta["_entry_quality"] = "blocked"
                meta["_entry_note"]    = (
                    f"Up {move_since_disclosure_pct:.1f}% ({atr_units:.1f}x ATR) "
                    f"since disclosure {days_since_disclosure}d ago — opportunity passed"
                )
                return True, f"{ticker} moved {atr_units:.1f}x ATR since disclosure", meta

            if move_since_disclosure_pct > 0 and atr_units >= caution_atr:
                meta["_entry_quality"] = "caution"
                meta["_entry_note"]    = (
                    f"Up {move_since_disclosure_pct:.1f}% ({atr_units:.1f}x ATR) "
                    f"since disclosure — confirm thesis still valid"
                )
                return False, "", meta

        # Discount entry — cheaper now than at disclosure
        if move_since_disclosure_pct <= disc_pct:
            meta["_entry_quality"] = "discount"
            meta["_entry_note"]    = (
                f"Down {abs(move_since_disclosure_pct):.1f}% since disclosure "
                f"(${price_at_disclosure:.2f} → ${current_price:.2f}) — "
                f"better entry than when disclosed"
            )
            return False, "", meta

        # Fresh — minimal move since disclosure
        meta["_entry_quality"] = "fresh"
        meta["_entry_note"]    = (
            f"{move_since_disclosure_pct:+.1f}% since disclosure "
            f"{days_since_disclosure}d ago — clean entry window"
        )
        return False, "", meta

    except Exception as exc:
        logger.debug("Entry assessment failed for %s: %s", ticker, exc)
        return False, "", meta


def _compute_structured_score(trade: dict, basket_score: int,
                               committee_overlap: int, power_score: int,
                               repeat_trader: bool) -> tuple[int, str]:
    """Compute a 0–100 structured signal score from tabular features.

    Does NOT use LLM. Claude receives this score and writes the narrative.

    Weights (research-informed, updated per Belmont 2022 / NBER 2025):
      Power/influence           → 0-35 pts  (strongest modern signal — NBER 2025 leadership paper)
      Committee overlap (0-3)  → 0-25 pts  (direct jurisdiction per Dong & Xu 2025)
      Owner type                → 0-20 pts  (spouse > self per Karadas 2019)
      Basket score (inverted)  → 0-10 pts  (concentration; unvalidated but intuitive)
      Repeat-trader bonus       → 0-10 pts  (repeated same-stock trades per Lazzaretto 2024)

    Removed (per Belmont 2022 — larger trades underperform post-STOCK Act):
      Trade size                → 0 pts
      Relative size history     → 0 pts
    """
    owner_type = trade.get("owner_type", "Unknown")

    power_pts     = min(35, power_score)
    committee_pts = min(25, committee_overlap * 8)      # 0, 8, 16, or 24 → cap 25
    owner_pts     = {3: 20, 2: 15, 1: 5, 0: 5}.get(_OWNER_WEIGHT.get(owner_type, 1), 5)
    basket_pts    = max(0, (3 - basket_score)) * 3      # 0, 3, 6, or 9
    repeat_pts    = 10 if repeat_trader else 0

    total = power_pts + committee_pts + owner_pts + basket_pts + repeat_pts

    breakdown = (
        f"power={power_pts} committee={committee_pts} owner={owner_pts} "
        f"concentration={basket_pts} repeat={repeat_pts}"
    )
    return min(100, total), breakdown


def _score_to_strength(score: int, basket_score: int) -> str:
    """Map structured score to signal_strength label."""
    if basket_score >= 3:
        return "weak"      # broad basket always weak regardless of score
    if score >= 55:
        return "strong"
    if score >= 35:
        return "moderate"
    return "weak"


# ---------------------------------------------------------------------------
# Main filter function
# ---------------------------------------------------------------------------

def filter_trades(
    trades: list[dict], config: dict, db
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split trades into (buy_alerts, sell_alerts, store_only).

    All qualifying Buy and Sell trades are stored.
    buy_alerts  — Buy trades that pass all quality gates
    sell_alerts — Sell trades that pass quality gates (exit signal)
    store_only  — Everything else (stored silently for tracking)
    """
    buy_alerts:   list[dict] = []
    sell_alerts:  list[dict] = []
    store_only:   list[dict] = []

    # Pre-compute basket scores using the full candidate set
    all_buys  = [t for t in trades if t.get("trade_type") == "Buy"]
    all_sells = [t for t in trades if t.get("trade_type") == "Sell"]

    min_size_band = int(config.get("min_size_band", 2))   # default: ≥ $1K–$15K band

    for trade in trades:
        trade_id   = trade.get("trade_id", "")
        trade_type = trade.get("trade_type", "")
        ticker     = trade.get("ticker")
        is_buy     = trade_type == "Buy"
        is_sell    = trade_type == "Sell"

        if not is_buy and not is_sell:
            continue

        if db.is_seen(trade_id):
            continue

        if not _is_valid_ticker(ticker):
            store_only.append(trade)
            continue

        if _size_band(trade.get("trade_size", "")) < min_size_band:
            store_only.append(trade)
            continue

        # Recency gate — trade_date within max_trade_age_days
        max_age   = int(config.get("max_trade_age_days", 90))
        trade_dt  = _parse_trade_date(trade.get("trade_date", ""))
        if trade_dt and (date.today() - trade_dt).days > max_age:
            store_only.append(trade)
            continue

        # Disclosure staleness gate — filing_date within max_days_since_disclosure
        # Alpha accrues quickly after disclosure and fades; stale filings are dead signals.
        max_disc_age = int(config.get("max_days_since_disclosure", 21))
        filing_dt    = _parse_trade_date(trade.get("filing_date", ""))
        if filing_dt and (date.today() - filing_dt).days > max_disc_age:
            logger.debug(
                "Trade %s → store-only (disclosure %dd old, limit %d)",
                trade_id, (date.today() - filing_dt).days, max_disc_age,
            )
            store_only.append(trade)
            continue

        # ── Structured scoring ──────────────────────────────────────────
        candidate_pool = all_buys if is_buy else all_sells
        basket_score   = _compute_basket_score(trade, candidate_pool)

        pol_name = trade.get("politician_name", "")

        # Power/influence score (Hall-Karadas-Schlosky, NBER 2025)
        try:
            from filters.power_score import get_power_score
            power_score, power_note = get_power_score(pol_name)
        except Exception:
            power_score, power_note = 5, ""

        # Committee overlap (may do yfinance sector lookup)
        try:
            from filters.committee_overlap import get_committee_overlap_score
            committee_overlap, committee_note = get_committee_overlap_score(pol_name, ticker)
        except Exception:
            committee_overlap, committee_note = 0, ""

        # Repeat-trader bonus (Lazzaretto 2024: repeated same-stock trades are speculative signals)
        try:
            repeat_count  = db.get_prior_trade_count(pol_name, ticker)
            repeat_trader = repeat_count > 0
        except Exception:
            repeat_trader = False

        # Relative size (kept for display/DB only — not used in score per Belmont 2022)
        history  = db.get_politician_trade_history(pol_name)
        rel_size = _compute_relative_size_score(trade, history)

        structured_score, score_breakdown = _compute_structured_score(
            trade, basket_score, committee_overlap, power_score, repeat_trader
        )
        signal_strength = _score_to_strength(structured_score, basket_score)

        # Attach computed features to trade dict for scorer and alert formatter
        trade["_basket_score"]        = basket_score
        trade["_rel_size_pct"]        = round(rel_size * 100, 1)
        trade["_committee_overlap"]   = committee_overlap
        trade["_committee_note"]      = committee_note
        trade["_power_score"]         = power_score
        trade["_power_note"]          = power_note
        trade["_repeat_trader"]       = repeat_trader
        trade["_structured_score"]    = structured_score
        trade["_score_breakdown"]     = score_breakdown
        trade["signal_strength"]      = signal_strength

        # ── Entry point (buys only) — disclosure-date clock ────────────
        if is_buy:
            blocked, block_reason, entry_meta = _compute_disclosure_entry(trade, config)
            trade.update(entry_meta)
            if blocked:
                trade["_entry_quality"] = "blocked"
                store_only.append(trade)
                logger.debug("Trade %s → store-only (entry blocked: %s)", trade_id, block_reason)
                continue
            # If yfinance returned nothing at all, the ticker is likely dead/delisted
            if not entry_meta.get("_current_price") and not entry_meta.get("_entry_quality"):
                logger.debug("Trade %s → store-only (ticker %s returned no price data)", trade_id, ticker)
                store_only.append(trade)
                continue

        logger.info(
            "%s %s %-6s  %-28s  score=%d (%s)  signal=%-8s  basket=%d  rel_size=%.0f%%  committee=%d",
            "BUY " if is_buy else "SELL",
            "→ ALERT" if signal_strength in ("strong", "moderate") else "→ weak ",
            ticker, trade.get("politician_name", "")[:28],
            structured_score, signal_strength,
            signal_strength, basket_score,
            rel_size * 100, committee_overlap,
        )

        if is_buy:
            buy_alerts.append(trade)
        else:
            sell_alerts.append(trade)

    logger.info(
        "Filter result: %d buy alerts, %d sell alerts, %d store-only",
        len(buy_alerts), len(sell_alerts), len(store_only),
    )
    return buy_alerts, sell_alerts, store_only
