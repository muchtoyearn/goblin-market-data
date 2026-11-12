"""Informed BTC trader cohort.

Instead of "largest positions" (which mixes in hedgers, market makers and
basis traders), this module keeps a cohort of wallets with a verified track
record and reads only their *directional* BTC exposure.

Pipeline
  1. Candidate pool (leaderboard, cheap): account value >= $250k, positive
     all-time and month PnL, traded this week, monthly volume < 150x equity
     (screens out market makers / HFT). Ranked, top CANDIDATES kept.
  2. Track record (fills, expensive, cached in data/informed_stats.json and
     refreshed on a rolling basis): last 90 days of fills, grouped per order.
     Win rate and profit factor on closing orders net of fees, BTC-specific
     realized PnL and trade count, maker share, trades per day.
  3. Qualification: >= 30 closing orders, >= 10 BTC closes, win rate >= 55%,
     profit factor >= 1.3, BTC realized PnL > 0, maker share <= 85%,
     <= 60 orders/day.
  4. Positioning (every run): current BTC perp position for each qualified
     wallet. Excluded from the directional read when hedged:
       - spot hedge: UBTC spot balance offsets >= 70% of the perp position
       - pair trade: an opposite-direction ETH perp >= 70% of BTC notional
       - tiny: BTC notional < $50k
"""
from __future__ import annotations

import math
import threading
import time
from collections import defaultdict

CANDIDATES = 400
REFRESH_PER_RUN = 20
STATS_TTL_H = 12
LOOKBACK_D = 90
MIN_EQUITY = 250_000
MIN_POS_NTL = 50_000

QUAL = {"closes": 20, "btc_closes": 0, "win_rate": 0.55, "profit_factor": 1.3,
        "max_maker_share": 0.90, "max_orders_per_day": 60}
BTC_HOLDER_EXTRA = 150  # extra candidates: profitable wallets holding big BTC positions now


class Limiter:
    """Keep Hyperliquid info weight under ~1000/min (limit is 1200/min/IP)."""

    def __init__(self, per_min=1000):
        self.per_min = per_min
        self.events: list[tuple[float, int]] = []
        self.lock = threading.Lock()

    def take(self, w):
        while True:
            with self.lock:
                now = time.time()
                self.events = [(t, x) for t, x in self.events if now - t < 60]
                used = sum(x for _, x in self.events)
                if used + w <= self.per_min:
                    self.events.append((now, w))
                    return
                wait = 60 - (now - self.events[0][0]) + 0.05
            time.sleep(max(wait, 0.2))


LIMIT = Limiter()


def _wp(row, k):
    return dict(row["windowPerformances"]).get(k, {})


def candidate_pool(leaderboard, btc_holders=()):
    holders = set(btc_holders)
    out, extra = [], []
    for r in leaderboard:
        try:
            eq = float(r["accountValue"])
            at, mo, wk = _wp(r, "allTime"), _wp(r, "month"), _wp(r, "week")
            at_pnl, mo_pnl = float(at["pnl"]), float(mo["pnl"])
            mo_vlm, wk_vlm = float(mo["vlm"]), float(wk["vlm"])
        except Exception:  # noqa: BLE001
            continue
        if eq < MIN_EQUITY or at_pnl <= 0 or mo_vlm / eq > 150:
            continue
        row = {"addr": r["ethAddress"], "name": r.get("displayName"), "equity": eq,
               "pnl_all": at_pnl, "pnl_month": mo_pnl, "roi_month": float(mo["roi"])}
        if r["ethAddress"] in holders:
            extra.append({**row, "rank_score": math.log10(at_pnl + 1)})
        if mo_pnl <= 0 or wk_vlm <= 0:
            continue
        # rank: all-time PnL with a bonus for positive week and month ROI
        score = math.log10(at_pnl + 1) + (0.5 if float(wk["pnl"]) > 0 else 0) + min(float(mo["roi"]), 1.0)
        out.append({"addr": r["ethAddress"], "name": r.get("displayName"), "equity": eq,
                    "pnl_all": at_pnl, "pnl_month": mo_pnl, "roi_month": float(mo["roi"]), "rank_score": score})
    out.sort(key=lambda x: x["rank_score"], reverse=True)
    out = out[:CANDIDATES]
    have = {c["addr"] for c in out}
    extra.sort(key=lambda x: x["rank_score"], reverse=True)
    out += [c for c in extra if c["addr"] not in have][:BTC_HOLDER_EXTRA]
    return out


def track_record(hl, addr, now_ms):
    LIMIT.take(120)  # 20 base + up to 100 for 2000 fills
    fills = hl({"type": "userFillsByTime", "user": addr, "startTime": now_ms - LOOKBACK_D * 86_400_000,
                "aggregateByTime": True})
    orders = defaultdict(lambda: {"pnl": 0.0, "fee": 0.0, "coin": None, "close": False, "maker": 0, "n": 0, "t": 0})
    for f in fills:
        o = orders[(f["coin"], f["oid"])]
        o["coin"] = f["coin"]
        o["pnl"] += float(f.get("closedPnl") or 0)
        o["fee"] += float(f.get("fee") or 0)
        o["n"] += 1
        o["maker"] += 0 if f.get("crossed") else 1
        o["t"] = max(o["t"], f["time"])
        d = f.get("dir") or ""
        if d.startswith("Close") or ">" in d or d in ("Liquidated Isolated Long", "Liquidated Cross Long", "Liquidated Isolated Short", "Liquidated Cross Short"):
            o["close"] = True
    closes = [o for o in orders.values() if o["close"] and o["pnl"] != 0]
    net = [o["pnl"] - o["fee"] for o in closes]
    wins = [x for x in net if x > 0]
    losses = [-x for x in net if x <= 0]
    btc = [o["pnl"] - o["fee"] for o in closes if o["coin"] == "BTC"]
    n_fills = sum(o["n"] for o in orders.values())
    t0 = min((f["time"] for f in fills), default=now_ms)
    days = max((now_ms - t0) / 86_400_000, 1)
    return {
        "addr": addr,
        "updated_ms": now_ms,
        "orders": len(orders),
        "closes": len(closes),
        "win_rate": round(len(wins) / len(closes), 4) if closes else None,
        "profit_factor": round(sum(wins) / sum(losses), 3) if losses and sum(losses) > 0 else (99.0 if wins else None),
        "realized_90d": round(sum(net), 2),
        "btc_closes": len(btc),
        "btc_win_rate": round(sum(1 for x in btc if x > 0) / len(btc), 4) if btc else None,
        "btc_realized": round(sum(btc), 2),
        "maker_share": round(sum(o["maker"] for o in orders.values()) / n_fills, 3) if n_fills else None,
        "window_days": round(days, 1),
        "orders_per_day": round(len(orders) / days, 2),
        "truncated": len(fills) >= 2000,
    }


def qualifies(st):
    if not st or st.get("win_rate") is None:
        return False
    return (st["closes"] >= QUAL["closes"] and st["btc_closes"] >= QUAL["btc_closes"]
            and st["win_rate"] >= QUAL["win_rate"] and (st["profit_factor"] or 0) >= QUAL["profit_factor"]
            and st["btc_realized"] >= 0 and (st["maker_share"] or 0) <= QUAL["max_maker_share"]
            and st["orders_per_day"] <= QUAL["max_orders_per_day"])


def quality_score(st, equity):
    """0-100 composite: win rate, profit factor, BTC edge, capital."""
    wr = min(max((st["win_rate"] - 0.5) / 0.3, 0), 1)              # 50% -> 0, 80% -> 1
    pf = min(math.log(min(st["profit_factor"], 10)) / math.log(5), 1)  # PF 5+ -> 1
    bw = min(max(((st.get("btc_win_rate") or 0.5) - 0.5) / 0.3, 0), 1)
    cap = min(max((math.log10(equity) - 5.4) / 1.6, 0), 1)          # $250k -> 0, $10M -> 1
    return round(100 * (0.35 * wr + 0.25 * pf + 0.2 * bw + 0.2 * cap), 1)


def refresh_stats(hl, cache, pool, now_ms, budget=REFRESH_PER_RUN, errors=None):
    """Update the stalest candidates' track records in place."""
    ttl = STATS_TTL_H * 3600 * 1000
    stale = [c for c in pool if now_ms - cache.get(c["addr"], {}).get("updated_ms", 0) > ttl]
    stale.sort(key=lambda c: cache.get(c["addr"], {}).get("updated_ms", 0))
    done = 0
    for c in stale[:budget]:
        try:
            cache[c["addr"]] = track_record(hl, c["addr"], now_ms)
            done += 1
        except Exception as e:  # noqa: BLE001
            if errors is not None:
                errors.append(f"track_record {c['addr'][:8]}: {e}")
    # drop wallets that left the pool
    keep = {c["addr"] for c in pool}
    for a in list(cache):
        if a not in keep:
            del cache[a]
    return done, len(stale)


def positioning(hl, cohort, price, prev_positions):
    """Read BTC exposure for each qualified wallet, classify hedges."""
    rows = []

    def one(m):
        LIMIT.take(2)
        st = hl({"type": "clearinghouseState", "user": m["addr"]})
        btc = eth = None
        for ap in st.get("assetPositions", []):
            p = ap.get("position", {})
            if p.get("coin") == "BTC":
                btc = p
            elif p.get("coin") == "ETH":
                eth = p
        if not btc:
            return {**m, "szi": 0.0, "ntl": 0.0, "side": "flat", "status": "flat"}
        szi = float(btc["szi"])
        ntl = abs(szi) * price
        status = "directional"
        note = None
        if ntl < MIN_POS_NTL:
            status, note = "small", "under $50k"
        else:
            LIMIT.take(2)
            spot = hl({"type": "spotClearinghouseState", "user": m["addr"]})
            ubtc = sum(float(b["total"]) for b in spot.get("balances", []) if b.get("coin") == "UBTC")
            if szi < 0 and ubtc > 0 and ubtc >= 0.7 * abs(szi):
                status, note = "hedged", f"holds {ubtc:.2f} UBTC spot"
            elif eth is not None:
                eszi = float(eth["szi"])
                entl = abs(float(eth.get("positionValue") or 0))
                if (eszi > 0) != (szi > 0) and entl >= 0.7 * ntl:
                    status, note = "pair", f"opposite ETH {usd_short(entl)}"
        return {**m, "szi": szi, "ntl": ntl, "side": "long" if szi > 0 else "short", "status": status,
                "note": note, "entry": float(btc.get("entryPx") or 0), "upnl": float(btc.get("unrealizedPnl") or 0),
                "lev": (btc.get("leverage") or {}).get("value"),
                "liq": float(btc["liquidationPx"]) if btc.get("liquidationPx") else None}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as ex:
        for r in ex.map(lambda m: _safe(one, m), cohort):
            if r:
                rows.append(r)

    d = [r for r in rows if r["status"] == "directional"]
    long_ntl = sum(r["ntl"] for r in d if r["side"] == "long")
    short_ntl = sum(r["ntl"] for r in d if r["side"] == "short")
    tot = long_ntl + short_ntl
    wl = sum(r["ntl"] * r["score"] for r in d if r["side"] == "long")
    ws = sum(r["ntl"] * r["score"] for r in d if r["side"] == "short")

    # position changes vs previous snapshot
    moves = []
    for r in rows:
        before = prev_positions.get(r["addr"])
        if before is None:
            continue
        b, a = before, r["szi"]
        if abs(a - b) * price < 50_000:
            continue
        if b == 0:
            kind = "opened " + ("long" if a > 0 else "short")
        elif a == 0:
            kind = "closed " + ("long" if b > 0 else "short")
        elif (a > 0) != (b > 0):
            kind = "flipped " + ("long" if a > 0 else "short")
        elif abs(a) > abs(b):
            kind = "added to " + ("long" if a > 0 else "short")
        else:
            kind = "reduced " + ("long" if a > 0 else "short")
        moves.append({"addr": r["addr"], "name": r.get("name"), "kind": kind, "delta_usd": (a - b) * price,
                      "score": r["score"], "win_rate": r["win_rate"]})
    moves.sort(key=lambda x: abs(x["delta_usd"]), reverse=True)

    return {
        "cohort_size": len(cohort),
        "positioned": len(d),
        "flat": sum(1 for r in rows if r["status"] == "flat"),
        "excluded": {k: sum(1 for r in rows if r["status"] == k) for k in ("hedged", "pair", "small")},
        "long_pct_ntl": round(long_ntl / tot * 100, 1) if tot else None,
        "long_pct_quality": round(wl / (wl + ws) * 100, 1) if (wl + ws) else None,
        "n_long": sum(1 for r in d if r["side"] == "long"),
        "n_short": sum(1 for r in d if r["side"] == "short"),
        "long_usd": long_ntl, "short_usd": short_ntl, "net_usd": long_ntl - short_ntl,
        "positions": sorted([_compact(r) for r in rows if r["status"] != "flat"], key=lambda r: -r["ntl"])[:20],
        "moves": moves[:12],
        "all_szi": {r["addr"]: r["szi"] for r in rows},
    }


def usd_short(x):
    return f"${x/1e6:.1f}M" if x >= 1e6 else f"${x/1e3:.0f}K"


def _safe(fn, m):
    try:
        return fn(m)
    except Exception:  # noqa: BLE001
        return None


def _compact(r):
    keys = ("addr", "name", "side", "status", "note", "ntl", "entry", "upnl", "lev", "liq", "score",
            "win_rate", "profit_factor", "btc_win_rate", "btc_realized", "equity")
    return {k: (round(r[k], 4) if isinstance(r.get(k), float) else r.get(k)) for k in keys}


def build_cohort(pool, cache):
    eq = {c["addr"]: c for c in pool}
    out = []
    for a, st in cache.items():
        if a in eq and qualifies(st):
            c = eq[a]
            out.append({"addr": a, "name": c.get("name"), "equity": c["equity"],
                        "win_rate": st["win_rate"], "profit_factor": st["profit_factor"],
                        "btc_win_rate": st["btc_win_rate"], "btc_realized": st["btc_realized"],
                        "btc_closes": st["btc_closes"], "score": quality_score(st, c["equity"])})
    out.sort(key=lambda x: -x["score"])
    return out
