"""Fixture-based tests for the Insider Radar pure logic."""
import math

import insider_core as core

H = core.H
T = 1_795_000_000_000 // H * H  # an hour boundary


def candles(prices, t0=T):
    out = []
    for i, p in enumerate(prices):
        o = prices[i - 1] if i else p
        out.append({"t": t0 + i * H, "o": o, "c": p, "h": max(o, p), "l": min(o, p)})
    return out


# ------------------------------------------------------------------ events
def test_find_move_picks_inflection_not_drift():
    # slow drift 100 -> 103 over 20h, then jump to 125 in 2h
    px = [100 + 0.15 * i for i in range(20)] + [110, 125, 125]
    c = candles(px)
    mv = core.find_move(c)
    assert mv["dir"] == "up"
    assert mv["start_ms"] == c[20]["t"]  # the jump candle, not the start of the drift
    ev = core.classify_event(mv, 125, 0.02)
    assert ev and ev["move_pct"] > 10 and ev["sigma"] > 3


def test_classify_rejects_normal_move_for_volatile_market():
    mv = {"dir": "up", "ref_px": 100.0}
    assert core.classify_event(mv, 108, 0.05) is None  # 8% but only 1.5 sigma
    assert core.classify_event(mv, 104, 0.001) is None  # many sigma but < 5%


def test_chaser_rules():
    start = T + 10 * H
    assert core.is_chaser(start + 1, start, 0.0, 20) is True        # opened after start
    assert core.is_chaser(start - H, start, 7.0, 20) is True         # price already ran >= 6% (30% of 20)
    assert core.is_chaser(start - H, start, 1.0, 20) is False


# ------------------------------------------------------------- open finding
def test_first_open_fill_requires_start_position_zero_and_orders_same_ms():
    fills = [
        {"coin": "xyz:NVDA", "time": T + 5, "px": "132.82", "sz": "1", "side": "B", "startPosition": "27.256"},
        {"coin": "xyz:NVDA", "time": T + 5, "px": "132.81", "sz": "27.256", "side": "B", "startPosition": "0.0", "_twap": True},
        {"coin": "xyz:NVDA", "time": T + 1, "px": "130", "sz": "5", "side": "A", "startPosition": "0.0"},  # a short open
        {"coin": "HYPE", "time": T, "px": "30", "sz": "5", "side": "B", "startPosition": "0.0"},
    ]
    f = core.first_open_fill(fills, "xyz:NVDA", "long", T, T + H)
    assert f["px"] == "132.81" and f["_twap"]


def test_open_from_funding_fresh_vs_held_before():
    q0 = T
    rows = [{"time": T + h * H, "coin": "xyz:NVDA", "szi": "100"} for h in range(10, 20)]
    st, oh, mx = core.open_from_funding(rows, "xyz:NVDA", "long", T + 2 * H, T + 30 * H, q0)
    assert st == "opened" and oh == T + 10 * H and mx == 100
    held = [{"time": T + h * H, "coin": "xyz:NVDA", "szi": "100"} for h in range(0, 20)]
    st, _, _ = core.open_from_funding(held, "xyz:NVDA", "long", T + 2 * H, T + 30 * H, q0)
    assert st == "held_before"
    st, _, _ = core.open_from_funding(rows, "xyz:NVDA", "short", T, T + 30 * H, q0)
    assert st == "none"


def test_lifecycles_peak_and_close():
    fills = [
        {"coin": "ETH", "time": T, "px": "100", "sz": "10", "side": "B", "startPosition": "0"},
        {"coin": "ETH", "time": T + H, "px": "101", "sz": "20", "side": "B", "startPosition": "10"},
        {"coin": "ETH", "time": T + 3 * H, "px": "105", "sz": "30", "side": "A", "startPosition": "30"},
    ]
    lc = core.lifecycles(fills)
    assert len(lc) == 1
    assert lc[0]["peak_sz"] == 30 and lc[0]["close_ms"] == T + 3 * H and lc[0]["side"] == "long"


# ----------------------------------------------------------------- scoring
def test_score_open_first_touch():
    up = candles([100, 101, 103, 105] + [105] * 50)
    s = core.score_open(T, "long", 100, up, 0.01)
    assert s["hit"] is True and s["barrier_pct"] == 2.0
    down = candles([100, 99, 97, 105] + [105] * 50)
    assert core.score_open(T, "long", 100, down, 0.01)["hit"] is False  # -2% touched first
    short = candles([100, 99, 97] + [97] * 50)
    assert core.score_open(T, "short", 100, short, 0.01)["hit"] is True
    assert core.score_open(T, "long", 100, candles([100, 101]), 0.01) is None  # window incomplete


def test_binomial_tail():
    assert math.isclose(core.binom_tail(1, 1, 0.3), 0.3)
    assert math.isclose(core.binom_tail(6, 6, 0.3), 0.3 ** 6)
    assert core.binom_tail(0, 5, 0.3) == 1.0


def test_decluster_same_direction_within_24h_is_one_trial():
    opens = [{"open_ms": T, "side": "long", "peak_ntl": 1},
             {"open_ms": T + 2 * H, "side": "long", "peak_ntl": 5},
             {"open_ms": T + 3 * H, "side": "short", "peak_ntl": 1},
             {"open_ms": T + 30 * H, "side": "long", "peak_ntl": 1}]
    tr = core.decluster(opens)
    assert len(tr) == 3 and tr[0]["peak_ntl"] == 5


def _wallet(hits=0, opens=(), stats=None, equity=5e6, **kw):
    w = {"address": "0xabc", "stats": stats or {}, "equity": equity, "positions": [], "opens": list(opens),
         "event_hits": [{"event_id": f"e{i}", "start_ms": T + i * 100 * H, "chaser": False} for i in range(hits)]}
    w.update(kw)
    return w


def _opens(n, hits, spacing=30):
    return [{"open_ms": T + i * spacing * H, "side": "long", "coin": "BTC", "peak_ntl": 1e6,
             "score": {"hit": i < hits, "fwd_24h": 3.0 if i < hits else -1.0}} for i in range(n)]


GOOD_STATS = {"closes": 40, "win_rate": 0.62, "profit_factor": 2.0, "pnl_90d": 1e6, "maker_share": 0.3,
              "orders_per_day": 5, "median_hold_h": 6}


def test_single_lucky_hit_never_high():
    w = _wallet(hits=1, stats=GOOD_STATS)
    prem = core.premove_block(w, 0.3, 50)
    tier, cohort, _ = core.assign_tier(w, prem)
    assert tier == "MEDIUM" and cohort == "proven_speculator"  # PF backdoor removed
    w = _wallet(hits=1)
    tier, cohort, _ = core.assign_tier(w, core.premove_block(w, 0.3, 50))
    assert (tier, cohort) == ("WATCH", "one_hit")


def test_high_requires_binomial_and_multiple_testing():
    w = _wallet(hits=3, opens=_opens(8, 8), stats=GOOD_STATS)
    prem = core.premove_block(w, 0.3, 50)
    assert prem["independent_opens"] == 8 and prem["p_val"] < 0.003
    assert core.assign_tier(w, prem)[0] == "HIGH"
    # same wallet but tested among thousands -> expected false positives too high
    prem = core.premove_block(w, 0.3, 10_000)
    assert core.assign_tier(w, prem)[0] != "HIGH"
    # 5/8 hits at 30% base rate is not enough
    w = _wallet(hits=3, opens=_opens(8, 5), stats=GOOD_STATS)
    assert core.assign_tier(w, core.premove_block(w, 0.3, 50))[0] != "HIGH"
    # event hits too close together count once
    w = _wallet(hits=0, opens=_opens(8, 8), stats=GOOD_STATS)
    w["event_hits"] = [{"event_id": f"e{i}", "start_ms": T + i * H, "chaser": False} for i in range(3)]
    assert core.premove_block(w, 0.3, 50)["independent_hits"] == 1


def test_noise_rules():
    w = _wallet(hits=3, opens=_opens(8, 8), stats=dict(GOOD_STATS, maker_share=0.95))
    assert core.assign_tier(w, core.premove_block(w, 0.3, 50))[:2] == ("NOISE", "hedger_mm")
    w = _wallet(stats=dict(GOOD_STATS, median_hold_h=0.3))
    assert core.assign_tier(w, core.premove_block(w, 0.3, 50))[0] == "NOISE"
    assert core.is_hedged([{"coin": "xyz:NVDA", "szi": 10, "ntl": 1e6}, {"coin": "flx:NVDA", "szi": -5, "ntl": 5e5}])
    assert core.is_hedged([{"coin": "xyz:NVDA", "szi": 10, "ntl": 1e6}, {"coin": "flx:NVDA", "szi": -1, "ntl": 1e5}]) is None


def test_fresh_whale_watch():
    w = _wallet(age_days=3, positions=[{"coin": "xyz:NVDA", "notional": 3e6}])
    assert core.assign_tier(w, core.premove_block(w, 0.3, 1))[:2] == ("WATCH", "fresh_whale")


# --------------------------------------------------------------- monitoring
def snap(ts, pos, ok=True):
    return {"ok": ok, "ts": ts, "pos": {c: {"szi": s, "entry": 100.0} for c, s in pos.items()}}


def test_diff_open_add_flip_close():
    marks = {"BTC": 100.0, "xyz:NVDA": 100.0, "ETH": 100.0, "SOL": 100.0}
    prev = {"": snap(T, {"BTC": 0 or 1000, "ETH": 1000, "SOL": 1000}), "xyz": snap(T, {})}
    cur = {"": snap(T + 600_000, {"BTC": 2000, "ETH": -1000}), "xyz": snap(T + 600_000, {"xyz:NVDA": 10_000})}
    acts = {a["coin"]: a["type"] for a in core.diff_positions(prev, cur, marks)}
    assert acts == {"BTC": "add", "ETH": "flip", "SOL": "close", "xyz:NVDA": "open"}


def test_diff_skips_failed_or_first_seen_dex_and_stale_baseline():
    marks = {"xyz:NVDA": 100.0}
    prev = {"": snap(T, {})}
    cur = {"": snap(T + 600_000, {}), "xyz": snap(T + 600_000, {"xyz:NVDA": 10_000})}
    assert core.diff_positions(prev, cur, marks) == []          # first observation of the xyz dex: seed silently
    prev = {"xyz": snap(T, {"xyz:NVDA": 10_000})}
    cur = {"xyz": {"ok": False, "ts": T + 600_000, "pos": {}}}
    assert core.diff_positions(prev, cur, marks) == []          # failed poll is not a close
    prev = {"xyz": snap(T, {})}
    cur = {"xyz": snap(T + 3 * H, {"xyz:NVDA": 10_000})}
    a = core.diff_positions(prev, cur, marks)
    assert a and a[0]["alertable"] is False                     # baseline too old


# ------------------------------------------------------------------- alerts
def act(t="open", notional=1e6, coin="xyz:NVDA", side="long", before=0, after=10_000):
    return {"type": t, "coin": coin, "side": side, "notional": notional, "prev_notional": 0, "szi_before": before,
            "szi_after": after, "alertable": True, "entry": 100.0, "ts": T, "address": "0xabc", "delta_usd": notional}


def test_alert_priorities():
    assert core.alert_for_action(act(), {"tier": "HIGH", "equity": 5e6}, 100, 0.02, False)[0] == "HIGH"
    assert core.alert_for_action(act(notional=4e5), {"tier": "HIGH", "equity": 5e6}, 100, 0.02, False) is None
    assert core.alert_for_action(act(), {"tier": "MEDIUM"}, 100, 0.02, False)[0] == "MEDIUM"
    assert core.alert_for_action(act(), {"tier": "WATCH", "cohort": "one_hit"}, 100, 0.02, False)[0] == "INFO"
    fw = core.alert_for_action(act(notional=3e6), {"tier": "WATCH", "cohort": "fresh_whale"}, 100, 0.02, False)
    assert fw[:2] == ("MEDIUM", "fresh_whale")
    cl = core.alert_for_action(act("close", before=10_000, after=0), {"tier": "WATCH"}, 100, 0.02, True)
    assert cl[:2] == ("MEDIUM", "tracked_close")
    assert core.alert_for_action(dict(act(), alertable=False), {"tier": "HIGH", "equity": 1}, 100, 0.02, False) is None


def test_chase_gate_downgrades():
    pr, ch, why = core.apply_chase("HIGH", "long", 100, 104, 0.02)
    assert pr == "MEDIUM" and ch == 4.0 and why.startswith("Late")
    assert core.apply_chase("HIGH", "long", 100, 101, 0.02)[0] == "HIGH"
    assert core.apply_chase("HIGH", "short", 100, 96, 0.02)[0] == "MEDIUM"


def test_cluster_counts_distinct_entities():
    reg = {f"0x{i}": {"tier": "MEDIUM", "entity_id": "ent1" if i < 3 else f"0x{i}"} for i in range(5)}
    acts = [dict(act(notional=1e6), address=f"0x{i}") for i in range(3)]
    assert core.find_clusters(acts, reg, T) == []  # 3 wallets, one entity
    acts = [dict(act(notional=1e6), address=f"0x{i}") for i in (0, 3, 4)]
    cl = core.find_clusters(acts, reg, T)
    assert cl and cl[0]["n_entities"] == 3


def test_dedup():
    d = {"k": T}
    assert not core.dedup_ok(d, "k", T + 10 * H, 24)
    assert core.dedup_ok(d, "k", T + 25 * H, 24)
    assert core.dedup_ok(d, "other", T, 24)


def test_outcomes_from_alert_mark_and_scorecard():
    a = {"ts_ms": T, "px": 100.0, "side": "short", "type": "tracked_open", "priority": "HIGH", "spread_bps": 10}
    core.fill_outcomes(a, T + H + 60_000, 98.0)
    assert a["outcome"]["h1"] == 2.0 and a["outcome"]["h4"] is None
    core.fill_outcomes(a, T + 24 * H + 60_000, 95.0)
    assert a["outcome"]["h4"] is None  # too late for the live mark and no history fetcher -> stays empty
    assert a["outcome"]["h24"] == 5.0 and a["net_h24"] == round(5.0 - 0.09 - 0.1, 2)
    sc = core.scorecard([a, {"type": "event", "priority": "INFO", "side": "long", "outcome": {"h24": -9}}])
    assert sc["n_alerts"] == 1 and sc["hit_rate_24h"] == 1.0  # fraction 0..1
