"""Trade filtering and structured pre-scoring for Capitol Radar.

Research-informed design (see RESEARCH_BRIEF.md):

1. BOTH buys and sells are tracked — the literature finds sell-side signal
   is at least as reliable as buy-side post-STOCK Act.

2. Size threshold is NOT a hard dollar cut-off. We use a relative-size
   percentile against the politician's own trade history. A $15K trade from
   a politician who normally files $1K buys is high-conviction; a $15K trade
   from someone who routinely files $100K+ is noise.

3. Basket scoring is sector-aware, not just count-based. Research (Wei & Zhou
   NBER 2025, GovGreed) shows alpha concentrates in sector-specific bets, not
   diversified rebalancing. 10 semiconductor buys = sector thesis (score 1);
   10 buys across healthcare/utilities/tech/financials = rebalancing (score 3).

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
from collections import Counter, defaultdict

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
# Sector lookup (cached per pipeline run)
# ---------------------------------------------------------------------------

_sector_cache: dict[str, str] = {}


def _get_sector(ticker: str) -> str:
    """Return GICS sector for a ticker; cached for the lifetime of the pipeline run."""
    if ticker in _sector_cache:
        return _sector_cache[ticker]
    try:
        import yfinance as yf
        sector = yf.Ticker(ticker).info.get("sector", "Unknown") or "Unknown"
    except Exception:
        sector = "Unknown"
    _sector_cache[ticker] = sector
    return sector


# ---------------------------------------------------------------------------
# Structured feature computation
# ---------------------------------------------------------------------------

def _compute_basket_score(trade: dict, all_candidates: list[dict]) -> int:
    """Score trade concentration vs broad rebalancing (0=conviction, 3=basket).

    Count-only thresholds are too aggressive: Ro Khanna's MU (+79%) and UCTT
    (+154%) trades were batch-filed alongside other positions. A 10-semiconductor
    buy cluster is a sector thesis; a 10-buy cluster spanning healthcare/utilities/
    tech/financials is routine rebalancing. We use sector diversity to distinguish.

    Scoring:
      0 — 1–3 tickers: concentrated conviction bet
      1 — 4–7 tickers, OR 8–20 tickers with ≥70% in 1–2 sectors: sector thesis
      2 — 8–20 tickers with ambiguous or missing sector data
      3 — 21+ tickers, OR 8–20 tickers spread across 3+ unrelated sectors
    """
    pol        = trade.get("politician_name", "")
    trade_date = trade.get("trade_date", "")
    same_day   = [
        t for t in all_candidates
        if t.get("politician_name") == pol
        and t.get("trade_date") == trade_date
    ]
    n = len(same_day)

    if n <= 3:   return 0   # concentrated conviction bet
    if n <= 7:   return 1   # small cluster

    if n > 20:   return 3   # always rebalancing at this scale

    # 8–20 tickers: sector diversity determines signal vs. noise
    tickers = [t.get("ticker") for t in same_day if t.get("ticker")]
    if len(tickers) >= 4:
        sector_counts = Counter(_get_sector(t) for t in tickers)
        sector_counts.pop("Unknown", None)
        if sector_counts:
            total_known = sum(sector_counts.values())
            top_count   = sector_counts.most_common(1)[0][1]
            top_pct     = top_count / total_known
            num_sectors = len(sector_counts)

            if top_pct >= 0.70 and num_sectors <= 2:
                return 1   # sector-concentrated thesis

            if num_sectors >= 3:
                return 3   # diversified across unrelated sectors = rebalancing

    return 2   # ambiguous — moderate cluster, no sector data


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


def _trading_days_since(filing_date: date) -> int:
    """Count Mon-Fri business days from the first tradable session after filing.

    Weekend filings push the clock to Monday so a Friday-evening disclosure
    doesn't lose freshness points over the weekend when no one can trade.
    Falls back to calendar days if numpy is unavailable.
    """
    wd = filing_date.weekday()   # 0=Mon, 5=Sat, 6=Sun
    if wd == 5:
        first_tradable = filing_date + timedelta(days=2)
    elif wd == 6:
        first_tradable = filing_date + timedelta(days=1)
    else:
        first_tradable = filing_date

    today = date.today()
    if today <= first_tradable:
        return 0
    try:
        import numpy as np
        return int(np.busday_count(str(first_tradable), str(today)))
    except Exception:
        return (today - first_tradable).days


def _compute_freshness_score(trading_days: int) -> int:
    """Return 0–20 pts based on trading days since first tradable session.

    Bands per ChatGPT review of Lazzaretto 2024 — alpha is temporary
    post-disclosure; continuous decay avoids calendar-day cliff effects.
    """
    if trading_days <= 2:   return 20
    if trading_days <= 5:   return 18
    if trading_days <= 10:  return 14
    if trading_days <= 15:  return 9
    if trading_days <= 21:  return 5
    return 0   # staleness gate at 21 td makes this unreachable in practice


def _compute_repeat_pts(prior_buys: int, prior_sells: int) -> int:
    """Return 0–6 pts for direction-aware repeat same-ticker buying.

    Counts only prior BUY transactions as conviction evidence; penalises
    alternating buy/sell activity which suggests routine two-way trading
    rather than informed accumulation (Lazzaretto 2024).
    """
    if prior_buys == 0:
        score = 0
    elif prior_buys == 1:
        score = 2
    elif prior_buys <= 3:
        score = 5
    else:
        score = 6

    # Penalise two-way trading — prior sells dilute conviction signal
    if prior_sells >= prior_buys:
        score -= 2
    elif prior_sells > 0:
        score -= 1

    return max(0, min(score, 6))


def _compute_structured_score(trade: dict, basket_score: int, committee_overlap: int,
                               power_score: int, prior_buys: int, prior_sells: int,
                               freshness_pts: int, contractor_pts: int) -> tuple[int, str]:
    """Compute a 0–100 structured signal score from tabular features.

    Does NOT use LLM. Claude receives this score and writes the narrative.

    Weights (research-informed, calibrated per ChatGPT review of literature):
      Power/influence           → 0-28 pts  (NBER 2025: formal leadership is the signal)
      Committee/issuer relevance→ 0-30 pts  (Dong & Xu 2025; currently sector-level proxy)
      Disclosure freshness      → 0-20 pts  (Lazzaretto 2024: alpha fades quickly)
      Federal contractor status → 0-12 pts  (NBER 2025: leaders buy firms that get contracts)
      Repeat-trader pattern     → 0-6 pts   (graduated, direction-aware)
      Owner type                → 0-5 pts   (post-STOCK Act spouse edge is weaker)
      Basket concentration      → 0-5 pts   (tiebreaker only; unvalidated)

    Removed (per Belmont 2022 — larger trades underperform post-STOCK Act):
      Trade size                → 0 pts
      Relative size history     → 0 pts
    """
    owner_type = trade.get("owner_type", "Unknown")

    power_pts     = min(28, power_score)
    committee_pts = min(15, committee_overlap * 5)      # 0, 5, 10, 15 — GICS proxy only
    freshness_pts = min(20, freshness_pts)
    contract_pts  = min(12, contractor_pts)
    repeat_pts    = _compute_repeat_pts(prior_buys, prior_sells)
    owner_weight  = _OWNER_WEIGHT.get(owner_type, 1)
    owner_pts     = {3: 5, 2: 3, 1: 1, 0: 1}.get(owner_weight, 1)
    basket_pts    = {0: 5, 1: 3, 2: 1, 3: 0}.get(basket_score, 0)

    # No cap — raw max is 91. Thresholds (45/65) are absolute, not percentages.
    total = (power_pts + committee_pts + freshness_pts + contract_pts
             + repeat_pts + owner_pts + basket_pts)

    breakdown = (
        f"power={power_pts} committee={committee_pts} fresh={freshness_pts} "
        f"contractor={contract_pts} repeat={repeat_pts} owner={owner_pts} basket={basket_pts}"
    )
    return total, breakdown


def _score_to_strength(score: int, basket_score: int,
                       power_pts: int, committee_pts: int, freshness_pts: int) -> str:
    """Map structured score to signal_strength tier.

    Tiers:
      strong       — 65+ AND top leadership (power≥22).
      high_moderate— 65+ without top leadership.
      moderate     — 35+ with any power (≥5) OR any committee overlap (≥5 pts = 1 matching committee).
                     Covers ~40 politicians with mapped committees or scored power.
      weak         — everything else.
    """
    if basket_score >= 3:
        return "weak"   # broad portfolio event — not a conviction signal

    if freshness_pts < 5:
        return "weak"   # beyond 21 trading days — unreachable given filter gate, but safety check

    if score >= 65 and power_pts >= 22:
        return "strong"

    if score >= 65:
        return "high_moderate"

    if score >= 35 and (power_pts >= 5 or committee_pts >= 5):
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

        # Disclosure staleness gate — filing_date within max_trading_days_since_disclosure
        # Using trading days (not calendar days) matches the freshness score clock.
        max_disc_td = int(config.get("max_trading_days_since_disclosure", 21))
        filing_dt   = _parse_trade_date(trade.get("filing_date", ""))
        if filing_dt and _trading_days_since(filing_dt) > max_disc_td:
            logger.debug(
                "Trade %s → store-only (disclosure %d trading-days old, limit %d td)",
                trade_id, _trading_days_since(filing_dt), max_disc_td,
            )
            store_only.append(trade)
            continue

        # ── Structured scoring ──────────────────────────────────────────
        candidate_pool = all_buys if is_buy else all_sells
        basket_score   = _compute_basket_score(trade, candidate_pool)

        pol_name = trade.get("politician_name", "")

        # Freshness score — trading days since first tradable session after disclosure
        trading_days  = _trading_days_since(filing_dt) if filing_dt else 14
        freshness_pts = _compute_freshness_score(trading_days)

        # Power/influence score (Hall-Karadas-Schlosky, NBER 2025)
        try:
            from filters.power_score import get_power_score
            power_score, power_note = get_power_score(pol_name)
        except Exception:
            power_score, power_note = 3, ""

        # Committee overlap (may do yfinance sector lookup)
        try:
            from filters.committee_overlap import get_committee_overlap_score
            committee_overlap, committee_note = get_committee_overlap_score(pol_name, ticker)
        except Exception:
            committee_overlap, committee_note = 0, ""

        # Repeat-trader pattern — direction-aware (Lazzaretto 2024)
        try:
            prior_buys, prior_sells = db.get_prior_buy_sell_counts(pol_name, ticker)
        except Exception:
            prior_buys, prior_sells = 0, 0

        # Contractor score — USAspending federal contract exposure (cached 7 days)
        try:
            from filters.contractor_score import get_contractor_score
            contractor_pts, contractor_note = get_contractor_score(
                trade.get("company_name", ""),
                ticker,
                cache_get=db.get_contractor_cache,
                cache_set=db.set_contractor_cache,
            )
        except Exception:
            contractor_pts, contractor_note = 0, ""

        # Relative size (kept for display/DB only — not used in score per Belmont 2022)
        history  = db.get_politician_trade_history(pol_name)
        rel_size = _compute_relative_size_score(trade, history)

        power_pts     = min(28, power_score)
        committee_pts = min(15, committee_overlap * 5)

        structured_score, score_breakdown = _compute_structured_score(
            trade, basket_score, committee_overlap, power_score,
            prior_buys, prior_sells, freshness_pts, contractor_pts
        )
        signal_strength = _score_to_strength(
            structured_score, basket_score, power_pts, committee_pts, freshness_pts
        )

        # Attach computed features to trade dict for scorer and alert formatter
        trade["_basket_score"]        = basket_score
        trade["_rel_size_pct"]        = round(rel_size * 100, 1)
        trade["_committee_overlap"]   = committee_overlap
        trade["_committee_note"]      = committee_note
        trade["_power_score"]         = power_score
        trade["_power_note"]          = power_note
        trade["_prior_buys"]          = prior_buys
        trade["_prior_sells"]         = prior_sells
        trade["_freshness_pts"]       = freshness_pts
        trade["_contractor_pts"]      = contractor_pts
        trade["_contractor_note"]     = contractor_note
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
