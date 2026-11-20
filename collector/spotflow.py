"""Spot flow: Coinbase vs Binance taker delta (CVD) and the Coinbase premium.

Writes data/tabs/spotflow.json (read by the dashboard) and keeps Coinbase hourly
buckets in data/tabs/cb_hourly.json.

Binance: data-api.binance.vision klines carry taker-buy volume, so delta = 2*takerBuy - volume.
Coinbase: public trades only. A trade's `side` is the MAKER side, so side == "sell"
means the taker bought. Trade ids are sequential, so we keep one contiguous range
[oldest_id, newest_id], extend it forward each run and backfill toward Jan 1 with
a time budget.
"""
from __future__ import annotations

import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "tabs"
OUT.mkdir(parents=True, exist_ok=True)
NOW = datetime.now(timezone.utc)
BN = "https://data-api.binance.vision/api/v3/klines"
CB = "https://api.exchange.coinbase.com/products/BTC-USD"
S = requests.Session()
S.headers["User-Agent"] = "curl/8.5.0"
BACKFILL_TO = datetime(NOW.year, 1, 1, tzinfo=timezone.utc)
FWD_BUDGET = 150      # seconds for catching up to now
BACK_BUDGET = 240     # seconds for history backfill per run (~20h of Coinbase trades)
KEEP_DAYS = 400


def r(x, n=2):
    if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))):
        return None
    return round(float(x), n)


def get(url, params=None, tries=4):
    for i in range(tries):
        try:
            q = S.get(url, params=params, timeout=(10, 25))
            if q.status_code == 429:
                time.sleep(1 + i)
                continue
            q.raise_for_status()
            return q
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(1 + i)


# ------------------------------------------------------------------ Coinbase trades
def cb_page(after):
    """Trades with trade_id < after (newest first)."""
    params = {"limit": 1000}
    if after is not None:
        params["after"] = after
    return get(CB + "/trades", params).json()


def add_trades(hours, trades):
    for t in trades:
        h = t["time"][:13]  # YYYY-MM-DDTHH
        b = hours.setdefault(h, [0.0, 0.0, 0])
        sz = float(t["size"])
        if t["side"] == "sell":   # maker sold -> taker bought
            b[0] += sz
        else:
            b[1] += sz
        b[2] += 1


def coinbase_sync():
    p = OUT / "cb_hourly.json"
    raw = json.loads(p.read_text()) if p.exists() else {}
    st = dict(raw.get("meta", {}), hours=raw.get("hours", {}))
    hours = st["hours"]
    newest, oldest = st.get("newest_id"), st.get("oldest_id")
    oldest_time = st.get("oldest_time")
    t0 = time.time()
    pages = 0
    if newest is None:
        top = cb_page(None)
        add_trades(hours, top)
        newest, oldest = top[0]["trade_id"], top[-1]["trade_id"]
        oldest_time = top[-1]["time"]
        pages += 1
    # forward: ids newest+1 .. latest, 1000 at a time
    caught_up = False
    while time.time() - t0 < FWD_BUDGET:
        page = cb_page(newest + 1001)
        pages += 1
        fresh = [t for t in page if t["trade_id"] > newest]
        if not fresh:
            caught_up = True
            break
        add_trades(hours, fresh)
        newest = max(t["trade_id"] for t in fresh)
        if len(fresh) < 1000:
            caught_up = True
            break
        time.sleep(0.12)
    # backward: older than oldest, until Jan 1 or budget
    t1 = time.time()
    done_back = oldest_time is not None and oldest_time < BACKFILL_TO.isoformat()
    while not done_back and time.time() - t1 < BACK_BUDGET:
        page = cb_page(oldest)
        pages += 1
        if not page:
            done_back = True
            break
        keep = [t for t in page if t["time"] >= BACKFILL_TO.isoformat()]
        add_trades(hours, keep)
        oldest = min(t["trade_id"] for t in page)
        oldest_time = min(t["time"] for t in page)
        if len(keep) < len(page):
            done_back = True
        time.sleep(0.12)
    cutoff = (NOW - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H")
    hours = {k: v for k, v in hours.items() if k >= cutoff}
    st = {"newest_id": newest, "oldest_id": oldest, "oldest_time": oldest_time, "caught_up": caught_up,
          "backfill_done": done_back, "updated": NOW.isoformat(), "pages_last_run": pages,
          "hours": {k: [round(v[0], 4), round(v[1], 4), v[2]] for k, v in sorted(hours.items())}}
    # one hour per line so git stores small diffs
    meta = {k: v for k, v in st.items() if k != "hours"}
    lines = ['"%s":%s' % (k, json.dumps(v, separators=(",", ":"))) for k, v in st["hours"].items()]
    p.write_text('{"meta":' + json.dumps(meta) + ',\n"hours":{\n' + ",\n".join(lines) + "\n}}\n")
    return st


# ------------------------------------------------------------------ Binance klines
def bn_klines(interval, start_ms=None, limit=1000):
    out = []
    params = {"symbol": "BTCUSDT", "interval": interval, "limit": limit}
    if start_ms is not None:
        params["startTime"] = start_ms
    while True:
        d = get(BN, params).json()
        out += d
        if start_ms is None or len(d) < limit:
            break
        params["startTime"] = d[-1][0] + 1
    return out


def bn_daily():
    start = int((NOW - timedelta(days=760)).timestamp() * 1000)
    rows = []
    for k in bn_klines("1d", start):
        v, tb = float(k[5]), float(k[9])
        rows.append({"date": datetime.fromtimestamp(k[0] / 1000, timezone.utc).date().isoformat(),
                     "o": float(k[1]), "c": float(k[4]), "vol": v, "delta": 2 * tb - v})
    return rows


def bn_hourly(hours=48):
    rows = []
    for k in bn_klines("1h", None, hours):
        v, tb = float(k[5]), float(k[9])
        rows.append({"t": k[0], "vol": v, "delta": 2 * tb - v, "c": float(k[4])})
    return rows


def cb_daily_candles(days=400):
    out, end = {}, NOW
    while (NOW - end).days < days:
        st = end - timedelta(days=299)
        d = get(CB + "/candles", {"granularity": 86400, "start": st.isoformat(), "end": end.isoformat()}).json()
        for t, lo, hi, o, c, v in d:
            out[datetime.fromtimestamp(t, timezone.utc).date().isoformat()] = {"o": o, "c": c, "vol": v}
        end = st
        time.sleep(0.3)
    return out


# ------------------------------------------------------------------ reading
def pctile(hist, x):
    hist = [h for h in hist if h is not None]
    if x is None or len(hist) < 20:
        return None
    return sum(1 for h in hist if h < x) / len(hist) * 100


def window_stats(daily, n):
    """Rolling n-day net delta % of volume for each day (needs n days)."""
    out = []
    for i in range(len(daily)):
        if i + 1 < n:
            out.append(None)
            continue
        w = daily[i + 1 - n:i + 1]
        vol = sum(x["vol"] for x in w)
        out.append(sum(x["delta"] for x in w) / vol * 100 if vol else None)
    return out


def venue_read(daily, today_partial=None, min_hist=30):
    """Classify the last 1/3/7 complete days vs this venue's own trailing-year history."""
    if len(daily) < 8:
        return None
    res = {}
    for n in (1, 3, 7):
        series = window_stats(daily, n)
        cur = series[-1]
        hist = [x for x in series[-366:-1] if x is not None]
        p = pctile(hist, cur) if len(hist) >= min_hist else None
        if p is not None:
            lean = "buyers" if p >= 70 else "sellers" if p <= 30 else "balanced"
        else:  # not enough history yet: fall back to raw sign with a band
            lean = "buyers" if cur is not None and cur > 2 else "sellers" if cur is not None and cur < -2 else "balanced"
        w = daily[-n:]
        res[f"d{n}"] = {"net_pct": r(cur, 2), "net_btc": r(sum(x["delta"] for x in w), 0), "pctile": r(p, 0),
                        "median_pct": r(sorted(hist)[len(hist) // 2], 2) if hist else None, "lean": lean,
                        "price_chg": r((daily[-1]["c"] / daily[-1 - n]["c"] - 1) * 100, 2) if len(daily) > n and daily[-1 - n].get("c") else None,
                        "basis": "own 1y history" if p is not None else "raw sign (history still filling)"}
    if today_partial:
        res["today"] = today_partial
    return res


def overall(bn, cb, prem):
    score, why = 0, []
    def lean_pts(v, name):
        nonlocal score
        if not v:
            return
        l7, l3 = v["d7"]["lean"], v["d3"]["lean"]
        pts = {"buyers": 1, "sellers": -1, "balanced": 0}
        s = pts[l7] + 0.5 * pts[l3]
        score += s
        if l7 != "balanced" or l3 != "balanced":
            why.append(f"{name}: {l7} over 7 days, {l3} over 3 days")
    lean_pts(cb, "Coinbase")
    lean_pts(bn, "Binance")
    if prem and prem.get("avg7") is not None:
        p, pp = prem["avg7"], prem.get("pctile7")
        if p < 0 and (pp is None or pp <= 30):
            score -= 1
            why.append(f"Coinbase trading at a {abs(p):.3f}% discount to Binance (7-day avg): US spot demand weak")
        elif p > 0 and (pp is None or pp >= 70):
            score += 1
            why.append(f"Coinbase premium +{p:.3f}% (7-day avg): US spot demand strong")
    if score <= -1.5:
        label = "Bearish flow"
    elif score <= -0.5:
        label = "Leaning bearish"
    elif score >= 1.5:
        label = "Bullish flow"
    elif score >= 0.5:
        label = "Leaning bullish"
    else:
        label = "Neutral"
    # divergence note on 7d
    notes = []
    for name, v in (("Coinbase", cb), ("Binance", bn)):
        if not v or v["d7"]["price_chg"] is None:
            continue
        pc, l = v["d7"]["price_chg"], v["d7"]["lean"]
        if pc <= -3 and l == "buyers":
            notes.append(f"{name}: price down {abs(pc):.1f}% over 7 days while takers net bought (absorption: dip being bought)")
        if pc >= 3 and l == "sellers":
            notes.append(f"{name}: price up {pc:.1f}% over 7 days while takers net sold (rally not backed by spot buying)")
        if pc <= -3 and l == "sellers":
            notes.append(f"{name}: selling confirms the drop (price down {abs(pc):.1f}% with net taker selling)")
        if pc >= 3 and l == "buyers":
            notes.append(f"{name}: buying confirms the rally (price up {pc:.1f}% with net taker buying)")
        if abs(pc) >= 5 and l == "balanced":
            notes.append(f"{name}: price {'down' if pc < 0 else 'up'} {abs(pc):.1f}% over 7 days but spot taker flow was normal for this venue, so the move was likely led by derivatives or other venues, not {name} spot")
    return {"label": label, "score": score, "why": why, "notes": notes}


def main():
    errors = []
    # Coinbase trade buckets
    try:
        cbst = coinbase_sync()
    except Exception as e:  # noqa: BLE001
        errors.append(f"coinbase trades: {e}")
        p = OUT / "cb_hourly.json"
        raw = json.loads(p.read_text()) if p.exists() else None
        cbst = dict(raw.get("meta", {}), hours=raw.get("hours", {})) if raw else None

    bnd = bn_daily()
    bnh = bn_hourly(48)
    try:
        cbc = cb_daily_candles(400)
    except Exception as e:  # noqa: BLE001
        errors.append(f"coinbase candles: {e}")
        cbc = {}

    today = NOW.date().isoformat()
    bn_done = [x for x in bnd if x["date"] < today]
    bn_today = next((x for x in bnd if x["date"] == today), None)

    # Coinbase daily from hourly buckets (only fully covered days)
    cb_days, cb_hours = [], []
    cov_from = None
    if cbst and cbst.get("hours"):
        hrs = cbst["hours"]
        cov_from = cbst.get("oldest_time")
        first_full = (datetime.fromisoformat(cov_from.replace("Z", "+00:00")) + timedelta(days=1)).date().isoformat() if cov_from else None
        agg = {}
        for k, (b, s, nn) in hrs.items():
            d = k[:10]
            a = agg.setdefault(d, [0.0, 0.0])
            a[0] += b
            a[1] += s
        for d in sorted(agg):
            if (first_full and d < first_full) or d >= today:
                continue
            b, s = agg[d]
            c = cbc.get(d, {}).get("c")
            cb_days.append({"date": d, "vol": b + s, "delta": b - s, "c": c})
        cut = (NOW - timedelta(hours=48)).strftime("%Y-%m-%dT%H")
        cb_hours = [{"h": k, "delta": r(v[0] - v[1], 1), "vol": r(v[0] + v[1], 1)} for k, v in sorted(hrs.items()) if k >= cut]
        tb = agg.get(today)
        cb_today = {"net_btc": r(tb[0] - tb[1], 0), "net_pct": r((tb[0] - tb[1]) / (tb[0] + tb[1]) * 100, 2) if tb and tb[0] + tb[1] else None} if tb and cbst.get("caught_up") else None
    else:
        cb_today = None
    bn_today_d = {"net_btc": r(bn_today["delta"], 0), "net_pct": r(bn_today["delta"] / bn_today["vol"] * 100, 2)} if bn_today and bn_today["vol"] else None

    bn_read = venue_read(bn_done, bn_today_d)
    cb_read = venue_read(cb_days, cb_today, min_hist=60) if len(cb_days) >= 8 else None

    # Coinbase premium (USD vs USDT; small USDT basis can bias it)
    prem = None
    pser = []
    for x in bn_done[-400:]:
        c = cbc.get(x["date"], {}).get("c")
        if c:
            pser.append([x["date"], r((c / x["c"] - 1) * 100, 3)])
    try:
        cbt = float(get(CB + "/ticker").json()["price"])
        bnt = float(get("https://data-api.binance.vision/api/v3/ticker/price", {"symbol": "BTCUSDT"}).json()["price"])
        live = (cbt / bnt - 1) * 100
    except Exception as e:  # noqa: BLE001
        errors.append(f"premium live: {e}")
        live = None
    if pser:
        last7 = [v for _, v in pser[-7:]]
        avg7 = sum(last7) / len(last7)
        hist7 = []
        vals = [v for _, v in pser]
        for i in range(7, len(vals)):
            hist7.append(sum(vals[i - 7:i]) / 7)
        prem = {"live": r(live, 3), "last": pser[-1][1], "avg7": r(avg7, 3), "pctile7": r(pctile(hist7[-365:], avg7), 0),
                "series": pser[-120:]}

    # YTD CVD series for the chart
    ytd = f"{NOW.year}-01-01"
    def cvd(days):
        s, out = 0.0, []
        for x in days:
            if x["date"] >= ytd:
                s += x["delta"]
                out.append([x["date"], r(s, 0)])
        return out
    bn_cvd = cvd(bn_done)
    cb_cvd = cvd(cb_days)

    ov = overall(bn_read, cb_read, prem)
    out = {
        "generated_at": NOW.isoformat(), "generated_ms": int(NOW.timestamp() * 1000),
        "reading": ov,
        "binance": {"read": bn_read, "cvd_ytd": bn_cvd, "daily": [[x["date"], r(x["delta"], 0), r(x["delta"] / x["vol"] * 100, 2)] for x in bn_done[-90:]],
                    "hourly": [[x["t"], r(x["delta"], 1)] for x in bnh]},
        "coinbase": {"read": cb_read, "cvd": cb_cvd, "daily": [[x["date"], r(x["delta"], 0), r(x["delta"] / x["vol"] * 100, 2) if x["vol"] else None] for x in cb_days[-90:]],
                     "hourly": cb_hours, "coverage_from": cov_from, "backfill_done": bool(cbst and cbst.get("backfill_done")),
                     "caught_up": bool(cbst and cbst.get("caught_up")), "days": len(cb_days)},
        "premium": prem,
        "evidence": ("Tested on Binance spot since 2020: daily and 7-day taker delta had near-zero correlation with BTC's next 1, 7 and 30-day returns, "
                     "and price/flow divergences did no better than the base rate. The Coinbase premium had a modest edge in 2020-22 "
                     "(premium weeks +2.5% next 7 days vs +0.7% in discount weeks) that has faded since 2023. "
                     "Read this as who is in control now, not a forecast."),
        "method": ("Delta = taker buy volume minus taker sell volume, in BTC. Binance from 1-day klines (taker-buy field); Coinbase summed from every public trade "
                   "(maker side 'sell' = taker buy). Each venue is judged against its own past year because Binance spot delta runs negative most days "
                   "(median about -1.5% of volume), so a falling Binance CVD line by itself is normal. Buyers = 7-day net delta in the top 30% of the past year; "
                   "sellers = bottom 30%. Premium compares Coinbase BTC-USD to Binance BTC-USDT closes."),
        "errors": errors,
    }
    (OUT / "spotflow.json").write_text(json.dumps(out, separators=(",", ":"), allow_nan=False))
    print(json.dumps({"label": ov["label"], "score": ov["score"], "cb_days": len(cb_days), "cb_from": cov_from, "errors": errors}))


if __name__ == "__main__":
    main()
