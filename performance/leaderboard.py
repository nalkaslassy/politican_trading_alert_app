"""Generates and posts the weekly politician performance leaderboard to Telegram."""

import asyncio
import logging
from datetime import date

from telegram import Bot
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


def get_leaderboard_message(db, top_n: int = 10) -> str:
    """Build a Telegram HTML leaderboard string from the top-performing politicians.

    Only includes politicians with at least 5 tracked trades.
    """
    rows = db.get_leaderboard(min_trades=5)
    if not rows:
        return (
            "🏆 <b>Capitol Radar Leaderboard</b>\n\n"
            "<i>Not enough data yet. Check back after more trades are tracked.</i>"
        )

    lines = [
        "🏆 <b>Capitol Radar Leaderboard</b>",
        "<i>Politicians ranked by 30-day win rate (min. 5 tracked trades)</i>",
        "",
    ]

    for rank, row in enumerate(rows[:top_n], start=1):
        name = row.get("politician_name", "Unknown")
        party = row.get("party") or "?"
        chamber = row.get("chamber") or "?"
        win_rate = row.get("win_rate_30d", 0.0) * 100
        avg_return = row.get("avg_return_30d", 0.0)
        total_buys = row.get("total_buys", 0)

        return_sign = "+" if avg_return >= 0 else ""
        lines.append(
            f"{rank}. <b>{name}</b> ({party} · {chamber})\n"
            f"   Win Rate: {win_rate:.0f}% · "
            f"Avg Return: {return_sign}{avg_return:.1f}% · "
            f"Trades: {total_buys}"
        )

    lines += ["", f"<i>Updated: {date.today().isoformat()}</i>"]
    return "\n".join(lines)


async def _send_message_async(token: str, chat_id: str, text: str) -> None:
    """Async helper: send a Telegram message and close the bot connection."""
    async with Bot(token=token) as bot:
        await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)


def post_leaderboard(db, config: dict, top_n: int = 10) -> None:
    """Build and send the leaderboard message to the configured Telegram target."""
    message = get_leaderboard_message(db, top_n=top_n)
    token = config.get("telegram_bot_token", "")
    chat_id = config.get("telegram_chat_id", "")

    if not token or not chat_id:
        logger.warning("Telegram credentials missing; cannot post leaderboard")
        return

    try:
        asyncio.run(_send_message_async(token, chat_id, message))
        logger.info("Leaderboard posted to Telegram chat %s", chat_id)
    except Exception as exc:
        logger.error("Failed to post leaderboard: %s", exc)
