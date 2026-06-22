"""Sends Capitol Radar trade alerts via the Telegram Bot API."""

import asyncio
import logging

from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)

_ENTRY_EMOJI = {
    "fresh":    "✅",
    "caution":  "⚡",
    "discount": "🟢",
    "blocked":  "🚫",
}

_SIGNAL_BADGE = {
    "strong":   "🔴 STRONG",
    "moderate": "🟡 MODERATE",
    "weak":     "⚪ WEAK",
}

_OWNER_LABEL = {
    "Spouse":    "👫 Spouse",
    "Self":      "🧑 Self",
    "Dependent": "👶 Dependent",
    "Unknown":   "👤 Unknown",
}


def _format_buy_alert(trade: dict, stats: dict | None) -> str:
    """Compose the HTML alert string for a Buy (entry) signal."""
    ticker       = trade.get("ticker", "N/A")
    company_name = trade.get("company_name", "")
    politician   = trade.get("politician_name", "Unknown")
    party        = trade.get("party") or "?"
    chamber      = trade.get("chamber") or "?"
    trade_size   = trade.get("trade_size", "N/A")
    trade_date   = trade.get("trade_date", "N/A")
    filing_date  = trade.get("filing_date", "N/A")
    owner_type   = trade.get("owner_type", "Unknown")
    signal       = _SIGNAL_BADGE.get(trade.get("signal_strength", "weak"), "⚪ WEAK")
    score        = trade.get("_structured_score", 0)
    reasoning    = trade.get("reasoning") or "No analysis available."
    watch_out    = trade.get("watch_out") or "None"
    source_url   = trade.get("source_url", "")

    # Structured score sub-features
    rel_size_pct     = trade.get("_rel_size_pct")
    committee_note   = trade.get("_committee_note", "")
    basket_score     = trade.get("_basket_score")

    # Entry point — disclosure-date clock
    entry_quality           = trade.get("_entry_quality", "")
    entry_note              = trade.get("_entry_note", "")
    entry_emoji             = _ENTRY_EMOJI.get(entry_quality, "📍")
    price_at_trade          = trade.get("_price_at_trade")
    price_at_disclosure     = trade.get("_price_at_disclosure")
    current_price           = trade.get("_current_price")
    move_pct_disc           = trade.get("_move_pct_since_disclosure")
    days_since_disclosure   = trade.get("_days_since_disclosure")
    atr_units               = trade.get("_atr_units_moved")

    owner_label = _OWNER_LABEL.get(owner_type, f"👤 {owner_type}")

    lines = [
        "🏛️ <b>Capitol Radar — BUY SIGNAL</b>",
        "",
        f"📈 <b>{ticker}</b>{f' ({company_name})' if company_name else ''}",
        f"👤 <b>{politician}</b> ({party} · {chamber})  {owner_label}",
        f"💰 Size: {trade_size}  |  🗓 Traded: {trade_date}",
        f"📊 Signal: <b>{signal}</b>  (score {score}/100)",
        f"🔍 {reasoning}",
    ]

    if watch_out and watch_out != "None":
        lines.append(f"⚠️ Risk: {watch_out}")

    # Structural signal sub-factors
    factors = []
    if rel_size_pct is not None:
        factors.append(f"Size: {rel_size_pct:.0f}th pct vs their history")
    if basket_score is not None:
        concentration = ["concentrated bet", "small cluster", "likely rebalancing", "broad basket"][min(basket_score, 3)]
        factors.append(f"Trades that day: {concentration}")
    if committee_note:
        factors.append(f"Committee: {committee_note}")
    if factors:
        lines += ["", "📐 <b>Score Drivers</b>"] + [f"  • {f}" for f in factors]

    # Entry point block — disclosure-date clock
    if price_at_disclosure and current_price:
        lines += ["", f"{entry_emoji} <b>Entry Point (disclosure-date clock)</b>"]
        if price_at_trade:
            lines.append(f"  Politician's cost: ${price_at_trade:.2f}")
        lines.append(f"  Price at disclosure: ${price_at_disclosure:.2f}")
        if move_pct_disc is not None:
            lines.append(f"  Current price: ${current_price:.2f}  ({move_pct_disc:+.1f}% since disclosure)")
        if days_since_disclosure is not None:
            lines.append(f"  Disclosed: {days_since_disclosure}d ago")
        if atr_units is not None:
            lines.append(f"  ATR units moved: {atr_units:.1f}x")
        if entry_note:
            lines.append(f"  {entry_note}")

    if stats and stats.get("total_buys", 0) >= 5:
        win_rate_pct = stats.get("win_rate_30d", 0.0) * 100
        avg_return   = stats.get("avg_return_30d", 0.0)
        total_buys   = stats.get("total_buys", 0)
        sign         = "+" if avg_return >= 0 else ""
        lines.append(
            f"\n⭐ Track Record: {win_rate_pct:.0f}% win rate · "
            f"{sign}{avg_return:.1f}% avg return · {total_buys} trades tracked"
        )

    lines += ["", f"📅 Disclosed: {filing_date}"]
    if source_url:
        lines.append(f'🔗 <a href="{source_url}">View Filing</a>')

    return "\n".join(lines)


def _format_sell_alert(trade: dict, stats: dict | None) -> str:
    """Compose the HTML alert string for a Sell (exit) signal.

    Molk & Partnoy: sell-side congressional trades carry reliable downside signal
    post-STOCK Act. We frame these as potential exit signals, not certainties.
    """
    ticker       = trade.get("ticker", "N/A")
    company_name = trade.get("company_name", "")
    politician   = trade.get("politician_name", "Unknown")
    party        = trade.get("party") or "?"
    chamber      = trade.get("chamber") or "?"
    trade_size   = trade.get("trade_size", "N/A")
    trade_date   = trade.get("trade_date", "N/A")
    filing_date  = trade.get("filing_date", "N/A")
    owner_type   = trade.get("owner_type", "Unknown")
    signal       = _SIGNAL_BADGE.get(trade.get("signal_strength", "weak"), "⚪ WEAK")
    score        = trade.get("_structured_score", 0)
    reasoning    = trade.get("reasoning") or "No analysis available."
    watch_out    = trade.get("watch_out") or "None"
    source_url   = trade.get("source_url", "")
    current_price = trade.get("_current_price")

    rel_size_pct   = trade.get("_rel_size_pct")
    committee_note = trade.get("_committee_note", "")
    basket_score   = trade.get("_basket_score")
    days_since_disclosure = trade.get("_days_since_disclosure")
    owner_label = _OWNER_LABEL.get(owner_type, f"👤 {owner_type}")

    lines = [
        "🏛️ <b>Capitol Radar — EXIT SIGNAL</b>",
        "",
        f"📉 <b>{ticker}</b>{f' ({company_name})' if company_name else ''} — SELL",
        f"👤 <b>{politician}</b> ({party} · {chamber})  {owner_label}",
        f"💰 Size: {trade_size}  |  🗓 Traded: {trade_date}",
        f"📊 Signal: <b>{signal}</b>  (score {score}/100)",
        "",
        f"⚠️ <b>Potential Downside Signal</b> — This politician is exiting their position.",
        f"🔍 {reasoning}",
    ]

    if watch_out and watch_out != "None":
        lines.append(f"⚠️ Note: {watch_out}")

    if current_price:
        lines.append(f"\n💵 Current price: ${current_price:.2f}")
    if days_since_disclosure is not None:
        lines.append(f"📅 Disclosed {days_since_disclosure}d ago")

    factors = []
    if rel_size_pct is not None:
        factors.append(f"Size: {rel_size_pct:.0f}th pct vs their history")
    if basket_score is not None:
        concentration = ["concentrated exit", "small cluster", "likely rebalancing", "broad basket"][min(basket_score, 3)]
        factors.append(f"Sells that day: {concentration}")
    if committee_note:
        factors.append(f"Committee: {committee_note}")
    if factors:
        lines += ["", "📐 <b>Score Drivers</b>"] + [f"  • {f}" for f in factors]

    if stats and stats.get("total_buys", 0) >= 5:
        win_rate_pct = stats.get("win_rate_30d", 0.0) * 100
        total_buys   = stats.get("total_buys", 0)
        lines.append(f"\n⭐ Buy track record: {win_rate_pct:.0f}% win rate · {total_buys} trades tracked")

    lines += ["", f"📅 Filed: {filing_date}"]
    if source_url:
        lines.append(f'🔗 <a href="{source_url}">View Filing</a>')

    return "\n".join(lines)


async def _send_async(token: str, chat_id: str, text: str) -> None:
    async with Bot(token=token) as bot:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)


def send_trade_alert(trade: dict, stats: dict | None, config: dict) -> None:
    """Format and dispatch a trade alert (buy or sell) to Telegram."""
    token   = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")

    if not token or not chat_id:
        logger.warning("Telegram credentials not configured; skipping alert for %s", trade.get("trade_id"))
        return

    trade_type = trade.get("trade_type", "Buy")
    if trade_type == "Sell":
        message = _format_sell_alert(trade, stats)
    else:
        message = _format_buy_alert(trade, stats)

    try:
        asyncio.run(_send_async(token, chat_id, message))
        logger.info(
            "Alert sent: %s %s · %s · signal=%s",
            trade_type.upper(), trade.get("ticker"),
            config.get("telegram_mode", "direct"),
            trade.get("signal_strength", "?"),
        )
    except Exception as exc:
        logger.error("Failed to send Telegram alert for %s: %s", trade.get("trade_id"), exc)
