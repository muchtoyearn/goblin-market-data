"""Deliver new Insider Radar alerts to Discord and/or Slack.

Runs after insider.py. Reads data/insider/state.json (source of truth for alerts), posts every
undelivered HIGH alert (and MEDIUM when INSIDER_ALERT_MEDIUM=1, the default) to
  DISCORD_INSIDER_WEBHOOK   Discord webhook URL (GitHub Actions secret)
  SLACK_INSIDER_WEBHOOK     Slack incoming-webhook URL (same text)
Without either secret it runs logging-only: alerts are printed and marked `logged`, never `delivered`.

Guards
- Skips everything when insider.json is stale (> 15 min) or status.poll_degraded is true.
- Only alerts younger than 30 minutes are sent, so adding the secret later does not dump a backlog.
- Each alert is sent once (delivered=true / logged=true are persisted to state.json and insider.json).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "insider" / "state.json"
TAB = ROOT / "data" / "tabs" / "insider.json"
STALE_MIN = 15
MAX_AGE_MIN = 30
def _atomic(p, obj):
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":")))
    os.replace(tmp, p)


DASH = "https://goblin-alpha.pplx.app/#/insider"
CAVEAT = ("Copying is a bet that this wallet's past timing repeats. It may be hedged elsewhere, "
          "and past hits can be luck. Paper-track before sizing.")


def short(a):
    return f"{a[:6]}…{a[-4:]}"


def usd(x):
    x = abs(x or 0)
    return f"${x/1e6:.2f}M" if x >= 1e6 else f"${x/1e3:.0f}K"


def wallet_lines(a):
    out = []
    for w in a.get("wallets", [])[:5]:
        addr = w["address"]
        out.append(f"- {short(addr)} — tier {w.get('tier')} ({w.get('cohort')}) — "
                   f"[Hyperdash](https://hyperdash.com/address/{addr}) · [Hypurrscan](https://hypurrscan.io/address/{addr})")
    return out


def render(a):
    """Plain-language text for one alert (shared by Discord and Slack)."""
    lines = [f"**{a['priority']}** · {a['title']}"]
    facts = []
    if a.get("notional"):
        facts.append(f"Size {usd(a['notional'])}")
    if a.get("px"):
        facts.append(f"mark now {a['px']:g}")
    if a.get("wallet_entry_px"):
        facts.append(f"wallet entry {a['wallet_entry_px']:g}")
    if a.get("chase_pct") is not None:
        facts.append(f"price moved {a['chase_pct']:+.1f}% since their entry")
    if a.get("spread_bps") is not None:
        facts.append(f"spread ~{a['spread_bps']:.0f} bps")
    if facts:
        lines.append(", ".join(facts))
    lines += [f"- {w}" for w in a.get("why", [])[:5]]
    lines += wallet_lines(a)
    lines.append(f"Caveat: {CAVEAT}")
    return "\n".join(lines)


def send_discord(hook, a):
    color = {"HIGH": 0xEF4444, "MEDIUM": 0xF59E0B}.get(a["priority"], 0x64748B)
    body = render(a)
    embed = {"title": a["title"][:250], "url": DASH, "description": body[:4000], "color": color,
             "footer": {"text": "Goblin Alpha · Insider Radar · paper-tracked"}, "timestamp": a["ts"]}
    r = requests.post(hook, json={"username": "Goblin Insider Radar", "embeds": [embed]}, timeout=15)
    r.raise_for_status()


def send_slack(hook, a):
    txt = render(a).replace("**", "*")
    # Slack uses <url|text> links
    import re
    txt = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"<\2|\1>", txt)
    r = requests.post(hook, json={"text": txt}, timeout=15)
    r.raise_for_status()


def select(alerts, now_ms, medium=True):
    pri = {"HIGH"} | ({"MEDIUM"} if medium else set())
    return [a for a in alerts if a.get("priority") in pri and not a.get("delivered") and not a.get("logged")
            and now_ms - (a.get("ts_ms") or 0) <= MAX_AGE_MIN * 60000]


def main():
    if not STATE.exists() or not TAB.exists():
        print("insider_alert: no state yet")
        return
    tab = json.loads(TAB.read_text())
    now_ms = int(time.time() * 1000)
    age = (now_ms - tab.get("updated_ms", 0)) / 60000
    if age > STALE_MIN or (tab.get("status") or {}).get("poll_degraded"):
        print(f"insider_alert: skip (age {age:.0f}m, poll_degraded={tab.get('status', {}).get('poll_degraded')})")
        return
    state = json.loads(STATE.read_text())
    discord = os.environ.get("DISCORD_INSIDER_WEBHOOK", "").strip()
    slack = os.environ.get("SLACK_INSIDER_WEBHOOK", "").strip()
    medium = os.environ.get("INSIDER_ALERT_MEDIUM", "1") != "0"
    todo = select(state.get("alerts", []), now_ms, medium)
    changed = {}
    for a in todo:
        sent, errs = False, []
        for name, hook, fn in (("discord", discord, send_discord), ("slack", slack, send_slack)):
            if not hook:
                continue
            try:
                fn(hook, a)
                sent = True
            except Exception as e:  # noqa: BLE001
                errs.append(f"{name}: {str(e)[:120]}")
        if sent:
            a["delivered"] = True
        else:
            a["logged"] = True
        if errs:
            a["delivery_error"] = "; ".join(errs)
        changed[a["id"]] = a
        print(("insider_alert: SENT " if sent else "insider_alert: LOG ") + render(a).replace("\n", " | ")[:600])
    if not changed:
        print(f"insider_alert: nothing new ({'live' if discord or slack else 'logging-only'})")
        return
    _atomic(STATE, state)
    for a in tab.get("alerts", []):
        if a.get("id") in changed:
            a["delivered"] = changed[a["id"]].get("delivered", False)
            a["logged"] = changed[a["id"]].get("logged", False)
    _atomic(TAB, tab)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"insider_alert failed: {e}", file=sys.stderr)
