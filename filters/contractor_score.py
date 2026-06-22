"""Federal contractor exposure scoring via the USAspending.gov public API.

Rationale (NBER 2025 leadership paper): firms purchased by congressional leaders
subsequently receive significantly more federal contract awards, particularly
noncompetitive awards. Contractor status is therefore a proxy for issuer-level
political relevance — a stronger signal than broad GICS sector overlap.

Score 0–12:
   3 pts  = company has any federal contracts in past 12 months
  +1–5 pts = contract volume tier ($10M / $100M / $1B+)
  +3 pts  = DOD / Intelligence / DHS contract exposure
  Cap 12

Results are cached per ticker for 7 days (set in config as usaspending_cache_ttl_days).
No API key required — USAspending is a public US government data portal.
"""

import logging
import re
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

_BASE    = "https://api.usaspending.gov/api/v2"
_TIMEOUT = 12

_DOD_KEYWORDS = frozenset([
    "department of defense", "army", "navy", "air force", "marine corps",
    "defense logistics agency", "defense intelligence agency",
    "national security agency", "space force", "missile defense agency",
    "defense advanced research", "defense health agency",
    "department of homeland security", "office of the director of national intelligence",
])

# Legal-entity suffixes to strip before searching
_SUFFIX_RE = re.compile(
    r'\s*\b(Inc\.?|Corp\.?|Corporation|Ltd\.?|LLC|Co\.?|PLC|Group|Holdings?'
    r'|Technologies?|Solutions?|Systems?|Enterprises?|International|Global)\b',
    flags=re.IGNORECASE,
)


def _clean_name(raw: str) -> str:
    return _SUFFIX_RE.sub("", raw).strip().rstrip(",").strip()


def _recent_contract_total(keyword: str) -> tuple[float, bool]:
    """Return (trailing_12m_award_total, has_dod_awards) for a company keyword.

    Uses the spending_by_award search endpoint with recipient_search_text.
    Sums the top-25 awards as a representative sample of the 12-month total.
    """
    try:
        import requests
    except ImportError:
        logger.debug("requests not installed — skipping USAspending lookup")
        return 0.0, False

    one_year_ago = (date.today() - timedelta(days=365)).isoformat()
    today        = date.today().isoformat()

    try:
        r = requests.post(
            f"{_BASE}/search/spending_by_award/",
            json={
                "filters": {
                    "recipient_search_text": [keyword],
                    "award_type_codes": ["A", "B", "C", "D"],
                    "time_period": [{"start_date": one_year_ago, "end_date": today}],
                },
                "fields": ["Award Amount", "Awarding Agency Name"],
                "sort": "Award Amount",
                "order": "desc",
                "limit": 25,
                "page": 1,
            },
            timeout=_TIMEOUT,
        )
        if r.status_code != 200:
            logger.debug("USAspending returned %d for %s", r.status_code, keyword)
            return 0.0, False

        results = r.json().get("results", [])
        total   = sum((a.get("Award Amount") or 0) for a in results)
        has_dod = any(
            any(kw in (a.get("Awarding Agency Name") or "").lower() for kw in _DOD_KEYWORDS)
            for a in results
        )
        return float(total), has_dod

    except Exception as exc:
        logger.debug("USAspending request failed for %s: %s", keyword, exc)
        return 0.0, False


def _score(total_awards: float, has_dod: bool) -> tuple[int, str]:
    if total_awards <= 0:
        return 0, "no federal contracts"

    parts: list[str] = ["federal contractor"]
    pts = 3  # base: any contracts found

    if total_awards >= 1_000_000_000:
        pts += 5
        parts.append(f"${total_awards / 1e9:.1f}B in 12m")
    elif total_awards >= 100_000_000:
        pts += 3
        parts.append(f"${total_awards / 1e6:.0f}M in 12m")
    elif total_awards >= 10_000_000:
        pts += 1
        parts.append(f"${total_awards / 1e6:.0f}M in 12m")

    if has_dod:
        pts += 3
        parts.append("DOD/intelligence exposure")

    return min(12, pts), "; ".join(parts)


def get_contractor_score(
    company_name: str,
    ticker: str,
    cache_get=None,
    cache_set=None,
) -> tuple[int, str]:
    """Return (contractor_pts 0-12, note) for a company using USAspending data.

    Args:
        cache_get: callable(ticker) -> Optional[tuple[int, str]]
        cache_set: callable(ticker, company_name, pts, note) -> None
    """
    if not company_name:
        return 0, ""

    if cache_get:
        hit = cache_get(ticker)
        if hit is not None:
            return hit

    keyword = _clean_name(company_name)
    if not keyword:
        return 0, ""

    total_awards, has_dod = _recent_contract_total(keyword)
    pts, note = _score(total_awards, has_dod)

    if cache_set:
        cache_set(ticker, company_name, pts, note)

    logger.info(
        "Contractor %s (%s): %d pts — %s", ticker, company_name[:30], pts, note
    )
    return pts, note
