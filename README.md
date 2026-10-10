# KD's AI Trading Desk

A realtime-ish stock analysis dashboard on GitHub Pages — no Vercel, no servers.

**Live site:** `https://karandarjishack.github.io/stock-analysis/`

## How it works

1. `scripts/fetch.py` pulls quotes + 3 months of history from Yahoo Finance (free, no API key)
   for 22 US large-caps, 20 NSE large-caps, and 5 indices.
2. It runs a 6-agent desk over every pick, in the style of the AI trading-desk reel:
   - **SCOUT** — finds stocks moving on big volume (most bought / most sold)
   - **NEWS** — latest headlines for why each pick is moving (Google News RSS)
   - **SENTIMENT** — bullish/bearish/mixed headline tone
   - **CHARTS** — entry / stop / target levels from 14-day ATR
   - **SKEPTIC** — argues against every pick (overbought, extended, thin volume)
   - **CHIEF** — final verdict: SETUP / WATCH / WAIT / SKIP
3. Results are written to `docs/data/market.json`.
4. `.github/workflows/update.yml` re-runs the screener **every 30 minutes on weekdays
   during market hours** and commits the fresh JSON — the site auto-refreshes while open.

## Honest limits

- Free market data is delayed ~15 minutes. This is the fastest free cadence;
  tick-level realtime needs a paid feed.
- Educational screener only — not financial advice.

## Run locally

```bash
python3 scripts/fetch.py        # writes docs/data/market.json
python3 -m http.server --directory docs 8000   # open http://localhost:8000
```
