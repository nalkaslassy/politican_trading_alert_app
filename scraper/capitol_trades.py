"""Scrapes https://www.capitoltrades.com/trades using a headless browser.

Strategy: launch Selenium headless (Chrome on Docker/Linux, Edge on Windows),
navigate through multiple pages of the trades table, and parse the DOM.  The
browser bypasses the CloudFront WAF that blocks direct Python requests to the
BFF API.

Pagination: capitoltrades.com uses ?page=N (1-indexed).  We scrape up to
`scrape_pages` pages in a single browser session, stopping early when a page
returns no rows (end of data reached).
"""

import json
import logging
import re
import time
from datetime import date

from selenium import webdriver
from selenium.webdriver.edge.options import Options as EdgeOptions
from selenium.webdriver.edge.service import Service as EdgeService
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
from webdriver_manager.microsoft import EdgeChromiumDriverManager

logger = logging.getLogger(__name__)

_TRADES_URL = "https://www.capitoltrades.com/trades"
_PAGE_LOAD_WAIT = 20      # seconds to wait for the table to appear on page 1
_PAGE_NAV_WAIT  = 10      # seconds for subsequent pages (JS bundle already cached)
_NETWORK_SETTLE = 3       # extra settle time after table appears


def _build_trade_id(politician_name: str, ticker: str, trade_date: str) -> str:
    parts = [
        (politician_name or "unknown").strip().lower().replace(" ", "_"),
        (ticker or "unknown").strip().upper(),
        (trade_date or "nodate").strip(),
    ]
    return "-".join(parts)


def _parse_ticker(raw: str | None) -> str | None:
    """Return a 1–5 char uppercase ticker; strip exchange suffix (e.g. 'AAPL:US')."""
    if not raw:
        return None
    candidate = raw.split(":")[0].strip().upper()
    if re.match(r"^[A-Z]{1,5}$", candidate):
        return candidate
    return None


def _normalise_trade_type(raw: str) -> str:
    low = (raw or "").lower()
    if "buy" in low or "purchase" in low:
        return "Buy"
    return "Sell"


def _make_driver() -> webdriver.Remote:
    """Build a headless WebDriver — tries Chrome first (Linux/Docker), then Edge (Windows)."""
    common_args = [
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--window-size=1280,800",
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    ]

    try:
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.chrome.service import Service as ChromeService
        from webdriver_manager.chrome import ChromeDriverManager

        opts = ChromeOptions()
        for arg in common_args:
            opts.add_argument(arg)
        service = ChromeService(ChromeDriverManager().install())
        driver = webdriver.Chrome(service=service, options=opts)
        logger.info("Using Chrome WebDriver")
        return driver
    except Exception as chrome_exc:
        logger.debug("Chrome unavailable (%s); trying Edge", chrome_exc)

    opts = EdgeOptions()
    for arg in common_args:
        opts.add_argument(arg)
    opts.set_capability("ms:loggingPrefs", {"performance": "ALL"})
    service = EdgeService(EdgeChromiumDriverManager().install())
    driver = webdriver.Edge(service=service, options=opts)
    logger.info("Using Edge WebDriver")
    return driver


def _parse_politician_cell(text: str) -> tuple[str, str | None, str | None]:
    """Extract (name, party, chamber) from a combined politician cell.

    Typical format: "Thomas Kean Jr\\nRepublicanHouseNJ"
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    name = lines[0] if lines else "Unknown"
    party = None
    chamber = None
    if len(lines) > 1:
        rest = lines[1]
        if "republican" in rest.lower():
            party = "R"
        elif "democrat" in rest.lower():
            party = "D"
        elif "independent" in rest.lower():
            party = "I"
        if "house" in rest.lower():
            chamber = "House"
        elif "senate" in rest.lower():
            chamber = "Senate"
    return name, party, chamber


def _scrape_page(driver: webdriver.Remote, page_num: int, first_page: bool) -> list[dict]:
    """Navigate to a single page of the trades table and return all trades found.

    Uses a longer wait for page 1 (cold browser) and a shorter wait for
    subsequent pages (JS bundle already cached in the session).
    """
    url = f"{_TRADES_URL}?page={page_num}" if page_num > 1 else _TRADES_URL
    logger.info("Scraping page %d → %s", page_num, url)
    driver.get(url)

    wait_secs = _PAGE_LOAD_WAIT if first_page else _PAGE_NAV_WAIT
    try:
        WebDriverWait(driver, wait_secs).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
        )
    except Exception:
        logger.warning("Trades table did not appear on page %d within %ds", page_num, wait_secs)
        return []

    time.sleep(_NETWORK_SETTLE)

    trades: list[dict] = []
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        logger.info("Page %d: found %d table rows", page_num, len(rows))

        for row in rows:
            cells = row.find_elements(By.TAG_NAME, "td")
            if len(cells) < 8:
                continue
            texts = [c.text.strip() for c in cells]

            politician_name, party, chamber = _parse_politician_cell(texts[0])

            # Issuer cell: "Apple Inc\nAAPL:US"
            issuer_parts = texts[1].split("\n")
            ticker_raw = issuer_parts[-1].strip() if len(issuer_parts) > 1 else issuer_parts[0].strip()
            ticker = _parse_ticker(ticker_raw)

            trade_type   = _normalise_trade_type(texts[6])
            trade_size   = texts[7]
            filing_date  = texts[2].replace("\n", " ")   # "PUBLISHED" column
            trade_date   = texts[3].replace("\n", " ")   # "TRADED" column

            source_url = _TRADES_URL
            try:
                link = row.find_element(By.TAG_NAME, "a")
                href = link.get_attribute("href")
                if href:
                    source_url = href
            except Exception:
                pass

            trade_id = _build_trade_id(politician_name, ticker or ticker_raw or "UNK", trade_date)
            trades.append(
                {
                    "trade_id":       trade_id,
                    "politician_name": politician_name,
                    "party":          party,
                    "chamber":        chamber,
                    "ticker":         ticker,
                    "trade_type":     trade_type,
                    "trade_size":     trade_size,
                    "trade_date":     trade_date,
                    "filing_date":    filing_date,
                    "source_url":     source_url,
                }
            )
    except Exception as exc:
        logger.warning("DOM parse failed on page %d: %s", page_num, exc)

    return trades


def _parse_simple_date(date_str: str) -> date | None:
    """Parse a trade_date string as returned by the scraper."""
    from datetime import datetime
    for fmt in ("%d %b %Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str.strip().replace("\n", " "), fmt).date()
        except ValueError:
            continue
    return None


def fetch_trades(config: dict | None = None) -> list[dict]:
    """Fetch congressional trades from capitoltrades.com across multiple pages.

    Launches one headless browser session and paginates until one of:
      1. A page returns no rows (end of available data).
      2. Every trade on the current page is older than max_trade_age_days —
         we've gone far enough back; older pages won't produce alerts.
      3. The hard safety cap of max_pages_hard (default 50) is reached to
         prevent runaway scraping if the site misbehaves.

    `scrape_pages` in config is now a soft advisory (used for logging); the
    age-cutoff is the primary stopping condition.
    """
    cfg              = config or {}
    max_age_days     = int(cfg.get("max_trade_age_days", 90))
    max_pages_hard   = int(cfg.get("scrape_pages_hard_cap", 50))
    cutoff           = date.today().__class__.fromordinal(
        date.today().toordinal() - max_age_days
    )

    driver: webdriver.Remote | None = None
    all_trades: list[dict] = []
    seen_ids: set[str] = set()

    try:
        logger.info(
            "Launching headless browser — scraping until trades older than %d days (%s), hard cap %d pages",
            max_age_days, cutoff, max_pages_hard,
        )
        driver = _make_driver()

        for page_num in range(1, max_pages_hard + 1):
            page_trades = _scrape_page(driver, page_num, first_page=(page_num == 1))

            if not page_trades:
                logger.info("Page %d returned no rows — end of data", page_num)
                break

            new_count = 0
            oldest_on_page: date | None = None

            for trade in page_trades:
                tid = trade["trade_id"]
                if tid not in seen_ids:
                    seen_ids.add(tid)
                    all_trades.append(trade)
                    new_count += 1

                td = _parse_simple_date(trade.get("trade_date", ""))
                if td and (oldest_on_page is None or td < oldest_on_page):
                    oldest_on_page = td

            logger.info(
                "Page %d: %d rows, %d new | oldest trade: %s (running total: %d)",
                page_num, len(page_trades), new_count, oldest_on_page, len(all_trades),
            )

            # Stop once all trades on this page predate our alert window
            if oldest_on_page and oldest_on_page < cutoff:
                logger.info(
                    "Oldest trade on page %d (%s) is before cutoff %s — stopping pagination",
                    page_num, oldest_on_page, cutoff,
                )
                break

    except Exception as exc:
        logger.warning("fetch_trades encountered an unrecoverable error: %s", exc)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    logger.info("Scrape complete — %d total trades across all pages", len(all_trades))
    return all_trades
