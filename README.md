# goblin-market-data

Collector behind the **Market Snapshot** tab in Goblin Alpha Command Center.

A GitHub Action (`.github/workflows/snapshot.yml`) runs every 10 minutes, pulls public
market data and writes:

| File | Contents |
| --- | --- |
| `data/snapshot.json` | Latest BTC 4H anchor, funding/OI/LS, tracked whales, macro backdrop, signal states |
| `data/history.json` | Rolling ~80h of compact readings (price, OI, L/S, funding, whale long %) |
| `data/signals.json` | Paper log of every signal firing with signed 4h / 24h / 72h forward returns |

Sources (no API keys): Hyperliquid info + stats API, OKX public API, Deribit public API, Yahoo Finance via `yfinance`.

Signals are rule-based alerts, not validated edges. Edge-triggered: a signal is logged once when it turns on.

Run locally: `pip install -r collector/requirements.txt && python collector/snapshot.py`
