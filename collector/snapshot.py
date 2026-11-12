"""Goblin Alpha market snapshot collector.

Runs on a schedule (GitHub Actions), pulls public market data, computes the
BTC 4H anchor, macro backdrop and rule-based signals, and writes:

  data/snapshot.json   latest snapshot read by the dashboard tab
  data/history.json    rolling 72h of compact readings (OI, L/S, price)
  data/signals.json    paper log of every signal firing + forward returns

Sources (all public, no keys):
  Hyperliquid info API  BTC candles, mark, funding, OI, whale positions
  Hyperliquid stats     leaderboard (to pick the whale universe)
  OKX public API        BTC account long/short ratio, OI history, funding
  Deribit public API    BTC-PERPETUAL OI and funding
  Yahoo Finance         S&P 500, VIX, Nasdaq Composite, DXY, US 10Y
"""
from __future__ import annotations

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import requests

import informed as inf

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

HL = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
OKX = "https://www.okx.com/api/v5"
DERIBIT = "https://www.deribit.com/api/v2/public"

WHALE_UNIVERSE = int(os.getenv("WHALE_UNIVERSE", "300"))  # top accounts by equity scanned
WHALE_COUNT = int(os.getenv("WHALE_COUNT", "50"))  # largest BTC positions tracked
WHALE_MIN_NTL = 250_000  # ignore BTC positions under $250k

S = requests.Session()
S.headers["User-Agent"] = "goblin-alpha-market-snapshot/1.0"

NOW = datetime.now(timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
ERRORS: list[str] = []


# ----------------------------------------------------------------- helpers
def hl(body: dict, retries: int = 3):
    for i in range(retries):
        try:
            r = S.post(HL, json=body, timeout=20)
            if r.status_code == 429:
                time.sleep(2 + i * 3)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if i == retries - 1:
                raise
            time.sleep(1 + i)
    raise RuntimeError("hyperliquid retries exhausted")


def get(url: str, **kw):
    r = S.get(url, timeout=kw.pop("timeout", 20), **kw)
    r.raise_for_status()
    return r.json()


def safe(label: str, fn, default=None):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        ERRORS.append(f"{label}: {type(e).__name__}: {str(e)[:160]}")
        return default


def load(name: str, default):
    p = DATA / name
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            pass
    return default


def save(name: str, obj):
    (DATA / name).write_text(json.dumps(obj, separators=(",", ":"), default=float))


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a / b - 1) * 100


def r2(x, n=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, n)


# -------------------------------------------------------------- indicators
def sma(v, n):
    return sum(v[-n:]) / n if len(v) >= n else None


def ema_series(v, n):
    k = 2 / (n + 1)
    out, e = [], None
    for x in v:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def rsi(v, n=14):
    """Wilder RSI on the full series, returns last value."""
    if len(v) <= n:
        return None
    gains = [max(v[i] - v[i - 1], 0) for i in range(1, len(v))]
    losses = [max(v[i - 1] - v[i], 0) for i in range(1, len(v))]
    ag = sum(gains[:n]) / n
    al = sum(losses[:n]) / n
    for g, l in zip(gains[n:], losses[n:]):
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def rsi_series(v, n=14):
    return [rsi(v[: i + 1], n) for i in range(len(v) - 3, len(v))]


def macd(v):
    e12, e26 = ema_series(v, 12), ema_series(v, 26)
    line = [a - b for a, b in zip(e12, e26)]
    sig = ema_series(line, 9)
    hist = [a - b for a, b in zip(line, sig)]
    return line, sig, hist


def candles(interval: str, lookback_ms: int):
    raw = hl({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": interval,
              "startTime": NOW_MS - lookback_ms, "endTime": NOW_MS}})
    return [{"t": c["t"], "o": float(c["o"]), "h": float(c["h"]), "l": float(c["l"]),
             "c": float(c["c"]), "v": float(c["v"])} for c in raw]


# ------------------------------------------------------------------ BTC
def btc_block():
    day = 86_400_000
    d1 = candles("1d", 400 * day)
    h4 = candles("4h", 120 * day)
    m15 = candles("15m", 2 * day)

    meta, ctxs = hl({"type": "metaAndAssetCtxs"})
    idx = next(i for i, u in enumerate(meta["universe"]) if u["name"] == "BTC")
    ctx = ctxs[idx]
    mark = float(ctx["markPx"])
    prev_day = float(ctx["prevDayPx"])

    closes_d = [c["c"] for c in d1]
    closes_4h = [c["c"] for c in h4]
    # replace last (in-progress) close with current mark for live readings
    closes_d[-1] = mark
    closes_4h[-1] = mark

    def close_ago(days):
        target = NOW_MS - days * day
        older = [c for c in h4 if c["t"] <= target]
        return older[-1]["c"] if older else None

    sma50 = sma(closes_d, 50)
    sma50_prev = sma(closes_d[:-5], 50)
    sma200 = sma(closes_d, 200)
    _, _, hist4 = macd(closes_4h)
    macd_bull = hist4[-1] > 0
    macd_cross = None
    if hist4[-1] > 0 >= hist4[-2] or hist4[-2] > 0 >= hist4[-3]:
        macd_cross = "bull"
    elif hist4[-1] < 0 <= hist4[-2] or hist4[-2] < 0 <= hist4[-3]:
        macd_cross = "bear"

    # session VWAP anchored at 00:00 UTC
    midnight = int(NOW.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    sess = [c for c in m15 if c["t"] >= midnight] or m15[-4:]
    pv = sum((c["h"] + c["l"] + c["c"]) / 3 * c["v"] for c in sess)
    vv = sum(c["v"] for c in sess)
    vwap = pv / vv if vv else None

    rsi4 = rsi(closes_4h)
    rsi4_prev = rsi(closes_4h[:-1])
    rsid = rsi(closes_d)

    above50 = sma50 is not None and mark > sma50
    rising50 = sma50_prev is not None and sma50 > sma50_prev
    if above50 and rising50 and macd_bull:
        regime = "Bullish"
    elif not above50 and not macd_bull:
        regime = "Bearish"
    elif not above50:
        regime = "Bearish lean"
    elif above50 and not macd_bull:
        regime = "Bullish lean"
    else:
        regime = "Neutral"

    funding_h = float(ctx["funding"])  # hourly rate on Hyperliquid
    hl_oi_usd = float(ctx["openInterest"]) * mark

    return {
        "price": mark,
        "chg24h": r2(pct(mark, prev_day)),
        "chg7d": r2(pct(mark, close_ago(7))),
        "chg30d": r2(pct(mark, close_ago(30))),
        "sma50": r2(sma50, 0),
        "sma200": r2(sma200, 0),
        "sma50_rising": rising50,
        "regime": regime,
        "macd_4h": "bull" if macd_bull else "bear",
        "macd_cross_4h": macd_cross,
        "rsi_4h": r2(rsi4, 1),
        "rsi_4h_prev": r2(rsi4_prev, 1),
        "rsi_1d": r2(rsid, 1),
        "vwap": r2(vwap, 0),
        "above_vwap": vwap is not None and mark > vwap,
        "hl_funding_hourly": funding_h,
        "hl_funding_apr": r2(funding_h * 24 * 365 * 100),
        "hl_oi_usd": hl_oi_usd,
        "spark_4h": [round(c, 1) for c in closes_4h[-42:]],  # last 7 days of 4H closes
    }


# ------------------------------------------------------------ derivatives
def okx_block():
    ls = get(f"{OKX}/rubik/stat/contracts/long-short-account-ratio?ccy=BTC&period=1H")["data"]
    oi = get(f"{OKX}/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=1H")["data"]
    fr = get(f"{OKX}/public/funding-rate?instId=BTC-USDT-SWAP")["data"][0]
    swap_oi = get(f"{OKX}/public/open-interest?instType=SWAP&instId=BTC-USDT-SWAP")["data"][0]
    ls_vals = [float(x[1]) for x in ls]  # newest first
    oi_vals = [float(x[1]) for x in oi]  # newest first, USD, all OKX BTC contracts
    ls_now = ls_vals[0]
    window = ls_vals[: 24 * 30]
    pctile = sum(1 for v in window if v <= ls_now) / len(window) * 100
    return {
        "ls_ratio": ls_now,
        "ls_ratio_24h_ago": ls_vals[24] if len(ls_vals) > 24 else None,
        "ls_pctile_30d": r2(pctile, 0),
        "ls_median_30d": sorted(window)[len(window) // 2],
        "oi_all_usd": oi_vals[0],
        "oi_all_chg24h": r2(pct(oi_vals[0], oi_vals[24])) if len(oi_vals) > 24 else None,
        "swap_oi_usd": float(swap_oi["oiUsd"]),
        "funding_8h": float(fr["fundingRate"]),
        "funding_apr": r2(float(fr["fundingRate"]) * 3 * 365 * 100),
    }


def deribit_block():
    d = get(f"{DERIBIT}/get_book_summary_by_instrument?instrument_name=BTC-PERPETUAL")["result"][0]
    return {
        "oi_usd": float(d["open_interest"]),  # BTC-PERPETUAL OI is quoted in USD
        "funding_8h": float(d.get("funding_8h") or 0),
        "funding_apr": r2(float(d.get("funding_8h") or 0) * 3 * 365 * 100),
    }


# ----------------------------------------------------------------- whales
def whales_block(price: float, lb):
    lb = sorted(lb, key=lambda r: float(r.get("accountValue") or 0), reverse=True)
    lb.sort(key=lambda r: float(r.get("accountValue") or 0), reverse=True)
    universe = lb[:WHALE_UNIVERSE]

    def pos(row):
        addr = row["ethAddress"]
        try:
            inf.LIMIT.take(2)
            st = hl({"type": "clearinghouseState", "user": addr})
        except Exception:  # noqa: BLE001
            return None
        for ap in st.get("assetPositions", []):
            p = ap.get("position", {})
            if p.get("coin") == "BTC":
                szi = float(p["szi"])
                ntl = abs(szi) * price
                if ntl >= WHALE_MIN_NTL:
                    return {"addr": addr, "name": row.get("displayName"), "szi": szi,
                            "ntl": ntl, "side": "long" if szi > 0 else "short",
                            "entry": float(p.get("entryPx") or 0),
                            "upnl": float(p.get("unrealizedPnl") or 0),
                            "lev": (p.get("leverage") or {}).get("value")}
        return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        found = [p for p in ex.map(pos, universe) if p]
    found.sort(key=lambda p: p["ntl"], reverse=True)
    top = found[:WHALE_COUNT]
    long_ntl = sum(p["ntl"] for p in top if p["side"] == "long")
    short_ntl = sum(p["ntl"] for p in top if p["side"] == "short")
    tot = long_ntl + short_ntl
    n_long = sum(1 for p in top if p["side"] == "long")
    return {
        "count": len(top),
        "scanned": len(universe),
        "long_pct_ntl": r2(long_ntl / tot * 100, 1) if tot else None,
        "long_pct_count": r2(n_long / len(top) * 100, 1) if top else None,
        "net_usd": long_ntl - short_ntl,
        "long_usd": long_ntl,
        "short_usd": short_ntl,
        "top": [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in p.items()} for p in top[:10]],
        "_holders": [p["addr"] for p in found],
    }


# --------------------------------------------------------- informed cohort
def informed_block(price: float, lb, holders=()):
    cache = load("informed_stats.json", {})
    pool = inf.candidate_pool(lb, holders)
    budget = int(os.getenv("INFORMED_REFRESH", str(inf.REFRESH_PER_RUN)))
    done, stale = inf.refresh_stats(hl_raw, cache, pool, NOW_MS, budget=budget, errors=ERRORS)
    save("informed_stats.json", cache)
    cohort = inf.build_cohort(pool, cache)
    prev = load("informed_positions.json", {})
    pos = inf.positioning(hl_raw, cohort, price, prev)
    save("informed_positions.json", pos.pop("all_szi"))
    scored = sum(1 for c in pool if c["addr"] in cache)
    pos.update({"pool": len(pool), "scored": scored, "refreshed_this_run": done, "stale_remaining": max(stale - done, 0),
                "criteria": {**inf.QUAL, "min_equity": inf.MIN_EQUITY, "lookback_days": inf.LOOKBACK_D,
                             "min_position": inf.MIN_POS_NTL}})
    return pos


def hl_raw(body):
    return hl(body)


# ------------------------------------------------------------------ macro
MACRO = [("SP500", "^GSPC"), ("VIX", "^VIX"), ("NASDAQ", "^IXIC"), ("DXY", "DX-Y.NYB"), ("US10Y", "^TNX")]


def macro_block():
    import yfinance as yf

    out = {}
    for name, sym in MACRO:
        try:
            t = yf.Ticker(sym)
            daily = t.history(period="10d", interval="1d")
            intra = t.history(period="5d", interval="15m")
            if daily.empty:
                raise RuntimeError("no daily data")
            last = float(intra["Close"].iloc[-1]) if not intra.empty else float(daily["Close"].iloc[-1])
            last_ts = (intra.index[-1] if not intra.empty else daily.index[-1]).to_pydatetime()
            # previous session close = last daily bar whose date is before the latest bar's date
            last_date = last_ts.date()
            prior = daily[[d.date() < last_date for d in daily.index]]
            prev_close = float(prior["Close"].iloc[-1]) if not prior.empty else float(daily["Close"].iloc[-2])
            cutoff = last_ts.timestamp() - 86400
            spark = [round(float(v), 3) for ts, v in intra["Close"].items() if ts.timestamp() >= cutoff]
            out[name] = {
                "symbol": sym,
                "last": round(last, 3),
                "prev_close": round(prev_close, 3),
                "chg_pct": r2(pct(last, prev_close)),
                "chg_abs": round(last - prev_close, 3),
                "as_of": last_ts.astimezone(timezone.utc).isoformat(),
                "spark": spark[-96:],
            }
        except Exception as e:  # noqa: BLE001
            ERRORS.append(f"macro {name}: {e}")
    return out


# --------------------------------------------------------------- signals
def evaluate_signals(b, okx, der, inf_, mac, oi_total_chg, inf_hist_4h=None):
    """Return list of signal dicts. Each: id, name, direction, active, detail."""
    sigs = []
    fund = b.get("hl_funding_apr")
    okx_f = okx.get("funding_apr") if okx else None
    funds = [f for f in (fund, okx_f, der.get("funding_apr") if der else None) if f is not None]
    avg_fund = sum(funds) / len(funds) if funds else None
    ls_p = okx.get("ls_pctile_30d") if okx else None
    px24 = b.get("chg24h")

    # 1. Crowded longs -> flush risk
    act = (avg_fund is not None and avg_fund > 30 and oi_total_chg is not None and oi_total_chg > 5
           and ls_p is not None and ls_p >= 80 and px24 is not None and px24 < 1)
    sigs.append({"id": "crowded_longs", "name": "Crowded longs", "direction": "short",
                 "active": bool(act),
                 "rule": "Avg funding > 30% APR, OI +5% in 24h, L/S in top 20% of 30d range, price stalling (< +1% 24h)",
                 "inputs": {"avg_funding_apr": r2(avg_fund), "oi_chg24h": oi_total_chg, "ls_pctile": ls_p, "price_24h": px24}})

    # 2. Short squeeze fuel
    act = (avg_fund is not None and avg_fund < 0 and oi_total_chg is not None and oi_total_chg > 3
           and px24 is not None and px24 >= -0.5)
    sigs.append({"id": "squeeze_fuel", "name": "Short squeeze fuel", "direction": "long",
                 "active": bool(act),
                 "rule": "Avg funding negative, OI +3% in 24h, price holding (>= -0.5% 24h)",
                 "inputs": {"avg_funding_apr": r2(avg_fund), "oi_chg24h": oi_total_chg, "price_24h": px24}})

    # 3. Informed traders vs crowd (hedge-adjusted, quality-filtered cohort)
    il = inf_.get("long_pct_ntl") if inf_ else None
    npos = inf_.get("positioned", 0) if inf_ else 0
    crowd_long = ls_p is not None and ls_p >= 60
    crowd_short = ls_p is not None and ls_p <= 40
    enough = npos >= 8
    if enough and il is not None and il <= 35 and crowd_long:
        d, act = "short", True
    elif enough and il is not None and il >= 65 and crowd_short:
        d, act = "long", True
    else:
        d, act = ("short" if (il if il is not None else 50) < 50 else "long"), False
    sigs.append({"id": "informed_vs_crowd", "name": "Informed traders vs crowd", "direction": d, "active": act,
                 "rule": "At least 8 informed traders positioned. Informed <= 35% long while crowd L/S in top 40% of its 30d range (short), or >= 65% long while crowd in bottom 40% (long)",
                 "inputs": {"informed_long_pct": il, "positioned": npos, "ls_pctile": ls_p}})

    # 3b. Informed flow shift over 4h
    il4 = inf_hist_4h
    shift = (il - il4) if (il is not None and il4 is not None) else None
    act = enough and shift is not None and abs(shift) >= 15
    sigs.append({"id": "informed_shift", "name": "Informed flow shift", "direction": "long" if (shift or 0) >= 0 else "short",
                 "active": bool(act),
                 "rule": "Informed traders' long share moved 15+ points in 4h (at least 8 positioned)",
                 "inputs": {"informed_long_pct": il, "four_h_ago": il4, "shift_pts": r2(shift, 1) if shift is not None else None}})

    # 4. Macro tailwind / headwind
    v, dx, ty = (mac.get(k, {}).get("chg_pct") for k in ("VIX", "DXY", "US10Y"))
    if None not in (v, dx, ty):
        tail = v < 0 and dx < 0 and ty < 0
        head = v > 0 and dx > 0 and ty > 0
        if tail:
            sigs.append({"id": "macro_tailwind", "name": "Macro tailwind", "direction": "long", "active": True,
                         "rule": "VIX, DXY and US10Y all down on the day",
                         "inputs": {"vix": v, "dxy": dx, "us10y": ty, "btc_24h": px24},
                         "note": "BTC lagging a risk-on tape" if (px24 or 0) < 0 else "BTC confirming"})
        else:
            sigs.append({"id": "macro_tailwind", "name": "Macro tailwind", "direction": "long", "active": False,
                         "rule": "VIX, DXY and US10Y all down on the day", "inputs": {"vix": v, "dxy": dx, "us10y": ty}})
        sigs.append({"id": "macro_headwind", "name": "Macro headwind", "direction": "short", "active": bool(head),
                     "rule": "VIX, DXY and US10Y all up on the day", "inputs": {"vix": v, "dxy": dx, "us10y": ty}})

    # 5. Momentum shift (4H)
    r_now, r_prev = b.get("rsi_4h"), b.get("rsi_4h_prev")
    cross = b.get("macd_cross_4h")
    bull = cross == "bull" and r_now is not None and r_now > 50 and b.get("above_vwap")
    bear = cross == "bear" and r_now is not None and r_now < 50 and not b.get("above_vwap")
    sigs.append({"id": "momentum_up", "name": "4H momentum turn up", "direction": "long", "active": bool(bull),
                 "rule": "4H MACD histogram flipped positive in last 2 bars, 4H RSI > 50, price above session VWAP",
                 "inputs": {"macd_cross": cross, "rsi_4h": r_now, "rsi_4h_prev": r_prev, "above_vwap": b.get("above_vwap")}})
    sigs.append({"id": "momentum_down", "name": "4H momentum turn down", "direction": "short", "active": bool(bear),
                 "rule": "4H MACD histogram flipped negative in last 2 bars, 4H RSI < 50, price below session VWAP",
                 "inputs": {"macd_cross": cross, "rsi_4h": r_now, "rsi_4h_prev": r_prev, "above_vwap": b.get("above_vwap")}})

    # 6. Spot-led (needs CVD stream; placeholder, never active)
    sigs.append({"id": "spot_led", "name": "Spot-led rally", "direction": "long", "active": False, "disabled": True,
                 "rule": "Spot CVD rising while perp CVD flat/unwinding and funding flat. Requires the spot vs perp flow stream (not yet enabled).",
                 "inputs": {}})
    return sigs


def update_signal_log(sigs, price, prev_active: set):
    log = load("signals.json", {"events": []})
    events = log["events"]
    # forward returns for open events
    hist = load("history.json", {"rows": []})["rows"]
    for ev in events:
        for h in (4, 24, 72):
            key = f"r{h}h"
            if ev.get(key) is not None:
                continue
            target = ev["t"] + h * 3600 * 1000
            if NOW_MS < target:
                continue
            # first history reading at/after target (fallback: current price if within 30 min)
            p = next((row["px"] for row in hist if row["t"] >= target), None)
            if p is None and NOW_MS - target < 30 * 60 * 1000:
                p = price
            if p is not None:
                raw = (p / ev["px"] - 1) * 100
                ev[key] = round(raw if ev["dir"] == "long" else -raw, 3)
    # new edge-triggered firings
    for s in sigs:
        if s["active"] and not s.get("disabled") and s["id"] not in prev_active:
            events.append({"id": s["id"], "name": s["name"], "dir": s["direction"], "t": NOW_MS,
                           "iso": NOW.isoformat(), "px": price, "inputs": s["inputs"]})
    log["events"] = events[-1000:]
    # stats per signal
    stats = {}
    for ev in log["events"]:
        st = stats.setdefault(ev["id"], {"name": ev["name"], "fires": 0, "r24": []})
        st["fires"] += 1
        if ev.get("r24h") is not None:
            st["r24"].append(ev["r24h"])
    log["stats"] = {k: {"name": v["name"], "fires": v["fires"], "scored_24h": len(v["r24"]),
                        "hit_rate_24h": r2(sum(1 for x in v["r24"] if x > 0) / len(v["r24"]) * 100, 1) if v["r24"] else None,
                        "avg_r24h": r2(sum(v["r24"]) / len(v["r24"]), 2) if v["r24"] else None}
                    for k, v in stats.items()}
    save("signals.json", log)
    return log


# ------------------------------------------------------------------- main
def main():
    btc = btc_block()  # hard requirement
    okx = safe("okx", okx_block, {})
    der = safe("deribit", deribit_block, {})
    lb = safe("leaderboard", lambda: get(LEADERBOARD, timeout=90)["leaderboardRows"], [])
    wh = safe("whales", lambda: whales_block(btc["price"], lb), {}) if lb else {}
    holders = wh.pop("_holders", []) if isinstance(wh, dict) else []
    infd = safe("informed", lambda: informed_block(btc["price"], lb, holders), {}) if lb else {}
    mac = safe("macro", macro_block, {})

    # combined OI across Hyperliquid + OKX (all BTC contracts) + Deribit perp
    oi_parts = {"hyperliquid": btc["hl_oi_usd"], "okx": okx.get("oi_all_usd"), "deribit": der.get("oi_usd")}
    oi_total = sum(v for v in oi_parts.values() if v)

    hist = load("history.json", {"rows": []})
    rows = [r for r in hist["rows"] if NOW_MS - r["t"] <= 80 * 3600 * 1000]
    rows.append({"t": NOW_MS, "px": btc["price"], "hl_oi": btc["hl_oi_usd"], "oi_total": oi_total,
                 "ls": okx.get("ls_ratio"), "fund": btc["hl_funding_apr"],
                 "whale_long": wh.get("long_pct_ntl"), "inf_long": infd.get("long_pct_ntl")})
    save("history.json", {"rows": rows})

    def ago(key, hours):
        tgt = NOW_MS - hours * 3600 * 1000
        older = [r for r in rows if r["t"] <= tgt + 15 * 60 * 1000 and r.get(key) is not None]
        return older[-1][key] if older and NOW_MS - older[-1]["t"] >= (hours - 1) * 3600 * 1000 else None

    hl_oi_24 = ago("hl_oi", 24)
    hl_oi_chg = r2(pct(btc["hl_oi_usd"], hl_oi_24)) if hl_oi_24 else None
    # total OI 24h change: from own history once available, else OKX's own 24h change as proxy
    tot_24 = ago("oi_total", 24)
    oi_total_chg = r2(pct(oi_total, tot_24)) if tot_24 else okx.get("oi_all_chg24h")
    oi_chg_source = "history" if tot_24 else "okx_proxy"

    sigs = evaluate_signals(btc, okx, der, infd, mac, oi_total_chg, ago("inf_long", 4))
    prev = load("snapshot.json", {})
    prev_active = {s["id"] for s in prev.get("signals", []) if s.get("active")}
    log = update_signal_log(sigs, btc["price"], prev_active)

    funds = {"hyperliquid": btc["hl_funding_apr"], "okx": okx.get("funding_apr"), "deribit": der.get("funding_apr")}
    weights = {"hyperliquid": btc["hl_oi_usd"], "okx": okx.get("swap_oi_usd"), "deribit": der.get("oi_usd")}
    num = sum(funds[k] * weights[k] for k in funds if funds[k] is not None and weights[k])
    den = sum(weights[k] for k in funds if funds[k] is not None and weights[k])

    snap = {
        "generated_at": NOW.isoformat(),
        "generated_ms": NOW_MS,
        "btc": btc,
        "funding": {"by_venue_apr": funds, "oi_weighted_apr": r2(num / den) if den else None},
        "oi": {"by_venue_usd": oi_parts, "total_usd": oi_total, "total_chg24h": oi_total_chg,
               "total_chg24h_source": oi_chg_source, "hl_chg24h": hl_oi_chg},
        "okx": okx,
        "deribit": der,
        "whales": wh,
        "informed": infd,
        "macro": mac,
        "signals": sigs,
        "signal_stats": log.get("stats", {}),
        "recent_events": log["events"][-25:][::-1],
        "errors": ERRORS,
    }
    save("snapshot.json", snap)
    print(json.dumps({"ok": True, "price": btc["price"], "errors": ERRORS,
                      "active": [s["id"] for s in sigs if s["active"]]}, indent=1))


if __name__ == "__main__":
    main()
