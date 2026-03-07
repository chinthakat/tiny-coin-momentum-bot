# TinyCoins Dashboard UI — Implementation Plan

Add a premium web dashboard to the trading system. Embedded aiohttp web server serves a single-page app that auto-refreshes every 5s.

## Dashboard Tabs

### Tab 1: Dashboard (Primary)
- **Connection status** — Binance API connected/disconnected, WebSocket health, last data received, clock offset
- **System stats** — universe size, promoted count, active positions, uptime
- **Shortlisted coins table** — all promoted symbols with radar score, label, price, 10s/30s/60s return, volume burst ratio
- **Coin detail panel** — click a coin to see deep features: book imbalance, flow imbalance, spread, micro return, lifecycle state, watch status

### Tab 2: Orders & Trades
- **Active positions** — symbol, entry price, current price, unrealized PnL, hold time, stop price, trailing status
- **Trade history** — closed trades with entry/exit prices, PnL, hold time, reason
- **Balance overview** — daily PnL, win rate, drawdown, consecutive losses
- **Risk status** — kill switch state, risk limits

## Proposed Changes

### Web Dashboard Module

#### [NEW] [dashboard/\_\_init\_\_.py](file:///c:/Projects/Trading/TinyCoins/dashboard/__init__.py)

#### [NEW] [dashboard/server.py](file:///c:/Projects/Trading/TinyCoins/dashboard/server.py)
aiohttp web server running on port 8080:
- `GET /` — serves `index.html`
- `GET /api/status` — system status, connection health, config
- `GET /api/radar` — promoted symbols with scores and features
- `GET /api/positions` — active positions with PnL
- `GET /api/trades` — closed trade history
- `GET /api/coin/{symbol}` — detailed deep features for a specific symbol

#### [NEW] [dashboard/static/index.html](file:///c:/Projects/Trading/TinyCoins/dashboard/static/index.html)
Single-page dashboard with:
- Dark premium design with glassmorphism, gradients, and micro-animations
- Two tab layout (Dashboard / Orders & Trades)
- Auto-refresh every 5 seconds via `fetch()` API
- Click-to-expand coin details
- Responsive layout

#### [MODIFY] [main.py](file:///c:/Projects/Trading/TinyCoins/main.py)
- Import and start the dashboard server as an additional background task
- Pass all component references to the dashboard so it can query live state

#### [MODIFY] [config.py](file:///c:/Projects/Trading/TinyCoins/core/config.py)
- Add `DashboardConfig` dataclass with `port`, `host`, `enabled` fields

## Verification
- Start system → open `http://localhost:8080` → confirm tabs render, data refreshes, coin selection works
