"""Insider Radar cycle (runs once per 10-minute workflow cycle, ~4 min budget).

Free Hyperliquid data only. Steps, in priority order (later steps are skipped if the
time budget runs out, but the output is always written):
  1. Sync the tape summaries written by tape.py (/tmp) into data/insider/tape/.
  2. Market snapshot: metaAndAssetCtxs for the main dex and every HIP-3 dex (marks, 24h change, spread).
  3. Position monitoring of tracked wallets on every dex they use, diffed against the last cycle
     (data/insider/positions.json) -> open / add / flip / close / reduce.
  4. Paper scorecard: fill forward returns of earlier alerts from the ALERT-time mark.
  5. Event detection: outsized moves vs each market's own daily volatility (1h + 1d candles).
  6. Event attribution: wallets that opened from flat in the move direction in the 48h before the move start
     (candidates: tape, leaderboard day/week PnL toppers, registry, optional ironflow), verified from
     userFunding (hourly held size) + ordinary AND TWAP fills (startPosition == 0), ranked by PnL.
  7. Fresh-whale candidates from the tape (new accounts with concentrated size).
  8. Registry enrichment: 90d fills (ordinary + TWAP) -> stats, lifecycles, large fresh opens scored
     (hits AND misses) with a first-touch barrier; ledger -> account age, funding source, entity_id; role.
  9. Tiers (binomial test vs the empirical base rate), alerts (de-dup, chase gate), output.

Writes data/tabs/insider.json (contract in INSIDER_RADAR_SPEC.md) and state under data/insider/.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import statistics
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import insider_core as core  # noqa: E402
from informed import Limiter  # noqa: E402

CFG = core.CFG
H = core.H
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IND = DATA / "insider"
CACHE = IND / ".cache"
TAPE_REPO = IND / "tape"
TAPE_LIVE = Path(os.environ.get("INSIDER_TAPE_DIR", "/tmp/goblin_tape"))
OUT = DATA / "tabs" / "insider.json"
HLURL = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
BUDGET_S = float(os.environ.get("INSIDER_BUDGET_S", "200"))  # hard stop: API calls refuse to start after this
LIMIT = Limiter(per_min=int(os.environ.get("INSIDER_WEIGHT_PER_MIN", "400")))
# Wallets known (from the 2026-11-23 X post / NVDA analyst) to check first for events on these underlyings.
PRIORITY_CANDIDATES = {"NVDA": ["0x65a093e4f2ee1063ea9d97f4bc30453b3dce23af", "0xdcb45a8992c3cea077c3d631ee92572f64778858",
                                "0x152e41f0b83e6cad4b5dc730c1d6279b7d67c9dc", "0x10e14ba2d77263543be1bdb2851b0933ddf4f4bd"]}
MAIN_WATCH = ["BTC", "ETH", "SOL", "HYPE", "XRP", "DOGE", "SUI", "BNB", "ZEC", "ENA", "PUMP", "FARTCOIN", "AVAX", "LINK", "LTC"]

S = requests.Session()
S.headers["User-Agent"] = "goblin-alpha-insider/1.0"
T0 = time.time()
NOW_MS = int(T0 * 1000)
ERRORS: list[str] = []
NOTES: list[str] = []
STATS = {"weight": 0, "calls": 0, "poll_ok": 0, "poll_err": 0, "wallets_polled": 0}


def iso(ms):
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def left():
    return BUDGET_S - (time.time() - T0)


def err(label, e):
    ERRORS.append(f"{label}: {type(e).__name__}: {str(e)[:160]}")


# ----------------------------------------------------------------- I/O
def load(p: Path, default):
    try:
        if p.exists():
            return json.loads(p.read_text())
    except Exception as e:  # noqa: BLE001
        err(f"load {p.name}", e)
    return default


def save(p: Path, obj, indent=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":") if indent is None else None, indent=indent, default=float))
    os.replace(tmp, p)


class BudgetOut(Exception):
    pass


def hl(body, base=20, per=None, retries=3):
    """POST /info with central weight accounting. per = items per extra weight unit (20 for fills, 60 candles)."""
    if left() <= 0:
        raise BudgetOut("time budget used up")
    LIMIT.take(base)
    STATS["weight"] += base
    STATS["calls"] += 1
    for i in range(retries):
        try:
            r = S.post(HLURL, json=body, timeout=25)
            if r.status_code == 429:
                time.sleep(3 + 4 * i)
                continue
            r.raise_for_status()
            out = r.json()
            if per and isinstance(out, list) and len(out) >= per:
                extra = len(out) // per
                LIMIT.take(extra)
                STATS["weight"] += extra
            return out
        except Exception:  # noqa: BLE001
            if i == retries - 1:
                raise
            time.sleep(1 + i)
    raise RuntimeError("hyperliquid 429 retries exhausted")


# ------------------------------------------------------------- market data
def market_snapshot():
    dexs = [d["name"] for d in hl({"type": "perpDexs"}) if d]
    mk = {}
    for dex in [""] + dexs:
        try:
            body = {"type": "metaAndAssetCtxs", **({"dex": dex} if dex else {})}
            meta, ctxs = hl(body)
            for u, c in zip(meta["universe"], ctxs):
                if u.get("isDelisted") or c.get("markPx") is None:
                    continue
                mark = float(c["markPx"])
                imp = c.get("impactPxs") or []
                mid = float(c.get("midPx") or mark)
                spread = None
                if len(imp) == 2 and mid:
                    spread = round((float(imp[1]) - float(imp[0])) / mid * 1e4, 1)
                mk[u["name"]] = {"dex": dex, "mark": mark, "prev": float(c.get("prevDayPx") or mark),
                                 "vlm": float(c.get("dayNtlVlm") or 0), "oi_usd": float(c.get("openInterest") or 0) * mark,
                                 "spread_bps": spread}
        except Exception as e:  # noqa: BLE001
            err(f"ctxs {dex or 'main'}", e)
    return dexs, mk


_CANDLES: dict = {}


def candles(coin, interval, start_ms, end_ms=None):
    key = (coin, interval, start_ms // H, (end_ms or NOW_MS) // (10 * 60000))
    if key in _CANDLES:
        return _CANDLES[key]
    raw = hl({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": start_ms,
                                                  "endTime": end_ms or NOW_MS}}, per=60)
    out = [{"t": c["t"], "o": float(c["o"]), "h": float(c["h"]), "l": float(c["l"]), "c": float(c["c"])} for c in raw]
    _CANDLES[key] = out
    return out


def c1h_cached(coin, start_ms):
    """Hourly candles from a small on-disk cache (gitignored), refreshed incrementally."""
    p = CACHE / "c1h" / (coin.replace(":", "_") + ".json")
    d = load(p, {"rows": []})
    rows = d["rows"]
    have0 = rows[0]["t"] if rows else None
    if have0 is None or have0 > start_ms + H:
        rows = candles(coin, "1h", start_ms)
    elif rows[-1]["t"] < NOW_MS - 2 * H:
        new = candles(coin, "1h", rows[-1]["t"])
        rows = [x for x in rows if x["t"] < rows[-1]["t"]] + new
    rows = [x for x in rows if x["t"] >= NOW_MS - 100 * 86_400_000]
    save(p, {"rows": rows})
    return [x for x in rows if x["t"] >= start_ms - H]


def sigma_for(coin, state):
    v = state.setdefault("vol", {}).get(coin)
    if v and NOW_MS - v["ts"] < 12 * H:
        return v["sigma"]
    d = candles(coin, "1d", NOW_MS - CFG["vol_days"] * 86_400_000)
    closes = [x["c"] for x in d[:-2]]  # exclude the latest two days (the move itself)
    s = core.daily_sigma(closes)
    if s:
        state["vol"][coin] = {"sigma": round(s, 5), "ts": NOW_MS}
    return s


# ---------------------------------------------------------------- wallets
def funding_rows(addr, start_ms, end_ms=None, pages=4):
    out, st = [], start_ms
    for _ in range(pages):
        body = {"type": "userFunding", "user": addr, "startTime": st}
        if end_ms:
            body["endTime"] = end_ms
        rows = hl(body, per=20)
        for x in rows:
            d = x.get("delta") or {}
            out.append({"time": x["time"], "coin": d.get("coin"), "szi": d.get("szi"), "usdc": d.get("usdc")})
        if len(rows) < 500:
            break
        st = max(x["time"] for x in rows) + 1
    return out


def fills_merged(addr, start_ms, end_ms=None, pages=1):
    """Ordinary fills + TWAP slice fills, paginated forward. Returns (fills, truncated)."""
    out, truncated = [], False
    for typ in ("userFillsByTime", "userTwapSliceFillsByTime"):
        st = start_ms
        for i in range(pages):
            body = {"type": typ, "user": addr, "startTime": st}
            if end_ms:
                body["endTime"] = end_ms
            rows = hl(body, per=20)
            if typ == "userTwapSliceFillsByTime":
                rows = [dict(x["fill"], _twap=True, twapId=x.get("twapId")) for x in rows if isinstance(x, dict) and x.get("fill")]
            out.extend(rows)
            if len(rows) < 2000:
                break
            st = max(f["time"] for f in rows) + 1
            if i == pages - 1:
                truncated = True
    seen, uniq = set(), []
    for f in out:
        k = (f.get("tid"), f.get("coin"), f.get("time"))
        if k not in seen:
            seen.add(k)
            uniq.append(f)
    uniq.sort(key=lambda f: (f["time"], abs(float(f.get("startPosition") or 0))))
    return uniq, truncated


BRIDGE_SENDERS = {"0x2222222222222222222222222222222222222222",   # HyperEVM native bridge (HYPE)
                  "0x6b9e773128f453f5c2c60935ee2de2cbc5390a24"}   # CCTP bridge
STABLES = (None, "", "USDC", "USDT0", "USDH")


def _usd_value(d):
    """USD value of a ledger delta; token quantities are never read as dollars. None if unknown."""
    for k in ("usdcValue", "usdc"):
        if d.get(k) is not None:
            try:
                return abs(float(d[k]))
            except (TypeError, ValueError):
                pass
    if d.get("amount") is not None and (d.get("token") or "").split(":")[0] in STABLES:
        try:
            return abs(float(d["amount"]))
        except (TypeError, ValueError):
            pass
    return None


def ledger_info(addr):
    """Account age and first funder. Spam (spotGenesis, airdrops/transfers < $100 USD value) is ignored for both."""
    addr = addr.lower()
    rows = hl({"type": "userNonFundingLedgerUpdates", "user": addr, "startTime": 0}, per=20)
    if not rows:
        return {"age_days": None, "funded_by": "unknown", "first_ts": None}
    rows.sort(key=lambda x: x["time"])
    first, funded = None, "unknown"
    for x in rows:
        d = x.get("delta") or {}
        t = d.get("type")
        if t == "spotGenesis":
            continue
        usd = _usd_value(d)
        incoming = (d.get("destination") or "").lower() == addr
        if t == "deposit":
            if (usd or 0) >= CFG["fund_dust_usd"] or usd is None:
                first = first or x["time"]
                funded = "bridge"
                break
            continue
        if t in ("send", "spotTransfer", "internalTransfer", "subAccountTransfer") and incoming:
            if usd is None or usd < CFG["fund_dust_usd"]:
                continue  # spam/dust (or unpriced token) cannot set age or funder
            first = first or x["time"]
            src = (d.get("user") or "").lower()
            if src in BRIDGE_SENDERS or src.startswith("0x20000000000000000000000000000000000"):
                funded = "bridge"
                break
            if src and src != addr:
                funded = src
                break
            continue
        if not incoming and (usd or 0) >= CFG["fund_dust_usd"]:
            first = first or x["time"]  # e.g. an outgoing withdrawal proves the account was live
    first = first or rows[0]["time"]
    return {"age_days": round((NOW_MS - first) / 86_400_000, 1), "funded_by": funded, "first_ts": iso(first),
            "ledger_truncated": len(rows) >= 2000, "ledger_v": 2}


def equity_of(addr, dex_avs):
    """True equity: (spot USDC total - hold) + sum of perp accountValues (unified accounts hold perp margin in spot)."""
    try:
        sp = hl({"type": "spotClearinghouseState", "user": addr}, base=2)
        usdc = next((b for b in sp.get("balances", []) if b.get("coin") == "USDC"), None)
        free = (float(usdc["total"]) - float(usdc.get("hold") or 0)) if usdc else 0.0
    except Exception as e:  # noqa: BLE001
        err(f"spot {addr[:8]}", e)
        free = 0.0
    return free + sum(dex_avs)


def chs(addr, dex):
    body = {"type": "clearinghouseState", "user": addr, **({"dex": dex} if dex else {})}
    return hl(body, base=2)


def parse_chs(st, ts):
    pos = {}
    for ap in st.get("assetPositions", []):
        p = ap.get("position") or {}
        szi = float(p.get("szi") or 0)
        if szi == 0:
            continue
        pos[p["coin"]] = {"szi": szi, "entry": float(p.get("entryPx") or 0), "ntl": abs(float(p.get("positionValue") or 0)),
                          "upnl": float(p.get("unrealizedPnl") or 0), "lev": (p.get("leverage") or {}).get("value")}
    av = float((st.get("marginSummary") or {}).get("accountValue") or 0)
    return {"ok": True, "ts": ts, "pos": pos, "av": av}


# ----------------------------------------------------------------- leaderboard
def leaderboard(state, max_age_min):
    p = CACHE / "lb.json"
    d = load(p, None)
    if d and NOW_MS - d.get("ts", 0) < max_age_min * 60000:
        return d
    if left() < 120:
        return d
    try:
        rows = S.get(LEADERBOARD, timeout=90).json()["leaderboardRows"]
    except Exception as e:  # noqa: BLE001
        err("leaderboard", e)
        return d
    eq, day, week, month = {}, [], [], {}
    for r in rows:
        try:
            a = r["ethAddress"].lower()
            wp = dict(r["windowPerformances"])
            eq[a] = round(float(r["accountValue"]))
            month[a] = round(float(wp["month"]["pnl"]))
            day.append((float(wp["day"]["pnl"]), a))
            week.append((float(wp["week"]["pnl"]), a))
        except Exception:  # noqa: BLE001
            continue
    day.sort(reverse=True)
    week.sort(reverse=True)
    d = {"ts": NOW_MS, "n": len(rows), "equity": eq, "month": month,
         "day_top": [a for _, a in day[:300]], "day_bottom": [a for _, a in day[-150:]],
         "week_top": [a for _, a in week[:300]]}
    save(p, d)
    return d


def ironflow_top(coin, t0, t1, n=40):
    """Best-effort keyless third-party candidates for the last 24h only. Silent on failure."""
    t0 = max(int(t0), NOW_MS - int(23.5 * H))  # the service only covers ~24h back
    if t0 >= t1:
        return []
    try:
        r = S.get("https://api.ironflow.sh/v1/analytics/market-top-wallets",
                  params={"market": coin, "from": int(t0), "to": int(t1), "limit": n}, timeout=15)
        if r.status_code != 200:
            return []
        j = r.json()
        rows = j.get("data") if isinstance(j, dict) else j
        out = []
        for x in rows or []:
            a = (x.get("address") or x.get("wallet") or "").lower()
            if a.startswith("0x"):
                out.append(a)
        return out
    except Exception:  # noqa: BLE001
        return []


# ------------------------------------------------------------------- tape
def sync_tape():
    """Copy CLOSED hours from the live tape (/tmp) into immutable repo files data/insider/tape/YYYY-MM-DD/HH.json.

    An hour is copied once, after the tape has flushed past its end, so files never change after commit
    (keeps git history small). The live (current) hour is read straight from /tmp by tape_rows().
    """
    import tape as tp
    TAPE_REPO.mkdir(parents=True, exist_ok=True)
    status = load(TAPE_LIVE / "status.json", {}) if TAPE_LIVE.exists() else {}
    flushed_h = (status.get("last_flush_ms") or 0) // H
    written = 0
    if TAPE_LIVE.exists():
        for p in TAPE_LIVE.glob("20??-??-??.json"):
            try:
                rows = tp.decode_day(load(p, {}))
            except Exception as e:  # noqa: BLE001
                err(f"tape read {p.name}", e)
                continue
            by_h = {}
            for k, v in rows.items():
                by_h.setdefault(k[0], {})[k] = v
            for h, rr in by_h.items():
                if h >= flushed_h:  # not closed yet (or not flushed after its end)
                    continue
                dt = datetime.fromtimestamp(h * 3600, timezone.utc)
                out = TAPE_REPO / dt.strftime("%Y-%m-%d") / f"{dt.strftime('%H')}.json"
                if not out.exists():
                    save(out, tp.encode_day(rr))
                    written += 1
        for name in ("status.json", "candidates.json"):
            if (TAPE_LIVE / name).exists():
                shutil.copyfile(TAPE_LIVE / name, TAPE_REPO / name)
    cut = datetime.fromtimestamp((NOW_MS - CFG["tape_keep_d"] * 86_400_000) / 1000, timezone.utc).strftime("%Y-%m-%d")
    for d in TAPE_REPO.glob("20??-??-??"):
        if d.is_dir() and d.name < cut:
            shutil.rmtree(d, ignore_errors=True)
    if written:
        NOTES.append(f"Saved {written} hour(s) of large-trade tape")
    status = status or load(TAPE_REPO / "status.json", {})
    cands = load(TAPE_REPO / "candidates.json", {}).get("candidates", [])
    return status, cands


def tape_rows(coin, t_lo, t_hi):
    """Sum tape net notional per wallet for coin within [t_lo, t_hi] (ms): repo hour files + live /tmp day files."""
    import tape as tp
    rows = tp.load_repo_hours(TAPE_REPO, t_lo // H - 1)
    if TAPE_LIVE.exists():
        for p in TAPE_LIVE.glob("20??-??-??.json"):
            try:
                rows.update(tp.decode_day(load(p, {})))
            except Exception:  # noqa: BLE001
                pass
    acc = {}
    for (h, c, w), r in rows.items():
        if c == coin and t_lo <= h * H <= t_hi:
            a = acc.setdefault(w, [0.0, None])
            a[0] += r[0]
            a[1] = r[4] if a[1] is None else min(a[1], r[4])
    return acc


# ------------------------------------------------------------ registry helpers
def new_wallet(addr, source, ts=NOW_MS):
    return {"address": addr, "tier": "WATCH", "score": 0, "cohort": "unproven", "labels": [], "why": [],
            "equity": None, "age_days": None, "funded_by": None, "entity_id": addr, "registered_ts": iso(ts),
            "discovery_source": source, "stats": {}, "opens": [], "event_hits": [], "positions": [],
            "last_action": None, "known_dexs": [], "enriched_ms": 0, "ledger_ms": 0, "role": None}


def get_wallet(reg, addr, source):
    addr = addr.lower()
    if addr not in reg:
        reg[addr] = new_wallet(addr, source)
    return reg[addr]


def merge_seeds(reg, state):
    se = load(IND / "seed_events.json", None)
    sr = load(IND / "seed_registry.json", None)
    if se:
        evs = se.get("events", se) if isinstance(se, dict) else se
        n = 0
        for ev in evs if isinstance(evs, list) else []:
            eid = ev.get("id")
            if not eid:
                continue
            cur = state["events"].get(eid)
            if cur is None:
                ev = dict(ev, seed=True, checked=ev.get("checked", {}))
                ev.setdefault("start_ms", _ms(ev.get("start_ts")))
                ev.setdefault("detect_ms", _ms(ev.get("detect_ts")) or ev.get("start_ms"))
                for k in ("wrong_way_openers_n", "wrong_way_notional"):
                    if ev.get(k):
                        ev["seed_" + k] = ev[k]
                state["events"][eid] = ev
                n += 1
            else:
                have = {o["address"].lower(): o for o in cur.get("openers", [])}
                for o in ev.get("openers", []):
                    a = o.get("address", "").lower()
                    if a and a not in have:
                        cur.setdefault("openers", []).append(dict(o, source="seed"))
                    elif a:
                        for k, v in o.items():
                            if have[a].get(k) is None and v is not None:
                                have[a][k] = v
                for k in ("headline", "caveat", "wrong_way_openers_n", "wrong_way_notional"):
                    if ev.get(k) and not cur.get(k):
                        cur[k] = ev[k]
                # hand-verified lower bounds: the live count may still be draining its queue
                for k in ("wrong_way_openers_n", "wrong_way_notional"):
                    if ev.get(k):
                        cur["seed_" + k] = ev[k]
            for o in ev.get("openers", []):
                if o.get("address") and not o.get("chaser"):
                    w = get_wallet(reg, o["address"], "seed")
                    w.setdefault("discovery_event", eid)
                    _add_event_hit(w, eid, ev.get("coin"), ev.get("dir"), _ms(ev.get("start_ts")) or 0,
                                   _ms(o.get("opened_ts")), False)
        if n:
            NOTES.append(f"Loaded {n} researched event(s) from the backtest")
    if sr:
        rows = sr.get("wallets", sr) if isinstance(sr, dict) else sr
        if isinstance(rows, dict):
            rows = [dict(v, address=k) for k, v in rows.items()]
        for s in rows or []:
            a = (s.get("address") or "").lower()
            if not a.startswith("0x"):
                continue
            w = get_wallet(reg, a, "seed")
            # Seed (backtest) evidence is descriptive only: kept for display, never used to promote the tier.
            w["seed"] = {k: s.get(k) for k in ("cohort", "labels", "why", "stats", "premove") if s.get(k) is not None}
            if s.get("premove"):
                w["seed_premove"] = s["premove"]
            for h in s.get("event_hits") or []:
                _add_event_hit(w, h.get("event_id"), h.get("coin"), h.get("dir"), _ms(h.get("start_ts")) or h.get("start_ms") or 0,
                               _ms(h.get("opened_ts")), bool(h.get("chaser")))
            for o in s.get("opens") or []:
                if o.get("open_ms") and o.get("score") and not any(x["open_ms"] == o["open_ms"] and x["coin"] == o.get("coin") for x in w["opens"]):
                    w["opens"].append(dict(o, seed=True))


def _ms(s):
    if not s:
        return None
    if isinstance(s, (int, float)):
        return int(s)
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:  # noqa: BLE001
        return None


def _add_event_hit(w, eid, coin, d, start_ms, opened_ms, chaser):
    for h in w["event_hits"]:
        if h["event_id"] == eid and opened_ms:
            h["opened_ms"], h["chaser"] = opened_ms, chaser  # re-attribution corrects the open time
    if not any(h["event_id"] == eid for h in w["event_hits"]):
        w["event_hits"].append({"event_id": eid, "coin": coin, "dir": d, "start_ms": start_ms, "opened_ms": opened_ms,
                                "chaser": chaser})


def import_speculators(reg, lb):
    st = load(DATA / "informed_stats.json", {})
    md = CFG["medium"]
    n = 0
    for a, s in st.items():
        a = a.lower()
        if a in reg or not s.get("win_rate"):
            continue
        eq = (lb or {}).get("equity", {}).get(a, 0)
        if (s["closes"] >= md["closes"] and s["win_rate"] >= md["win_rate"] and (s.get("profit_factor") or 0) >= md["profit_factor"]
                and s.get("realized_90d", 0) > 0 and eq >= md["equity"] and (s.get("maker_share") or 0) <= CFG["noise"]["maker_share"]
                and s.get("orders_per_day", 0) <= CFG["noise"]["orders_per_day"]):
            w = get_wallet(reg, a, "leaderboard")
            w["equity"] = eq
            w["stats"] = {"closes": s["closes"], "win_rate": s["win_rate"], "profit_factor": s["profit_factor"],
                          "pnl_90d": s["realized_90d"], "maker_share": s.get("maker_share"), "orders_per_day": s.get("orders_per_day"),
                          "median_hold_h": None, "window_days": s.get("window_days"), "truncated": s.get("truncated"),
                          "provisional": True}
            n += 1
    if n:
        NOTES.append(f"Imported {n} proven-speculator candidates from the informed-trader cohort")


# --------------------------------------------------------------- monitoring
def monitor(reg, positions, dexs, marks, state, phase, min_left):
    """phase A: MEDIUM+ wallets and wallets with alerted positions; phase B: the rest (WATCH)."""
    alerted = {k.split("|")[0] for k in state.get("alerted_pos", {})}
    tracked = [w for w in reg.values() if w["tier"] != "NOISE"]
    tracked.sort(key=lambda w: (-core.TIER_ORDER.get(w["tier"], 0), -(w.get("score") or 0)))
    tracked = tracked[: CFG["max_tracked"]]
    is_a = lambda w: core.TIER_ORDER.get(w["tier"], 0) >= 2 or w["address"] in alerted  # noqa: E731
    tracked = [w for w in tracked if is_a(w) == (phase == "A")]
    # rotate: least recently polled first, so wallets skipped for time go first next cycle
    last = lambda w: (positions.get(w["address"], {}).get("") or {}).get("ts", 0)  # noqa: E731
    live_ev = {k for k, e in state["events"].items() if NOW_MS - (e.get("detect_ms") or e.get("start_ms") or 0) < 72 * H}
    hot = lambda w: any(h["event_id"] in live_ev and not h.get("chaser") for h in w["event_hits"])  # noqa: E731
    tracked.sort(key=lambda w: (not hot(w), last(w)))  # recent-event openers every cycle, then least recently polled
    cyc = state.get("cycle", 0)
    actions = []
    new_sweeps = 0
    for i, w in enumerate(tracked):
        if left() < min_left:
            NOTES.append(f"Ran out of time this cycle: checked {i} of {len(tracked)} wallets; the rest are checked next cycle")
            break
        a = w["address"]
        first = a not in positions
        if first:
            if new_sweeps >= CFG["new_sweeps_per_cycle"]:
                continue
            new_sweeps += 1
        STATS["wallets_polled"] += 1
        full = (w["tier"] == "HIGH" or first or (int(a[2:10], 16) + cyc) % CFG["sweep_every"] == 0)
        want = [""] + (dexs if full else [d for d in w.get("known_dexs", []) if d])
        prev = positions.get(a, {})
        cur = {}
        for dex in want:
            try:
                cur[dex] = parse_chs(chs(a, dex), NOW_MS)
                STATS["poll_ok"] += 1
                if dex and cur[dex]["pos"] and dex not in w["known_dexs"]:
                    w["known_dexs"].append(dex)
            except BudgetOut:
                break  # not a poll failure: this wallet's other dexs carry forward
            except Exception as e:  # noqa: BLE001
                STATS["poll_err"] += 1
                err(f"poll {a[:8]} {dex or 'main'}", e)
        acts = core.diff_positions(prev, cur, {c: m["mark"] for c, m in marks.items()})
        # carry forward dexs not polled (or failed) this cycle
        merged = dict(prev)
        merged.update({d: v for d, v in cur.items() if v.get("ok")})
        positions[a] = merged
        allpos = [dict(coin=c, **p) for d, v in merged.items() for c, p in v["pos"].items()]
        w["positions"] = sorted([{"coin": p["coin"], "side": core.side_of(p["szi"]),
                                  "notional": round(abs(p["szi"]) * marks.get(p["coin"], {}).get("mark", p["entry"])),
                                  "entry": p["entry"], "upnl": round(p["upnl"]), "lev": p["lev"], "size": abs(p["szi"]),
                                  "opened_ts": (w.get("open_ts") or {}).get(p["coin"])} for p in allpos],
                                key=lambda p: -p["notional"])[:12]
        w["dex_ts"] = {d: v.get("ts") for d, v in merged.items()}
        w["hedge"] = core.is_hedged([{"coin": p["coin"], "szi": p["szi"], "ntl": abs(p["szi"]) * marks.get(p["coin"], {}).get("mark", p["entry"])}
                                     for p in allpos])
        if full and "" in cur:
            w["equity_live"] = round(equity_of(a, [v["av"] for v in cur.values() if v.get("ok")]))
        for act in acts:
            act["address"] = a
            actions.append(act)
            w["last_action"] = {"ts": iso(act["ts"]), "type": act["type"], "coin": act["coin"], "side": act["side"],
                                "notional": act["notional"]}
            if act["type"] in ("open", "flip"):
                w.setdefault("open_ts", {})[act["coin"]] = iso(act["ts"])
                w["opens"].append({"coin": act["coin"], "side": act["side"], "open_ms": act["ts"],
                                   "open_px": marks.get(act["coin"], {}).get("mark") or act.get("entry"),
                                   "peak_ntl": act["notional"], "live": True, "via_twap": None})
    return actions


# ---------------------------------------------------------------- events
def detect_events(marks, state, reg):
    new = []
    evs = state["events"]
    screened = [(c, m) for c, m in marks.items() if m["vlm"] >= CFG["event_min_vlm"] and m["prev"]
                and abs(m["mark"] / m["prev"] - 1) * 100 >= CFG["event_screen_pct"]]
    # markets whose underlying already has a recent event: just refresh move/px from the mark (no candle calls)
    active = {}
    for e in evs.values():
        if NOW_MS - (e.get("detect_ms") or 0) < CFG["event_attr_h"] * H:
            active[(core.underlying(e["coin"]), e["dir"])] = e
            m = marks.get(e["coin"])
            if m and e.get("ref_px"):
                e["px"] = m["mark"]
                e["move_pct"] = round((m["mark"] / e["ref_px"] - 1) * 100, 2)
                if e.get("sigma_d"):
                    e["sigma"] = round(abs(math.log(m["mark"] / e["ref_px"])) / e["sigma_d"], 2)
    screened.sort(key=lambda x: -x[1]["vlm"])  # higher-volume venue first = primary coin of a merged event
    n_checked = 0
    for coin, m in screened:
        if left() < 100 or n_checked >= CFG["event_max_checks"]:
            break
        d = "up" if m["mark"] > m["prev"] else "down"
        ae = active.get((core.underlying(coin), d))
        if ae is not None and (coin == ae["coin"] or coin in ae.get("coins", [])):
            continue
        n_checked += 1
        try:
            c1 = candles(coin, "1h", NOW_MS - (CFG["event_lookback_h"] + 2) * H)
            mv = core.find_move(c1)
            sig = sigma_for(coin, state)
            ev = core.classify_event(mv, m["mark"], sig)
            if not ev:
                continue
            start = refine_start(coin, mv)
            eid = None
            for k, e in evs.items():
                if (core.underlying(e["coin"]) == core.underlying(coin) and e["dir"] == mv["dir"]
                        and abs((e.get("start_ms") or 0) - start) <= CFG["event_merge_h"] * H):
                    eid = k
                    break
            if eid is not None and evs[eid]["coin"] != coin:
                # same underlying on another dex: list it on the primary (higher-volume) event, do not make a second card
                e = evs[eid]
                if coin not in e.setdefault("coins", [e["coin"]]):
                    e["coins"].append(coin)
                e.setdefault("also", {})[coin] = {"move_pct": ev["move_pct"], "sigma": ev["sigma"], "px": m["mark"]}
                continue
            if eid is None:
                eid = f"{coin}|{iso(start)[:10]}"
                evs[eid] = {"id": eid, "coin": coin, "coins": [coin], "dir": mv["dir"], "start_ms": start, "detect_ms": NOW_MS,
                            "ref_px": mv["ref_px"], "headline": None, "window_h": CFG["window_h"], "openers": [],
                            "checked": {}, "caveat": None}
                new.append(eid)
            e = evs[eid]
            e.update({"move_pct": ev["move_pct"], "sigma": ev["sigma"], "px": m["mark"], "sigma_d": sig})
            if not e.get("start_ms"):
                e["start_ms"] = start
        except Exception as ex:  # noqa: BLE001
            err(f"event {coin}", ex)
    # drop old events
    for k in [k for k, e in evs.items() if NOW_MS - (e.get("detect_ms") or e.get("start_ms") or 0) > CFG["event_keep_d"] * 86_400_000]:
        del evs[k]
    return new


def refine_start(coin, mv):
    """Refine the inflection to the 5-minute candle where the move begins inside the start hour."""
    try:
        c5 = candles(coin, "5m", mv["start_ms"] - H, mv["start_ms"] + 2 * H)
        sign = 1 if mv["dir"] == "up" else -1
        best, bt = 0, mv["start_ms"]
        for i in range(len(c5)):
            for j in range(i, min(len(c5), i + 6)):
                m = sign * math.log(c5[j]["c"] / c5[i]["o"])
                if m > best:
                    best, bt = m, c5[i]["t"]
        return bt
    except Exception:  # noqa: BLE001
        return mv["start_ms"]


def attribute(eid, state, reg, lb, positions, marks, tape_status):
    e = state["events"][eid]
    if NOW_MS - (e.get("detect_ms") or 0) > CFG["event_attr_h"] * H:
        return
    coin, dex = e["coin"], core.dex_of(e["coin"])
    side = "long" if e["dir"] == "up" else "short"
    start = e["start_ms"]
    t_lo = start - CFG["window_h"] * H
    checked = e.setdefault("checked", {})
    if e.get("attr_v") != 2:  # v2 open selection (position carried into the move): re-check found openers once
        for a, stt in list(checked.items()):
            if stt in ("opener", "chaser"):
                del checked[a]
        e["attr_v"] = 2
    # candidates, in priority order
    cands = [(a, "post_event", True) for a in PRIORITY_CANDIDATES.get(core.underlying(coin), [])]
    tape = tape_rows(coin, t_lo, start)
    sign = 1 if side == "long" else -1
    for a, (net, _t) in sorted(tape.items(), key=lambda x: -sign * x[1][0]):
        if sign * net >= CFG["attr_tape_min_usd"]:
            cands.append((a, "live_tape", True))
    for a, w in reg.items():
        held = any(p["coin"] == coin for p in w.get("positions", []))
        if held:
            cands.append((a, w.get("discovery_source") or "registry", True))
    if lb:
        top = lb["day_top"] if NOW_MS - start < 24 * H else lb["week_top"]
        k = CFG["attr_lb_closed_check"]
        for i, a in enumerate(top[:k]):
            cands.append((a, "post_event", True))
        # day losers (wrong-way candidates) before the long tail of day winners
        bottom = lb.get("day_bottom", []) if NOW_MS - start < 24 * H else []
        for a in list(reversed(bottom))[: CFG["attr_lb_bottom"]]:
            cands.append((a, "wrong_way_check", False))
        for a in top[k: CFG["attr_lb_top"]]:
            cands.append((a, "post_event", False))
    for a in ironflow_top(coin, t_lo, start):
        cands.append((a, "post_event", True))
    seen, todo = set(), []
    for a, src, deep in cands:
        a = a.lower()
        if a in seen or a in checked:
            continue
        seen.add(a)
        todo.append((a, src, deep))
    e["pending"] = len(todo)
    n = 0
    for a, src, deep in todo:
        if n >= CFG["attr_per_cycle"] or left() < CFG["attr_reserve_s"]:
            break
        n += 1
        try:
            res = check_opener(a, e, side, t_lo, deep, marks, wrong_way=src == "wrong_way_check")
        except Exception as ex:  # noqa: BLE001
            err(f"attr {a[:8]}", ex)
            continue
        checked[a] = res["status"]
        if res["status"] in ("opener", "chaser"):
            o = res["opener"]
            o["source"] = src
            e["openers"] = [x for x in e["openers"] if x["address"] != a] + [o]
            known = a in reg
            w = get_wallet(reg, a, src if src not in ("registry", "wrong_way_check") else "post_event")
            if not known or w.get("discovery_source") == "post_event":
                w.setdefault("discovery_event", eid)
            _add_event_hit(w, eid, coin, e["dir"], start, _ms(o["opened_ts"]), o["chaser"])
            if o.get("equity"):
                w["equity"] = w.get("equity") or o["equity"]
            w.setdefault("open_ts", {})[coin] = o["opened_ts"]
            if not any(x["coin"] == coin and abs(x["open_ms"] - (_ms(o["opened_ts"]) or 0)) < 2 * H for x in w["opens"]):
                w["opens"].append({"coin": coin, "side": side, "open_ms": _ms(o["opened_ts"]), "open_px": o["first_px"],
                                   "peak_ntl": o["notional"], "discovery": True, "via_twap": o.get("via_twap")})
            else:
                for x in w["opens"]:
                    if x["coin"] == coin and abs(x["open_ms"] - (_ms(o["opened_ts"]) or 0)) < 2 * H:
                        x["discovery"] = True
            if dex and dex not in w["known_dexs"]:
                w["known_dexs"].append(dex)
        elif res["status"] == "wrong_way":
            e.setdefault("wrong_way", {})[a] = res.get("notional", 0)
    e["pending"] = max(0, len(todo) - n)
    if n:
        NOTES.append(f"{e.get('coin', eid)} move: looked at {n} more possible early traders, {e['pending']} still to check")


def check_opener(a, e, side, t_lo, deep, marks, wrong_way=False):
    coin, dex = e["coin"], core.dex_of(e["coin"])
    start, detect = e["start_ms"], e.get("detect_ms") or NOW_MS
    st = parse_chs(chs(a, dex), NOW_MS)
    p = st["pos"].get(coin)
    want = 1 if side == "long" else -1
    if p and core.sgn(p["szi"]) != want:
        fr = funding_rows(a, t_lo - 2 * H, None, pages=2)
        s2, oh, _ = core.open_from_funding(fr, coin, core.side_of(p["szi"]), t_lo, start, t_lo - 2 * H)
        if s2 == "opened":
            return {"status": "wrong_way", "notional": abs(p["szi"]) * marks.get(coin, {}).get("mark", p["entry"])}
        return {"status": "other_side"}
    if not p and wrong_way:
        # losers of the day: did they open the other side from flat before the move (and since closed)?
        opp = "short" if side == "long" else "long"
        fr = funding_rows(a, t_lo - 2 * H, None, pages=2)
        s2, oh, mx = core.open_from_funding(fr, coin, opp, t_lo, start, t_lo - 2 * H)
        if s2 == "opened":
            c1 = c1h_cached(coin, t_lo - 8 * H)
            px = core._px_at(c1, oh) or 0
            if mx * px >= CFG["opener_min_usd"]:
                return {"status": "wrong_way", "notional": mx * px}
        return {"status": "no_position"}
    if not p and not deep:
        return {"status": "no_position"}
    q0 = t_lo - 2 * H
    fr = funding_rows(a, q0, None, pages=3)
    status, open_h, max_sz = core.open_from_funding(fr, coin, side, t_lo, detect, q0, ref_ms=start)
    if status != "opened":
        return {"status": status if status != "none" else "no_position"}
    # exact first fill: open happened in (open_h - 1h, open_h]
    fills, _ = fills_merged(a, open_h - H - 5 * 60000, open_h + 5 * 60000)
    f = core.first_open_fill(fills, coin, side, open_h - H - 5 * 60000, min(open_h + 5 * 60000, max(start, open_h - H)), last=True) \
        or core.first_open_fill(fills, coin, side, open_h - H - 5 * 60000, open_h + 5 * 60000)
    if f is None and any(x.get("coin") == coin and float(x.get("startPosition") or 0) != 0 for x in fills):
        return {"status": "flipped_or_added"}  # position existed (other side or residual) when this hour began
    opened_ms = f["time"] if f else open_h - H
    first_px = float(f["px"]) if f else (p["entry"] if p else None)
    mark = marks.get(coin, {}).get("mark")
    still = bool(p)
    size = abs(p["szi"]) if p else max_sz
    realized = None
    if not still:
        rows = sorted([x for x in fr if x["coin"] == coin and core.sgn(float(x["szi"])) == want and x["time"] >= open_h],
                      key=lambda x: x["time"])
        last_h = rows[-1]["time"] if rows else open_h
        cf, _ = fills_merged(a, last_h, last_h + H + 5 * 60000)
        closing = [x for x in cf if x["coin"] == coin and float(x.get("closedPnl") or 0) != 0]
        realized = round(sum(float(x["closedPnl"]) - float(x.get("fee") or 0) for x in closing)) if closing else None
    ntl = size * (mark if still and mark else (first_px or 0))
    if ntl < CFG["opener_min_usd"]:
        return {"status": "small"}
    c1 = c1h_cached(coin, t_lo - 8 * H)
    pre = core.pre_open_move(c1, opened_ms, e["dir"], open_px=first_px if f else None)
    chaser = core.is_chaser(opened_ms, start, pre, e.get("move_pct"))
    eq = equity_of(a, [st["av"]] + ([] if dex == "" else []))
    if dex:
        try:
            eq = equity_of(a, [st["av"], parse_chs(chs(a, ""), NOW_MS)["av"]])
        except Exception:  # noqa: BLE001
            pass
    opener = {"address": a, "opened_ts": iso(opened_ms), "first_px": first_px, "lead_h": round((start - opened_ms) / H, 1),
              "size": round(size, 4), "notional": round(ntl), "upnl": round(p["upnl"]) if p else None,
              "realized": realized, "still_open": still, "entry": p["entry"] if p else None,
              "equity": round(eq) if eq else None, "pct_equity": round(ntl / eq * 100, 1) if eq else None,
              "pre_open_move_pct": pre, "chaser": chaser, "via_twap": bool(f and f.get("_twap")),
              "exact_first_fill": bool(f), "open_hour_ts": iso(open_h)}
    return {"status": "chaser" if chaser else "opener", "opener": opener}


# --------------------------------------------------------------- enrichment
def enrich(reg, lb, state):
    """Refresh stats/ledger/role for the most important stale wallets; score opens."""
    ttl = CFG["stats_ttl_h"] * H
    order = sorted(reg.values(), key=lambda w: (0 if w["event_hits"] else 1, -(core.TIER_ORDER.get(w["tier"], 0)),
                                                 w.get("enriched_ms", 0)))
    done = 0
    for w in order:
        if left() < 20:
            break
        a = w["address"]
        try:
            if lb:
                w["equity"] = lb["equity"].get(a) or w.get("equity_live") or w.get("equity")
                w["pnl_month"] = lb["month"].get(a)
            elif w.get("equity_live"):
                w["equity"] = w["equity_live"]
            if not w.get("ledger_ms") or w.get("ledger_v") != 2:
                li = ledger_info(a)
                w.update({k: li.get(k) for k in ("age_days", "funded_by", "ledger_v")})
                w["first_ts"] = li["first_ts"]
                w["ledger_ms"] = NOW_MS
                done += 1
            elif w.get("first_ts"):
                w["age_days"] = round((NOW_MS - _ms(w["first_ts"])) / 86_400_000, 1)
            if w.get("role") is None:
                w["role"] = (hl({"type": "userRole", "user": a}, base=60) or {}).get("role")
            if NOW_MS - w.get("enriched_ms", 0) > ttl and left() > 80:
                fills, trunc = fills_merged(a, NOW_MS - CFG["lookback_d"] * 86_400_000, None, pages=2)
                st = core.track_stats(fills, NOW_MS, trunc)
                st["pnl_month"] = w.get("pnl_month")
                w["stats"] = st
                lcs = core.lifecycles(fills)
                for lc in core.large_opens(lcs, w.get("equity")):
                    if not any(o["coin"] == lc["coin"] and abs(o["open_ms"] - lc["open_ms"]) < 2 * H for o in w["opens"]):
                        w["opens"].append({"coin": lc["coin"], "side": lc["side"], "open_ms": lc["open_ms"], "open_px": lc["open_px"],
                                           "peak_ntl": round(lc["peak_ntl"]), "close_ms": lc["close_ms"], "via_twap": lc["via_twap"]})
                w["enriched_ms"] = NOW_MS
                done += 1
        except Exception as e:  # noqa: BLE001
            err(f"enrich {a[:8]}", e)
            w["enriched_ms"] = NOW_MS - ttl + 2 * H  # retry in ~2h
        if done >= 12:
            break
    # score open trades whose 48h window is complete (budgeted candle fetches)
    fetched = 0
    for w in reg.values():
        for o in w["opens"]:
            if o.get("score") or not o.get("open_ms") or NOW_MS - o["open_ms"] < CFG["hit_window_h"] * H + H:
                continue
            if NOW_MS - o["open_ms"] > 95 * 86_400_000 or left() < 45 or fetched >= 25:
                continue
            try:
                c1 = c1h_cached(o["coin"], NOW_MS - 92 * 86_400_000)
                fetched += 1
                sig = sigma_for(o["coin"], state)
                sc = core.score_open(o["open_ms"], o["side"], o["open_px"], c1, sig)
                if sc:
                    o["score"] = sc
            except Exception as e:  # noqa: BLE001
                err(f"score {o['coin']}", e)
    w_opens_cap(reg)


def w_opens_cap(reg):
    for w in reg.values():
        w["opens"] = sorted(w["opens"], key=lambda o: o["open_ms"] or 0)[-150:]


def entities(reg):
    """Wallets sharing a direct funding counterparty share an entity_id. Hubs funding > 10 wallets are ignored."""
    by = {}
    for a, w in reg.items():
        f = w.get("funded_by") or ""
        if f.startswith("0x"):
            by.setdefault(f, []).append(a)
    for a, w in reg.items():
        f = w.get("funded_by") or ""
        if f in reg:  # funded directly by another registry wallet
            w["entity_id"] = reg[f].get("entity_id") or f
        elif f.startswith("0x") and 2 <= len(by.get(f, [])) <= 10:
            w["entity_id"] = f
        else:
            w["entity_id"] = a


def base_rate(reg):
    hits = n = 0
    tested = 0
    for w in reg.values():
        sc = [o for o in w["opens"] if o.get("score") and not o.get("discovery")]
        if sc:
            tested += 1
        n += len(sc)
        hits += sum(1 for o in sc if o["score"]["hit"])
    p0 = hits / n if n >= CFG["p0_min_n"] else CFG["p0_default"]
    return max(p0, CFG["p0_floor"]), tested, n


def seed_cap(w):
    """Wallets that came from the backtest seed stay WATCH unless our own live measurements say otherwise.
    Seed stats never promote; a NOISE verdict from our own measurements (vault, maker, hedge) is kept."""
    sd = w.get("seed")
    if not sd:
        return
    if w["tier"] in ("HIGH", "MEDIUM") and w.get("discovery_source") == "seed" and not (w.get("stats") or {}).get("closes"):
        w["tier"] = "WATCH"
    if w["tier"] == "WATCH" and w["cohort"] == "unproven":
        w["cohort"] = "one_hit"  # backtest-only evidence: never shown as repeat_premove
        w["why"] = list(sd.get("why") or []) or w["why"]
        if (sd.get("premove") or {}).get("hits"):
            pm = sd["premove"]
            w["why"].insert(0, f"Backtest: {pm['hits']} pre-move hits on {pm.get('opens_scored')} scored opens "
                               f"(hit rate {pm.get('hit_rate', 0)*100:.0f}%); not validated live")
    for x in sd.get("labels") or []:
        if x not in w["labels"]:
            w["labels"].append(x)


def labels_for(w, prem, events):
    L = []
    for h in w["event_hits"]:
        ev = events.get(h["event_id"]) or {}
        tag = "Mid-move chaser" if h.get("chaser") else "Pre-move opener"
        L.append(f"{tag} {h.get('coin')} {iso(h.get('start_ms'))[5:10].replace('-', '/') if h.get('start_ms') else ''}".strip())
        if ev.get("seed"):
            pass
    if w.get("age_days") is not None:
        L.append("New account" if w["age_days"] < 14 else "Old account" if w["age_days"] > 90 else f"{w['age_days']:.0f}d old")
    pm = w.get("pnl_month")
    if pm is not None:
        L.append("Down on month" if pm < 0 else "Up on month")
    big = max([p["notional"] for p in w.get("positions", []) if ":" in p["coin"]] or [0])
    if big >= CFG["offvenue_hedge_usd"] and (pm or 0) < 0:
        L.append("Possible off-venue hedge (HL 30d PnL < 0)")
    if (w.get("stats") or {}).get("truncated"):
        L.append("Fill history truncated")
    if w.get("cohort") == "proven_speculator" and w.get("tier") == "WATCH":
        L.append("Provisional: hold time not measured")
    if w.get("hedge"):
        L.append("Offset position")
    if w.get("role") and w["role"] not in ("user",):
        L.append(f"Role: {w['role']}")
    return L


# ------------------------------------------------------------------- alerts
def make_alerts(actions, reg, marks, state, new_events):
    alerts = state["alerts"]
    dedup = state.setdefault("dedup", {})
    alerted = state.setdefault("alerted_pos", {})
    out = []

    def push(a):
        a.setdefault("id", f"{a['type']}|{a.get('coin')}|{a.get('side')}|{a['ts_ms']}|{(a['wallets'][0]['address'][:10] if a.get('wallets') else '')}")
        a.setdefault("outcome", {k: None for k in CFG["horizons"]})
        a.setdefault("delivered", False)
        out.append(a)

    for act in actions:
        w = reg.get(act["address"]) or {}
        coin = act["coin"]
        mk = marks.get(coin, {})
        sig = (state.get("vol", {}).get(coin) or {}).get("sigma")
        key_pos = f"{act['address']}|{coin}"
        res = core.alert_for_action(act, w, mk.get("mark"), sig, key_pos in alerted)
        if not res:
            continue
        pr, typ, extra = res
        ent = w.get("entity_id") or act["address"]
        dkey = f"{ent}|{coin}|{act['side']}|{'open' if typ in ('tracked_open', 'tracked_add', 'fresh_whale') else typ}"
        if typ not in ("tracked_close", "tracked_flip") and not core.dedup_ok(dedup, dkey, NOW_MS, CFG["dedup_h"]):
            continue
        entry = act.get("entry")
        why = list(extra)
        ch = None
        if typ in ("tracked_open", "tracked_add", "fresh_whale", "tracked_flip"):
            pr, ch, late = core.apply_chase(pr, act["side"], entry, mk.get("mark"), sig)
            if late:
                why.append(late)
        why += [f"Tier {w.get('tier')} ({core.COHORTS.get(w.get('cohort'), ('', ''))[0]}): " + "; ".join(w.get("why", [])[:2])]
        verb = {"tracked_open": "opened", "tracked_add": "added to", "tracked_flip": "flipped to", "tracked_close": "closed",
                "fresh_whale": "opened"}[typ]
        title = (f"{w.get('tier')} wallet {act['address'][:6]}…{act['address'][-4:]} {verb} a {usd_s(act['notional'] if typ != 'tracked_close' else act['prev_notional'])} "
                 f"{coin} {act['side']}")
        if typ == "fresh_whale":
            title = f"Unproven anomaly: {w.get('age_days', 0):.0f}-day-old account opened {usd_s(act['notional'])} {coin} {act['side']}"
        a = {"ts": iso(NOW_MS), "ts_ms": NOW_MS, "priority": pr, "type": typ, "coin": coin, "side": act["side"],
             "title": title, "why": why,
             "wallets": [{"address": act["address"], "tier": w.get("tier"), "cohort": w.get("cohort"), "entity_id": ent}],
             "notional": act["notional"] if typ != "tracked_close" else act["prev_notional"], "px": mk.get("mark"),
             "wallet_entry_px": entry, "chase_pct": ch, "spread_bps": mk.get("spread_bps"), "follow_exit_ret": None}
        push(a)
        if typ in ("tracked_close", "tracked_flip") and key_pos in alerted:
            orig = next((x for x in alerts if x["id"] == alerted[key_pos]), None)
            if orig and orig.get("px") and mk.get("mark"):
                orig["follow_exit_ret"] = round((1 if orig["side"] == "long" else -1) * (mk["mark"] / orig["px"] - 1) * 100, 2)
            alerted.pop(key_pos, None)
        if typ in ("tracked_open", "tracked_add", "fresh_whale", "tracked_flip") and pr in ("HIGH", "MEDIUM"):
            dedup[dkey] = NOW_MS
            alerted[key_pos] = a["id"]
        elif typ == "tracked_open":
            dedup[dkey] = NOW_MS

    # clusters of distinct entities
    recent = [x for x in state.get("actions", []) if NOW_MS - x["ts"] <= CFG["cluster"]["window_h"] * H]
    for cl in core.find_clusters(recent, reg, NOW_MS):
        dkey = f"cluster|{cl['coin']}|{cl['side']}"
        if not core.dedup_ok(dedup, dkey, NOW_MS, CFG["dedup_h"]):
            continue
        mk = marks.get(cl["coin"], {})
        ws = [x for x in recent if x["coin"] == cl["coin"] and x["side"] == cl["side"] and x["type"] in ("open", "add")]
        entries = [x.get("entry") for x in ws if x.get("entry")]
        entry = statistics.median(entries) if entries else None
        sig = (state.get("vol", {}).get(cl["coin"]) or {}).get("sigma")
        pr, ch, late = core.apply_chase("HIGH", cl["side"], entry, mk.get("mark"), sig)
        why = [f"{cl['n_entities']} separate entities (MEDIUM tier or better) opened or added {cl['side']} within 6h, {usd_s(cl['usd'])} combined"]
        if late:
            why.append(late)
        push({"ts": iso(NOW_MS), "ts_ms": NOW_MS, "priority": pr, "type": "cluster", "coin": cl["coin"], "side": cl["side"],
              "title": f"{cl['n_entities']} tracked wallets went {cl['side']} {cl['coin']} ({usd_s(cl['usd'])})", "why": why,
              "wallets": [{"address": x["address"], "tier": reg.get(x["address"], {}).get("tier"),
                           "cohort": reg.get(x["address"], {}).get("cohort")} for x in ws][:10],
              "notional": cl["usd"], "px": mk.get("mark"), "wallet_entry_px": entry, "chase_pct": ch,
              "spread_bps": mk.get("spread_bps"), "follow_exit_ret": None})
        dedup[dkey] = NOW_MS

    # events
    for eid, e in state["events"].items():
        pre = [o for o in e.get("openers", []) if not o.get("chaser")]
        side = "long" if e["dir"] == "up" else "short"
        if eid in new_events and core.dedup_ok(dedup, f"event|{eid}", NOW_MS, 10_000):
            dedup[f"event|{eid}"] = NOW_MS
            push({"ts": iso(NOW_MS), "ts_ms": NOW_MS, "priority": "INFO", "type": "event", "coin": e["coin"], "side": side,
                  "title": f"{e['coin']} moved {e['move_pct']:+.1f}% ({e['sigma']:.1f} sigma) since {iso(e['start_ms'])[11:16]} UTC",
                  "why": ["Outsized move vs this market's own daily volatility; scanning for wallets that opened before it"],
                  "wallets": [], "notional": None, "px": e.get("px"), "wallet_entry_px": None, "chase_pct": None,
                  "spread_bps": marks.get(e["coin"], {}).get("spread_bps"), "follow_exit_ret": None, "event_id": eid})
        if len(pre) >= CFG["event_medium_openers"] and core.dedup_ok(dedup, f"event3|{eid}", NOW_MS, 10_000):
            dedup[f"event3|{eid}"] = NOW_MS
            top = sorted(pre, key=lambda o: -((o.get("upnl") or 0) + (o.get("realized") or 0)))[:5]
            push({"ts": iso(NOW_MS), "ts_ms": NOW_MS, "priority": "MEDIUM", "type": "event", "coin": e["coin"], "side": side,
                  "title": f"Several wallets opened {e['coin']} {side} before the {e['move_pct']:+.1f}% move",
                  "why": [f"{o['address'][:6]}…{o['address'][-4:]} opened {o['lead_h']:.0f}h before at {o['first_px']}, {usd_s(o['notional'])}" for o in top]
                  + ["After-the-fact discovery: these wallets are WATCH tier until they repeat"],
                  "wallets": [{"address": o["address"], "tier": reg.get(o["address"], {}).get("tier"),
                               "cohort": reg.get(o["address"], {}).get("cohort")} for o in top],
                  "notional": round(sum(o.get("notional") or 0 for o in pre)), "px": marks.get(e["coin"], {}).get("mark"),
                  "wallet_entry_px": None, "chase_pct": None, "spread_bps": marks.get(e["coin"], {}).get("spread_bps"),
                  "follow_exit_ret": None, "event_id": eid})
        # keep the still-recent event alert current (title has no count; why/size refresh in place)
        top = sorted(pre, key=lambda o: -((o.get("upnl") or 0) + (o.get("realized") or 0)))[:5]
        for al in alerts:
            if al.get("type") == "event" and al.get("event_id") == eid and al.get("priority") == "MEDIUM" \
                    and NOW_MS - al["ts_ms"] < 24 * H:
                al["title"] = f"Several wallets opened {e['coin']} {side} before the {e['move_pct']:+.1f}% move"
                al["why"] = [f"{o['address'][:6]}…{o['address'][-4:]} opened {o['lead_h']:.0f}h before at {o['first_px']}, "
                             f"{usd_s(o['notional'])}" for o in top] + [f"{len(pre)} pre-move openers found so far",
                                                                         "After-the-fact discovery: these wallets are WATCH tier until they repeat"]
                al["wallets"] = [{"address": o["address"], "tier": reg.get(o["address"], {}).get("tier"),
                                  "cohort": reg.get(o["address"], {}).get("cohort")} for o in top]
                al["notional"] = round(sum(o.get("notional") or 0 for o in pre))
                al["updated_ms"] = NOW_MS
    for k in [k for k, t in dedup.items() if NOW_MS - t > 7 * 86_400_000]:
        del dedup[k]
    state["alerts"] = (out[::-1] + alerts)[:500]
    return out


def usd_s(x):
    return core.usd(x)


def outcomes(state, marks):
    fetched = [0]

    for a in state["alerts"]:
        if not a.get("px") or (all(v is not None for v in (a.get("outcome") or {}).values()) and a.get("outcome")):
            continue
        coin = a["coin"]

        def hist(due, coin=coin):
            if fetched[0] >= 10 or left() < 60:
                return None
            fetched[0] += 1
            c = candles(coin, "5m", due - 5 * 60000, due + 10 * 60000)
            return c[0]["c"] if c else None

        core.fill_outcomes(a, NOW_MS, marks.get(coin, {}).get("mark"), hist_px=hist)


# ------------------------------------------------------------------- output
METHOD = [
    "Events: a market is flagged when its move since the start of a sharp run (at most 12 hourly candles, start refined to "
    "5-minute candles) is at least 5% AND at least 3 daily sigmas of its own last ~45 days. start_ts is the inflection, not detection time.",
    "Openers: wallets whose position in that market went from flat to the move's direction in the 48h before start_ts. Open time is "
    "found from hourly funding rows (which carry position size) and confirmed from ordinary AND TWAP fills with startPosition = 0; "
    "first_px is that first fill. Wallets that already closed are included with realized PnL from their closing fills.",
    "Candidates come from our own trade tape (who was buying before the move), leaderboard day/week PnL toppers, wallets already in "
    "the registry, and (best-effort, last 24h only) a free third-party wallet ranking. The leaderboard misses small new accounts, "
    "and the tape only covers markets and hours it was running.",
    "Mid-move chasers (opened after start_ts, or after the price had already run >= max(3%, 30% of the move) in the prior 6h) are shown "
    "but never counted as hits.",
    "Tiers: post-event discovery can never exceed WATCH. HIGH needs >= 3 independent pre-move hits AND >= 6 independent large fresh "
    "opens whose hit rate beats the base rate with a binomial p <= 0.003 and expected false positives < 0.5 across all wallets tested, "
    "positive mean 24h return across hits and misses, median hold >= 1.5h, and no hedge. An empty HIGH list is expected early on.",
    "Base rate: in a backtest of 440 sampled leaderboard wallets, large fresh opens preceded a big move 8.4% of the time versus 8.2% "
    "for random timing, and no wallet reached HIGH. That 8.4% is the chance rate (floor 8%) until 50+ opens are scored live. "
    "Wallets loaded from that backtest are shown as WATCH; backtest numbers never raise a tier.",
    "A scored large fresh open = opened from flat, peak size within 6h >= max($250k, 5% of equity). Hit = price touched +X in its "
    "direction before -X within 48h, X = max(2%, 1 daily sigma x sqrt(2)). Same-direction opens within 24h count as one trial.",
    "MEDIUM (proven speculator) is a 90-day after-fee track record (20+ closes, win rate 55%+, PF 1.3+, 90d PnL > 0, equity $250k+). "
    "It says nothing about advance knowledge. Fill history is capped by Hyperliquid (latest ~10k fills); truncated records are labelled.",
    "NOISE: mostly maker fills, > 60 orders/day, median hold < 1h, or offset positions (same underlying across dexs, BTC/ETH pair). "
    "A big position is not a signal by itself: it can be a hedge of something held off Hyperliquid.",
    "Alerts compare each tracked wallet's positions on every dex with the previous cycle. A wallet's first observation never alerts; "
    "a failed dex poll carries the prior state forward. HIGH alerts are downgraded when price already moved more than max(2%, 0.75 x 1h sigma) "
    "from the wallet's entry. Wallets funded by the same counterparty are grouped as one entity for de-dup and clusters.",
    "Scorecard is strictly prospective: forward returns at 1h/4h/24h/72h measured from the mark price when the alert was logged, before spread, fees, funding and delay. "
    "net_h24 subtracts a 0.09% round-trip fee and the quoted spread. Nothing here proves anyone traded on inside information.",
]


def sync_opener(o, w, e, marks):
    """Keep event cards live from the latest position poll (no extra API calls)."""
    o = dict(o)
    if not o.get("still_open") or o.get("chaser") is None:
        return o
    coin = e["coin"]
    want = "long" if e["dir"] == "up" else "short"
    dex = core.dex_of(coin)
    ts = (w.get("dex_ts") or {}).get(dex)
    if not ts or NOW_MS - ts > 30 * 60000:
        return o
    p = next((p for p in w.get("positions", []) if p["coin"] == coin and p["side"] == want), None)
    if p:
        if p.get("size") is not None:
            o["size"] = round(p["size"], 4)
        o["notional"], o["upnl"] = p["notional"], p["upnl"]
        if o.get("equity"):
            o["pct_equity"] = round(p["notional"] / o["equity"] * 100, 1)
    elif len(w.get("positions", [])) < 12:  # list is capped at 12; only trust absence when it is complete
        o["still_open"], o["upnl"], o["closed_after_check"] = False, None, True
    return o


def build_tab(state, reg, tape_status, dexs, marks, poll_degraded, n_tested):
    evs = []
    for e in sorted(state["events"].values(), key=lambda e: -(e.get("start_ms") or 0)):
        ops = []
        for o in e.get("openers", []):
            w = reg.get(o["address"].lower(), {})
            o = sync_opener(o, w, e, marks)
            ops.append({**{k: o.get(k) for k in ("address", "opened_ts", "first_px", "lead_h", "size", "notional", "upnl", "realized",
                                                 "still_open", "equity", "pct_equity", "pre_open_move_pct", "chaser", "via_twap")},
                        "tier": w.get("tier"), "cohort": w.get("cohort"), "labels": w.get("labels", [])[:6],
                        "entity_id": w.get("entity_id"), "source": o.get("source")})
        ops.sort(key=lambda o: (o.get("chaser") is True, -((o.get("upnl") or 0) + (o.get("realized") or 0))))
        for i, o in enumerate(ops):
            o["rank"] = i + 1
        pre = [o for o in ops if not o.get("chaser")]
        ww = e.get("wrong_way") or {}
        ww_n = len(ww) if ww else e.get("wrong_way_openers_n")
        ww_usd = round(sum(ww.values())) if ww else e.get("wrong_way_notional")
        if e.get("seed_wrong_way_openers_n") and (ww_n or 0) < e["seed_wrong_way_openers_n"]:
            ww_n, ww_usd = e["seed_wrong_way_openers_n"], e.get("seed_wrong_way_notional") or ww_usd
        evs.append({"id": e["id"], "coin": e["coin"], "dir": e["dir"], "move_pct": e.get("move_pct"), "sigma": e.get("sigma"),
                    "start_ts": iso(e.get("start_ms")), "detect_ts": iso(e.get("detect_ms")), "ref_px": e.get("ref_px"),
                    "px": marks.get(e["coin"], {}).get("mark", e.get("px")), "headline": e.get("headline"),
                    "window_h": e.get("window_h", CFG["window_h"]), "n_openers": len(pre),
                    "total_upnl": round(sum((o.get("upnl") or 0) for o in pre)),
                    "caveat": e.get("caveat") or ("Found after the move. Wallets that opened early and lost on other bets are not "
                                                  "visible here; one well-timed trade is not proof of inside information."),
                    "wrong_way_openers_n": ww_n, "wrong_way_notional": ww_usd,
                    "coins": e.get("coins") or [e["coin"]], "also": e.get("also"),
                    "candidates_checked": len(e.get("checked", {})), "candidates_pending": e.get("pending"),
                    "openers": ops})
    wallets = []
    for w in sorted(reg.values(), key=lambda w: (-core.TIER_ORDER.get(w["tier"], 0), -(w.get("score") or 0))):
        st = w.get("stats") or {}
        wallets.append({"address": w["address"], "tier": w["tier"], "score": w.get("score"), "cohort": w["cohort"],
                        "labels": w.get("labels", []), "why": w.get("why", []), "equity": w.get("equity"),
                        "age_days": w.get("age_days"), "funded_by": w.get("funded_by"), "entity_id": w.get("entity_id"),
                        "registered_ts": w.get("registered_ts"), "discovery_source": w.get("discovery_source"),
                        "stats": {"closes": st.get("closes"), "win_rate": st.get("win_rate"),
                                  "profit_factor": min(st["profit_factor"], 99.0) if st.get("profit_factor") is not None else None,
                                  "pnl_90d": st.get("pnl_90d"), "pnl_month": w.get("pnl_month"), "maker_share": st.get("maker_share"),
                                  "median_hold_h": st.get("median_hold_h"), "orders_per_day": st.get("orders_per_day")},
                        "premove": w.get("premove"), "positions": w.get("positions", [])[:8], "last_action": w.get("last_action")})
    cohorts = []
    for key, (name, desc) in core.COHORTS.items():
        mem = [w for w in reg.values() if w["cohort"] == key]
        nl = sum(p["notional"] for w in mem for p in w.get("positions", []) if p["side"] == "long")
        ns = sum(p["notional"] for w in mem for p in w.get("positions", []) if p["side"] == "short")
        coins = {}
        for w in mem:
            for p in w.get("positions", []):
                c = coins.setdefault(p["coin"], [0, set()])
                c[0] += p["notional"] * (1 if p["side"] == "long" else -1)
                c[1].add(w["address"])
        top = sorted(coins.items(), key=lambda x: -abs(x[1][0]))[:5]
        als = [a for a in state["alerts"] if a.get("type") != "event" and any(x.get("cohort") == key for x in a.get("wallets", []))]
        sc = core.scorecard(als)
        cohorts.append({"key": key, "name": name, "desc": desc, "n": len(mem), "net_long_usd": round(nl), "net_short_usd": round(ns),
                        "top_coins": [{"coin": c, "net_usd": round(v[0]), "n_wallets": len(v[1])} for c, v in top],
                        "scorecard": {"alerts": sc["n_alerts"], "scored": sc["n_scored"], "hit_rate_24h": sc["hit_rate_24h"],
                                      "avg_fwd_24h": sc["avg_fwd_24h"]}})
    tape_ok = bool(tape_status) and NOW_MS - (tape_status.get("last_flush_ms") or 0) < 15 * 60000
    hook = bool(os.environ.get("DISCORD_INSIDER_WEBHOOK", "").strip() or os.environ.get("SLACK_INSIDER_WEBHOOK", "").strip())
    return {
        "updated": iso(NOW_MS), "updated_ms": NOW_MS,
        "status": {"tape_running": tape_ok, "tape_since": (tape_status or {}).get("started"),
                   "markets_watched": (tape_status or {}).get("n_markets"), "markets_screened": len(marks),
                   "dexs": len(dexs) + 1, "wallets_tracked": sum(1 for w in reg.values() if w["tier"] != "NOISE"),
                   "wallets_registry": len(reg), "discord": "configured" if hook else "logging_only",
                   "poll_degraded": poll_degraded, "n_wallets_tested": n_tested,
                   "weight_used": STATS["weight"], "wallets_polled": STATS["wallets_polled"], "runtime_s": round(time.time() - T0, 1), "notes": NOTES[-12:]},
        "alerts": state["alerts"][: CFG["keep_alerts"]],
        "events": evs,
        "wallets": wallets[:300],
        "cohorts": cohorts,
        "scorecard": core.scorecard(state["alerts"]),
        "method": METHOD,
        "errors": ERRORS[:30],
    }


# --------------------------------------------------------------------- main
def main():
    state = load(IND / "state.json", {})
    state.setdefault("events", {})
    state.setdefault("alerts", [])
    state["cycle"] = state.get("cycle", 0) + 1
    reg = load(IND / "wallets.json", {})
    positions = load(IND / "positions.json", {})
    for w in reg.values():  # migration: wallets found by attribution before discovery_event existed
        if w.get("discovery_source") in ("post_event", "seed") and not w.get("discovery_event") and w.get("event_hits"):
            w["discovery_event"] = min(w["event_hits"], key=lambda h: h.get("start_ms") or 0)["event_id"]
    dexs, marks, tape_status, poll_degraded, n_tested, new_events = [], {}, {}, True, 0, []
    try:
        tape_status, tape_cands = sync_tape()
        dexs, marks = market_snapshot()
        merge_seeds(reg, state)
        need_lb = any(NOW_MS - (e.get("detect_ms") or 0) < CFG["event_attr_h"] * H for e in state["events"].values())
        lb = leaderboard(state, 20 if need_lb else 60)
        import_speculators(reg, lb)
        actions = monitor(reg, positions, dexs, marks, state, "A", 120) if marks else []
        outcomes(state, marks)
        if left() > 100:
            new_events = detect_events(marks, state, reg)
        for eid in sorted(state["events"], key=lambda k: -marks.get(state["events"][k]["coin"], {}).get("vlm", 0)):
            if left() < 90:
                break
            attribute(eid, state, reg, lb, positions, marks, tape_status)
        if marks:
            actions += monitor(reg, positions, dexs, marks, state, "B", 45)
        tot = STATS["poll_ok"] + STATS["poll_err"]
        poll_degraded = (not marks) or (tot > 0 and STATS["poll_err"] / tot > 0.2)
        state["actions"] = [a for a in state.get("actions", []) if NOW_MS - a["ts"] < 24 * H] + actions
        fresh_whales(tape_cands, reg, marks)
        enrich(reg, lb, state)
        entities(reg)
        p0, n_tested, n_opens = base_rate(reg)
        for w in reg.values():
            prem = core.premove_block(w, p0, n_tested)
            if w.get("seed_premove") and (w["seed_premove"].get("opens_scored") or 0) > prem["opens_scored"]:
                prem["seed"] = w["seed_premove"]
            w["premove"] = prem
            w["tier"], w["cohort"], w["why"] = core.assign_tier(w, prem)
            w["score"] = core.wallet_score(w, prem)
            w["labels"] = labels_for(w, prem, state["events"])
            seed_cap(w)
        if n_opens >= CFG["p0_min_n"]:
            NOTES.append(f"Chance rate for a big move after a large open: {p0*100:.1f}%, measured from {n_opens} live-scored opens")
        else:
            NOTES.append(f"Chance rate for a big move after a large open: {p0*100:.1f}% (backtest of 440 leaderboard wallets; "
                         f"only {n_opens} live-scored opens so far)")
        if not poll_degraded:
            make_alerts(actions, reg, marks, state, new_events)
        else:
            NOTES.append("No alerts this cycle: too many position checks failed, so changes could not be trusted")
    except Exception as e:  # noqa: BLE001
        err("cycle", e)
        traceback.print_exc()
    finally:
        tab = build_tab(state, reg, tape_status, dexs, marks, poll_degraded, n_tested)
        save(OUT, tab, indent=None)
        save(IND / "state.json", state)
        save(IND / "wallets.json", reg)
        save(IND / "positions.json", positions)
        print(f"insider: {len(tab['events'])} events, {len(reg)} wallets, {len(tab['alerts'])} alerts, "
              f"weight {STATS['weight']}, {time.time()-T0:.0f}s, errors {len(ERRORS)}")


def fresh_whales(cands, reg, marks):
    n = 0
    for c in cands or []:
        if n >= 8 or left() < 70:
            break
        a = c["address"].lower()
        if a in reg or abs(c.get("net_usd") or 0) < CFG["fresh"]["pos_usd"]:
            continue
        n += 1
        try:
            li = ledger_info(a)
            if li["age_days"] is None or li["age_days"] >= CFG["fresh"]["age_d"]:
                continue
            dex = core.dex_of(c["coin"])
            st = parse_chs(chs(a, dex), NOW_MS)
            p = st["pos"].get(c["coin"])
            if not p:
                continue
            ntl = abs(p["szi"]) * marks.get(c["coin"], {}).get("mark", p["entry"])
            if ntl < CFG["fresh"]["pos_usd"]:
                continue
            w = get_wallet(reg, a, "live_tape")
            w.update({k: li[k] for k in ("age_days", "funded_by")})
            w["first_ts"], w["ledger_ms"] = li["first_ts"], NOW_MS
            if dex:
                w["known_dexs"].append(dex)
            NOTES.append(f"Fresh concentrated wallet {a[:8]}: {li['age_days']:.0f}d old, {core.usd(ntl)} {c['coin']}")
        except Exception as e:  # noqa: BLE001
            err(f"fresh {a[:8]}", e)


if __name__ == "__main__":
    main()
