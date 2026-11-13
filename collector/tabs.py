"""Goblin Alpha: data for the non-snapshot tabs.

Runs after snapshot.py in the same loop. Writes data/tabs/*.json:

  oi_hist.json    every run   Hyperliquid OI per perp (26h, 10-min steps) for OI velocity
  weekly.json     hourly      BTC weekly-pattern stats + current week state (Weekly Predictor)
  setups.json     hourly      BTC weekly/daily setups: live status + recomputed backtests
  macro.json      hourly      OFR Financial Stress Index, FRED credit/curve, VIX term, BTC by stress regime
  screener.json   hourly      Confluence screener (200 EMA d/w, FVG, volume POC, crossover, displacement)

All sources are free and keyless: Hyperliquid, Yahoo Finance, OFR, FRED.
Each hourly job keeps its previous file if a source fails.
"""
from __future__ import annotations

import io
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "tabs"
OUT.mkdir(parents=True, exist_ok=True)
HL = "https://api.hyperliquid.xyz/info"
S = requests.Session()
S.headers["User-Agent"] = "Mozilla/5.0 goblin-alpha-collector/1.0"
NOW = datetime.now(timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)
ERRORS: list[str] = []


def load(name, default):
    p = OUT / name
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            pass
    return default


def save(name, obj):
    (OUT / name).write_text(json.dumps(obj, separators=(",", ":"), default=float, allow_nan=False))


def due(name, minutes):
    d = load(name, {})
    try:
        t = datetime.fromisoformat(d["generated_at"])
        return (NOW - t).total_seconds() > minutes * 60
    except Exception:  # noqa: BLE001
        return True


def r(x, n=2):
    if x is None:
        return None
    try:
        if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
            return None
        return round(float(x), n)
    except Exception:  # noqa: BLE001
        return None


def hl(body):
    for i in range(3):
        try:
            q = S.post(HL, json=body, timeout=25)
            if q.status_code == 429:
                time.sleep(3 + 3 * i)
                continue
            q.raise_for_status()
            return q.json()
        except Exception:  # noqa: BLE001
            if i == 2:
                raise
            time.sleep(2)
    raise RuntimeError("hyperliquid retries exhausted")


def csv_get(url, tries=3):
    import pandas as pd
    last = None
    for i in range(tries):
        try:
            q = requests.get(url, timeout=(10, 25), headers={"User-Agent": "curl/8.5.0"})
            q.raise_for_status()
            return pd.read_csv(io.StringIO(q.text))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(3 + 3 * i)
    raise last


# ------------------------------------------------------------ OI history
def oi_history():
    meta, ctxs = hl({"type": "metaAndAssetCtxs"})
    oi = {}
    for a, c in zip(meta["universe"], ctxs):
        if a.get("isDelisted"):
            continue
        try:
            px = float(c.get("markPx") or 0)
            oi[a["name"]] = int(float(c["openInterest"]) * px)
        except Exception:  # noqa: BLE001
            continue
    hist = load("oi_hist.json", {"rows": []})
    rows = [x for x in hist.get("rows", []) if NOW_MS - x["t"] < 26 * 3600 * 1000]
    rows.append({"t": NOW_MS, "oi": oi})
    save("oi_hist.json", {"generated_at": NOW.isoformat(), "rows": rows})


# ------------------------------------------------------------ BTC daily data
def btc_daily():
    import yfinance as yf
    df = yf.Ticker("BTC-USD").history(period="max", interval="1d", auto_adjust=False)
    if df.empty or len(df) < 1500:
        raise RuntimeError("BTC daily history unavailable")
    df = df[["Open", "High", "Low", "Close"]].copy()
    df.index = [d.date() for d in df.index.tz_convert("UTC")] if df.index.tz is not None else [d.date() for d in df.index]
    return df


def build_weeks(df, start="2017-01-02"):
    """Group daily bars into UTC Monday-Sunday weeks. Returns list of dicts."""
    from datetime import date
    s = date.fromisoformat(start)
    weeks = {}
    for d, row in df.iterrows():
        if d < s:
            continue
        mon = d - timedelta(days=d.weekday())
        weeks.setdefault(mon, []).append((d.weekday(), d, row))
    out = []
    for mon in sorted(weeks):
        days = sorted(weeks[mon], key=lambda x: x[0])
        if days[0][0] != 0:
            continue
        o = float(days[0][2]["Open"])
        bars = {wd: {"date": d.isoformat(), "o": float(rw["Open"]), "h": float(rw["High"]),
                     "l": float(rw["Low"]), "c": float(rw["Close"])} for wd, d, rw in days}
        out.append({"mon": mon.isoformat(), "open": o, "bars": bars, "complete": 6 in bars and len(bars) == 7 and bars[6]["date"] < NOW.date().isoformat()})
    return out


def binom_p(k, n, p=0.5):
    """One-sided P(X >= k) for Binomial(n, p)."""
    if n == 0:
        return None
    from math import comb
    return sum(comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


def summarize(trades, recent_from):
    """trades: list of (date_str, ret). Win = ret > 0."""
    n = len(trades)
    if n == 0:
        return {"n": 0}
    rets = sorted(t[1] for t in trades)
    wins = sum(1 for t in trades if t[1] > 0)
    rec = [t for t in trades if t[0] >= recent_from]
    rw = sum(1 for t in rec if t[1] > 0)
    return {
        "n": n, "wins": wins, "win_rate": r(wins / n * 100, 1),
        "avg": r(sum(rets) / n * 100, 2), "median": r(rets[n // 2] * 100, 2),
        "worst": r(rets[0] * 100, 2), "best": r(rets[-1] * 100, 2),
        "p_value": r(binom_p(wins, n), 4),
        "recent_n": len(rec), "recent_win_rate": r(rw / len(rec) * 100, 1) if rec else None,
        "recent_avg": r(sum(t[1] for t in rec) / len(rec) * 100, 2) if rec else None,
        "last": [{"date": d, "ret": r(x * 100, 2)} for d, x in trades[-6:]][::-1],
    }


def verdict(st):
    if not st or st.get("n", 0) < 10:
        return "too few"
    good = st["win_rate"] >= 58 and st["avg"] > 0 and (st["p_value"] or 1) < 0.05
    recent_ok = st["recent_n"] < 5 or (st["recent_avg"] or 0) > 0
    if good and st["n"] < 20:
        return "small sample"
    if good and recent_ok:
        return "holds up"
    if good and not recent_ok:
        return "fading"
    if st["avg"] > 0 and st["win_rate"] >= 52:
        return "weak"
    return "no edge"


def weekly_job(df):
    weeks = build_weeks(df)
    done = [w for w in weeks if w["complete"]]
    cur = weeks[-1] if weeks and not weeks[-1]["complete"] else None
    recent_from = (NOW.date() - timedelta(days=730)).isoformat()
    names = ["Mon", "Tue", "Wed", "Thu"]
    base_red = sum(1 for w in done if w["bars"][6]["c"] < w["open"]) / len(done) * 100

    rows = []
    for d in range(4):
        bear, bull = [], []
        for w in done:
            b = w["bars"]
            if any(i not in b for i in range(d + 1)):
                continue
            hi = max(b[i]["h"] for i in range(d + 1))
            cd = b[d]["c"]
            close = b[6]["c"]
            rem = close / cd - 1
            red = close < w["open"]
            rec = (w["mon"], rem, red)
            (bear if hi <= w["open"] * 1.01 else bull).append(rec)
        def pack(lst, want_red):
            n = len(lst)
            hits = sum(1 for x in lst if x[2] == want_red)
            # tradable remainder: short for bear, long for bull
            sign = -1 if want_red else 1
            tr = [(x[0], sign * x[1]) for x in lst]
            s = summarize(tr, recent_from)
            s.update({"week_hit": r(hits / n * 100, 1) if n else None, "week_hits": hits})
            return s
        rows.append({"day": names[d], "bear": pack(bear, True), "bull": pack(bull, False)})

    # year by year, Wednesday bear rule
    years = {}
    for w in done:
        b = w["bars"]
        if any(i not in b for i in range(3)):
            continue
        if max(b[i]["h"] for i in range(3)) <= w["open"] * 1.01:
            y = w["mon"][:4]
            yy = years.setdefault(y, [0, 0])
            yy[0] += 1
            yy[1] += b[6]["c"] < w["open"]
    by_year = [{"year": y, "n": v[0], "red_pct": r(v[1] / v[0] * 100, 1)} for y, v in sorted(years.items())]

    state = None
    if cur:
        b = cur["bars"]
        last_d = max(b)
        state = {"mon": cur["mon"], "open": r(cur["open"], 2),
                 "high": r(max(x["h"] for x in b.values()), 2), "low": r(min(x["l"] for x in b.values()), 2),
                 "last_close": r(b[last_d]["c"], 2), "last_day": last_d,
                 "bars": [{"wd": k, **{kk: r(vv, 2) if kk != "date" else vv for kk, vv in v.items()}} for k, v in sorted(b.items())]}
    save("weekly.json", {"generated_at": NOW.isoformat(), "weeks": len(done), "since": done[0]["mon"],
                         "base_red_pct": r(base_red, 1), "rows": rows, "by_year": by_year, "current": state,
                         "rule": "Bearish read: by the day's close, BTC has not traded more than 1% above Monday's UTC open. Bullish read: it has."})
    return weeks


def setups_job(df, weeks):
    done = [w for w in weeks if w["complete"]]
    cur = weeks[-1] if weeks and not weeks[-1]["complete"] else None
    recent_from = (NOW.date() - timedelta(days=730)).isoformat()
    out = []

    def add(sid, name, tier_note, rules, trade, trades, status, detail, extra=None):
        st = summarize(trades, recent_from)
        out.append({"id": sid, "name": name, "history_note": tier_note, "rules": rules, "trade": trade,
                    "stats": st, "verdict": verdict(st), "status": status, "status_detail": detail, **(extra or {})})

    # A. Tight Low: by Thu close, no dip > 0.25% below Monday open -> long Thu close -> Sun close
    tr = []
    for w in done:
        b = w["bars"]
        if all(i in b for i in range(4)) and min(b[i]["l"] for i in range(4)) >= w["open"] * 0.9975:
            tr.append((w["mon"], b[6]["c"] / b[3]["c"] - 1))
    st, det = "inactive", ""
    if cur:
        b = cur["bars"]
        lo = min(x["l"] for x in b.values())
        held = lo >= cur["open"] * 0.9975
        last_d = max(b)
        if not held:
            st, det = "off", f"Broke {r((lo / cur['open'] - 1) * 100)}% below Monday's open this week"
        elif last_d < 3:
            st, det = "armed", f"Low has held within 0.25% of Monday's open ({r(cur['open'])}) so far; confirms at Thursday close"
        elif last_d == 3:
            st, det = "triggered", "Held through Thursday: long into Sunday close"
        else:
            st, det = "in trade", "Held through Thursday: long into Sunday close"
    add("tight_low", "Tight Low", "Old claim: 47/47 green weeks",
        "By Thursday's close, BTC has never traded more than 0.25% below Monday's UTC open.",
        "Long at Thursday close, stop just under Monday's open, exit Sunday close.", tr, st, det)

    # B. Wednesday Momentum: Wed close >= +3% (tier 5%) above Monday open -> long Wed close -> Sun close
    for thr, sid in ((0.03, "wed_mom_3"), (0.05, "wed_mom_5")):
        tr = [(w["mon"], w["bars"][6]["c"] / w["bars"][2]["c"] - 1) for w in done
              if 2 in w["bars"] and w["bars"][2]["c"] >= w["open"] * (1 + thr)]
        st, det = "inactive", ""
        if cur:
            b = cur["bars"]; last_d = max(b)
            gain = b[min(last_d, 2)]["c"] / cur["open"] - 1
            if last_d < 2:
                st, det = ("armed" if gain >= thr else "watching"), f"Week so far {r(gain * 100)}% vs +{int(thr * 100)}% needed at Wednesday close"
            else:
                g = b[2]["c"] / cur["open"] - 1
                st = ("in trade" if last_d > 2 else "triggered") if g >= thr else "off"
                det = f"Wednesday closed {r(g * 100)}% vs Monday open"
        add(sid, f"Wednesday Momentum +{int(thr * 100)}%", "Old claim: 86% / 91% green weeks",
            f"Wednesday closes at least {int(thr * 100)}% above Monday's UTC open.",
            "Long at Wednesday close, exit Sunday close.", tr, st, det)

    # C. Wednesday no-breakout short (Weekly Predictor bear rule as a trade)
    tr = []
    for w in done:
        b = w["bars"]
        if all(i in b for i in range(3)) and max(b[i]["h"] for i in range(3)) <= w["open"] * 1.01:
            tr.append((w["mon"], -(b[6]["c"] / b[2]["c"] - 1)))
    st, det = "inactive", ""
    if cur:
        b = cur["bars"]; last_d = max(b)
        hi = max(b[i]["h"] for i in b if i <= 2)
        if hi > cur["open"] * 1.01:
            st, det = "off", f"BTC traded {r((hi / cur['open'] - 1) * 100)}% above Monday's open"
        elif last_d < 2:
            st, det = "armed", "No push above +1% yet; confirms at Wednesday close"
        else:
            st, det = ("triggered" if last_d == 2 else "in trade"), "No +1% push through Wednesday"
    add("wed_no_breakout", "Wednesday No-Breakout Short", "Old claim: 85% red weeks",
        "Through Wednesday's close, BTC never traded more than 1% above Monday's UTC open.",
        "Short at Wednesday close, exit Sunday close.", tr, st, det)

    # D. Tuesday erases Monday: Monday green -> short Monday close, cover Tuesday close
    all_tr, since = [], []
    for w in done:
        b = w["bars"]
        if 0 in b and 1 in b and b[0]["c"] > w["open"]:
            x = (w["mon"], -(b[1]["c"] / b[0]["c"] - 1))
            all_tr.append(x)
            if w["mon"] >= "2025-10-01":
                since.append(x)
    st, det = "inactive", ""
    if cur:
        b = cur["bars"]; last_d = max(b)
        if 0 in b and last_d >= 0:
            if b[0]["c"] > cur["open"] and last_d == 0:
                st, det = "triggered", "Monday closed green: short into Tuesday close"
            elif b[0]["c"] > cur["open"] and last_d >= 1:
                st, det = "done", f"Tuesday {'erased' if b[1]['c'] < b[0]['c'] else 'extended'} Monday's move"
            else:
                st, det = "off", "Monday did not close green"
    add("tue_erase", "Tuesday Fade of a Green Monday", "Old claim: 85% red Tuesdays since Oct 2025",
        "Monday closes above its UTC open.", "Short at Monday close, cover at Tuesday close.", all_tr, st, det,
        {"regime_stats": summarize(since, "2025-10-01"), "regime_label": "Since Oct 2025"})

    # E. Sunday Erasure: week up through Saturday, Sunday closes back below Monday open -> long next week
    tr = []
    for i, w in enumerate(done[:-1]):
        b = w["bars"]
        if b[5]["c"] > w["open"] and b[6]["c"] < w["open"]:
            nx = done[i + 1]
            tr.append((nx["mon"], nx["bars"][6]["c"] / nx["open"] - 1))
    prev = done[-1] if done else None
    st, det = "inactive", ""
    if prev and cur:
        b = prev["bars"]
        if b[5]["c"] > prev["open"] and b[6]["c"] < prev["open"]:
            st, det = "in trade", "Last Sunday erased the week's gains: long this week"
        else:
            st, det = "off", "Last week did not have a Sunday erasure"
    add("sun_erasure", "Sunday Erasure", "Old claim: 70% next week green",
        "Week is green through Saturday, then Sunday closes back below Monday's open.",
        "Long next Monday open, exit Sunday close.", tr, st, det)

    # F. Red streak into Sunday: >=5 consecutive red daily closes ending Sunday -> long next week
    closes = list(df.itertuples())
    idx = {row.Index: k for k, row in enumerate(closes)}
    tr = []
    for i, w in enumerate(done[:-1]):
        sun = datetime.fromisoformat(w["bars"][6]["date"]).date()
        k = idx.get(sun)
        if k is None:
            continue
        streak = 0
        while k - streak >= 0 and closes[k - streak].Close < closes[k - streak].Open:
            streak += 1
        if streak >= 5:
            nx = done[i + 1]
            tr.append((nx["mon"], nx["bars"][6]["c"] / nx["open"] - 1))
    st, det = "inactive", ""
    if prev and cur:
        sun = datetime.fromisoformat(prev["bars"][6]["date"]).date()
        k = idx.get(sun); streak = 0
        while k is not None and k - streak >= 0 and closes[k - streak].Close < closes[k - streak].Open:
            streak += 1
        st, det = ("in trade", f"{streak} red days into last Sunday: long this week") if streak >= 5 else ("off", f"Red streak into last Sunday: {streak} days")
    add("red_streak", "Red Streak Sunday Bounce", "Old claim: 77% win at 6+ red days",
        "Five or more consecutive red daily candles ending on Sunday.",
        "Long next Monday open, exit Sunday close.", tr, st, det)

    # G. Red Sunday Reclaim: week A green through Sat, Sunday <= -2.5%, week B >= +5% -> long week C
    tr = []
    for i in range(len(done) - 2):
        a, bb, c = done[i], done[i + 1], done[i + 2]
        sa = a["bars"][6]["c"] / a["bars"][5]["c"] - 1
        if a["bars"][5]["c"] > a["open"] and sa <= -0.025 and bb["bars"][6]["c"] / bb["open"] - 1 >= 0.05:
            tr.append((c["mon"], c["bars"][6]["c"] / c["open"] - 1))
    st, det = "inactive", ""
    if len(done) >= 2 and cur:
        a, bb = done[-2], done[-1]
        sa = a["bars"][6]["c"] / a["bars"][5]["c"] - 1
        if a["bars"][5]["c"] > a["open"] and sa <= -0.025 and bb["bars"][6]["c"] / bb["open"] - 1 >= 0.05:
            st, det = "in trade", "Green week, red Sunday, then a +5% reclaim week: long this week"
        else:
            st, det = "off", "Sequence not present in the last two weeks"
    add("red_sun_reclaim", "Red Sunday Reclaim", "Old claim: 9/10 at loose thresholds",
        "Week A green through Saturday, Sunday drops 2.5%+, then week B gains 5%+.",
        "Long week C from Monday open to Sunday close.", tr, st, det)

    save("setups.json", {"generated_at": NOW.isoformat(), "since": done[0]["mon"] if done else None,
                         "recent_from": recent_from, "setups": out,
                         "method": "Recomputed from Yahoo Finance BTC-USD daily candles in UTC weeks. Returns are the tradable part only (entry at the trigger close, exit at the stated close), before fees and funding. Win = positive return."})


# ------------------------------------------------------------ macro
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="


def fred(series):
    import pandas as pd
    d = csv_get(FRED + series)
    d.columns = ["date", "v"]
    d["v"] = pd.to_numeric(d["v"], errors="coerce")
    d = d.dropna()
    d["date"] = pd.to_datetime(d["date"])
    return d.set_index("date")["v"]


def macro_job():
    import pandas as pd
    import yfinance as yf
    prev = load("macro.json", {})
    out = {"generated_at": NOW.isoformat(), "errors": []}

    # OFR FSI (daily, with components)
    try:
        f = csv_get("https://www.financialresearch.gov/financial-stress-index/data/fsi.csv")
        f["Date"] = pd.to_datetime(f["Date"])
        f = f.set_index("Date").sort_index()
        s = f["OFR FSI"].dropna()
        last = float(s.iloc[-1])
        pctl = float((s < last).mean() * 100)
        def ago(days):
            t = s.index[-1] - pd.Timedelta(days=days)
            x = s[s.index <= t]
            return float(x.iloc[-1]) if len(x) else None
        comps = {c: r(float(f[c].dropna().iloc[-1]), 3) for c in ["Credit", "Equity valuation", "Safe assets", "Funding", "Volatility"]}
        yr = s[s.index >= s.index[-1] - pd.Timedelta(days=365)]
        out["fsi"] = {"value": r(last, 3), "date": s.index[-1].date().isoformat(), "pctile": r(pctl, 1),
                      "chg_1w": r(last - ago(7), 3) if ago(7) is not None else None,
                      "chg_1m": r(last - ago(30), 3) if ago(30) is not None else None,
                      "components": comps,
                      "series": [[d.date().isoformat(), r(v, 3)] for d, v in yr.iloc[::2].items()],
                      "label": "Low stress" if last < -1 else "Below-average stress" if last < 0 else "Above-average stress" if last < 2 else "High stress"}
        fsi_series = s
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"ofr fsi: {e}")
        out["fsi"] = prev.get("fsi")
        fsi_series = None

    # FRED series
    fred_defs = [("stlfsi", "STLFSI4", "St. Louis Fed stress index", ""), ("nfci", "NFCI", "Chicago Fed financial conditions", ""),
                 ("hy_oas", "BAMLH0A0HYM2", "High-yield credit spread", "%"), ("curve", "T10Y2Y", "10Y minus 2Y yield", "%"),
                 ("real10", "DFII10", "10Y real yield", "%")]
    out["fred"] = {}
    for key, sid, label, unit in fred_defs:
        try:
            s = fred(sid)
            last = float(s.iloc[-1])
            t = s.index[-1] - pd.Timedelta(days=30)
            p = s[s.index <= t]
            yr = s[s.index >= s.index[-1] - pd.Timedelta(days=365)]
            step = max(1, len(yr) // 60)
            out["fred"][key] = {"label": label, "unit": unit, "value": r(last, 3), "date": s.index[-1].date().isoformat(),
                                "chg_1m": r(last - float(p.iloc[-1]), 3) if len(p) else None,
                                "series": [r(v, 3) for v in yr.iloc[::step].values]}
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"fred {sid}: {e}")
            if prev.get("fred", {}).get(key):
                out["fred"][key] = prev["fred"][key]

    # market legs
    legs = [("vix", "^VIX", "VIX"), ("vix3m", "^VIX3M", "VIX 3-month"), ("dxy", "DX-Y.NYB", "Dollar index"),
            ("oil", "CL=F", "WTI crude"), ("gold", "GC=F", "Gold"), ("spx", "^GSPC", "S&P 500"), ("btc", "BTC-USD", "Bitcoin")]
    out["markets"] = {}
    closes = {}
    for key, sym, label in legs:
        try:
            h = yf.Ticker(sym).history(period="1y", interval="1d")["Close"].dropna()
            closes[key] = h
            last = float(h.iloc[-1])
            def chg(n):
                return r((last / float(h.iloc[-1 - n]) - 1) * 100, 2) if len(h) > n else None
            out["markets"][key] = {"label": label, "symbol": sym, "value": r(last, 3), "chg_1d": chg(1), "chg_1m": chg(21),
                                   "series": [r(v, 3) for v in h.iloc[-120::2].values]}
        except Exception as e:  # noqa: BLE001
            out["errors"].append(f"yahoo {sym}: {e}")
            if prev.get("markets", {}).get(key):
                out["markets"][key] = prev["markets"][key]
    try:
        v, v3 = out["markets"]["vix"]["value"], out["markets"]["vix3m"]["value"]
        out["vix_term"] = {"ratio": r(v / v3, 3), "state": "backwardation (near-term fear)" if v > v3 else "contango (normal)"}
    except Exception:  # noqa: BLE001
        out["vix_term"] = None

    # BTC forward 30d return by stress regime (since 2015)
    try:
        if fsi_series is None:
            raise RuntimeError("no FSI")
        btc = yf.Ticker("BTC-USD").history(period="max", interval="1d")["Close"].dropna()
        btc.index = btc.index.tz_localize(None).normalize()
        fs = fsi_series[fsi_series.index >= "2015-01-01"]
        df = pd.DataFrame({"fsi": fs})
        df["chg20"] = df["fsi"] - df["fsi"].shift(20)
        df = df.join(btc.rename("btc"), how="inner")
        df["fwd30"] = df["btc"].shift(-21) / df["btc"] - 1
        def regime(row):
            lvl = "Low stress" if row.fsi < 0 else "Elevated stress"
            tr = "easing" if row.chg20 < 0 else "rising"
            return f"{lvl}, {tr}"
        df = df.dropna(subset=["chg20"])
        df["reg"] = df.apply(regime, axis=1)
        cur_reg = df["reg"].iloc[-1]
        hist = df.dropna(subset=["fwd30"])
        mat = []
        for reg in ["Low stress, easing", "Low stress, rising", "Elevated stress, easing", "Elevated stress, rising"]:
            x = hist[hist["reg"] == reg]["fwd30"]
            mat.append({"regime": reg, "days": int(len(x)), "avg_30d": r(x.mean() * 100, 1) if len(x) else None,
                        "median_30d": r(x.median() * 100, 1) if len(x) else None,
                        "up_pct": r((x > 0).mean() * 100, 1) if len(x) else None, "current": reg == cur_reg})
        out["regimes"] = {"current": cur_reg, "rows": mat,
                          "note": "OFR stress level (above/below 0) and its 20-day change, matched to BTC's next ~30-day return since 2015. Days overlap, so the counts overstate independent samples."}
    except Exception as e:  # noqa: BLE001
        out["errors"].append(f"regimes: {e}")
        out["regimes"] = prev.get("regimes")
    save("macro.json", out)


# ------------------------------------------------------------ screener
UNIVERSE = [
    ("BTC-USD", "BTC", "crypto"), ("ETH-USD", "ETH", "crypto"), ("SOL-USD", "SOL", "crypto"), ("XRP-USD", "XRP", "crypto"),
    ("BNB-USD", "BNB", "crypto"), ("DOGE-USD", "DOGE", "crypto"), ("ADA-USD", "ADA", "crypto"), ("AVAX-USD", "AVAX", "crypto"),
    ("LINK-USD", "LINK", "crypto"), ("LTC-USD", "LTC", "crypto"), ("HYPE32196-USD", "HYPE", "crypto"), ("SUI20947-USD", "SUI", "crypto"),
    ("SPY", "SPY", "etf"), ("QQQ", "QQQ", "etf"), ("IWM", "IWM", "etf"), ("SMH", "SMH", "etf"), ("XLF", "XLF", "etf"),
    ("XLE", "XLE", "etf"), ("GLD", "GLD", "etf"), ("SLV", "SLV", "etf"), ("TLT", "TLT", "etf"), ("IBIT", "IBIT", "etf"),
    ("NVDA", "NVDA", "stock"), ("AAPL", "AAPL", "stock"), ("MSFT", "MSFT", "stock"), ("AMZN", "AMZN", "stock"),
    ("META", "META", "stock"), ("GOOGL", "GOOGL", "stock"), ("TSLA", "TSLA", "stock"), ("AMD", "AMD", "stock"),
    ("AVGO", "AVGO", "stock"), ("NFLX", "NFLX", "stock"), ("PLTR", "PLTR", "stock"), ("COIN", "COIN", "stock"),
    ("MSTR", "MSTR", "stock"), ("HOOD", "HOOD", "stock"), ("JPM", "JPM", "stock"), ("GS", "GS", "stock"),
    ("UNH", "UNH", "stock"), ("LLY", "LLY", "stock"), ("XOM", "XOM", "stock"), ("BA", "BA", "stock"),
]


def ema(v, n):
    k = 2 / (n + 1)
    out, e = [], None
    for x in v:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def screen_one(sym, label, kind, d):
    d = d.dropna()
    if len(d) < 260:
        return None
    o, h, l, c, v = (list(map(float, d[k].values)) for k in ("Open", "High", "Low", "Close", "Volume"))
    px = c[-1]
    e200 = ema(c, 200)[-1]
    wk = d["Close"].resample("W-SUN").last().dropna()
    w200 = ema(list(map(float, wk.values)), 200)[-1] if len(wk) >= 200 else None
    e20, e50 = ema(c, 20), ema(c, 50)
    f = {}
    dd = (px / e200 - 1) * 100
    f["d200"] = {"hit": abs(dd) <= 3, "dist": r(dd, 2)}
    if w200:
        dw = (px / w200 - 1) * 100
        f["w200"] = {"hit": abs(dw) <= 5, "dist": r(dw, 2)}
    else:
        f["w200"] = {"hit": False, "dist": None}
    # unfilled daily FVG within 2% of price (last 90 bars)
    best = None
    n = len(c)
    for i in range(max(2, n - 90), n):
        if l[i] > h[i - 2]:  # bullish gap zone [h[i-2], l[i]]
            lo_z, hi_z, side = h[i - 2], l[i], "bull"
            filled = min(l[i + 1:], default=1e18) <= lo_z
        elif h[i] < l[i - 2]:  # bearish gap zone [h[i], l[i-2]]
            lo_z, hi_z, side = h[i], l[i - 2], "bear"
            filled = max(h[i + 1:], default=-1) >= hi_z
        else:
            continue
        if filled:
            continue
        mid = (lo_z + hi_z) / 2
        dist = (mid / px - 1) * 100
        if abs(dist) <= 2 and (best is None or abs(dist) < abs(best["dist"])):
            best = {"side": side, "lo": r(lo_z, 4), "hi": r(hi_z, 4), "dist": r(dist, 2), "date": d.index[i].date().isoformat()}
    f["fvg"] = {"hit": best is not None, **(best or {})}
    # 90-day volume profile point of control
    lo90, hi90 = min(l[-90:]), max(h[-90:])
    bins = 40
    vol = [0.0] * bins
    for i in range(n - 90, n):
        tp = (h[i] + l[i] + c[i]) / 3
        k = min(bins - 1, int((tp - lo90) / (hi90 - lo90 + 1e-12) * bins))
        vol[k] += v[i]
    kmax = max(range(bins), key=lambda k: vol[k])
    poc = lo90 + (kmax + 0.5) * (hi90 - lo90) / bins
    dp = (px / poc - 1) * 100
    f["poc"] = {"hit": abs(dp) <= 2, "level": r(poc, 4), "dist": r(dp, 2)}
    # 20/50 EMA cross in last 5 bars
    cross = None
    for i in range(n - 5, n):
        if (e20[i] - e50[i]) * (e20[i - 1] - e50[i - 1]) < 0:
            cross = "bull" if e20[i] > e50[i] else "bear"
    f["cross"] = {"hit": cross is not None, "side": cross}
    # displacement: last 3 bars, range > 2x ATR14 and body > 60% of range
    tr = [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    atr = sum(tr[-15:-1]) / 14
    disp = None
    for i in range(n - 3, n):
        rng = h[i] - l[i]
        if rng > 2 * atr and abs(c[i] - o[i]) > 0.6 * rng:
            disp = "bull" if c[i] > o[i] else "bear"
    f["disp"] = {"hit": disp is not None, "side": disp}
    score = sum(1 for x in f.values() if x["hit"])
    sides = [x.get("side") for x in (f["fvg"], f["cross"], f["disp"]) if x["hit"] and x.get("side")]
    trend = "above" if px > e200 else "below"
    lean = "bull" if sides.count("bull") > sides.count("bear") else "bear" if sides.count("bear") > sides.count("bull") else ("bull" if trend == "above" else "bear")
    return {"symbol": label, "ticker": sym, "kind": kind, "price": r(px, 4), "chg_1d": r((c[-1] / c[-2] - 1) * 100, 2),
            "trend": trend, "lean": lean, "score": score, "factors": f, "as_of": d.index[-1].date().isoformat()}


def screener_job():
    import yfinance as yf
    tick = [u[0] for u in UNIVERSE]
    data = yf.download(tick, period="5y", interval="1d", group_by="ticker", auto_adjust=False, progress=False, threads=True)
    rows, missing = [], []
    for sym, label, kind in UNIVERSE:
        try:
            d = data[sym][["Open", "High", "Low", "Close", "Volume"]]
            if getattr(d.index, "tz", None) is not None:
                d.index = d.index.tz_localize(None)
            x = screen_one(sym, label, kind, d)
            if x:
                rows.append(x)
            else:
                missing.append(label)
        except Exception as e:  # noqa: BLE001
            missing.append(label)
            ERRORS.append(f"screener {sym}: {str(e)[:80]}")
    rows.sort(key=lambda x: (-x["score"], x["symbol"]))
    save("screener.json", {"generated_at": NOW.isoformat(), "rows": rows, "missing": missing,
                           "factors": {"d200": "Within 3% of the daily 200 EMA", "w200": "Within 5% of the weekly 200 EMA",
                                       "fvg": "Unfilled daily fair-value gap within 2%", "poc": "Within 2% of the 90-day volume point of control",
                                       "cross": "20/50 EMA cross in the last 5 days", "disp": "Displacement candle (range over 2x ATR) in the last 3 days"}})


def main():
    jobs = []
    try:
        oi_history()
    except Exception as e:  # noqa: BLE001
        ERRORS.append(f"oi_hist: {e}")
    if due("weekly.json", 55) or due("setups.json", 55):
        try:
            df = btc_daily()
            weeks = weekly_job(df)
            setups_job(df, weeks)
            jobs.append("weekly+setups")
        except Exception as e:  # noqa: BLE001
            ERRORS.append(f"weekly/setups: {type(e).__name__}: {e}")
    if due("macro.json", 55):
        try:
            macro_job()
            jobs.append("macro")
        except Exception as e:  # noqa: BLE001
            ERRORS.append(f"macro: {type(e).__name__}: {e}")
    if due("screener.json", 55):
        try:
            screener_job()
            jobs.append("screener")
        except Exception as e:  # noqa: BLE001
            ERRORS.append(f"screener: {type(e).__name__}: {e}")
    print(json.dumps({"tabs_ok": True, "jobs": jobs, "errors": ERRORS}, indent=1))


if __name__ == "__main__":
    main()
