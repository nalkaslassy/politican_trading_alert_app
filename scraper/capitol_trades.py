"""Scrapes https://www.capitoltrades.com/trades using a headless Microsoft Edge browser.

Strategy: launch Selenium with Edge headless, navigate to the trades page,
intercept the XHR/fetch request to bff.capitoltrades.com/trades via the
browser's performance log, and parse the JSON payload.  This bypasses the
CloudFront WAF that blocks direct Python requests.
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
_BFF_BASE = "https://bff.capitoltrades.com"
_PAGE_LOAD_WAIT = 20       # seconds to wait for the table to appear
_NETWORK_SETTLE_WAIT = 5   # extra seconds for XHR to complete


def _build_trade_id(politician_name: str, ticker: str, trade_date: str) -> str:
    """Construct a unique, deterministic trade identifier."""
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
    """Map site trade-type strings to 'Buy' or 'Sell'."""
    low = (raw or "").lower()
    if "buy" in low or "purchase" in low:
        return "Buy"
    return "Sell"


def _parse_bff_trades(data: dict) -> list[dict]:
    """Convert a BFF /trades JSON payload into a list of canonical trade dicts."""
    trades: list[dict] = []
    items = data.get("data", [])
    if not items:
        logger.warning("BFF trades response contained no 'data'; keys: %s", list(data.keys()))
        return trades

    for item in items:
        try:
            politician = item.get("politician") or {}
            issuer = item.get("issuer") or {}

            politician_name = politician.get("fullName") or politician.get("name", "Unknown")
            party_raw = (politician.get("party") or "").lower()
            party_map = {"democrat": "D", "republican": "R", "independent": "I", "d": "D", "r": "R", "i": "I"}
            party = party_map.get(party_raw, party_raw[:1].upper() or None)

            chamber_raw = (politician.get("chamber") or "").lower()
            chamber = "House" if "house" in chamber_raw else (
                "Senate" if "senate" in chamber_raw else None
            )

            ticker_raw = issuer.get("issuerTicker") or issuer.get("ticker")
            ticker = _parse_ticker(ticker_raw)
            trade_type = _normalise_trade_type(item.get("txType") or item.get("type", ""))
            trade_size = str(item.get("txSize") or item.get("tradeSize") or item.get("size") or "Unknown")
            trade_date = item.get("txDate") or item.get("tradeDate") or str(date.today())
            filing_date = item.get("filedDate") or item.get("filingDate") or trade_date

            politician_id = politician.get("_politicianId") or politician.get("id", "")
            source_url = (
                f"https://www.capitoltrades.com/politicians/{politician_id}"
                if politician_id else _TRADES_URL
            )

            trade_id = _build_trade_id(politician_name, ticker or ticker_raw or "UNK", trade_date)
            trades.append(
                {
                    "trade_id": trade_id,
                    "politician_name": politician_name,
                    "party": party,
                    "chamber": chamber,
                    "ticker": ticker,
                    "trade_type": trade_type,
                    "trade_size": trade_size,
                    "trade_date": trade_date,
                    "filing_date": filing_date,
                    "source_url": source_url,
                }
            )
        except Exception as exc:
            logger.debug("Skipped item due to parse error: %s", exc)

    return trades


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

    # Try Chrome first (works in Docker/Linux and most environments)
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

    # Fall back to Edge (pre-installed on Windows 11)
    opts = EdgeOptions()
    for arg in common_args:
        opts.add_argument(arg)
    opts.set_capability("ms:loggingPrefs", {"performance": "ALL"})
    service = EdgeService(EdgeChromiumDriverManager().install())
    driver = webdriver.Edge(service=service, options=opts)
    logger.info("Using Edge WebDriver")
    return driver


def _extract_bff_json_from_logs(driver: webdriver.Edge) -> dict | None:
    """Scan browser performance logs for a bff.capitoltrades.com/trades response."""
    try:
        logs = driver.get_log("performance")
    except Exception as exc:
        logger.warning("Could not retrieve performance logs: %s", exc)
        return None

    for entry in logs:
        try:
            msg = json.loads(entry["message"])["message"]
            if msg.get("method") != "Network.responseReceived":
                continue
            url = msg.get("params", {}).get("response", {}).get("url", "")
            if "bff.capitoltrades.com/trades" not in url:
                continue

            request_id = msg["params"]["requestId"]
            result = driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": request_id}
            )
            body_text = result.get("body", "")
            data = json.loads(body_text)
            logger.info("Captured BFF /trades response from %s (%d bytes)", url, len(body_text))
            return data
        except Exception:
            continue

    return None


def _parse_politician_cell(text: str) -> tuple[str, str | None, str | None]:
    """Extract (name, party, chamber) from a combined politician cell.

    Typical format: "Thomas Kean Jr\\nRepublicanHouseNJ"
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    name = lines[0] if lines else "Unknown"
    party = None
    chamber = None
    if len(lines) > 1:
        rest = lines[1]  # e.g. "RepublicanHouseNJ"
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


def _scrape_via_dom(driver: webdriver.Edge) -> list[dict]:
    """Fallback: parse the rendered DOM table if network logs are unavailable.

    Column layout confirmed from live DOM inspection:
      [0] Politician (name + party + chamber + state, newline-separated)
      [1] Issuer (company name + ticker, newline-separated)
      [2] Published / filing date
      [3] Traded / trade date
      [4] Filed after (days)
      [5] Owner
      [6] Type (BUY / SELL)
      [7] Size
      [8] Price
      [9] Detail link text
    """
    trades: list[dict] = []
    try:
        rows = driver.find_elements(By.CSS_SELECTOR, "table tbody tr")
        logger.info("DOM fallback: found %d table rows", len(rows))
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

            trade_type = _normalise_trade_type(texts[6])
            trade_size = texts[7]
            filing_date = texts[2].replace("\n", " ")   # "PUBLISHED" column
            trade_date = texts[3].replace("\n", " ")    # "TRADED" column

            # Try to extract the detail page URL from the link element in the row
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
                    "trade_id": trade_id,
                    "politician_name": politician_name,
                    "party": party,
                    "chamber": chamber,
                    "ticker": ticker,
                    "trade_type": trade_type,
                    "trade_size": trade_size,
                    "trade_date": trade_date,
                    "filing_date": filing_date,
                    "source_url": source_url,
                }
            )
    except Exception as exc:
        logger.warning("DOM fallback failed: %s", exc)
    return trades


def fetch_trades() -> list[dict]:
    """Fetch the latest congressional trades from capitoltrades.com.

    Launches a headless Edge browser, navigates to the trades page, and
    captures the BFF API response.  Falls back to DOM parsing if network
    log capture fails.  Returns an empty list on unrecoverable errors.
    """
    driver: webdriver.Edge | None = None
    try:
        logger.info("Starting headless Edge → %s", _TRADES_URL)
        driver = _make_driver()

        # Enable CDP Network events so we can retrieve response bodies
        driver.execute_cdp_cmd("Network.enable", {})

        driver.get(_TRADES_URL)
        logger.info("Page navigation complete; waiting up to %ds for trades table…", _PAGE_LOAD_WAIT)

        # Wait for at least one table row to appear
        try:
            WebDriverWait(driver, _PAGE_LOAD_WAIT).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "table tbody tr"))
            )
        except Exception:
            logger.warning("Trades table did not appear within %ds", _PAGE_LOAD_WAIT)

        # Extra settle time for XHR to fully complete
        time.sleep(_NETWORK_SETTLE_WAIT)

        # Primary: extract JSON from network logs
        data = _extract_bff_json_from_logs(driver)
        if data:
            trades = _parse_bff_trades(data)
            if trades:
                logger.info("Extracted %d trades from BFF network log", len(trades))
                return trades

        # Fallback: parse the DOM table
        logger.info("Network log capture yielded no trades; falling back to DOM parsing")
        trades = _scrape_via_dom(driver)
        logger.info("DOM fallback returned %d trades", len(trades))
        return trades

    except Exception as exc:
        logger.warning("fetch_trades encountered an unrecoverable error: %s", exc)
        return []
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass
