"""Pure logic for the Insider Radar (no network, no file I/O) so it can be unit tested.

Everything that decides tiers, events, opens, diffs, alerts and the scorecard
lives here; insider.py only fetches data and calls these functions.
All thresholds live in CFG.
"""
from __future__ import annotations

import math
import statistics

H = 3_600_000  # one hour in ms

CFG = {
    # ---------------------------------------------------------------- events
    "event_min_vlm": 5_000_000,      # only markets with >= $5M 24h volume are screened
    "event_screen_pct": 3.0,         # cheap pre-screen on |mark/prevDay - 1| before fetching candles
    "event_min_pct": 5.0,            # move must be >= 5% ...
    "event_sigma": 3.0,              # ... and >= 3 daily sigmas
    "event_max_span_h": 12,
    "event_start_keep": 0.80,        # start = latest candle that still captures 80% of the move          # the move must happen within 12 hourly candles (sharp, not drift)
    "event_lookback_h": 36,          # search window for the move start
    "event_merge_h": 24,
    "event_max_checks": 12,          # candle checks per cycle (each ~40-60 weight)             # a second detection within 24h of the same start updates the same event
    "event_keep_d": 60,
    "event_attr_h": 72,              # keep attributing an event for 72h after detection
    "vol_days": 45,                  # daily candles for the sigma baseline
    # ----------------------------------------------------------- attribution
    "window_h": 48,                  # opens up to 48h before the move start count as pre-move
    "chaser_prior_h": 6,             # if price ran >= max(3%, 30% of move) in the 6h before the open -> chaser
    "chaser_min_pct": 3.0,
    "chaser_frac": 0.30,
    "attr_per_cycle": 40,            # position checks per event per cycle
    "attr_lb_top": 120,              # leaderboard day/week PnL toppers checked per event
    "attr_lb_closed_check": 30,      # of those, funding-check the top N even without an open position
    "attr_tape_min_usd": 100_000,    # tape net notional in the move direction to become a candidate
    "opener_min_usd": 100_000,       # ignore openers smaller than this
    # ------------------------------------------------------------- registry
    "large_open_usd": 250_000,       # scored large fresh open: >= max($250k, 5% equity)
    "large_open_eq_frac": 0.05,
    "open_build_h": 6,               # position size measured at its peak within 6h of the first fill
    "hit_window_h": 48,
    "hit_min_pct": 2.0,              # barrier = max(2%, 1.0 x daily sigma x sqrt(2)) — first touch wins
    "hit_sigma_k": 1.0,
    "decluster_h": 24,               # same-direction opens within 24h = one independent trial
    "event_indep_h": 48,             # event hits must be >= 48h apart to be independent
    "p0_floor": 0.08,
    "p0_default": 0.30,              # used until enough opens are scored across wallets
    "p0_min_n": 50,
    "high": {"indep_hits": 3, "indep_opens": 6, "p_val": 0.003, "expected_fp": 0.5, "min_hold_h": 1.5},
    "medium": {"closes": 20, "win_rate": 0.55, "profit_factor": 1.3, "pnl_90d": 0, "equity": 250_000, "min_hold_h": 1.0},
    "noise": {"maker_share": 0.90, "orders_per_day": 60, "min_hold_h": 1.0, "cross_dex_offset": 0.40, "pair_offset": 0.70},
    "fresh": {"age_d": 14, "pos_usd": 1_000_000, "alert_usd": 2_000_000},
    "offvenue_hedge_usd": 2_000_000,
    "stats_ttl_h": 24,
    "lookback_d": 90,
    # ------------------------------------------------------------ monitoring
    "min_action_usd": 50_000,
    "baseline_max_age_min": 40,      # diffs against an older snapshot are not alerted
    "max_tracked": 200,
    "sweep_every": 6,                # WATCH wallets: full all-dex sweep once every N cycles
    "new_sweeps_per_cycle": 10,      # first-time full sweeps of WATCH wallets per cycle (each costs ~24 weight)
    # ---------------------------------------------------------------- alerts
    "alert_high_open_usd": 500_000, "alert_high_open_eq": 0.10,
    "alert_medium_open_usd": 250_000,
    "alert_watch_open_usd": 250_000,
    "add_frac": 0.50,
    "cluster": {"n_entities": 3, "window_h": 6, "usd": 2_000_000},
    "dedup_h": 24,
    "chase_min_pct": 2.0, "chase_sigma1h_k": 0.75,
    "event_medium_openers": 3,
    "keep_alerts": 200,
    "fee_rt_pct": 0.09,              # taker fee round trip haircut for net_h24
    "horizons": {"h1": 1, "h4": 4, "h24": 24, "h72": 72},
    "outcome_live_tol_min": 25,
    "stale_min": 15,
    "tape_keep_d": 7,                # closed-hour tape files kept in the repo
}

COHORTS = {
    "repeat_premove": ("Repeat pre-move", "Opened before outsized moves on 3+ independent occasions, and the hit rate on all its "
                       "large fresh opens beats the base rate by a margin that survives a multiple-testing check."),
    "proven_speculator": ("Proven speculator", "Profitable 90-day record after fees (20+ closes, win rate 55%+, profit factor 1.3+), "
                          "holds positions long enough to copy. No proof of advance knowledge."),
    "fresh_whale": ("Fresh concentrated", "Account under 14 days old with one concentrated position of $1M+. An anomaly worth watching, unproven."),
    "one_hit": ("Unproven: one hit", "Found after the fact as a pre-move opener. One hit is not evidence of skill or information."),
    "hedger_mm": ("Hedger / market maker", "Mostly maker fills, very frequent orders, very short holds, or the position is offset elsewhere. Not a directional signal."),
    "unproven": ("Unproven", "In the registry but no qualifying evidence yet (e.g. a fresh account that aged, or a speculator whose stats lapsed)."),
}
TIER_ORDER = {"HIGH": 3, "MEDIUM": 2, "WATCH": 1, "NOISE": 0}


# ---------------------------------------------------------------- helpers
def dex_of(coin: str) -> str:
    return coin.split(":", 1)[0] if ":" in coin else ""


def underlying(coin: str) -> str:
    return coin.split(":", 1)[1] if ":" in coin else coin


def sgn(x):
    return 1 if x > 0 else -1 if x < 0 else 0


def side_of(x):
    return "long" if x > 0 else "short"


def r(x, n=2):
    return None if x is None else round(float(x), n)


def usd(x):
    x = abs(x or 0)
    return f"${x/1e6:.1f}M" if x >= 1e6 else f"${x/1e3:.0f}K"


def binom_tail(h: int, n: int, p0: float) -> float:
    """P(X >= h) for X ~ Binomial(n, p0)."""
    if n <= 0 or h <= 0:
        return 1.0
    return min(1.0, sum(math.comb(n, k) * p0 ** k * (1 - p0) ** (n - k) for k in range(h, n + 1)))


def daily_sigma(closes):
    """Std of daily log returns (fraction)."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 10:
        return None
    return statistics.pstdev(rets)


# ---------------------------------------------------------------- events
def find_move(c1h, cfg=CFG):
    """Locate the sharpest move in the recent 1h candles.

    c1h: list of dicts with t (ms), o, c (floats), oldest first.
    Returns {dir, start_ms, ref_px, peak_px, span_h} for the largest |log(close_j / open_i)| with
    j - i < max_span within the lookback; start_ms = open time of candle i (the inflection).
    """
    if not c1h:
        return None
    c = c1h[-cfg["event_lookback_h"]:]
    best = None
    for i in range(len(c)):
        o = c[i]["o"]
        if o <= 0:
            continue
        for j in range(i, min(len(c), i + cfg["event_max_span_h"])):
            m = math.log(c[j]["c"] / o)
            if best is None or abs(m) > abs(best[0]):
                best = (m, i, j)
    if best is None:
        return None
    m, i, j = best
    # tighten the start to the inflection: latest candle that still captures >= 90% of the move
    for k in range(j, i, -1):
        if c[k]["o"] > 0 and abs(math.log(c[j]["c"] / c[k]["o"])) >= cfg["event_start_keep"] * abs(m):
            i = k
            break
    return {"dir": "up" if m > 0 else "down", "start_ms": c[i]["t"], "ref_px": c[i]["o"], "peak_px": c[j]["c"],
            "span_h": j - i + 1, "log_move": m}


def classify_event(move, mark, sigma_d, cfg=CFG):
    """Decide whether a located move is an event. Uses the current mark vs the pre-move reference."""
    if not move or not sigma_d or mark is None:
        return None
    sign = 1 if move["dir"] == "up" else -1
    lm = math.log(mark / move["ref_px"])
    if sgn(lm) != sign:
        return None
    pct = (mark / move["ref_px"] - 1) * 100
    sig = abs(lm) / sigma_d
    if abs(pct) < cfg["event_min_pct"] or sig < cfg["event_sigma"]:
        return None
    return {"move_pct": round(pct, 2), "sigma": round(sig, 2)}


def pre_open_move(c1h, open_ms, direction, cfg=CFG):
    """Signed % move in the event direction over the chaser_prior_h hours before open_ms."""
    sign = 1 if direction in ("up", "long") else -1
    t0 = open_ms - cfg["chaser_prior_h"] * H
    before = [x for x in c1h if x["t"] <= t0]
    at = [x for x in c1h if x["t"] <= open_ms]
    if not before or not at:
        return None
    p0 = before[-1]["c"]
    p1 = at[-1]["c"]
    return round(sign * (p1 / p0 - 1) * 100, 2)


def is_chaser(opened_ms, start_ms, pre_move_pct, move_pct, cfg=CFG):
    if opened_ms is None:
        return False
    if opened_ms >= start_ms:
        return True
    lim = max(cfg["chaser_min_pct"], cfg["chaser_frac"] * abs(move_pct or 0))
    return pre_move_pct is not None and pre_move_pct >= lim


# ------------------------------------------------------- opens and fills
def first_open_fill(fills, coin, side, t_lo, t_hi):
    """Earliest fill in coin that opens from flat (startPosition == 0) in `side` within [t_lo, t_hi].

    fills: merged ordinary + TWAP fills (dicts with coin, time, px, sz, side, startPosition, dir).
    Same-ms fills are ordered by |startPosition| so the true first slice wins.
    """
    want = "B" if side == "long" else "A"
    cand = [f for f in fills if f.get("coin") == coin and t_lo <= f["time"] <= t_hi
            and float(f.get("startPosition") or 0) == 0 and f.get("side") == want]
    if not cand:
        return None
    cand.sort(key=lambda f: (f["time"], abs(float(f.get("startPosition") or 0))))
    return cand[0]


def open_from_funding(entries, coin, side, t_lo, t_hi, q_start):
    """Locate an open from hourly funding rows (which carry szi).

    Returns (status, open_hour_ms, max_abs_szi):
      status "opened"      first held at open_hour_ms (open happened in the hour before), no row the hour before
      status "held_before" position already held at the start of the queried range (not a fresh open)
      status "none"        no position in `side` within the window
    """
    want = 1 if side == "long" else -1
    rows = sorted([e for e in entries if e.get("coin") == coin], key=lambda e: e["time"])
    if not rows:
        return "none", None, 0.0
    hours = {e["time"] // H: float(e["szi"]) for e in rows}
    best = None
    for e in rows:
        h = e["time"] // H
        sz = float(e["szi"])
        if sgn(sz) != want:
            continue
        prev = hours.get(h - 1)
        fresh = prev is None or sgn(prev) != want
        if not fresh:
            continue
        if e["time"] - H < q_start + H:  # held at the very start of the query: pre-dates the window
            best = best or ("held_before", e["time"])
            continue
        if t_lo <= e["time"] - H and e["time"] - H <= t_hi:
            best = ("opened", e["time"])
            break
    if not best:
        return "none", None, 0.0
    mx = max((abs(float(e["szi"])) for e in rows if e["time"] >= best[1] and sgn(float(e["szi"])) == want), default=0.0)
    return best[0], best[1], mx


def lifecycles(fills, build_h=CFG["open_build_h"]):
    """Rebuild position lifecycles per coin from fills (oldest first).

    A lifecycle starts at a fill with startPosition == 0 and ends when the position returns to 0 or flips.
    Returns list of {coin, side, open_ms, open_px, peak_sz, peak_ntl, close_ms, via_twap}.
    """
    out, cur = [], {}
    for f in sorted(fills, key=lambda f: (f["time"], abs(float(f.get("startPosition") or 0)))):
        coin = f["coin"]
        if coin.startswith("@") or "/" in coin:  # spot
            continue
        sp = float(f.get("startPosition") or 0)
        sz = float(f["sz"]) * (1 if f.get("side") == "B" else -1)
        end = sp + sz
        lc = cur.get(coin)
        if sp == 0 and sz != 0:
            lc = cur[coin] = {"coin": coin, "side": side_of(sz), "open_ms": f["time"], "open_px": float(f["px"]),
                              "peak_sz": 0.0, "peak_ntl": 0.0, "close_ms": None, "via_twap": bool(f.get("_twap"))}
            out.append(lc)
        if lc is None:
            continue
        if f["time"] - lc["open_ms"] <= build_h * H and sgn(end) == (1 if lc["side"] == "long" else -1):
            if abs(end) > lc["peak_sz"]:
                lc["peak_sz"] = abs(end)
                lc["peak_ntl"] = abs(end) * float(f["px"])
        if end == 0 or sgn(end) != sgn(sp if sp else sz):
            lc["close_ms"] = f["time"]
            cur.pop(coin, None)
            if end != 0:  # flipped: a new lifecycle opens on the other side
                lc2 = cur[coin] = {"coin": coin, "side": side_of(end), "open_ms": f["time"], "open_px": float(f["px"]),
                                   "peak_sz": abs(end), "peak_ntl": abs(end) * float(f["px"]), "close_ms": None,
                                   "via_twap": bool(f.get("_twap"))}
                out.append(lc2)
    return out


def track_stats(fills, now_ms, truncated):
    """90d closing-order stats from merged fills (after fees), plus median hold from lifecycles."""
    orders = {}
    for f in fills:
        k = (f["coin"], f.get("oid"))
        o = orders.setdefault(k, {"pnl": 0.0, "fee": 0.0, "close": False, "maker": 0, "n": 0})
        o["pnl"] += float(f.get("closedPnl") or 0)
        o["fee"] += float(f.get("fee") or 0)
        o["n"] += 1
        o["maker"] += 0 if f.get("crossed") else 1
        d = f.get("dir") or ""
        if d.startswith("Close") or ">" in d or d.startswith("Liquidated"):
            o["close"] = True
    closes = [o for o in orders.values() if o["close"] and o["pnl"] != 0]
    net = [o["pnl"] - o["fee"] for o in closes]
    wins = [x for x in net if x > 0]
    losses = [-x for x in net if x <= 0]
    n_fills = sum(o["n"] for o in orders.values())
    t0 = min((f["time"] for f in fills), default=now_ms)
    days = max((now_ms - t0) / 86_400_000, 1)
    lcs = lifecycles(fills)
    holds = [(lc["close_ms"] - lc["open_ms"]) / H for lc in lcs if lc["close_ms"]]
    return {
        "closes": len(closes),
        "win_rate": round(len(wins) / len(closes), 4) if closes else None,
        "profit_factor": round(sum(wins) / sum(losses), 3) if losses and sum(losses) > 0 else (99.0 if wins else None),
        "pnl_90d": round(sum(net), 2),
        "maker_share": round(sum(o["maker"] for o in orders.values()) / n_fills, 3) if n_fills else None,
        "orders_per_day": round(len(orders) / days, 2),
        "median_hold_h": round(statistics.median(holds), 2) if holds else None,
        "span_start_ms": t0 if fills else None,
        "window_days": round(days, 1),
        "truncated": truncated,
    }


def large_opens(lcs, equity, cfg=CFG):
    thr = max(cfg["large_open_usd"], cfg["large_open_eq_frac"] * (equity or 0))
    return [lc for lc in lcs if lc["peak_ntl"] >= thr]


def score_open(open_ms, side, open_px, c1h, sigma_d, cfg=CFG):
    """First-touch barrier test. c1h: hourly candles covering [open, open+48h].

    Barrier X = max(hit_min_pct, k * daily_sigma * sqrt(window/24)). Hit if +X (in the open's direction)
    is touched before -X within the window. Also returns fwd_24h (% in direction, from open_px).
    Returns None if the window is not complete yet.
    """
    w = cfg["hit_window_h"]
    sign = 1 if side == "long" else -1
    seg = [x for x in c1h if open_ms - H < x["t"] <= open_ms + w * H]
    if not seg or seg[-1]["t"] < open_ms + (w - 1) * H:
        return None
    x = max(cfg["hit_min_pct"], 100 * cfg["hit_sigma_k"] * (sigma_d or 0) * math.sqrt(w / 24))
    hit = False
    for c in seg:
        if c["t"] + H <= open_ms:
            continue
        hi = sign * (c["h"] / open_px - 1) * 100 if sign > 0 else sign * (c["l"] / open_px - 1) * 100
        lo = sign * (c["l"] / open_px - 1) * 100 if sign > 0 else sign * (c["h"] / open_px - 1) * 100
        if lo <= -x and hi >= x:
            hit = False  # both inside one hour: unknown order, count as a miss (conservative)
            break
        if hi >= x:
            hit = True
            break
        if lo <= -x:
            break
    at24 = [c for c in seg if c["t"] <= open_ms + 24 * H]
    fwd24 = sign * (at24[-1]["c"] / open_px - 1) * 100 if at24 else None
    return {"hit": hit, "barrier_pct": round(x, 2), "fwd_24h": r(fwd24, 2)}


def decluster(opens, cfg=CFG):
    """Group same-direction opens within decluster_h into one trial (represented by the largest)."""
    trials = []
    for o in sorted(opens, key=lambda o: o["open_ms"]):
        for t in trials:
            if t["side"] == o["side"] and abs(o["open_ms"] - t["open_ms"]) <= cfg["decluster_h"] * H:
                if o.get("peak_ntl", 0) > t["rep"].get("peak_ntl", 0):
                    t["rep"] = o
                break
        else:
            trials.append({"side": o["side"], "open_ms": o["open_ms"], "rep": o})
    return [t["rep"] for t in trials]


def independent_event_hits(hits, cfg=CFG):
    """Count event hits whose move starts are >= event_indep_h apart."""
    n, last = 0, None
    for h in sorted(hits, key=lambda h: h["start_ms"]):
        if h.get("chaser"):
            continue
        if last is None or h["start_ms"] - last >= cfg["event_indep_h"] * H:
            n += 1
            last = h["start_ms"]
    return n


def premove_block(w, p0, n_tested, cfg=CFG):
    scored = [o for o in w.get("opens", []) if o.get("score") and not o.get("discovery")]
    trials = decluster(scored, cfg)
    n = len(trials)
    h = sum(1 for o in trials if o["score"]["hit"])
    fwd = [o["score"]["fwd_24h"] for o in scored if o["score"].get("fwd_24h") is not None]
    p_val = binom_tail(h, n, p0) if n else None
    ev_hits = [x for x in w.get("event_hits", []) if not x.get("chaser")]
    return {
        "hits": len(ev_hits),
        "independent_hits": independent_event_hits(ev_hits, cfg),
        "opens_scored": len(scored),
        "independent_opens": n,
        "independent_open_hits": h,
        "hit_rate": round(h / n, 3) if n else None,
        "avg_fwd_24h": round(sum(fwd) / len(fwd), 2) if fwd else None,
        "base_rate_p0": round(p0, 3),
        "p_val": round(p_val, 5) if p_val is not None else None,
        "n_wallets_tested": n_tested,
        "expected_fp": round(n_tested * p_val, 3) if p_val is not None else None,
        "truncated": bool((w.get("stats") or {}).get("truncated")),
        "live_opens": sum(1 for o in w.get("opens", []) if o.get("live")),
    }


def is_hedged(positions, cfg=CFG):
    """Cross-dex same-underlying offset or opposite BTC/ETH pair. positions: list of {coin, szi, ntl}."""
    by_u = {}
    for p in positions:
        by_u.setdefault(underlying(p["coin"]), []).append(p)
    for u, ps in by_u.items():
        lng = sum(p["ntl"] for p in ps if p["szi"] > 0)
        sht = sum(p["ntl"] for p in ps if p["szi"] < 0)
        if lng and sht and min(lng, sht) >= cfg["noise"]["cross_dex_offset"] * max(lng, sht):
            return f"{u} long {usd(lng)} vs short {usd(sht)} across venues"
    b = [p for p in positions if p["coin"] == "BTC"]
    e = [p for p in positions if p["coin"] == "ETH"]
    if b and e and sgn(b[0]["szi"]) != sgn(e[0]["szi"]):
        if min(b[0]["ntl"], e[0]["ntl"]) >= cfg["noise"]["pair_offset"] * max(b[0]["ntl"], e[0]["ntl"]):
            return "BTC/ETH opposite pair trade"
    return None


def assign_tier(w, prem, cfg=CFG):
    """Return (tier, cohort, why[]) for a registry wallet. Pure function of the wallet record."""
    st = w.get("stats") or {}
    nz = cfg["noise"]
    why = []
    hold = st.get("median_hold_h")
    if w.get("role") == "vault":
        return "NOISE", "hedger_mm", ["Hyperliquid reports this address as a vault"]
    if (st.get("maker_share") or 0) > nz["maker_share"]:
        return "NOISE", "hedger_mm", [f"{st['maker_share']*100:.0f}% of fills are maker (market-making profile)"]
    if (st.get("orders_per_day") or 0) > nz["orders_per_day"]:
        return "NOISE", "hedger_mm", [f"{st['orders_per_day']:.0f} orders/day (too frequent to copy)"]
    if hold is not None and hold < nz["min_hold_h"] and (st.get("closes") or 0) >= 10:
        return "NOISE", "hedger_mm", [f"median hold {hold:.1f}h (too fast to copy)"]
    if w.get("hedge"):
        return "NOISE", "hedger_mm", [f"Offset position: {w['hedge']}"]

    hi = cfg["high"]
    if (prem["independent_hits"] >= hi["indep_hits"] and prem["independent_opens"] >= hi["indep_opens"]
            and prem["p_val"] is not None and prem["p_val"] <= hi["p_val"]
            and prem["expected_fp"] is not None and prem["expected_fp"] < hi["expected_fp"]
            and (prem["avg_fwd_24h"] or 0) > 0 and hold is not None and hold >= hi["min_hold_h"]):
        why.append(f"{prem['independent_hits']} independent pre-move hits")
        why.append(f"{prem['independent_open_hits']}/{prem['independent_opens']} independent large opens hit "
                   f"(base rate {prem['base_rate_p0']*100:.0f}%, p={prem['p_val']:.4f}, "
                   f"expected false positives {prem['expected_fp']:.2f} across {prem['n_wallets_tested']} wallets)")
        return "HIGH", "repeat_premove", why

    md = cfg["medium"]
    eq = w.get("equity") or 0
    open_upnl = sum(p.get("upnl") or 0 for p in w.get("positions") or [])
    if (st.get("closes", 0) >= md["closes"] and (st.get("win_rate") or 0) >= md["win_rate"]
            and (st.get("profit_factor") or 0) >= md["profit_factor"] and (st.get("pnl_90d") or 0) > md["pnl_90d"]
            and (st.get("pnl_90d") or 0) + open_upnl > 0
            and eq >= md["equity"] and (hold is None or hold >= md["min_hold_h"])):
        why.append(f"{st['closes']} closes in {st.get('window_days') or 90:.0f}d, win rate {st['win_rate']*100:.0f}%, "
                   f"profit factor {min(st['profit_factor'], 99):.2f}, after-fee PnL {usd(st['pnl_90d'])}"
                   + (f", open uPnL {'-' if open_upnl < 0 else ''}{usd(open_upnl)}" if open_upnl else ""))
        if prem["hits"]:
            why.append(f"MEDIUM+ : {prem['hits']} pre-move hit(s), not enough for HIGH")
        if hold is None or st.get("truncated") or st.get("provisional"):
            why.append("Provisional: hold time not measured from a complete fill history, so WATCH until it is")
            return "WATCH", "proven_speculator", why
        return "MEDIUM", "proven_speculator", why

    fr = cfg["fresh"]
    big = max([p.get("notional") or 0 for p in w.get("positions", [])] or [0])
    if w.get("age_days") is not None and w["age_days"] < fr["age_d"] and big >= fr["pos_usd"]:
        return "WATCH", "fresh_whale", [f"Account {w['age_days']:.0f} days old holding a {usd(big)} position (unproven anomaly)"]
    if prem["hits"]:
        why.append(f"{prem['hits']} pre-move hit(s) found after the fact; "
                   f"{prem['independent_opens']} other large opens scored" + (f", {prem['independent_open_hits']} hit" if prem['independent_opens'] else ""))
        return "WATCH", "one_hit", why
    return "WATCH" if w.get("discovery_source") in ("live_tape", "seed") else "NOISE", "unproven", ["No qualifying evidence yet"]


def wallet_score(w, prem, cfg=CFG):
    """0-100 ranking score. Shrunk hit rate (Beta prior at the base rate) x sample, plus record quality and size."""
    p0 = prem["base_rate_p0"]
    n, h = prem["independent_opens"], prem["independent_open_hits"]
    a, b = 3 * p0, 3 * (1 - p0)
    shrunk = (h + a) / (n + a + b)
    pm = max(0.0, min(1.0, (shrunk - p0) / max(1 - p0, 1e-9) * 2)) * min(1.0, n / 10)
    ev = min(prem["independent_hits"], 3) / 3
    st = w.get("stats") or {}
    q = 0.0
    if st.get("win_rate") is not None and st.get("closes", 0) >= 10:
        q = 0.5 * min(max((st["win_rate"] - 0.5) / 0.3, 0), 1) + 0.5 * min(math.log(max(min(st.get("profit_factor") or 1, 10), 1)) / math.log(5), 1)
    eq = w.get("equity") or 0
    cap = min(max((math.log10(eq) - 5.4) / 1.6, 0), 1) if eq > 0 else 0
    s = 100 * (0.35 * pm + 0.25 * ev + 0.25 * q + 0.15 * cap)
    if w.get("tier") == "NOISE":
        s *= 0.2
    return round(s, 1)


# ------------------------------------------------------------ monitoring
def diff_positions(prev, cur, marks, cfg=CFG):
    """Diff one wallet's per-dex snapshots.

    prev/cur: {dex: {"ok": bool, "ts": ms, "pos": {coin: {"szi": float, "entry": float}}}}
    Only dexs that were OK in both snapshots (and prev not too old) are diffed.
    Returns list of actions {type, coin, side, szi_before, szi_after, delta_usd, notional, entry, alertable}.
    """
    out = []
    for dex, c in cur.items():
        p = prev.get(dex)
        if not c.get("ok") or not p or not p.get("ok"):
            continue
        age_min = (c["ts"] - p["ts"]) / 60000
        alertable = age_min <= cfg["baseline_max_age_min"]
        coins = set(c["pos"]) | set(p["pos"])
        for coin in coins:
            b = p["pos"].get(coin, {}).get("szi", 0.0)
            a = c["pos"].get(coin, {}).get("szi", 0.0)
            if a == b:
                continue
            px = marks.get(coin) or c["pos"].get(coin, {}).get("entry") or p["pos"].get(coin, {}).get("entry") or 0
            d_usd = (a - b) * px
            if abs(d_usd) < cfg["min_action_usd"] and not (a == 0 or b == 0 or sgn(a) != sgn(b)):
                continue
            if b == 0:
                t = "open"
            elif a == 0:
                t = "close"
            elif sgn(a) != sgn(b):
                t = "flip"
            elif abs(a) > abs(b):
                t = "add"
            else:
                t = "reduce"
            if t in ("open", "close", "flip") and max(abs(a), abs(b)) * px < cfg["min_action_usd"]:
                continue
            out.append({"type": t, "coin": coin, "side": side_of(a if a else b), "szi_before": b, "szi_after": a,
                        "delta_usd": round(d_usd), "notional": round(abs(a) * px), "prev_notional": round(abs(b) * px),
                        "entry": c["pos"].get(coin, {}).get("entry"), "alertable": alertable, "ts": c["ts"]})
    return out


def chase_pct(side, entry, mark):
    if not entry or not mark:
        return None
    return round((1 if side == "long" else -1) * (mark / entry - 1) * 100, 2)


def chase_limit(sigma_d, cfg=CFG):
    s1h = (sigma_d or 0) / math.sqrt(24) * 100
    return max(cfg["chase_min_pct"], cfg["chase_sigma1h_k"] * s1h)


def downgrade(p):
    return {"HIGH": "MEDIUM", "MEDIUM": "INFO"}.get(p, p)


def alert_for_action(act, w, mark, sigma_d, alerted_open, cfg=CFG):
    """Map a monitored action to (priority, type, extra_why) or None. Pure."""
    tier = w.get("tier")
    eq = w.get("equity") or 0
    t = act["type"]
    if not act.get("alertable"):
        return None
    if t in ("close", "flip") and alerted_open:
        return ("MEDIUM", f"tracked_{t}", ["Closes/flips a position we alerted on earlier"])
    if t == "open":
        if tier == "HIGH" and act["notional"] >= max(cfg["alert_high_open_usd"], cfg["alert_high_open_eq"] * eq):
            pr = "HIGH"
        elif tier == "MEDIUM" and act["notional"] >= cfg["alert_medium_open_usd"]:
            pr = "MEDIUM"
        elif tier == "WATCH" and act["notional"] >= cfg["alert_watch_open_usd"]:
            pr = "INFO"
            if w.get("cohort") == "fresh_whale" and ":" in act["coin"] and act["notional"] >= cfg["fresh"]["alert_usd"]:
                return ("MEDIUM", "fresh_whale", ["Unproven anomaly: very new account, concentrated position"])
        else:
            return None
        return (pr, "tracked_open", [])
    if t == "add" and tier == "HIGH":
        before = abs(act["szi_before"])
        if before and (abs(act["szi_after"]) - before) / before >= cfg["add_frac"]:
            return ("MEDIUM", "tracked_add", [])
    if t == "flip" and tier == "HIGH":
        return ("MEDIUM", "tracked_flip", [])
    return None


def apply_chase(priority, side, entry, mark, sigma_d, cfg=CFG):
    ch = chase_pct(side, entry, mark)
    lim = chase_limit(sigma_d, cfg)
    if ch is not None and ch > lim and priority in ("HIGH", "MEDIUM"):
        return downgrade(priority), ch, f"Late: price already moved {ch:.1f}% since the wallet's entry"
    return priority, ch, None


def find_clusters(actions, wallets, now_ms, cfg=CFG):
    """>= n distinct entities at MEDIUM+ opening the same coin/side within window_h, combined >= usd."""
    cl = cfg["cluster"]
    groups = {}
    for a in actions:
        if a["type"] not in ("open", "add") or now_ms - a["ts"] > cl["window_h"] * H:
            continue
        w = wallets.get(a["address"]) or {}
        if TIER_ORDER.get(w.get("tier"), 0) < 2:
            continue
        g = groups.setdefault((a["coin"], a["side"]), {})
        ent = w.get("entity_id") or a["address"]
        g[ent] = g.get(ent, 0) + abs(a.get("delta_usd") or a.get("notional") or 0)
    out = []
    for (coin, side), ents in groups.items():
        if len(ents) >= cl["n_entities"] and sum(ents.values()) >= cl["usd"]:
            out.append({"coin": coin, "side": side, "n_entities": len(ents), "usd": round(sum(ents.values()))})
    return out


def dedup_ok(dedup, key, now_ms, hours):
    t = dedup.get(key)
    return t is None or now_ms - t >= hours * H


# ------------------------------------------------------------- scorecard
def fill_outcomes(alert, now_ms, mark, cfg=CFG, hist_px=None):
    """Fill signed forward returns (% in alert direction) from the alert-time mark.

    mark: current mark (used if the horizon came due within outcome_live_tol_min);
    hist_px: optional fn(due_ms) -> price for late fills.
    """
    if not alert.get("px") or alert.get("side") not in ("long", "short"):
        return alert
    sign = 1 if alert["side"] == "long" else -1
    oc = alert.setdefault("outcome", {k: None for k in cfg["horizons"]})
    for k, h in cfg["horizons"].items():
        if oc.get(k) is not None:
            continue
        due = alert["ts_ms"] + h * H
        if now_ms < due:
            continue
        px = None
        if mark and now_ms - due <= cfg["outcome_live_tol_min"] * 60000:
            px = mark
        elif hist_px:
            px = hist_px(due)
        if px:
            oc[k] = round(sign * (px / alert["px"] - 1) * 100, 2)
    if oc.get("h24") is not None:
        alert["net_h24"] = round(oc["h24"] - cfg["fee_rt_pct"] - (alert.get("spread_bps") or 0) / 100, 2)
    return alert


def scorecard(alerts, cfg=CFG):
    live = [a for a in alerts if a.get("type") != "event" and a.get("side")]

    def block(xs):
        sc = [a for a in xs if (a.get("outcome") or {}).get("h24") is not None]
        return {"n": len(xs), "scored": len(sc),
                "hit_rate_24h": round(sum(1 for a in sc if a["outcome"]["h24"] > 0) / len(sc), 3) if sc else None,
                "avg_fwd_24h": round(sum(a["outcome"]["h24"] for a in sc) / len(sc), 2) if sc else None}

    allb = block(live)
    return {"n_alerts": allb["n"], "n_scored": allb["scored"], "hit_rate_24h": allb["hit_rate_24h"],
            "avg_fwd_24h": allb["avg_fwd_24h"],
            "by_priority": {p: {k: v for k, v in block([a for a in live if a["priority"] == p]).items() if k != "scored"}
                            for p in ("HIGH", "MEDIUM", "INFO")},
            "note": "Strictly prospective: only alerts emitted live by this collector, measured from the mark price at "
                    "alert time (what a copy-trader could get). Event alerts are excluded. Small samples mean nothing yet."}
