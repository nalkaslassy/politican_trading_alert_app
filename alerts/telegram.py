"""Sends Capitol Radar trade alerts via the Telegram Bot API."""

# PHASE 3: Add send_sms_alert() here using Plivo for paid SMS subscribers

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


def _format_alert_message(trade: dict, stats: dict | None) -> str:
    """Compose the Telegram HTML alert string for a single trade."""
    ticker       = trade.get("ticker", "N/A")
    company_name = trade.get("company_name", "")
    trade_type   = trade.get("trade_type", "N/A")
    politician   = trade.get("politician_name", "Unknown")
    party = trade.get("party") or "?"
    chamber = trade.get("chamber") or "?"
    trade_size = trade.get("trade_size", "N/A")
    trade_date = trade.get("trade_date", "N/A")
    signal = (trade.get("signal_strength") or "unknown").upper()
    reasoning = trade.get("reasoning") or "No analysis available."
    watch_out = trade.get("watch_out") or "None"
    filing_date = trade.get("filing_date", "N/A")
    source_url = trade.get("source_url", "")

    # Entry-point context from the screener
    entry_quality = trade.get("_entry_quality", "")
    entry_note    = trade.get("_entry_note", "")
    entry_emoji   = _ENTRY_EMOJI.get(entry_quality, "📍")
    price_at_trade   = trade.get("_price_at_trade")
    current_price    = trade.get("_current_price")
    move_pct         = trade.get("_move_pct_since_trade")

    lines = [
        "🏛️ <b>Capitol Radar Signal</b>",
        "",
        f"📈 <b>{ticker}</b>{f' ({company_name})' if company_name else ''} — {trade_type}",
        f"👤 <b>{politician}</b> ({party} · {chamber})",
        f"💰 Size: {trade_size}  |  🗓 Traded: {trade_date}",
        f"📊 Signal: <b>{signal}</b>",
        f"🔍 {reasoning}",
        f"⚠️ Risk: {watch_out}",
    ]

    # Entry point block — only shown if we have price data
    if price_at_trade and current_price and move_pct is not None:
        move_str = f"{move_pct:+.1f}%"
        lines += [
            "",
            f"{entry_emoji} <b>Entry Point</b>",
            f"  Politician paid: ${price_at_trade:.2f}",
            f"  Current price:   ${current_price:.2f}  ({move_str} since trade)",
        ]
        if entry_note:
            lines.append(f"  {entry_note}")

    if stats and stats.get("total_buys", 0) >= 5:
        win_rate_pct = stats.get("win_rate_30d", 0.0) * 100
        avg_return   = stats.get("avg_return_30d", 0.0)
        total_buys   = stats.get("total_buys", 0)
        return_sign  = "+" if avg_return >= 0 else ""
        lines.append(
            f"\n⭐ Track Record: {win_rate_pct:.0f}% win rate · "
            f"{return_sign}{avg_return:.1f}% avg return · "
            f"{total_buys} trades tracked"
        )

    lines += [
        "",
        f"📅 Filed: {filing_date}",
    ]

    if source_url:
        lines.append(f'🔗 <a href="{source_url}">View Filing</a>')

    return "\n".join(lines)


async def _send_async(token: str, chat_id: str, text: str) -> None:
    """Async helper that sends a single Telegram message."""
    async with Bot(token=token) as bot:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)


def send_trade_alert(trade: dict, stats: dict | None, config: dict) -> None:
    """Format and dispatch a trade alert to Telegram.

    Works in both "direct" (personal chat) and "channel" broadcast modes.
    On any send failure, logs the error and returns without raising.
    """
    token = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")

    if not token or not chat_id:
        logger.warning(
            "Telegram credentials not configured; skipping alert for %s",
            trade.get("trade_id"),
        )
        return

    message = _format_alert_message(trade, stats)

    try:
        asyncio.run(_send_async(token, chat_id, message))
        logger.info(
            "Alert sent for trade %s (%s · %s)",
            trade.get("trade_id"),
            trade.get("ticker"),
            config.get("telegram_mode", "direct"),
        )
    except Exception as exc:
        logger.error(
            "Failed to send Telegram alert for trade %s: %s",
            trade.get("trade_id"),
            exc,
        )
