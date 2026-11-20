"""Discord alert when the spot flow reading flips to bullish or bearish.

Runs after spotflow.py. Reads data/tabs/spotflow.json, keeps state in
data/tabs/spotflow_alert.json, and posts to the webhook in the
DISCORD_SPOTFLOW_WEBHOOK env var (a GitHub Actions secret). Without the
secret it still tracks flips (paper mode) but sends nothing.

Rules
- Direction = bullish (Leaning bullish / Bullish flow), bearish, or neutral.
- A new label must hold for CONFIRM_RUNS consecutive runs (~10 min apart)
  before it counts, so a score sitting on a boundary doesn't flicker.
- Alert when the confirmed direction changes to bullish or bearish, or
  strengthens from Leaning to full Bullish/Bearish flow.
- Moves back to neutral are recorded but not alerted.
- Same direction will not re-alert within COOLDOWN_H hours (stops
  bearish -> neutral -> bearish chatter).
- Skips when spotflow.json is stale or has collection errors.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "tabs" / "spotflow.json"
STATE = ROOT / "data" / "tabs" / "spotflow_alert.json"
CONFIRM_RUNS = 2
COOLDOWN_H = 12
STALE_MIN = 45
DASH = "https://goblin-alpha.pplx.app/#/"
NOW = datetime.now(timezone.utc)


def direction(label):
    l = (label or "").lower()
    return "bull" if "bullish" in l else "bear" if "bearish" in l else "neutral"


def strong(label):
    return label in ("Bullish flow", "Bearish flow")


def btc_price():
    for url, key in (("https://api.exchange.coinbase.com/products/BTC-USD/ticker", "price"),
                     ("https://data-api.binance.vision/api/v3/ticker/price?symbol=BTCUSDT", "price")):
        try:
            return float(requests.get(url, timeout=10, headers={"User-Agent": "goblin-alpha"}).json()[key])
        except Exception:  # noqa: BLE001
            pass
    return None


def send(hook, label, prev_label, reading, price):
    d = direction(label)
    color = 0x22C55E if d == "bull" else 0xEF4444
    lines = [f"- {w}" for w in reading.get("why", [])]
    notes = [f"- {n}" for n in reading.get("notes", [])]
    desc = f"Was: **{prev_label}**\n\n" + "\n".join(lines)
    if notes:
        desc += "\n\n" + "\n".join(notes)
    desc += ("\n\nShows who is in control of spot right now. In testing it did not "
             "predict next-day/week returns, so treat it as context, not a trade signal.")
    embed = {
        "title": f"BTC spot flow flipped: {label}",
        "url": DASH,
        "description": desc[:4000],
        "color": color,
        "fields": [{"name": "Score", "value": f"{reading.get('score')}", "inline": True}]
        + ([{"name": "BTC", "value": f"${price:,.0f}", "inline": True}] if price else []),
        "footer": {"text": "Goblin Alpha · Coinbase vs Binance spot flow"},
        "timestamp": NOW.isoformat(),
    }
    r = requests.post(hook, json={"username": "Goblin Spot Flow", "embeds": [embed]}, timeout=15)
    r.raise_for_status()


def main():
    if not SRC.exists():
        print("spotflow_alert: no spotflow.json")
        return
    sf = json.loads(SRC.read_text())
    reading = sf.get("reading") or {}
    label = reading.get("label")
    age_min = (NOW.timestamp() * 1000 - sf.get("generated_ms", 0)) / 60000
    if not label or age_min > STALE_MIN or sf.get("errors"):
        print(f"spotflow_alert: skip (label={label}, age={age_min:.0f}m, errors={sf.get('errors')})")
        return

    st = json.loads(STATE.read_text()) if STATE.exists() else {}
    hook = os.environ.get("DISCORD_SPOTFLOW_WEBHOOK", "").strip()
    mode = "live" if hook else "paper"

    if "confirmed_label" not in st:  # first run: baseline, no alert
        st = {"confirmed_label": label, "confirmed_at": NOW.isoformat(), "pending_label": None,
              "pending_runs": 0, "last_alert": {}, "history": []}
        STATE.write_text(json.dumps(st, indent=1))
        print(f"spotflow_alert: baseline {label}")
        return

    if label == st["confirmed_label"]:
        st["pending_label"], st["pending_runs"] = None, 0
    else:
        st["pending_runs"] = st["pending_runs"] + 1 if st.get("pending_label") == label else 1
        st["pending_label"] = label
        if st["pending_runs"] >= CONFIRM_RUNS:
            prev = st["confirmed_label"]
            pd, nd = direction(prev), direction(label)
            st["confirmed_label"], st["confirmed_at"] = label, NOW.isoformat()
            st["pending_label"], st["pending_runs"] = None, 0
            alert = nd != "neutral" and (nd != pd or (strong(label) and not strong(prev)))
            last = st["last_alert"].get(label)
            if alert and last and (NOW - datetime.fromisoformat(last)).total_seconds() < COOLDOWN_H * 3600:
                alert, why = False, "cooldown"
            else:
                why = "alert" if alert else "no-alert"
            price = btc_price()
            ev = {"t": NOW.isoformat(), "from": prev, "to": label, "score": reading.get("score"),
                  "btc": price, "action": why, "mode": mode}
            if alert:
                st["last_alert"][label] = NOW.isoformat()
                if hook:
                    try:
                        send(hook, label, prev, reading, price)
                        ev["sent"] = True
                    except Exception as e:  # noqa: BLE001
                        ev["sent"], ev["error"] = False, str(e)[:200]
            st["history"] = (st.get("history", []) + [ev])[-200:]
            print("spotflow_alert:", json.dumps(ev))
    STATE.write_text(json.dumps(st, indent=1))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"spotflow_alert failed: {e}", file=sys.stderr)
