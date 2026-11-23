"""Hyperliquid trade tape for the Insider Radar.

Long-running websocket listener (started in the background by the workflow).
Subscribes to `trades` for a watchlist built at start (top HIP-3 markets by
24h volume across every perp dex plus the biggest main-dex perps). Each trade
carries `users: [buyer, seller]`, so we can see WHO was accumulating before a
move, including brand-new wallets that are on no leaderboard.

Aggregation is per (wallet, coin, UTC hour): net signed notional (+buy/-sell),
gross notional, taker share, trade count, first/last time and price. Only rows
above a size floor are written, so files stay small (public repo).

Output (atomic writes, every FLUSH_S seconds) to OUT_DIR (default
/tmp/goblin_tape, outside the git work tree so the workflow's
`git reset --hard` cannot race with us; insider.py copies the files into
data/insider/tape/ each cycle):
  YYYY-MM-DD.json   {"coins": [...], "wallets": [...], "rows": [[...], ...]}
                    row = [hour, coin_i, wallet_i, net, gross, taker_pct, n,
                           first_s, first_px, last_s, last_px]
                    hour = epoch hours; first_s/last_s = seconds into the hour
  candidates.json   wallets with |net| >= CAND_MIN_USD in one coin over 24h
  status.json       started/last flush/markets/messages/reconnects/errors

Survives disconnects (reconnect with backoff, de-dup on (coin, tid)), exits
cleanly at --until (epoch seconds), never lets one bad message kill the loop.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
REPO_TAPE = ROOT / "data" / "insider" / "tape"
HL = "https://api.hyperliquid.xyz/info"
WS = "wss://api.hyperliquid.xyz/ws"

CFG = {
    "hip3_top": 50,            # top HIP-3 markets by 24h notional volume
    "hip3_min_vlm": 1_000_000,
    "main_top": 15,            # biggest main-dex perps
    "floor_hip3": 150_000,     # keep a (wallet, coin, hour) row if |net| or taker notional >= floor
    "floor_main": 1_000_000,
    "cand_min_usd": 1_000_000,  # fresh-whale candidates: |net| in one coin over 24h
    "cand_window_h": 24,
    "flush_s": 180,
    "retention_d": 3,          # in /tmp; the repo keeps closed hours (see insider.sync_tape)
    "ping_s": 30,
    "recv_timeout_s": 5,
    "stall_s": 90,             # reconnect when no message for this long
}


def now_s():
    return time.time()


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hl(body, retries=3):
    for i in range(retries):
        try:
            r = requests.post(HL, json=body, timeout=20, headers={"User-Agent": "goblin-alpha-tape/1.0"})
            if r.status_code == 429:
                time.sleep(3 + 3 * i)
                continue
            r.raise_for_status()
            return r.json()
        except Exception:  # noqa: BLE001
            if i == retries - 1:
                raise
            time.sleep(1 + i)
    raise RuntimeError("retries exhausted")


def build_watchlist(cfg=CFG):
    """Top HIP-3 markets by 24h volume across all perp dexs + top main perps."""
    dexs = [d["name"] for d in hl({"type": "perpDexs"}) if d]
    hip3, main = [], []
    for dex in [""] + dexs:
        try:
            body = {"type": "metaAndAssetCtxs"}
            if dex:
                body["dex"] = dex
            meta, ctxs = hl(body)
            for u, c in zip(meta["universe"], ctxs):
                if u.get("isDelisted"):
                    continue
                v = float(c.get("dayNtlVlm") or 0)
                (hip3 if dex else main).append((v, u["name"]))
        except Exception as e:  # noqa: BLE001
            print(f"tape: watchlist {dex or 'main'} failed: {e}", file=sys.stderr)
        time.sleep(0.3)
    hip3.sort(reverse=True)
    main.sort(reverse=True)
    coins = [n for v, n in hip3 if v >= cfg["hip3_min_vlm"]][: cfg["hip3_top"]]
    coins += [n for v, n in main][: cfg["main_top"]]
    return coins


class Tape:
    """In-memory (hour, coin, wallet) aggregator. Pure logic, no I/O except save/load."""

    def __init__(self, cfg=CFG):
        self.cfg = cfg
        # key (hour, coin, wallet) -> [net, gross, taker, n, t0, p0, t1, p1]
        self.live: dict = {}
        self.frozen: dict = {}  # rows loaded from disk at start (already floor-filtered)
        self.seen = set()
        self.seen_q: deque = deque()
        self.msgs = 0
        self.trades = 0
        self.dupes = 0

    def floor(self, coin):
        return self.cfg["floor_hip3"] if ":" in coin else self.cfg["floor_main"]

    def add_trade(self, t):
        coin = t["coin"]
        key = (coin, t.get("tid"))
        if key in self.seen:
            self.dupes += 1
            return False
        self.seen.add(key)
        self.seen_q.append((t["time"], key))
        while self.seen_q and self.seen_q[0][0] < t["time"] - 15 * 60 * 1000:
            self.seen.discard(self.seen_q.popleft()[1])
        users = t.get("users") or []
        if len(users) != 2 or users[0] == users[1]:
            return False
        px, sz = float(t["px"]), float(t["sz"])
        ntl = px * sz
        ts = t["time"] / 1000
        hour = int(ts // 3600)
        taker = users[0] if t.get("side") == "B" else users[1]
        for w, sign in ((users[0], 1), (users[1], -1)):
            k = (hour, coin, w.lower())
            r = self.live.get(k)
            if r is None:
                r = self.live[k] = [0.0, 0.0, 0.0, 0, ts, px, ts, px]
            r[0] += sign * ntl
            r[1] += ntl
            if w == taker:
                r[2] += ntl
            r[3] += 1
            if ts < r[4]:
                r[4], r[5] = ts, px
            if ts >= r[6]:
                r[6], r[7] = ts, px
        self.trades += 1
        return True

    def keep(self, coin, r):
        f = self.floor(coin)
        return abs(r[0]) >= f or r[2] >= f

    def merged(self):
        """Combine frozen (from disk) and live rows; apply floor."""
        out = {}
        for k, r in self.frozen.items():
            out[k] = list(r)
        for k, r in self.live.items():
            if k in out:
                o = out[k]
                o[0] += r[0]
                o[1] += r[1]
                o[2] += r[2]
                o[3] += r[3]
                if r[4] < o[4]:
                    o[4], o[5] = r[4], r[5]
                if r[6] >= o[6]:
                    o[6], o[7] = r[6], r[7]
            else:
                out[k] = list(r)
        return {k: r for k, r in out.items() if self.keep(k[1], r)}

    def compact(self, cur_hour):
        """Drop sub-floor rows of closed hours from memory."""
        for k in [k for k, r in self.live.items() if k[0] < cur_hour and not self.keep(k[1], r)]:
            del self.live[k]

    def candidates(self, now, rows=None):
        rows = self.merged() if rows is None else rows
        h0 = int(now // 3600) - self.cfg["cand_window_h"]
        acc = defaultdict(lambda: {"net": 0.0, "gross": 0.0, "taker": 0.0, "n": 0, "hours": 0,
                                   "t0": None, "p0": None, "t1": None, "p1": None})
        for (h, coin, w), r in rows.items():
            if h < h0:
                continue
            a = acc[(w, coin)]
            a["net"] += r[0]
            a["gross"] += r[1]
            a["taker"] += r[2]
            a["n"] += r[3]
            a["hours"] += 1
            if a["t0"] is None or r[4] < a["t0"]:
                a["t0"], a["p0"] = r[4], r[5]
            if a["t1"] is None or r[6] >= a["t1"]:
                a["t1"], a["p1"] = r[6], r[7]
        out = []
        for (w, coin), a in acc.items():
            if abs(a["net"]) >= self.cfg["cand_min_usd"]:
                out.append({"address": w, "coin": coin, "side": "long" if a["net"] > 0 else "short",
                            "net_usd": round(a["net"]), "gross_usd": round(a["gross"]),
                            "taker_share": round(a["taker"] / a["gross"], 3) if a["gross"] else None,
                            "trades": a["n"], "hours": a["hours"],
                            "first_ts": iso(a["t0"]), "first_px": a["p0"], "last_ts": iso(a["t1"]), "last_px": a["p1"]})
        out.sort(key=lambda x: -abs(x["net_usd"]))
        return out


def day_of_hour(h):
    return datetime.fromtimestamp(h * 3600, timezone.utc).strftime("%Y-%m-%d")


def encode_day(rows):
    coins, wallets, ci, wi, out = [], [], {}, {}, []
    for (h, coin, w), r in sorted(rows.items()):
        if coin not in ci:
            ci[coin] = len(coins)
            coins.append(coin)
        if w not in wi:
            wi[w] = len(wallets)
            wallets.append(w)
        out.append([h, ci[coin], wi[w], round(r[0]), round(r[1]), round(100 * r[2] / r[1]) if r[1] else 0, r[3],
                    int(r[4] - h * 3600), r[5], int(r[6] - h * 3600), r[7]])
    return {"coins": coins, "wallets": wallets, "rows": out}


def decode_day(d):
    """Inverse of encode_day -> {(hour, coin, wallet): [net, gross, taker, n, t0, p0, t1, p1]}."""
    out = {}
    coins, wallets = d.get("coins", []), d.get("wallets", [])
    for row in d.get("rows", []):
        h, c, w, net, gross, tp, n, s0, p0, s1, p1 = row
        out[(h, coins[c], wallets[w])] = [float(net), float(gross), gross * tp / 100.0, n,
                                          h * 3600 + s0, p0, h * 3600 + s1, p1]
    return out


def atomic_write(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    os.replace(tmp, path)


def load_repo_hours(base: Path, since_hour: int):
    """Read the immutable closed-hour files data/insider/tape/YYYY-MM-DD/HH.json since since_hour."""
    out = {}
    for p in sorted(base.glob("20??-??-??/??.json")):
        try:
            h = int(datetime.strptime(f"{p.parent.name} {p.stem}", "%Y-%m-%d %H").replace(tzinfo=timezone.utc).timestamp() // 3600)
            if h >= since_hour:
                out.update(decode_day(json.loads(p.read_text())))
        except Exception as e:  # noqa: BLE001
            print(f"tape: could not load {p}: {e}", file=sys.stderr)
    return out


def load_existing(tape: Tape, out_dir: Path, now):
    """Seed frozen rows from the last two days (/tmp day files if this machine has them, else the repo's hour files)."""
    since = int(now // 3600) - 48
    got = False
    for dd in (0, 1, 2):
        day = datetime.fromtimestamp(now - dd * 86400, timezone.utc).strftime("%Y-%m-%d")
        p = out_dir / f"{day}.json"
        if p.exists():
            try:
                tape.frozen.update({k: v for k, v in decode_day(json.loads(p.read_text())).items() if k[0] >= since})
                got = True
            except Exception as e:  # noqa: BLE001
                print(f"tape: could not load {p}: {e}", file=sys.stderr)
    if not got:
        tape.frozen.update(load_repo_hours(REPO_TAPE, since))


def flush(tape: Tape, out_dir: Path, status: dict, now):
    rows = tape.merged()
    by_day = defaultdict(dict)
    for k, r in rows.items():
        by_day[day_of_hour(k[0])][k] = r
    for day, rr in by_day.items():
        atomic_write(out_dir / f"{day}.json", encode_day(rr))
    atomic_write(out_dir / "candidates.json", {"updated": iso(now), "updated_ms": int(now * 1000),
                                               "window_h": tape.cfg["cand_window_h"],
                                               "min_usd": tape.cfg["cand_min_usd"],
                                               "candidates": tape.candidates(now, rows)[:200]})
    status.update({"last_flush": iso(now), "last_flush_ms": int(now * 1000), "trades": tape.trades,
                   "messages": tape.msgs, "dupes": tape.dupes, "rows": len(rows)})
    atomic_write(out_dir / "status.json", status)
    tape.compact(int(now // 3600))
    # drop frozen rows older than 2 days from memory (already on disk)
    cut = int(now // 3600) - 48
    for k in [k for k in tape.frozen if k[0] < cut]:
        del tape.frozen[k]
    for k in [k for k in tape.live if k[0] < cut]:
        del tape.live[k]
    prune(out_dir, now, tape.cfg["retention_d"])


def prune(d: Path, now, days):
    cut = datetime.fromtimestamp(now - days * 86400, timezone.utc).strftime("%Y-%m-%d")
    for p in d.glob("20??-??-??.json"):
        if p.stem < cut:
            try:
                p.unlink()
            except OSError:
                pass


def run(until, out_dir: Path, coins=None):
    from websockets.sync.client import connect  # imported here so tests don't need it

    out_dir.mkdir(parents=True, exist_ok=True)
    start = now_s()
    tape = Tape()
    load_existing(tape, out_dir, start)
    coins = coins or build_watchlist()
    status = {"started": iso(start), "started_ms": int(start * 1000), "until": iso(until), "pid": os.getpid(),
              "markets": coins, "n_markets": len(coins), "reconnects": 0, "errors": [], "running": True}
    print(f"tape: watching {len(coins)} markets until {iso(until)}", flush=True)
    last_flush = start
    backoff = 1.0
    while now_s() < until:
        try:
            with connect(WS, open_timeout=15, close_timeout=5, max_size=2 ** 24, ping_interval=None) as ws:
                for c in coins:
                    ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": c}}))
                last_ping = last_msg = now_s()
                backoff = 1.0
                while now_s() < until:
                    t = now_s()
                    if t - last_ping >= CFG["ping_s"]:
                        ws.send(json.dumps({"method": "ping"}))
                        last_ping = t
                    if t - last_flush >= CFG["flush_s"]:
                        try:
                            flush(tape, out_dir, status, t)
                        except Exception as e:  # noqa: BLE001
                            status["errors"] = (status["errors"] + [f"{iso(t)} flush: {e}"])[-20:]
                        last_flush = t
                    if t - last_msg > CFG["stall_s"]:
                        raise TimeoutError("stalled feed")
                    try:
                        raw = ws.recv(timeout=CFG["recv_timeout_s"])
                    except TimeoutError:
                        continue
                    last_msg = now_s()
                    tape.msgs += 1
                    try:
                        m = json.loads(raw)
                        if m.get("channel") == "trades":
                            for tr in m.get("data") or []:
                                tape.add_trade(tr)
                    except Exception as e:  # noqa: BLE001
                        status["errors"] = (status["errors"] + [f"{iso(now_s())} msg: {str(e)[:120]}"])[-20:]
        except Exception as e:  # noqa: BLE001
            status["reconnects"] += 1
            status["errors"] = (status["errors"] + [f"{iso(now_s())} ws: {type(e).__name__}: {str(e)[:120]}"])[-20:]
            print(f"tape: reconnect after {type(e).__name__}: {e}", file=sys.stderr, flush=True)
            time.sleep(min(backoff + random.random(), max(until - now_s(), 0)))
            backoff = min(backoff * 2, 60)
    status["running"] = False
    try:
        flush(tape, out_dir, status, now_s())
    except Exception as e:  # noqa: BLE001
        print(f"tape: final flush failed: {e}", file=sys.stderr)
    print(f"tape: done, {tape.trades} trades, {status['reconnects']} reconnects", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", type=float, help="epoch seconds to stop at")
    ap.add_argument("--minutes", type=float, help="run this many minutes (alternative to --until)")
    ap.add_argument("--out", default=os.environ.get("INSIDER_TAPE_DIR", "/tmp/goblin_tape"))
    a = ap.parse_args()
    until = a.until or (now_s() + 60 * (a.minutes or 340))
    run(until, Path(a.out))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"tape failed: {e}", file=sys.stderr)
