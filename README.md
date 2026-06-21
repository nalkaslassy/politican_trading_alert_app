# Capitol Radar 🏛️📡

[![MIT License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Capitol Radar monitors congressional stock trade disclosures, filters them against a configurable watchlist of high-signal politicians, and fires real-time Telegram alerts scored by Claude AI. It tracks every alerted buy's performance over 30 and 60 days, automatically building a win-rate leaderboard, and posts it to your Telegram channel every Monday.

---

## How It Works

```
Scrape → Filter → Alpha Check → Score → Alert
```

1. **Scrape** — Capitol Radar polls [capitoltrades.com](https://www.capitoltrades.com/trades) for new congressional trade filings.
2. **Filter** — Trades are screened: only Buys, valid tickers, and size ≥ $15,001 proceed.
3. **Alpha Check** — The trade is tested against your chosen watchlist mode (see below). Non-watchlist trades are still stored silently for performance tracking.
4. **Score** — Claude Haiku analyses the trade in the context of the politician's historical track record and returns a signal strength (`strong` / `moderate` / `weak`), sector, and one-line reasoning.
5. **Alert** — A formatted message is sent to your Telegram chat or channel.

**Background jobs (run automatically):**

- **Outcome Tracker** (daily 8:00am ET) — fetches closing prices via yfinance at 30 and 60 days post-trade, records `win` / `loss`, and recalculates each politician's win rate.
- **Weekly Leaderboard** (every Monday 9:05am ET) — posts a ranked Telegram message of the top 10 politicians by 30-day win rate.

---

## Watchlist Modes

| Mode | Behaviour |
|------|-----------|
| `strict` | Only alert on politicians explicitly listed in `watchlist_politicians`. Best for getting started with known high-performers. |
| `dynamic` | Alert on any politician who has accumulated ≥ `min_trades_for_dynamic` tracked buys **and** a win rate ≥ `min_win_rate`. Requires ~60 days of data to be meaningful. |
| `all` | Alert on every qualifying trade regardless of who made it. Use with caution — high volume. |

---

## Telegram Setup

### 1. Create a bot (BotFather)
1. Open Telegram and search for `@BotFather`.
2. Send `/newbot` and follow the prompts.
3. Copy the **bot token** (looks like `123456:ABC-DEF...`).

### 2. Find your personal Chat ID (userinfobot)
1. Search for `@userinfobot` and send `/start`.
2. It replies with your numeric chat ID (e.g. `987654321`).
3. Use this as `telegram_chat_id` for direct/testing mode.

### 3. Set up a public channel (production)
1. Create a channel in Telegram → Settings → Channel type → Public.
2. Add your bot as an **Administrator** with "Post Messages" permission.
3. Use the channel's `@username` or numeric ID as `telegram_chat_id`.
4. Set `telegram_mode: "channel"` in your config.

---

## Self-Hosting Setup

```bash
# 1. Clone the repo
git clone https://github.com/yourname/capitol-radar.git
cd capitol-radar

# 2. Create your config
cp config.example.yaml config.yaml
# Edit config.yaml — add your Anthropic API key, Telegram bot token, and chat ID

# 3. Run with Docker
docker-compose up -d

# 4. Test the pipeline immediately
docker-compose exec capitol-radar python main.py --run-now
```

> **Data persistence** — the SQLite database lives in `./data/` which is mounted as a Docker volume. Your trade history and stats survive restarts.

---

## CLI Reference

| Command | Description |
|---------|-------------|
| `python main.py` | Start the scheduler (default mode) |
| `python main.py --run-now` | Run the full scrape → filter → score → alert pipeline immediately |
| `python main.py --update-outcomes` | Fetch latest price data and update win/loss records immediately |
| `python main.py --leaderboard` | Post the leaderboard to Telegram immediately |
| `python main.py --config /path/to/config.yaml` | Use a custom config file path |

---

## Scaling Up

### Phase 1 — Personal Alerts (Week 1)
Set `telegram_mode: "direct"` and `watchlist_mode: "strict"`. Start with the 8 politicians in `config.example.yaml`. You receive personal Telegram alerts for their buys.

### Phase 2 — Public Channel (Week 2+)
Switch to `telegram_mode: "channel"`. Point `telegram_chat_id` at your public channel username. Share the channel link anywhere you like — alerts are broadcast to all subscribers automatically.

### Phase 3 — Dynamic Mode (Day 60+)
Once you have ~60 days of performance data, switch to `watchlist_mode: "dynamic"`. Capitol Radar promotes any politician who clears your win-rate threshold automatically, and stops alerting ones whose edge dries up — no manual list maintenance.

### Phase 4 — SMS Paid Tier
See the `TODO` comment in [alerts/telegram.py](alerts/telegram.py). Integrate Plivo (or Twilio) to offer SMS alerts to paid subscribers. Subscribers who opt in receive the same scored signals as a text message.

---

## Hosted Version

> A hosted version of Capitol Radar with a managed Telegram channel is planned. Subscribe to get notified when it launches: _coming soon_.

---

## Project Structure

```
capitol-radar/
├── main.py                 Entry point — CLI flags + scheduler start
├── scheduler.py            APScheduler job definitions
├── scraper/
│   └── capitol_trades.py   Polls capitoltrades.com for new filings
├── filters/
│   └── screener.py         Filters by trade type, size, alpha list
├── scorer/
│   └── signal.py           Claude Haiku scores each signal
├── performance/
│   ├── tracker.py          Fetches prices and calculates outcomes
│   └── leaderboard.py      Ranks politicians by win rate
├── alerts/
│   └── telegram.py         Sends Telegram alerts
├── storage/
│   └── db.py               SQLite layer (no ORM)
├── config.example.yaml     Config template
├── Dockerfile
└── docker-compose.yml
```

---

## License

MIT © 2024

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
