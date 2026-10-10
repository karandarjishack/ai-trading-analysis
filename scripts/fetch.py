#!/usr/bin/env python3
"""
KD's AI Trading Desk — data pipeline v2 (runner-safe sources).

US stocks : Nasdaq official API (no key) — per-symbol daily history.
IN stocks : NSE official bhavcopy CSVs (no key) — full-market daily files.
US indices: Nasdaq API (COMP) + Stooq daily history (^spx, ^dji).
IN indices: NSE ind_close_all CSV (Nifty 50, Nifty Bank).

Runs a 6-agent desk over the results, and writes docs/data/market.json:
SCOUT (momentum+volume screen), NEWS (RSS headlines), SENTIMENT (headline
score), CHARTS (ATR entry/stop/target), SKEPTIC (risks), CHIEF (verdict).

Fail-safe: if zero symbols are fetched, the previous market.json is kept
untouched so the site never goes blank.
"""

import csv
import io
import json
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
FROMDATE = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%d")


def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fnum(x):
    try:
        return float(str(x).replace("$", "").replace(",", "").strip() or 0)
    except Exception:
        return 0.0


# ---------------- indicators (unchanged math) ----------------
def rsi(closes, n=14):
    if len(closes) < n + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, n + 1):
        d = closes[-i] - closes[-i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains) / n, sum(losses) / n
    if al == 0:
        return 100.0
    return round(100 - 100 / (1 + ag / al), 1)


def atr(bars, n=14):
    if len(bars) < n + 1:
        return 0.0
    trs = []
    for i in range(1, n + 1):
        h, l, pc = bars[-i][2], bars[-i][3], bars[-i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return round(sum(trs) / n, 2)


def pct(a, b):
    return round((a - b) / b * 100, 2) if b else 0.0


def bias_label(trend, chg_pct, sentiment=None):
    """BULLISH/BEARISH/NEUTRAL from confluence of trend + momentum + headlines."""
    votes, why = 0, []
    if trend == "UPTREND":
        votes += 1
        why.append("uptrend")
    elif trend == "DOWNTREND":
        votes -= 1
        why.append("downtrend")
    if chg_pct >= 2:
        votes += 1
        why.append(f"up {chg_pct:+.1f}% today")
    elif chg_pct <= -2:
        votes -= 1
        why.append(f"down {chg_pct:+.1f}% today")
    if sentiment == "bullish":
        votes += 1
        why.append("bullish headlines")
    elif sentiment == "bearish":
        votes -= 1
        why.append("bearish headlines")
    label = "BULLISH" if votes >= 2 else "BEARISH" if votes <= -2 else "NEUTRAL"
    return label, why


# ---------------- US: Nasdaq API ----------------
def nasdaq_history(symbol, tries=3):
    """Daily bars (oldest->newest) as (date, o, h, l, c, v) tuples."""
    sym = symbol.replace("-", ".")  # BRK-B -> BRK.B
    url = (f"https://api.nasdaq.com/api/quote/{sym}/historical"
           f"?assetclass=stocks&fromdate={FROMDATE}&limit=9999")
    for a in range(tries):
        try:
            d = json.loads(http_get(url))
            rows = d["data"]["tradesTable"]["rows"]
            bars = []
            for r in rows:
                dt = datetime.strptime(r["date"], "%m/%d/%Y")
                bars.append((dt, fnum(r["open"]), fnum(r["high"]),
                             fnum(r["low"]), fnum(r["close"]), fnum(r["volume"])))
            bars.sort(key=lambda b: b[0])
            if len(bars) >= 25:
                return bars
        except Exception:
            time.sleep(2 * (a + 1))
    return []


# ---------------- IN: NSE bhavcopy ----------------
def bhav_url(dt):
    return ("https://nsearchives.nseindia.com/products/content/"
            f"sec_bhavdata_full_{dt.strftime('%d%m%Y')}.csv")


def fetch_bhav(dt):
    try:
        raw = http_get(bhav_url(dt)).decode("utf-8", "ignore")
        out = {}
        for row in csv.DictReader(io.StringIO(raw), skipinitialspace=True):
            if row.get("SERIES", "").strip() != "EQ" or not row.get("SYMBOL"):
                continue
            sym = row["SYMBOL"].strip()
            out[sym] = (dt, fnum(row["OPEN_PRICE"]), fnum(row["HIGH_PRICE"]),
                        fnum(row["LOW_PRICE"]), fnum(row["CLOSE_PRICE"]),
                        fnum(row["TTL_TRD_QNTY"]))
        return out if out else None
    except Exception:
        return None


def nse_history(tickers, max_days=60):
    """{base_symbol: [bars oldest->newest]} + latest trading date."""
    days = {}  # date -> {sym: bar}
    today = datetime.now(timezone.utc).date()
    for back in range(120):
        if len(days) >= max_days:
            break
        dt = today - timedelta(days=back)
        if dt.weekday() >= 5:
            continue
        bars = fetch_bhav(dt)
        if bars:
            days[dt] = bars
    hist = {t: [] for t in tickers}
    for dt in sorted(days):
        for t in tickers:
            if t in days[dt]:
                hist[t].append(days[dt][t])
    latest = max(days) if days else None
    return hist, latest


# ---------------- indices ----------------
def idx_from_nasdaq(sym):
    url = (f"https://api.nasdaq.com/api/quote/{sym}/historical"
           f"?assetclass=index&fromdate={FROMDATE}&limit=9999")
    d = json.loads(http_get(url))
    rows = d["data"]["tradesTable"]["rows"]
    bars = sorted(
        ((datetime.strptime(r["date"], "%m/%d/%Y"), fnum(r["close"]))
         for r in rows), key=lambda b: b[0])
    if len(bars) < 2:
        raise ValueError("too few bars")
    return {"price": bars[-1][1], "chg_pct": pct(bars[-1][1], bars[-2][1])}


def idx_from_stooq(sym):
    url = f"https://stooq.com/q/d/l/?s={urllib.parse.quote(sym)}&i=d"
    lines = http_get(url).decode().strip().splitlines()
    rows = [l.split(",") for l in lines[1:] if l.strip()]
    if len(rows) < 2:
        raise ValueError("too few rows")
    return {"price": fnum(rows[-1][4]), "chg_pct": pct(fnum(rows[-1][4]), fnum(rows[-2][4]))}


def idx_from_nse_csv():
    today = datetime.now(timezone.utc).date()
    for back in range(10):
        dt = today - timedelta(days=back)
        if dt.weekday() >= 5:
            continue
        url = ("https://nsearchives.nseindia.com/content/indices/"
               f"ind_close_all_{dt.strftime('%d%m%Y')}.csv")
        try:
            raw = http_get(url).decode("utf-8", "ignore")
            out = {}
            for row in csv.DictReader(io.StringIO(raw), skipinitialspace=True):
                name = (row.get("Index Name") or "").strip()
                if name in ("Nifty 50", "Nifty Bank"):
                    out[name] = {"price": fnum(row["Closing Index Value"]),
                                 "chg_pct": fnum(row["Change(%)"])}
            if len(out) == 2:
                return {"Nifty 50": out["Nifty 50"], "Bank Nifty": out["Nifty Bank"]}
        except Exception:
            continue
    raise ValueError("no NSE index file")


def fetch_indices(prev):
    idx = {}
    old_idx = prev.get("indices", {}) or {}
    # key migration: v1 (Yahoo) -> v2 keys, so fallback works across the switch
    for _new, _olds in (("spx", ["^GSPC"]), ("ndx", ["^IXIC"]),
                        ("dji", ["^DJI"]), ("nifty", ["^NSEI"]),
                        ("banknifty", ["^NSEBANK"])):
        for o in _olds:
            if o in old_idx and _new not in old_idx:
                old_idx[_new] = old_idx[o]

    def put(key, label, mkt, fn, *a):
        try:
            idx[key] = {"name": label, "market": mkt, **fn(*a)}
        except Exception as e:
            old = (prev.get("indices", {}) or {}).get(key)
            if old:
                idx[key] = old
            print(f"  index {label} failed ({e}); kept previous" if old
                  else f"  index {label} failed ({e}); no previous")

    put("spx", "S&P 500", "US", idx_from_stooq, "^spx")
    put("ndx", "Nasdaq", "US", idx_from_nasdaq, "COMP")
    put("dji", "Dow Jones", "US", idx_from_stooq, "^dji")
    try:
        nse = idx_from_nse_csv()
        idx["nifty"] = {"name": "Nifty 50", "market": "IN", **nse["Nifty 50"]}
        idx["banknifty"] = {"name": "Bank Nifty", "market": "IN", **nse["Bank Nifty"]}
    except Exception as e:
        for k, label in (("nifty", "Nifty 50"), ("banknifty", "Bank Nifty")):
            old = (prev.get("indices", {}) or {}).get(k)
            if old:
                idx[k] = old
        print(f"  NSE indices failed ({e}); kept previous")
    return idx


# ---------------- shared analysis ----------------
def analyze_bars(symbol, name, market, bars):
    closes = [b[4] for b in bars]
    vols = [b[5] for b in bars]
    price, prev = closes[-1], closes[-2]
    chg = pct(price, prev)
    avg_vol = sum(vols[-21:-1]) / 20 if len(vols) > 21 else (sum(vols) / len(vols) if vols else 1)
    vol_ratio = round((vols[-1] / avg_vol), 2) if avg_vol else 0
    r = rsi(closes)
    hi60 = max(closes[-60:]) if len(closes) >= 60 else max(closes)
    lo60 = min(closes[-60:]) if len(closes) >= 60 else min(closes)
    a = atr(bars)
    trend = ("UPTREND" if price > sum(closes[-20:]) / 20 > sum(closes[-50:]) / 50
             else "DOWNTREND" if price < sum(closes[-20:]) / 20 < sum(closes[-50:]) / 50
             else "RANGE")
    blab, bwhy = bias_label(trend, chg)
    return {
        "symbol": symbol, "name": name, "market": market,
        "price": round(price, 2), "chg_pct": chg,
        "vol_ratio": vol_ratio, "rsi_14": r, "trend": trend,
        "bias": blab, "bias_why": ", ".join(bwhy) if bwhy else "mixed signals",
        "high_60d": round(hi60, 2), "low_60d": round(lo60, 2),
        "atr_14": a, "spark": [round(c, 2) for c in closes[-30:]],
    }


def fetch_news(query, n=3):
    try:
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(query + " stock") + "&hl=en-IN&gl=IN&ceid=IN:en")
        import xml.etree.ElementTree as ET
        xml = http_get(url, timeout=20).decode("utf-8", "ignore")
        return [it.find("title").text for it in ET.fromstring(xml).findall(".//item")[:n]
                if it.find("title") is not None]
    except Exception:
        return []


BULL = {"surge", "soar", "rally", "jump", "gain", "record", "upgrade", "beat",
        "bullish", "breakout", "high", "profit", "growth", "buy", "strong"}
BEAR = {"plunge", "tumble", "fall", "drop", "crash", "downgrade", "miss",
        "bearish", "warning", "loss", "fraud", "probe", "sell", "weak", "cut"}


def desk_card(s, news_items):
    """Flat schema — matches what docs/index.html card() renders."""
    txt = " ".join(news_items).lower()
    bull = sum(w in txt for w in BULL)
    bear = sum(w in txt for w in BEAR)
    label = "bullish" if bull > bear else "bearish" if bear > bull else "mixed"
    blab, bwhy = bias_label(s["trend"], s["chg_pct"], label)
    a = s["atr_14"] or s["price"] * 0.02
    bull_play = s["chg_pct"] > 0
    entry = round(s["price"], 2)
    stop = round(entry - 1.5 * a, 2) if bull_play else round(entry + 1.5 * a, 2)
    tgt1 = round(entry + 2.5 * a, 2) if bull_play else round(entry - 2.5 * a, 2)
    tgt2 = round(entry + 4.0 * a, 2) if bull_play else round(entry - 4.0 * a, 2)
    verdict = ("SETUP" if (s["vol_ratio"] >= 1.8 and abs(s["chg_pct"]) >= 3
                           and ((bull_play and s["rsi_14"] < 75)
                                or (not bull_play and s["rsi_14"] > 25)))
               else "WATCH" if s["vol_ratio"] >= 1.3 else "WAIT")
    return {
        "bias": {"label": blab,
                 "why": ", ".join(bwhy) if bwhy else "mixed signals"},
        "scout": (f"{s['chg_pct']:+.2f}% on {s['vol_ratio']}x relative volume, "
                  f"RSI {s['rsi_14']}, {s['trend']}."),
        "news": [{"title": t, "source": "Google News"} for t in news_items],
        "sentiment": {"label": label,
                      "note": f"{bull} bullish vs {bear} bearish headline signals."},
        "charts": {"levels": {"entry": entry, "stop": stop,
                              "target_1": tgt1, "target_2": tgt2}},
        "skeptic": (f"Momentum can reverse fast after a {abs(s['chg_pct']):.1f}% day; "
                    f"volume spike may be one-off; wider market can override the setup."),
        "chief": {"verdict": verdict,
                  "why": ("strong directional move with volume"
                          if verdict == "SETUP" else "not enough confirmation yet")},
    }


def market_status(now):
    h = now.hour + now.minute / 60
    us_open = now.weekday() < 5 and 13.5 <= h < 20      # 9:30-16:00 ET
    ist_h = (h + 5.5) % 24
    in_open = now.weekday() < 5 and 3.75 <= ist_h < 9.5  # 9:15-15:30 IST
    return {"us_open": us_open, "india_open": in_open}


def load_tickers():
    with open("docs/data/tickers.json") as f:
        t = json.load(f)
    return ([(e["symbol"], e["name"]) for e in t["US"]],
            [(e["symbol"], e["name"]) for e in t["IN"]])


def main():
    t0 = time.time()
    prev = {}
    try:
        prev = json.load(open("docs/data/market.json"))
    except Exception:
        pass

    US_T, IN_T = load_tickers()
    print(f"universe: {len(US_T)} US + {len(IN_T)} IN", flush=True)

    # ---- US via Nasdaq ----
    def us_one(sn):
        sym, name = sn
        bars = nasdaq_history(sym)
        return analyze_bars(sym, name, "US", bars) if bars else {"error": sym}

    # ---- IN via NSE bhavcopy ----
    in_tickers = [s.replace(".NS", "") for s, _ in IN_T]
    in_name = {s.replace(".NS", ""): n for s, n in IN_T}
    print("fetching NSE bhavcopy history...", flush=True)
    in_hist, in_latest = nse_history(in_tickers)
    print(f"  NSE: {len(in_hist)} tickers, latest file {in_latest}", flush=True)

    symbols = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        us_res = list(ex.map(us_one, US_T))
    for r in us_res:
        if "error" not in r:
            symbols.append(r)
    for t in in_tickers:
        bars = in_hist.get(t, [])
        if len(bars) >= 25:
            symbols.append(analyze_bars(t + ".NS", in_name[t], "IN", bars))

    ok = [s for s in symbols if "error" not in s]
    print(f"fetched: {len(ok)} symbols in {time.time()-t0:.0f}s", flush=True)

    if not ok:
        print("no symbols fetched; keeping previous market.json unchanged")
        return

    us = [s for s in ok if s["market"] == "US"]
    ins = [s for s in ok if s["market"] == "IN"]
    most_bought = sorted([s for s in ok if s["chg_pct"] >= 3 and s["vol_ratio"] >= 1.5],
                         key=lambda s: (s["chg_pct"], s["vol_ratio"]), reverse=True)[:8]
    most_sold = sorted([s for s in ok if s["chg_pct"] <= -2.5 and s["vol_ratio"] >= 1.5],
                       key=lambda s: (s["chg_pct"], -s["vol_ratio"]))[:8]
    breakouts = sorted([s for s in ok if s["price"] >= s["high_60d"] * 0.985
                        and s["chg_pct"] >= 2 and s["vol_ratio"] >= 1.3],
                       key=lambda s: s["vol_ratio"], reverse=True)[:8]
    weak = sorted([s for s in ok if (s["price"] <= s["low_60d"] * 1.015
                                    or s["chg_pct"] <= -2) and s["vol_ratio"] >= 1.2],
                  key=lambda s: s["chg_pct"])[:8]

    # ---- 6-agent desk on the shortlist ----
    desk_syms = []
    for s in most_bought + most_sold + breakouts + weak:
        if s["symbol"] not in desk_syms:
            desk_syms.append(s["symbol"])
    by_sym = {s["symbol"]: s for s in ok}
    desk = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        news_map = dict(zip(desk_syms[:24],
                            ex.map(lambda sym: fetch_news(by_sym[sym]["name"]),
                                   desk_syms[:24])))
    for sym in desk_syms[:24]:
        desk[sym] = desk_card(by_sym[sym], news_map.get(sym, []))

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
        "indices": fetch_indices(prev),
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
          f"{len(breakouts)} breakouts, {len(weak)} weak, {len(desk)} desk cards, "
          f"{len(payload['indices'])} indices, total {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
