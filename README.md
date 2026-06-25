# Capitol Radar 🏛️📡

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Capitol Radar monitors congressional STOCK Act trade disclosures, scores them using a research-backed algorithmic model, and fires Telegram alerts when a credentialed politician makes a high-conviction buy. Claude AI writes the human-readable narrative — it does **not** determine signal quality. All scoring is deterministic and auditable.

---

## How It Works

```
Scrape → Filter & Score (algorithm) → Claude narrative → Telegram alert
```

1. **Scrape** — Polls [capitoltrades.com](https://www.capitoltrades.com/trades) daily via headless Chrome. Stops pagination once trades age beyond 90 days.
2. **Score** — Every buy trade receives a structured 0–100 score across 7 components (see below). Sells are stored silently.
3. **Gate** — Trade must score ≥45 AND have a credential (power≥5 pts OR committee≥5 pts). Basket rebalancing events and stale disclosures (>21 trading days) are suppressed.
4. **Entry check** — Live price fetched via yfinance. If stock has moved >3× ATR since disclosure, the alert is blocked — opportunity has passed.
5. **Narrative** — Claude Haiku writes a one-sentence reasoning and risk note in JSON. It receives the pre-computed score; it cannot change it.
6. **Alert** — Formatted Telegram message with trade date, entry price, disclosure gap, score breakdown, and ATR movement.

**Background jobs:**
- **Outcome updater** (daily 8:00 AM ET) — fetches 7/30/60/90-day forward prices for all alerted trades
- **Weekly leaderboard** (Monday 9:05 AM ET) — posts a win-rate ranking to Telegram

---

## Scoring Model (0–100)

| Component | Max pts | Source |
|-----------|---------|--------|
| Power / influence | 28 | NBER 2025 — alpha concentrated in formal leaders |
| Committee × sector overlap | 15 | Dong & Xu 2025 — sector-relevant committee buys outperform |
| Disclosure freshness | 20 | Lazzaretto 2024 — alpha fades after ~21 trading days |
| Federal contractor exposure | 12 | NBER 2025 — leaders buy firms that win contracts |
| Repeat buying (direction-aware) | 6 | Lazzaretto 2024 — accumulation > one-off |
| Owner type (Spouse/Self/Dependent) | 5 | Karadas — spouse accounts show edge |
| Basket concentration | 5 | Tiebreaker — concentrated bet vs. broad rebalancing |

**Signal tiers:**
- `strong` — score ≥65 AND power ≥22 (top congressional leadership)
- `high_moderate` — score ≥65, no top leadership requirement
- `moderate` — score ≥45 AND (power ≥5 OR committee ≥5 pts)
- `weak` — everything else (suppressed, stored only)

---

## Power Score Reference

Formal agenda-setting roles only — seniority without a leadership role has no validated alpha per NBER 2025.

| Score | Role |
|-------|------|
| 28 | Speaker of the House |
| 26 | Senate/House Majority or Minority Leader |
| 22 | Whip, Conference/Caucus Chair, President Pro Tem |
| 16 | Major committee Chair (Armed Services, Intelligence, Finance…) |
| 12 | Other committee Chair |
| 10 | Ranking Member; former Speaker with documented track record |
| 5–8 | Empirically documented top performers (2025 rankings) |
| 0 | Regular member — no validated informational edge |

---

## Data Validation

Every trade passes a STOCK Act compliance check before scoring:
- `filing_date < trade_date` → **data error, skipped** (CapitolTrades parsing issue)
- `filing_date − trade_date > 45 days` → **late filer flagged** in log (legal but noted)
- Ticker not matching `^[A-Z]{1,5}$` → dropped
- yfinance returns no price data → dropped (delisted/invalid)

---

## Setup (Windows)

```bash
# 1. Clone
git clone https://github.com/nalkaslassy/politican_trading_alert_app.git
cd politican_trading_alert_app/capitol-radar

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp config.example.yaml config.yaml
# Fill in: anthropic_api_key, telegram_bot_token, telegram_chat_id

# 4. Test immediately
python main.py --run-now

# 5. Start the scheduler
python main.py
```

**Auto-start on Windows login:** place `CapitolRadar.bat` in your Startup folder (`shell:startup`). The BAT file includes an auto-restart loop — if the scheduler crashes it relaunches after 60 seconds.

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `python main.py` | Start the scheduler (Mon–Fri 9 AM ET) |
| `python main.py --run-now` | Run the full pipeline immediately |
| `python main.py --update-outcomes` | Update forward prices for all alerted trades |
| `python main.py --leaderboard` | Post the leaderboard to Telegram immediately |
| `python backtest.py` | Backtest current criteria against stored historical trades |
| `python resend_alerts.py` | Resend moderate/strong alerts with fresh prices |
| `python resend_alerts.py PLTR ALB` | Resend specific tickers only |

---

## Configuration (`config.yaml`)

```yaml
anthropic_api_key: "..."
telegram_bot_token: "..."
telegram_chat_id: "..."          # your Telegram user ID or channel ID
telegram_mode: "direct"          # "direct" or "channel"

watchlist_mode: "all"            # "all" | "strict" | "dynamic"
min_signal_strength: "moderate"  # alert threshold: "strong" | "moderate" | "weak"
max_alerts_per_politician: 3     # cap per politician per pipeline run
max_trade_age_days: 90           # ignore trades older than this
max_trading_days_since_disclosure: 21  # ignore stale filings
scrape_pages_hard_cap: 50

db_path: "./data/capitol_radar.db"
log_level: "INFO"
```

---

## Project Structure

```
capitol-radar/
├── main.py                      Entry point — CLI flags + scheduler start
├── scheduler.py                 APScheduler job definitions (3 jobs)
├── backtest.py                  Backtest scoring criteria against stored history
├── resend_alerts.py             Manually resend alerts with fresh prices
├── scraper/
│   └── capitol_trades.py        Headless Chrome scraper for capitoltrades.com
├── filters/
│   ├── screener.py              Main filter + structured scoring pipeline
│   ├── power_score.py           Formal leadership power scores (0–28)
│   ├── committee_overlap.py     Committee × sector relevance scoring (0–15)
│   └── contractor_score.py      USAspending.gov federal contractor lookup (0–12)
├── scorer/
│   └── signal.py                Claude Haiku narrative generation (JSON only)
├── performance/
│   ├── tracker.py               Forward price fetcher (7/30/60/90d)
│   └── leaderboard.py           Weekly win-rate leaderboard
├── alerts/
│   └── telegram.py              Telegram Bot API dispatcher
├── storage/
│   └── db.py                    SQLite layer — no ORM
├── config.example.yaml
└── requirements.txt
```

---

## Backtesting

```bash
python backtest.py               # test current criteria against all stored buys
python backtest.py --combo high_power   # only formal leadership trades
python backtest.py --min-date 2026-01-01
```

Re-scores every Buy trade in the DB using live scoring logic (freshness set to maximum to simulate day-0 detection), fetches actual 30/60/90-day returns via yfinance, and reports win rate and alpha vs SPY.

---

## Expected Alert Frequency

Congressional filings are irregular. Expect:
- **Active weeks:** 3–8 alerts
- **Quiet weeks / recess:** 0–2 alerts
- **Batch filing days:** 5–12 alerts in one run, then quiet

Alerts are intentionally sparse — the gate is designed to fire only when a credentialed politician makes a fresh, concentrated, sector-relevant buy that still has a clean entry point.

---

## License

MIT © 2026
