"""Resend specific alerts with freshly-fetched prices and Claude narrative.

Usage:
    python resend_alerts.py                     # resend all moderate/strong in alert_performance
    python resend_alerts.py PLTR ALB            # resend specific tickers only
"""

import sys
import os
import logging
import sqlite3
import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("resend")

with open("config.yaml") as f:
    config = yaml.safe_load(f)

from storage.db import Database as DB
from filters.screener import (
    _compute_basket_score,
    _compute_structured_score,
    _score_to_strength,
    _compute_disclosure_entry,
    _compute_freshness_score,
    _trading_days_since,
    _compute_relative_size_score,
    _parse_trade_date,
)
from filters.power_score import get_power_score
from filters.committee_overlap import get_committee_overlap_score
from filters.contractor_score import get_contractor_score
from scorer.signal import score_trade
from alerts.telegram import send_trade_alert

db = DB(config["db_path"])

# Tickers to resend (from CLI args, else all moderate/strong)
filter_tickers = set(t.upper() for t in sys.argv[1:]) if len(sys.argv) > 1 else None

conn = sqlite3.connect(config["db_path"])
conn.row_factory = sqlite3.Row
cur = conn.cursor()

# Pull candidates from alert_performance, best scores first
sql = """
    SELECT ap.trade_id, ap.signal_strength, ap.structured_score
    FROM   alert_performance ap
    WHERE  ap.signal_strength IN ('moderate', 'strong')
    ORDER  BY ap.structured_score DESC
"""
cur.execute(sql)
candidates = cur.fetchall()

if not candidates:
    logger.info("No moderate/strong trades in alert_performance — nothing to resend.")
    sys.exit(0)

sent = 0
for row in candidates:
    trade_id       = row["trade_id"]
    orig_strength  = row["signal_strength"]
    orig_score     = row["structured_score"]

    # Load full trade row from all_trades
    cur.execute("SELECT * FROM all_trades WHERE trade_id = ?", (trade_id,))
    trow = cur.fetchone()
    if not trow:
        logger.warning("Trade %s not found in all_trades; skipping.", trade_id)
        continue

    trade = dict(trow)
    ticker   = trade["ticker"]
    pol_name = trade["politician_name"]

    if filter_tickers and ticker not in filter_tickers:
        continue

    logger.info("Rescoring %s — %s (was %s, score %d)", ticker, pol_name, orig_strength, orig_score)

    # ── Re-score with fresh data ──────────────────────────────────────────

    # Basket: count same-day same-direction trades in DB
    trade_type = trade["trade_type"]
    cur.execute(
        "SELECT * FROM all_trades WHERE politician_name = ? AND trade_date = ? AND trade_type = ?",
        (pol_name, trade["trade_date"], trade_type),
    )
    same_day = [dict(r) for r in cur.fetchall()]
    basket_score = _compute_basket_score(trade, same_day)

    filing_dt     = _parse_trade_date(trade.get("filing_date", ""))
    trading_days  = _trading_days_since(filing_dt) if filing_dt else 14
    freshness_pts = _compute_freshness_score(trading_days)

    power_score,     power_note     = get_power_score(pol_name)
    committee_overlap, committee_note = get_committee_overlap_score(pol_name, ticker)
    prior_buys, prior_sells          = db.get_prior_buy_sell_counts(pol_name, ticker)
    contractor_pts, contractor_note  = get_contractor_score(
        trade.get("company_name", ""), ticker,
        cache_get=db.get_contractor_cache,
        cache_set=db.set_contractor_cache,
    )
    history  = db.get_politician_trade_history(pol_name)
    rel_size = _compute_relative_size_score(trade, history)

    power_pts     = min(28, power_score)
    committee_pts = min(15, committee_overlap * 5)

    structured_score, score_breakdown = _compute_structured_score(
        trade, basket_score, committee_overlap, power_score,
        prior_buys, prior_sells, freshness_pts, contractor_pts,
    )
    signal_strength = _score_to_strength(
        structured_score, basket_score, power_pts, committee_pts, freshness_pts
    )

    trade.update({
        "_basket_score":      basket_score,
        "_rel_size_pct":      round(rel_size * 100, 1),
        "_committee_overlap": committee_overlap,
        "_committee_note":    committee_note,
        "_power_score":       power_score,
        "_power_note":        power_note,
        "_prior_buys":        prior_buys,
        "_prior_sells":       prior_sells,
        "_freshness_pts":     freshness_pts,
        "_contractor_pts":    contractor_pts,
        "_contractor_note":   contractor_note,
        "_structured_score":  structured_score,
        "_score_breakdown":   score_breakdown,
        "signal_strength":    signal_strength,
    })

    # Fresh entry assessment — fetches today's price from yfinance
    blocked, block_reason, entry_meta = _compute_disclosure_entry(trade, config)
    trade.update(entry_meta)

    current_price = entry_meta.get("_current_price")
    move_pct      = entry_meta.get("_move_pct_since_disclosure")
    entry_quality = entry_meta.get("_entry_quality", "")

    logger.info(
        "%s  score=%d  signal=%s  entry=%s  now=$%.2f  move=%+.1f%%",
        ticker, structured_score, signal_strength, entry_quality,
        current_price or 0, move_pct or 0,
    )

    if blocked:
        logger.info("  Skipping — entry blocked: %s", block_reason)
        continue

    if signal_strength not in ("strong", "moderate"):
        logger.info("  Score dropped below moderate on rescore — skipping.")
        continue

    stats = db.get_politician_stats(pol_name)
    trade = score_trade(trade, stats, config)
    send_trade_alert(trade, stats, config)
    sent += 1
    logger.info("  Alert sent.")

conn.close()
logger.info("Done — %d alert(s) sent.", sent)
