"""Scrapes https://www.capitoltrades.com/trades for the latest congressional stock filings."""

import logging
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.capitoltrades.com"
_TRADES_URL = f"{_BASE_URL}/trades"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def _build_trade_id(politician_name: str, ticker: str, trade_date: str) -> str:
    """Construct a unique, deterministic trade identifier."""
    parts = [
        politician_name.strip().lower().replace(" ", "_"),
        (ticker or "unknown").strip().upper(),
        (trade_date or "nodate").strip(),
    ]
    return "-".join(parts)


def _parse_ticker(raw: str) -> str | None:
    """Extract a 1–5 char uppercase ticker from raw text; return None if invalid."""
    if not raw:
        return None
    candidate = raw.strip().upper()
    if re.match(r"^[A-Z]{1,5}$", candidate):
        return candidate
    return None


def _extract_trades_from_table(table, source_url: str) -> list[dict]:
    """Parse an HTML <table> element and return a list of trade dicts."""
    trades = []
    thead = table.find("thead")
    if not thead:
        logger.warning("No <thead> found in trades table; skipping")
        return trades

    header_cells = thead.find_all(["th", "td"])
    headers = [c.get_text(strip=True).lower() for c in header_cells]
    logger.debug("Table headers: %s", headers)

    tbody = table.find("tbody")
    if not tbody:
        logger.warning("No <tbody> found in trades table")
        return trades

    for row in tbody.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue

        def cell_text(idx: int) -> str:
            """Safely return stripped text for the cell at position idx."""
            if idx < len(cells):
                return cells[idx].get_text(separator=" ", strip=True)
            return ""

        # Attempt flexible column mapping by header names first, then fall
        # back to positional guesses matching the typical capitoltrades layout:
        # [traded_date, filed_date, politician, party/chamber, ticker, asset, type, size]
        def find_col(*names: str) -> int:
            for name in names:
                for i, h in enumerate(headers):
                    if name in h:
                        return i
            return -1

        traded_col = find_col("traded", "trade date", "date")
        filed_col = find_col("filed", "filing", "disclosed")
        politician_col = find_col("politician", "representative", "senator", "name")
        ticker_col = find_col("ticker", "symbol")
        type_col = find_col("type", "transaction", "trade type")
        size_col = find_col("size", "amount", "range", "value")

        # Positional fallbacks when headers are ambiguous
        if traded_col == -1:
            traded_col = 0
        if filed_col == -1:
            filed_col = 1
        if politician_col == -1:
            politician_col = 2
        if ticker_col == -1:
            ticker_col = 4
        if type_col == -1:
            type_col = 6
        if size_col == -1:
            size_col = 7

        politician_raw = cell_text(politician_col)
        ticker_raw = cell_text(ticker_col)
        type_raw = cell_text(type_col)
        size_raw = cell_text(size_col)
        traded_raw = cell_text(traded_col)
        filed_raw = cell_col = cell_text(filed_col)

        # Normalise trade type
        type_norm = "Buy" if "buy" in type_raw.lower() or "purchase" in type_raw.lower() else "Sell"
        if "sell" in type_raw.lower() or "sale" in type_raw.lower():
            type_norm = "Sell"

        # Parse politician name and party/chamber from combined cells
        party = None
        chamber = None
        politician_name = politician_raw

        # Many sites embed party in a sub-element; try to extract it
        politician_cell = cells[politician_col] if politician_col < len(cells) else None
        if politician_cell:
            party_tag = politician_cell.find(class_=re.compile(r"party|badge", re.I))
            if party_tag:
                party_text = party_tag.get_text(strip=True).upper()
                if party_text in ("D", "R", "I", "L"):
                    party = party_text
                politician_name = politician_raw.replace(party_tag.get_text(strip=True), "").strip()

        # Chamber heuristics from table or data attributes
        chamber_tag = row.find(attrs={"data-chamber": True})
        if chamber_tag:
            chamber = chamber_tag["data-chamber"].capitalize()
        else:
            chamber_cell_text = cell_text(find_col("chamber", "house", "senate") if find_col("chamber", "house", "senate") >= 0 else -1)
            if "house" in chamber_cell_text.lower():
                chamber = "House"
            elif "senate" in chamber_cell_text.lower():
                chamber = "Senate"

        ticker = _parse_ticker(ticker_raw)
        trade_date = traded_raw.strip()
        filing_date = filed_raw.strip()

        # Build the filing URL if there is a link in the row
        link_tag = row.find("a", href=True)
        filing_link = ""
        if link_tag:
            href = link_tag["href"]
            filing_link = href if href.startswith("http") else f"{_BASE_URL}{href}"
        if not filing_link:
            filing_link = source_url

        trade_id = _build_trade_id(politician_name, ticker or ticker_raw, trade_date)

        trades.append(
            {
                "trade_id": trade_id,
                "politician_name": politician_name,
                "party": party,
                "chamber": chamber,
                "ticker": ticker,
                "trade_type": type_norm,
                "trade_size": size_raw,
                "trade_date": trade_date,
                "filing_date": filing_date,
                "source_url": filing_link,
            }
        )

    return trades


def fetch_trades() -> list[dict]:
    """Fetch the latest congressional trades from capitoltrades.com.

    Returns a list of trade dicts.  On any HTTP or parse error, logs a
    warning and returns an empty list — never raises.
    """
    try:
        response = requests.get(_TRADES_URL, headers=_HEADERS, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("HTTP error fetching trades: %s", exc)
        return []

    try:
        soup = BeautifulSoup(response.text, "lxml")
    except Exception as exc:
        logger.warning("Failed to parse HTML: %s", exc)
        return []

    trades: list[dict] = []

    # Strategy 1: find a <table> element that looks like a trades table
    tables = soup.find_all("table")
    if tables:
        # Prefer the table with the most rows
        best_table = max(tables, key=lambda t: len(t.find_all("tr")), default=None)
        if best_table:
            try:
                trades = _extract_trades_from_table(best_table, _TRADES_URL)
                logger.info("Parsed %d trades from HTML table", len(trades))
            except Exception as exc:
                logger.warning("Failed to parse trades table: %s", exc)

    # Strategy 2: fall back to looking for trade cards / list items
    if not trades:
        logger.info("No table found; attempting card-based parsing")
        cards = soup.find_all(
            ["div", "li", "article"],
            class_=re.compile(r"trade|filing|disclosure", re.I),
        )
        logger.debug("Found %d candidate trade cards", len(cards))
        for card in cards:
            try:
                text = card.get_text(separator=" ", strip=True)
                ticker_match = re.search(r"\b([A-Z]{1,5})\b", text)
                ticker = ticker_match.group(1) if ticker_match else None

                date_match = re.search(r"\d{4}-\d{2}-\d{2}", text)
                trade_date = date_match.group(0) if date_match else str(date.today())

                link_tag = card.find("a", href=True)
                source = (
                    link_tag["href"]
                    if link_tag and link_tag["href"].startswith("http")
                    else f"{_BASE_URL}{link_tag['href']}" if link_tag else _TRADES_URL
                )

                type_norm = "Sell"
                if re.search(r"\bbuy\b|\bpurchase\b", text, re.I):
                    type_norm = "Buy"

                politician_name = "Unknown"
                name_match = re.search(r"by ([A-Z][a-z]+ [A-Z][a-z]+)", text)
                if name_match:
                    politician_name = name_match.group(1)

                trade_id = _build_trade_id(politician_name, ticker or "UNK", trade_date)
                trades.append(
                    {
                        "trade_id": trade_id,
                        "politician_name": politician_name,
                        "party": None,
                        "chamber": None,
                        "ticker": ticker,
                        "trade_type": type_norm,
                        "trade_size": "",
                        "trade_date": trade_date,
                        "filing_date": trade_date,
                        "source_url": source,
                    }
                )
            except Exception as exc:
                logger.debug("Skipped card due to parse error: %s", exc)

        if trades:
            logger.info("Card-based parser found %d trades", len(trades))
        else:
            logger.warning("Could not extract any trades from %s", _TRADES_URL)

    return trades
