"""Explanation-only Claude scoring for Capitol Radar.

Architecture (research-informed):
  - The structured score computed in filters/screener.py determines signal_strength.
    Claude does NOT decide signal quality — it writes the human-readable narrative.
  - Claude receives the pre-computed score, key trade features, and politician stats,
    and returns ONLY: reasoning (why this trade matters), watch_out (key risk),
    and sector (GICS classification for the alert).
  - This eliminates LLM hallucination of signal quality and makes the scoring
    reproducible and auditable from tabular features alone.
"""

import json
import logging

import anthropic

logger = logging.getLogger(__name__)

# Haiku is sufficient for structured JSON generation (1 sentence + sector label).
# ~10x cheaper than Sonnet with no quality loss for this task.
_MODEL = "claude-haiku-4-5-20251001"

# Module-level singleton — avoids re-initialising the HTTP client on every alert.
_client: anthropic.Anthropic | None = None


def _get_client(api_key: str) -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


_SYSTEM_PROMPT = (
    "You are a financial signal analyst for Capitol Radar, a congressional trade "
    "monitoring service. A structured algorithmic score has already determined the "
    "signal strength of a trade. Your job is to write the human-readable explanation "
    "only. Return a JSON object with exactly these keys: "
    "\"sector\" (GICS sector string), "
    "\"reasoning\" (one sentence: why this trade is noteworthy, referencing the "
    "specific politician, company, and signal drivers), "
    "\"watch_out\" (one sentence: the main risk or caveat, or null if none). "
    "No preamble, no markdown, just raw JSON."
)

# Fields sent to Claude for narrative generation — enough context without the noise
_NARRATIVE_FIELDS = (
    "politician_name", "party", "chamber", "ticker", "company_name",
    "trade_type", "owner_type", "trade_size", "trade_date", "filing_date",
    "_structured_score", "_score_breakdown", "_committee_note",
    "_entry_quality", "_basket_score", "_rel_size_pct",
    "_move_pct_since_disclosure", "_days_since_disclosure",
)

_DEFAULT_NARRATIVE = {
    "sector":    "Unknown",
    "reasoning": "Analysis unavailable.",
    "watch_out": None,
}


def _build_narrative_prompt(trade: dict, stats: dict | None) -> str:
    clean = {k: v for k, v in trade.items() if k in _NARRATIVE_FIELDS and v is not None}

    signal_strength = trade.get("signal_strength", "unknown")
    score           = trade.get("_structured_score", 0)
    breakdown       = trade.get("_score_breakdown", "")

    lines = [
        f"Signal strength (pre-determined by algorithm): {signal_strength.upper()} (score {score}/100)",
        f"Score breakdown: {breakdown}",
        f"Trade data: {json.dumps(clean, default=str)}",
    ]

    if stats and stats.get("total_buys", 0) >= 3:
        win_pct = stats.get("win_rate_30d", 0.0) * 100
        avg_ret = stats.get("avg_return_30d", 0.0)
        lines.append(
            f"Politician track record: {stats['total_buys']} tracked trades, "
            f"{win_pct:.0f}% 30-day win rate, {avg_ret:+.1f}% avg return."
        )

    lines.append(
        "Write a brief explanation of WHY this trade is interesting given the signal drivers above. "
        'Return ONLY JSON: {"sector":"...","reasoning":"...","watch_out":"..." or null}'
    )
    return "\n".join(lines)


def score_trade(trade: dict, stats: dict | None, config: dict) -> dict:
    """Add Claude-generated narrative to a pre-scored trade dict.

    Signal strength is already set from the structured score in filters/screener.py.
    This function only adds: sector, reasoning, watch_out.
    Falls back to defaults on any API error.
    """
    trade = dict(trade)

    try:
        client = _get_client(config["anthropic_api_key"])
        message = client.messages.create(
            model=_MODEL,
            max_tokens=150,
            system=_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": _build_narrative_prompt(trade, stats)}
            ],
        )
        raw_text = message.content[0].text.strip()

        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]

        narrative = json.loads(raw_text)
        trade.update(
            {
                "sector":    narrative.get("sector", "Unknown"),
                "reasoning": narrative.get("reasoning", ""),
                "watch_out": narrative.get("watch_out"),
            }
        )
        logger.info(
            "Narrative generated for %s [%s · score=%d]",
            trade.get("ticker"), trade.get("signal_strength"), trade.get("_structured_score", 0),
        )

    except json.JSONDecodeError as exc:
        logger.warning("Claude returned non-JSON for %s: %s", trade.get("trade_id"), exc)
        trade.update(_DEFAULT_NARRATIVE)
    except Exception as exc:
        logger.warning("Narrative generation failed for %s: %s", trade.get("trade_id"), exc)
        trade.update(_DEFAULT_NARRATIVE)

    return trade
