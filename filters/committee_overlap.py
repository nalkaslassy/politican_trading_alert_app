"""Static committee-to-sector overlap scoring for Capitol Radar.

Maps known politicians to their committee assignments, then scores
how closely a traded stock's GICS sector aligns with those committees.
A politician buying a stock in a sector they oversee is a structural
signal — they likely have informational advantage.

Score 0-3:
  3 = direct oversight (e.g. Armed Services member buys defense stock)
  2 = adjacent oversight (e.g. Intelligence member buys cybersecurity)
  1 = broad relevance (e.g. any committee member buys in related space)
  0 = no committee overlap detected
"""

import logging

logger = logging.getLogger(__name__)

# Committee → GICS sectors that committee oversees
# GICS sector strings match what yfinance returns in ticker.info['sector']
_COMMITTEE_SECTORS: dict[str, list[str]] = {
    "Armed Services":            ["Industrials", "Information Technology", "Aerospace & Defense"],
    "Intelligence":              ["Information Technology", "Industrials", "Communication Services"],
    "Financial Services":        ["Financials", "Real Estate"],
    "Banking":                   ["Financials", "Real Estate"],
    "Energy and Commerce":       ["Energy", "Health Care", "Communication Services", "Information Technology"],
    "Ways and Means":            ["Health Care", "Financials", "Consumer Discretionary"],
    "Foreign Affairs":           ["Industrials", "Materials", "Energy"],
    "Agriculture":               ["Consumer Staples", "Materials"],
    "Science Space Technology":  ["Information Technology", "Energy", "Industrials"],
    "Homeland Security":         ["Information Technology", "Industrials"],
    "Judiciary":                 ["Information Technology", "Communication Services"],
    "Commerce":                  ["Consumer Discretionary", "Information Technology", "Industrials"],
    "Health Education Labor":    ["Health Care", "Consumer Staples"],
    "Appropriations":            ["Industrials", "Health Care", "Information Technology"],
    "Budget":                    ["Financials"],
    "Oversight":                 ["Information Technology", "Industrials", "Health Care"],
    "Transportation":            ["Industrials", "Energy", "Consumer Discretionary"],
}

# Politician → list of committee keys they sit on
# Sources: official House/Senate committee rosters (public record)
_POLITICIAN_COMMITTEES: dict[str, list[str]] = {
    # ── Current watchlist politicians ──────────────────────────────────
    "Nancy Pelosi":            ["Appropriations", "Oversight"],   # House leadership access
    "Michael McCaul":          ["Foreign Affairs", "Science Space Technology"],  # + Semiconductor Caucus
    "Ro Khanna":               ["Armed Services", "Oversight", "Science Space Technology"],
    "Dan Crenshaw":            ["Intelligence", "Homeland Security"],
    "Brian Mast":              ["Foreign Affairs", "Transportation"],
    "Josh Gottheimer":         ["Financial Services", "Homeland Security"],
    "Tommy Tuberville":        ["Armed Services", "Agriculture"],
    "Warren Davidson":         ["Financial Services"],
    "Rick Scott":              ["Banking", "Budget", "Commerce"],
    "Nick LaLota":             ["Financial Services", "Homeland Security"],
    "Marjorie Taylor Greene":  ["Budget", "Oversight"],
    "Tim Moore":               ["Judiciary"],
    "Donald Norcross":         ["Armed Services", "Transportation"],
    "Terri Sewell":            ["Ways and Means"],
    "David Rouzer":            ["Agriculture", "Transportation"],
    # ── Additional politicians likely to appear in "all" mode ──────────
    "John Boozman":            ["Agriculture", "Appropriations", "Banking"],
    "John Fetterman":          ["Agriculture", "Banking", "Judiciary"],
    "Gary Peters":             ["Armed Services", "Homeland Security", "Commerce"],
    "Steve Cohen":             ["Judiciary", "Transportation"],
    "Mike Kelly":              ["Ways and Means", "Oversight"],
    "Warren Davidson":         ["Financial Services"],
    "John McGuire":            ["Armed Services"],
    "David Taylor":            ["Financial Services", "Oversight"],
    "Nicholas Begich III":     ["Armed Services", "Transportation"],
    "Rick Allen":              ["Agriculture", "Budget"],
    "Thomas Kean Jr":          ["Science Space Technology", "Homeland Security"],
    "Debbie Wasserman Schultz":["Appropriations"],
    "Kevin Hern":              ["Ways and Means", "Budget"],
    "Virginia Foxx":           ["Education", "Oversight"],
    "Andy Barr":               ["Financial Services"],
    "French Hill":             ["Financial Services", "Intelligence"],
    "Bill Foster":             ["Financial Services", "Science Space Technology"],
    "Patrick McHenry":         ["Financial Services"],
    "Jared Moskowitz":         ["Appropriations", "Oversight"],
    "Matt Van Epps":           [],
    "Jonathan Jackson":        ["Oversight"],
    "Julie Johnson":           ["Judiciary", "Oversight"],
    "Chip Roy":                ["Budget", "Oversight"],
    "David Taylor":            ["Financial Services", "Oversight"],
}

# GICS sector aliases — yfinance returns inconsistent strings; normalise them
_SECTOR_ALIASES: dict[str, str] = {
    "technology":              "Information Technology",
    "tech":                    "Information Technology",
    "financial":               "Financials",
    "finance":                 "Financials",
    "healthcare":              "Health Care",
    "health care":             "Health Care",
    "consumer defensive":      "Consumer Staples",
    "consumer cyclical":       "Consumer Discretionary",
    "basic materials":         "Materials",
    "real estate":             "Real Estate",
    "utilities":               "Utilities",
    "energy":                  "Energy",
    "industrials":             "Industrials",
    "communication services":  "Communication Services",
}


def _normalise_sector(raw: str) -> str:
    return _SECTOR_ALIASES.get(raw.strip().lower(), raw.strip())


def get_committee_overlap_score(politician_name: str, ticker: str) -> tuple[int, str]:
    """Return (score 0-3, explanation) for a politician-ticker pair.

    Fetches the stock's GICS sector from yfinance (cached by caller if needed)
    and checks against the politician's known committee assignments.
    """
    committees = _POLITICIAN_COMMITTEES.get(politician_name)
    if not committees:
        return 0, "No committee data for this politician"

    sector = _fetch_sector(ticker)
    if not sector:
        return 0, f"Could not determine sector for {ticker}"

    sector_norm = _normalise_sector(sector)

    matching_committees = []
    for committee in committees:
        covered = _COMMITTEE_SECTORS.get(committee, [])
        if sector_norm in covered:
            matching_committees.append(committee)

    if len(matching_committees) >= 2:
        score = 3
        note  = f"Direct multi-committee overlap ({', '.join(matching_committees)}) with {sector_norm}"
    elif len(matching_committees) == 1:
        score = 2
        note  = f"Committee overlap: {matching_committees[0]} oversees {sector_norm}"
    else:
        # Check for partial/adjacent sector overlap
        adjacent = _check_adjacent(committees, sector_norm)
        if adjacent:
            score = 1
            note  = f"Adjacent oversight: {adjacent}"
        else:
            score = 0
            note  = f"No committee overlap (sector: {sector_norm})"

    return score, note


def _fetch_sector(ticker: str) -> str | None:
    """Fetch the GICS sector for a ticker from yfinance; return None on failure."""
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).info
        return info.get("sector") or info.get("industryKey") or None
    except Exception:
        return None


def _check_adjacent(committees: list[str], sector: str) -> str:
    """Check for loose sector adjacency when there's no direct committee match."""
    adjacencies = {
        "Information Technology": ["Commerce", "Oversight", "Judiciary"],
        "Financials":             ["Budget", "Oversight"],
        "Health Care":            ["Oversight", "Appropriations"],
        "Energy":                 ["Appropriations", "Science Space Technology"],
        "Industrials":            ["Appropriations", "Transportation"],
        "Communication Services": ["Commerce", "Judiciary"],
    }
    related = adjacencies.get(sector, [])
    for c in committees:
        if c in related:
            return f"{c} has adjacent oversight of {sector}"
    return ""
