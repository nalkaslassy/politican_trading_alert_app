"""Uses Claude Haiku to score and summarise each congressional trade signal."""

import json
import logging

import anthropic

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"

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


def _build_user_prompt(trade: dict, stats: dict | None) -> str:
    """Compose the prompt sent to Haiku including trade data and optional stats."""
    lines = [
        "Analyze this congressional stock trade and return the JSON exactly as specified.",
        "",
        f"Trade details: {json.dumps(trade, default=str)}",
    ]

    if stats:
        lines += [
            "",
            "Politician track record:",
            f"  - Win rate (30-day): {stats.get('win_rate_30d', 0.0):.1%}",
            f"  - Average return (30-day): {stats.get('avg_return_30d', 0.0):.2f}%",
            f"  - Total tracked buys: {stats.get('total_buys', 0)}",
        ]
        if stats.get("win_rate_30d", 0.0) >= 0.70:
            lines.append("  - NOTE: This politician has a strong historical track record (≥70% win rate).")

    lines += [
        "",
        "Return ONLY this JSON object with no other text:",
        '{',
        '  "signal_strength": "strong" | "moderate" | "weak",',
        '  "sector": "<sector name>",',
        '  "reasoning": "<one sentence referencing politician track record if stats available>",',
        '  "watch_out": "<one sentence risk or null>"',
        '}',
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
        logger.warning("Haiku returned non-JSON for trade %s: %s", trade.get("trade_id"), exc)
        trade.update(_DEFAULT_SCORE)
    except Exception as exc:
        logger.warning("Scoring failed for trade %s: %s", trade.get("trade_id"), exc)
        trade.update(_DEFAULT_SCORE)

    return trade
