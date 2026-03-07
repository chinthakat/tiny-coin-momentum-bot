# TinyCoins Momentum Trading System — Walkthrough

## What Was Built

A complete **multi-layer crypto momentum trading system** for Binance Spot with a **live web dashboard**.

### Architecture

```mermaid
graph TD
    A["main.py"] --> B["Exchange Client"]
    A --> C["Universe Manager"]
    A --> D["Radar Service"]
    A --> E["Long Engine"]
    A --> F["Execution Engine"]
    A --> G["Risk Engine"]
    A --> H["Dashboard Server :8080"]
    
    D --> I["Radar Features & Scorer"]
    I -->|promote| J["Stream Manager"]
    J --> K["Deep Features"]
    K --> E
    E --> F
    G -->|kill switch| F
    H -->|REST API| I
    H -->|REST API| K
    H -->|REST API| F
```

**20+ Python modules** across 11 packages, implementing the full pipeline from the strategy document.

---

## Web Dashboard

Premium dark-themed dashboard with auto-refresh every 5 seconds at `http://localhost:8080`.

### Dashboard Tab
Shows connection status, universe stats, and all shortlisted coins with radar scores, labels, returns, volume burst, and buildup metrics. Click any coin for deep microstructure details.

![Dashboard Tab](file:///C:/Users/chint/.gemini/antigravity/brain/d58ad8c3-8c29-45c4-a12e-9ec85cbb3d2e/tinycoins_dashboard_tab_1772849348882.png)

### Orders & Trades Tab
Shows daily PnL, win rate, drawdown, risk status, active positions with unrealized PnL, and completed trade history.

![Orders Tab](file:///C:/Users/chint/.gemini/antigravity/brain/d58ad8c3-8c29-45c4-a12e-9ec85cbb3d2e/tinycoins_orders_tab_1772849375829.png)

---

## Minimum Quantity Mode

When `trade_mode: "minimum_quantity"`:
- `metadata.get_min_trade_quantity()` computes `max(LOT_SIZE.minQty, MIN_NOTIONAL/price)` rounded up to stepSize
- All scoring, signals, risk limits, and kill switches still operate normally

---

## How to Run

```powershell
cd c:\Projects\Trading\TinyCoins
python main.py
# Dashboard opens at http://localhost:8080
```

> [!TIP]
> Defaults: `dry_run: true` + `trade_mode: minimum_quantity`. Set `dry_run: false` in `config.yaml` when ready for live minimum-quantity trading.
