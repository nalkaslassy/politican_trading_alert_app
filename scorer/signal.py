"""Uses Claude Haiku to score and summarise each congressional trade signal."""

import json
import logging

import anthropic

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"

# Prompt cache prefix: static system prompt is sent first so Anthropic can cache it
# across repeated calls within the same session, saving input tokens.
_SYSTEM_PROMPT = (
    "You are a financial signal analyst. Analyze congressional stock trades and return "
    "a JSON object only. No preamble, no markdown, just raw JSON."
)

_DEFAULT_SCORE = {
    "signal_strength": "unknown",
    "sector": "unknown",
    "reasoning": "Score unavailable.",
    "watch_out": None,
}


_RELEVANT_TRADE_FIELDS = (
    "politician_name", "party", "chamber", "ticker", "trade_type",
    "trade_size", "trade_date", "_entry_quality", "_move_pct_since_trade",
    "_price_at_trade", "_current_price",
)


def _build_user_prompt(trade: dict, stats: dict | None) -> str:
    """Build a compact prompt — only send fields the model actually needs."""
    # Strip internal screener metadata and anything not relevant to scoring
    clean_trade = {k: v for k, v in trade.items() if k in _RELEVANT_TRADE_FIELDS}

    lines = [
        f"Congressional trade: {json.dumps(clean_trade, default=str)}",
    ]

    if stats and stats.get("total_buys", 0) >= 3:
        win_pct = stats.get("win_rate_30d", 0.0) * 100
        avg_ret = stats.get("avg_return_30d", 0.0)
        lines.append(
            f"Politician stats: {stats['total_buys']} tracked buys, "
            f"{win_pct:.0f}% 30d win rate, {avg_ret:+.1f}% avg return."
        )
        if win_pct >= 70:
            lines.append("Strong historical track record.")

    lines += [
        'Return ONLY: {"signal_strength":"strong"|"moderate"|"weak",'
        '"sector":"<sector>","reasoning":"<one sentence>","watch_out":"<one sentence or null>"}',
    ]
    return "\n".join(lines)


def score_trade(trade: dict, stats: dict | None, config: dict) -> dict:
    """Call Claude Haiku to produce a signal score for a trade.

    Attaches score fields to a copy of the trade dict and returns it.
    On any failure returns the trade dict with default score fields.
    """
    trade = dict(trade)  # avoid mutating caller's dict

    try:
        client = anthropic.Anthropic(api_key=config["anthropic_api_key"])
        message = client.messages.create(
            model=_MODEL,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _build_user_prompt(trade, stats),
                }
            ],
        )
        raw_text = message.content[0].text.strip()

        # Strip accidental markdown fences
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]

        scored = json.loads(raw_text)
        trade.update(
            {
                "signal_strength": scored.get("signal_strength", "unknown"),
                "sector": scored.get("sector", "unknown"),
                "reasoning": scored.get("reasoning", ""),
                "watch_out": scored.get("watch_out"),
            }
        )
        logger.info(
            "Scored trade %s → %s", trade.get("trade_id"), trade["signal_strength"]
        )

    except json.JSONDecodeError as exc:
        logger.warning("Sonnet returned non-JSON for trade %s: %s", trade.get("trade_id"), exc)
        trade.update(_DEFAULT_SCORE)
    except Exception as exc:
        logger.warning("Scoring failed for trade %s: %s", trade.get("trade_id"), exc)
        trade.update(_DEFAULT_SCORE)

    return trade
