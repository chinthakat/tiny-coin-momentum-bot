"""
Dashboard web server — aiohttp-based REST API + static file serving.

Exposes live system state to the web dashboard.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web
import structlog

logger = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class DashboardServer:
    """
    Embedded web server for the TinyCoins dashboard.

    Serves:
    - Static HTML/CSS/JS from dashboard/static/
    - REST API endpoints for live system state
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self._app = web.Application()
        self._runner = None
        self._start_time = time.time()

        # Component references (set by main.py)
        self.config = None
        self.universe = None
        self.radar_scorer = None
        self.radar_features = None
        self.state_engine = None
        self.deep_features = None
        self.execution = None
        self.risk_engine = None
        self.ws_manager = None
        self.lifecycle = None
        self.clock = None
        self._trade_history = []

        # Setup routes
        self._app.router.add_get("/", self._serve_index)
        self._app.router.add_get("/api/status", self._api_status)
        self._app.router.add_get("/api/radar", self._api_radar)
        self._app.router.add_get("/api/positions", self._api_positions)
        self._app.router.add_get("/api/trades", self._api_trades)
        self._app.router.add_get("/api/coin/{symbol}", self._api_coin_detail)
        self._app.router.add_static("/static", STATIC_DIR, name="static")

    def record_trade(self, trade_data: dict) -> None:
        """Record a completed trade for history."""
        self._trade_history.append(trade_data)

    async def start(self) -> None:
        """Start the web server."""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("dashboard_started", url=f"http://localhost:{self.port}")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ─── Static serving ───

    async def _serve_index(self, request: web.Request) -> web.Response:
        index_path = STATIC_DIR / "index.html"
        return web.FileResponse(index_path)

    # ─── API Endpoints ───

    async def _api_status(self, request: web.Request) -> web.Response:
        """System status: connection health, config, uptime."""
        data = {
            "uptime_seconds": round(time.time() - self._start_time, 1),
            "trade_mode": self.config.trade_mode if self.config else "",
            "dry_run": self.config.dry_run if self.config else True,
            "testnet": self.config.testnet if self.config else False,
            "clock_offset_ms": round(self.clock.offset_ms, 1) if self.clock else 0,
            "websocket": {
                "connected": True,
                "last_message_age_s": round(
                    time.time() - self.ws_manager.last_message_time, 1
                ) if self.ws_manager else 0,
            },
            "universe": {
                "monitored": len(self.universe.universe.monitored) if self.universe else 0,
                "radar_eligible": len(self.universe.universe.radar_eligible) if self.universe else 0,
                "long_eligible": len(self.universe.universe.long_eligible) if self.universe else 0,
                "last_refresh": round(self.universe.universe.updated_at, 1) if self.universe else 0,
            },
            "promoted_count": len(self.radar_scorer.promoted_symbols) if self.radar_scorer else 0,
            "position_count": self.execution.position_count if self.execution else 0,
            "risk": self.risk_engine.get_status_summary() if self.risk_engine else {},
        }
        return web.json_response(data)

    async def _api_radar(self, request: web.Request) -> web.Response:
        """Promoted symbols with scores, labels, and radar features."""
        symbols = []
        if self.radar_scorer:
            promoted = self.radar_scorer.promoted_symbols
            results = self.radar_scorer.last_results

            for sym in sorted(promoted):
                result = results.get(sym)
                entry = {
                    "symbol": sym,
                    "score": round(result.composite_score, 4) if result else 0,
                    "label": result.label.value if result else "normal",
                }
                if result and result.features:
                    f = result.features
                    entry["features"] = {
                        "return_10s": round(f.return_10s, 3),
                        "return_30s": round(f.return_30s, 3),
                        "return_60s": round(f.return_60s, 3),
                        "volume_burst": round(f.volume_burst_ratio, 2),
                        "spread_compression": round(f.spread_compression, 2),
                        "activity_ratio": round(f.quote_activity_ratio, 2),
                        "buildup_score": round(f.early_buildup_score, 3),
                    }
                symbols.append(entry)

            # Also include top non-promoted for context
            all_results = sorted(
                results.values(),
                key=lambda r: r.composite_score,
                reverse=True,
            )
            for r in all_results[:30]:
                if r.symbol not in promoted:
                    entry = {
                        "symbol": r.symbol,
                        "score": round(r.composite_score, 4),
                        "label": r.label.value,
                        "promoted": False,
                    }
                    if r.features:
                        entry["features"] = {
                            "return_10s": round(r.features.return_10s, 3),
                            "return_30s": round(r.features.return_30s, 3),
                            "return_60s": round(r.features.return_60s, 3),
                            "volume_burst": round(r.features.volume_burst_ratio, 2),
                            "spread_compression": round(r.features.spread_compression, 2),
                            "activity_ratio": round(r.features.quote_activity_ratio, 2),
                            "buildup_score": round(r.features.early_buildup_score, 3),
                        }
                    symbols.append(entry)

        # Mark promoted symbols
        promoted_set = self.radar_scorer.promoted_symbols if self.radar_scorer else set()
        for s in symbols:
            if "promoted" not in s:
                s["promoted"] = s["symbol"] in promoted_set

        symbols.sort(key=lambda x: x["score"], reverse=True)
        return web.json_response({"symbols": symbols})

    async def _api_positions(self, request: web.Request) -> web.Response:
        """Active positions with unrealized PnL."""
        positions = []
        if self.execution:
            for sym, pos in self.execution.positions.items():
                state = self.state_engine.get(sym) if self.state_engine else None
                current_price = state.mid_price if state else pos.entry_price
                pnl_pct = pos.unrealized_pnl_pct(current_price)
                positions.append({
                    "symbol": sym,
                    "side": pos.side,
                    "entry_price": pos.entry_price,
                    "current_price": round(current_price, 8),
                    "quantity": pos.quantity,
                    "notional": round(pos.notional, 4),
                    "unrealized_pnl_pct": round(pnl_pct, 3),
                    "hold_time_s": round(pos.hold_time, 1),
                    "stop_price": round(pos.stop_price, 8),
                    "trailing_active": pos.trailing_active,
                    "trailing_stop_price": round(pos.trailing_stop_price, 8),
                    "trade_mode": pos.trade_mode,
                    "is_dry_run": pos.order_record.is_dry_run if pos.order_record else True,
                })
        return web.json_response({"positions": positions})

    async def _api_trades(self, request: web.Request) -> web.Response:
        """Trade history + balance summary."""
        stats = self.risk_engine.get_status_summary() if self.risk_engine else {}
        return web.json_response({
            "trades": self._trade_history[-100:],  # Last 100
            "summary": stats,
        })

    async def _api_coin_detail(self, request: web.Request) -> web.Response:
        """Detailed info for a specific symbol."""
        symbol = request.match_info["symbol"].upper()

        detail: Dict[str, Any] = {"symbol": symbol}

        # Radar features
        if self.radar_scorer and symbol in self.radar_scorer.last_results:
            r = self.radar_scorer.last_results[symbol]
            detail["radar"] = {
                "score": round(r.composite_score, 4),
                "label": r.label.value,
            }
            if r.features:
                detail["radar"]["features"] = {
                    "return_10s": round(r.features.return_10s, 3),
                    "return_30s": round(r.features.return_30s, 3),
                    "return_60s": round(r.features.return_60s, 3),
                    "return_acceleration": round(r.features.return_acceleration, 3),
                    "volume_burst": round(r.features.volume_burst_ratio, 2),
                    "spread_compression": round(r.features.spread_compression, 2),
                    "activity_ratio": round(r.features.quote_activity_ratio, 2),
                    "buildup_score": round(r.features.early_buildup_score, 3),
                }

        # Deep features (if promoted)
        if self.deep_features and self.state_engine:
            deep = self.deep_features.compute(symbol)
            if deep:
                detail["deep"] = {
                    "book_imbalance": round(deep.book_imbalance, 4),
                    "flow_imbalance": round(deep.flow_imbalance, 4),
                    "ask_depletion": round(deep.ask_depletion, 4),
                    "spread_stability": round(deep.spread_stability, 4),
                    "volume_acceleration": round(deep.volume_acceleration, 2),
                    "micro_return": round(deep.micro_return, 3),
                    "book_depth_quality": round(deep.book_depth_quality, 2),
                }

        # State
        if self.state_engine:
            state = self.state_engine.get(symbol)
            if state:
                detail["state"] = {
                    "mid_price": round(state.mid_price, 8),
                    "spread_pct": round(state.spread_pct, 4),
                    "best_bid": round(state.best_bid, 8),
                    "best_ask": round(state.best_ask, 8),
                    "warmed_up": state.is_warmed_up,
                }
                buy, sell = state.buy_sell_flow(5)
                detail["state"]["buy_flow_5s"] = round(buy, 2)
                detail["state"]["sell_flow_5s"] = round(sell, 2)

        # Lifecycle
        if self.lifecycle:
            lc = self.lifecycle.get(symbol)
            if lc:
                detail["lifecycle"] = {
                    "state": lc.state.value,
                    "time_in_state_s": round(lc.time_in_state, 1),
                }

        # Ticker data from universe
        if self.universe and symbol in self.universe.ticker_data:
            t = self.universe.ticker_data[symbol]
            detail["ticker"] = {
                "price": t.get("lastPrice", "0"),
                "change_24h_pct": t.get("priceChangePercent", "0"),
                "volume_24h": t.get("quoteVolume", "0"),
                "high_24h": t.get("highPrice", "0"),
                "low_24h": t.get("lowPrice", "0"),
                "trades_24h": t.get("count", 0),
            }

        return web.json_response(detail)
