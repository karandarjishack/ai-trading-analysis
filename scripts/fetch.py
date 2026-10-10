#!/usr/bin/env python3
"""KD's AI Trading Desk - data pipeline.

Fetches quotes + 3 months of daily history from Yahoo Finance (no API key),
runs a 6-agent desk over the results, and writes docs/data/market.json:

  SCOUT     - finds stocks moving on big volume (most bought / most sold)
  NEWS      - pulls latest headlines explaining why each pick is moving
  SENTIMENT - headline tone (bullish / bearish / mixed) via word scoring
  CHARTS    - entry / stop / target levels from ATR and 20-day range
  SKEPTIC   - argues against every pick (overbought, extended, thin volume)
  CHIEF     - final verdict: SETUP / WAIT / SKIP

Run locally or in GitHub Actions on a schedule. Educational only.
"""
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"}
# Yahoo sometimes blocks specific hosts/IP ranges (e.g. cloud runners), so we
# fail over across hosts and retry with backoff before giving up on a symbol.
YAHOO_HOSTS = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]

US_SYMBOLS = [
    ("AAPL", "Apple"), ("MSFT", "Microsoft"), ("NVDA", "NVIDIA"), ("TSLA", "Tesla"),
    ("AMZN", "Amazon"), ("GOOGL", "Alphabet"), ("META", "Meta"), ("AMD", "AMD"),
    ("AVGO", "Broadcom"), ("NFLX", "Netflix"), ("JPM", "JPMorgan"), ("XOM", "Exxon Mobil"),
    ("UNH", "UnitedHealth"), ("V", "Visa"), ("MA", "Mastercard"), ("COST", "Costco"),
    ("WMT", "Walmart"), ("DIS", "Disney"), ("PLTR", "Palantir"), ("COIN", "Coinbase"),
    ("CRM", "Salesforce"), ("ORCL", "Oracle"),
]
IN_SYMBOLS = [
    ("RELIANCE.NS", "Reliance Ind"), ("TCS.NS", "TCS"), ("HDFCBANK.NS", "HDFC Bank"),
    ("INFY.NS", "Infosys"), ("ICICIBANK.NS", "ICICI Bank"), ("SBIN.NS", "SBI"),
    ("EICHERMOT.NS", "Eicher Motors"), ("TATAMOTORS.NS", "Tata Motors"),
    ("AXISBANK.NS", "Axis Bank"), ("KOTAKBANK.NS", "Kotak Bank"), ("LT.NS", "L&T"),
    ("TITAN.NS", "Titan"), ("M&M.NS", "M&M"), ("MARUTI.NS", "Maruti Suzuki"),
    ("SUNPHARMA.NS", "Sun Pharma"), ("NTPC.NS", "NTPC"), ("POWERGRID.NS", "Power Grid"),
    ("BAJFINANCE.NS", "Bajaj Finance"), ("HCLTECH.NS", "HCL Tech"), ("ADANIENT.NS", "Adani Ent"),
]
US_INDICES = [("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^DJI", "Dow Jones")]
IN_INDICES = [("^NSEI", "Nifty 50"), ("^NSEBANK", "Bank Nifty")]

POS_WORDS = {"surge", "surging", "jumps", "rally", "rallies", "beats", "upgrade",
             "upgraded", "record", "gains", "strong", "bullish", "profit", "growth",
             "soars", "rebound", "outperform", "buy", "breakthrough", "high"}
NEG_WORDS = {"falls", "drops", "sinks", "misses", "downgrade", "downgraded", "weak",
             "bearish", "loss", "plunge", "probe", "fraud", "slumps", "tumbles",
             "sell", "warning", "cut", "lawsuit", "crash", "fear"}


def http_get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_chart(symbol):
    """Fetch daily chart with host failover and retries. Raises on total failure."""
    last_err = None
    for host in YAHOO_HOSTS:
        for attempt in range(3):
            try:
                return json.loads(http_get(
                    f"https://{host}/v8/finance/chart/{symbol}?interval=1d&range=3mo",
                    timeout=25))
            except Exception as e:
                last_err = e
                time.sleep(1.0 * (attempt + 1))
    raise last_err


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[-i] - closes[-i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains) / period, sum(losses) / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


def sma(values, n):
    return sum(values[-n:]) / n if len(values) >= n else None


def atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i],
                       abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
    return sum(trs[-period:]) / period if len(trs) >= period else None


def clean(s):
    return re.sub(r"\s+", " ", s or "").strip()


def fetch_news(query, market, limit=3):
    """NEWS agent: latest headlines via Google News RSS (free, no key)."""
    hl = "en-US" if market == "US" else "en-IN"
    gl, ceid = ("US", "US:en") if market == "US" else ("IN", "IN:en")
    url = ("https://news.google.com/rss/search?q=" +
           urllib.parse.quote(query) + f"&hl={hl}&gl={gl}&ceid={ceid}")
    try:
        root = ET.fromstring(http_get(url, timeout=15))
        items = []
        for item in list(root.iter("item"))[:limit]:
            title = clean(item.findtext("title"))
            src = clean(item.findtext("source"))
            pub = clean(item.findtext("pubDate"))
            if title:
                items.append({"title": title, "source": src, "published": pub})
        return items
    except Exception:
        return []


def sentiment(headlines):
    """SENTIMENT agent: naive bullish/bearish tone score from headline words."""
    pos = neg = 0
    for h in headlines:
        words = set(re.findall(r"[a-z]+", h["title"].lower()))
        pos += len(words & POS_WORDS)
        neg += len(words & NEG_WORDS)
    total = pos + neg
    if total == 0:
        return {"label": "mixed", "score": 0, "note": "headlines carry no clear tone"}
    score = round((pos - neg) / total, 2)
    label = "bullish" if score >= 0.34 else ("bearish" if score <= -0.34 else "mixed")
    return {"label": label, "score": score,
            "note": f"{pos} bullish vs {neg} bearish cue words in headlines"}


def analyze(symbol, name, market):
    try:
        res = fetch_chart(symbol)["chart"]["result"][0]
    except Exception as e:
        return {"symbol": symbol, "name": name, "market": market, "error": str(e)[:120]}
    meta = res["meta"]
    q = res["indicators"]["quote"][0]
    closes = [c for c in q["close"] if c]
    if len(closes) < 2:
        return {"symbol": symbol, "name": name, "market": market, "error": "no data"}
    highs = [h if h else closes[min(i, len(closes) - 1)] for i, h in enumerate(q["high"])]
    lows = [l if l else closes[min(i, len(closes) - 1)] for i, l in enumerate(q["low"])]
    vols = [v or 0 for v in q["volume"]]

    price = meta.get("regularMarketPrice") or closes[-1]
    prev = closes[-2]
    chg_pct = (price - prev) / prev * 100 if prev else 0
    vol_today = vols[-1] if vols else 0
    hist_vols = vols[-21:-1]
    avg_vol = sum(hist_vols) / len(hist_vols) if hist_vols else 0
    vol_ratio = vol_today / avg_vol if avg_vol else 0
    hi20 = max(closes[-20:]) if len(closes) >= 20 else max(closes)
    lo20 = min(closes[-20:]) if len(closes) >= 20 else min(closes)
    s20, s50 = sma(closes, 20), sma(closes, 50)
    trend = "mixed"
    if s20 and s50:
        trend = "up" if price > s20 > s50 else ("down" if price < s20 < s50 else "mixed")
    ext = (price - s20) / s20 * 100 if s20 else 0
    atr14 = atr(highs, lows, closes)
    rng = (highs[-1] or price) - (lows[-1] or price)
    range_pos = ((price - (lows[-1] or price)) / rng) if rng else 0.5

    return {
        "symbol": symbol, "name": name, "market": market,
        "price": round(price, 2), "prev_close": round(prev, 2),
        "chg_pct": round(chg_pct, 2),
        "day_high": round(highs[-1], 2), "day_low": round(lows[-1], 2),
        "volume": vol_today, "avg_volume_20": round(avg_vol),
        "vol_ratio": round(vol_ratio, 2),
        "high_20": round(hi20, 2), "low_20": round(lo20, 2),
        "dist_to_high_pct": round((hi20 - price) / hi20 * 100, 2) if hi20 else None,
        "dist_to_low_pct": round((price - lo20) / lo20 * 100, 2) if lo20 else None,
        "rsi_14": rsi(closes), "trend": trend,
        "ext_sma20_pct": round(ext, 1), "atr_14": round(atr14, 2) if atr14 else None,
        "range_pos": round(range_pos, 2),
        "spark": [round(c, 2) for c in closes[-30:]],
        "currency": meta.get("currency", "USD"),
    }


def desk_card(s, news_items):
    """Build the 6-agent desk card for one pick."""
    senti = sentiment(news_items)
    price, a = s["price"], s["atr_14"] or 0
    levels = None
    if a:
        levels = {"entry": round(price, 2),
                  "stop": round(price - 1.5 * a, 2),
                  "target_1": round(price + a, 2),
                  "target_2": round(price + 2 * a, 2)}
    risks = []
    if s["rsi_14"] is not None and s["rsi_14"] >= 70:
        risks.append(f"RSI {s['rsi_14']} is overbought — chasing here burns")
    if s["ext_sma20_pct"] >= 10:
        risks.append(f"{s['ext_sma20_pct']}% above its 20-day average — extended")
    if s["vol_ratio"] < 1.0:
        risks.append("volume is below average — weak conviction")
    if s["trend"] == "down" and s["chg_pct"] > 0:
        risks.append("one green day inside a downtrend — could be a dead-cat bounce")
    if senti["label"] == "bearish":
        risks.append("headlines are running bearish — sentiment against it")
    skeptic = risks[0] if risks else "No red flag on the tape — but size small anyway."
    if s["chg_pct"] >= 2 and s["vol_ratio"] >= 1.2 and (s["rsi_14"] or 0) < 70 and s["trend"] != "down":
        verdict, why = "SETUP", "momentum + volume + trend aligned"
    elif s["chg_pct"] <= -2 and (s["rsi_14"] or 100) <= 35:
        verdict, why = "WATCH", "sold off hard, possibly washed out — bounce watch, high risk"
    elif (s["rsi_14"] or 0) >= 75 or s["ext_sma20_pct"] >= 12:
        verdict, why = "SKIP", "overextended — let it come back to you"
    else:
        verdict, why = "WAIT", "no clean edge on the tape right now"
    direction = "up" if s["chg_pct"] >= 0 else "down"
    scout = (f"{direction} {abs(s['chg_pct']):.2f}% on {s['vol_ratio']:.1f}x average volume, "
             f"RSI {s['rsi_14']}, trend {s['trend']}.")
    return {
        "scout": "SCOUT found: " + scout,
        "news": news_items,
        "sentiment": senti,
        "charts": {"levels": levels,
                   "note": "Entry/stop/targets from 14-day ATR. Educational levels, not advice."} if levels else None,
        "skeptic": skeptic,
        "chief": {"verdict": verdict, "why": why},
    }


def market_status(now_utc):
    wd, mins = now_utc.weekday(), now_utc.hour * 60 + now_utc.minute
    return {"us_open": wd < 5 and 13 * 60 + 30 <= mins < 20 * 60,
            "india_open": wd < 5 and 3 * 60 + 45 <= mins < 10 * 60}


def main():
    symbols, indices = [], {}
    for sym, name in US_SYMBOLS:
        symbols.append(analyze(sym, name, "US"))
        time.sleep(0.8)
    for sym, name in IN_SYMBOLS:
        symbols.append(analyze(sym, name, "IN"))
        time.sleep(0.8)
    for sym, name in US_INDICES + IN_INDICES:
        mkt = "US" if sym in dict(US_INDICES) else "IN"
        try:
            a = analyze(sym, name, mkt)
            if "error" not in a:
                indices[sym] = {"name": name, "market": mkt,
                                "price": a["price"], "chg_pct": a["chg_pct"]}
        except Exception:
            pass
        time.sleep(0.3)

    ok = [s for s in symbols if "error" not in s]
    if not ok:
        # FAIL-SAFE: never overwrite last good data with an empty run
        # (e.g. if the data source blocks this runner's IP).
        print("no symbols fetched; keeping previous market.json unchanged")
        return
    movers = [s for s in ok if abs(s["chg_pct"]) >= 2.0 and s["vol_ratio"] >= 1.2]
    most_bought = sorted([s for s in movers if s["chg_pct"] > 0], key=lambda x: -x["chg_pct"])[:8]
    most_sold = sorted([s for s in movers if s["chg_pct"] < 0], key=lambda x: x["chg_pct"])[:8]
    breakouts = sorted(
        [s for s in ok if s["dist_to_high_pct"] is not None
         and s["dist_to_high_pct"] <= 1.5 and s["vol_ratio"] >= 1.0],
        key=lambda x: -x["vol_ratio"])[:8]
    weak = sorted(
        [s for s in ok if s["dist_to_low_pct"] is not None and s["dist_to_low_pct"] <= 1.5],
        key=lambda x: x["chg_pct"])[:8]

    desk_syms = []
    for s in most_bought + most_sold + breakouts + weak:
        if s["symbol"] not in desk_syms:
            desk_syms.append(s["symbol"])
    desk = {}
    by_sym = {s["symbol"]: s for s in ok}
    for sym in desk_syms[:24]:
        s = by_sym[sym]
        query = s["name"] + (" stock" if s["market"] == "US" else " share price")
        news_items = fetch_news(query, s["market"])
        desk[sym] = desk_card(s, news_items)
        time.sleep(0.4)

    def pick(s):
        d = desk.get(s["symbol"], {})
        return {"symbol": s["symbol"], "name": s["name"], "market": s["market"],
                "price": s["price"], "chg_pct": s["chg_pct"],
                "vol_ratio": s["vol_ratio"], "rsi_14": s["rsi_14"],
                "trend": s["trend"], "desk": d}

    now = datetime.now(timezone.utc)
    payload = {
        "generated_at_utc": now.strftime("%Y-%m-%d %H:%M UTC"),
        "market_status": market_status(now),
        "indices": indices,
        "symbols": ok,
        "screen": {
            "most_bought": [pick(s) for s in most_bought],
            "most_sold": [pick(s) for s in most_sold],
            "breakouts": [pick(s) for s in breakouts],
            "weak": [pick(s) for s in weak],
        },
        "disclaimer": ("Educational screener only — not financial advice. "
                       "Free market data can be delayed ~15 min."),
    }
    with open("docs/data/market.json", "w") as f:
        json.dump(payload, f)
    print(f"wrote docs/data/market.json: {len(ok)} symbols, "
          f"{len(most_bought)} bought, {len(most_sold)} sold, "
          f"{len(breakouts)} breakouts, {len(weak)} weak, {len(desk)} desk cards")


if __name__ == "__main__":
    main()
