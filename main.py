"""
TinyCoins Momentum Event Trading System — Main Orchestrator

Entry point that wires all components together and runs the async event loop.
"""

import asyncio
import signal
import sys
from pathlib import Path

import structlog

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import load_config
from core.clock import Clock
from exchange.client import ExchangeClient
from exchange.metadata import MetadataCache
from exchange.websocket_manager import WebSocketManager
from exchange.order_manager import OrderManager
from universe.manager import UniverseManager
from radar.features import RadarFeatureEngine
from radar.scorer import RadarScorer
from radar.service import RadarService
from monitor.state import StateEngine
from monitor.features import DeepFeatureEngine
from monitor.stream_manager import PromotedStreamManager
from signals.lifecycle import LifecycleManager
from signals.long_engine import LongEngine
from execution.engine import ExecutionEngine
from execution.exit_manager import ExitManager
from risk.engine import RiskEngine
from dashboard.server import DashboardServer
from logging_.logger import setup_logging

logger = structlog.get_logger("main")


class TinyCoinsSystem:
    """
    Main orchestrator — wires all components and runs the event loop.
    """

    def __init__(self):
        self._config = None
        self._running = False
        self._tasks = []

    async def start(self) -> None:
        """Initialize and start all system components."""
        # ─── 1. Load configuration ───
        self._config = load_config()
        setup_logging(self._config.logging)

        logger.info(
            "system_starting",
            trade_mode=self._config.trade_mode,
            dry_run=self._config.dry_run,
            testnet=self._config.testnet,
        )

        if not self._config.api_key or self._config.api_key == "your_api_key_here":
            logger.error("NO_API_KEY — Please set BINANCE_API_KEY in .env file")
            print("\n⚠️  No Binance API key configured!")
            print("   Copy .env.example to .env and set your credentials.")
            print("   See: https://www.binance.com/en/my/settings/api-management\n")
            return

        # ─── 2. Initialize exchange client ───
        exchange = ExchangeClient(self._config)
        await exchange.connect()

        # ─── 3. Initialize clock ───
        clock = Clock()
        await clock.sync(exchange.client)

        # ─── 4. Initialize metadata cache ───
        metadata = MetadataCache()
        await metadata.refresh(exchange)

        # ─── 5. Initialize all components ───
        universe = UniverseManager(self._config, metadata)
        await universe.refresh(exchange)

        radar_features = RadarFeatureEngine(self._config.radar)
        radar_scorer = RadarScorer(self._config.radar, radar_features)
        radar_service = RadarService(
            self._config, universe, radar_features, radar_scorer
        )

        state_engine = StateEngine(self._config.monitor)
        deep_features = DeepFeatureEngine(self._config.monitor, state_engine)
        lifecycle = LifecycleManager()

        ws_manager = WebSocketManager(self._config, exchange.socket_manager)
        stream_manager = PromotedStreamManager(
            self._config, ws_manager, state_engine
        )

        order_manager = OrderManager(self._config, exchange, metadata)
        execution = ExecutionEngine(
            self._config, metadata, order_manager,
            state_engine, deep_features, lifecycle
        )
        exit_manager = ExitManager(self._config, state_engine, deep_features)
        risk_engine = RiskEngine(self._config, order_manager)

        long_engine = LongEngine(
            self._config, state_engine, deep_features, lifecycle
        )

        # ─── 6. Promotion/demotion callback ───
        async def on_promotion_change(to_promote, to_demote):
            for symbol in to_demote:
                await stream_manager.unsubscribe(symbol)
            for symbol in to_promote:
                await stream_manager.subscribe(symbol)

        # ─── 7. Print startup summary ───
        uni = universe.universe
        print("\n" + "=" * 60)
        print("  🪙  TinyCoins Momentum Trading System")
        print("=" * 60)
        print(f"  Mode:       {self._config.trade_mode}")
        print(f"  Dry Run:    {self._config.dry_run}")
        print(f"  Testnet:    {self._config.testnet}")
        print(f"  Universe:   {len(uni.monitored)} monitored")
        print(f"              {len(uni.radar_eligible)} radar-eligible")
        print(f"              {len(uni.long_eligible)} long-eligible")
        print(f"  Symbols:    {len(metadata.symbols)} total cached")
        print(f"  Clock:      offset={clock.offset_ms:.0f}ms")
        print("  Press Ctrl+C to stop")
        print(f"  Dashboard:  http://localhost:{self._config.dashboard_port}")
        print("=" * 60 + "\n")

        # ─── 8. Start background tasks ───
        self._running = True

        # Dashboard
        dashboard = DashboardServer(port=self._config.dashboard_port)
        dashboard.config = self._config
        dashboard.universe = universe
        dashboard.radar_scorer = radar_scorer
        dashboard.radar_features = radar_features
        dashboard.state_engine = state_engine
        dashboard.deep_features = deep_features
        dashboard.execution = execution
        dashboard.risk_engine = risk_engine
        dashboard.ws_manager = ws_manager
        dashboard.lifecycle = lifecycle
        dashboard.clock = clock
        await dashboard.start()

        # Clock sync
        self._tasks.append(
            asyncio.create_task(clock.start_periodic_sync(exchange.client))
        )
        # Metadata refresh
        self._tasks.append(
            asyncio.create_task(metadata.start_periodic_refresh(exchange))
        )
        # Universe refresh
        self._tasks.append(
            asyncio.create_task(universe.start_periodic_refresh(exchange))
        )
        # Mini-ticker stream for radar
        self._tasks.append(
            asyncio.create_task(
                ws_manager.start_mini_ticker_stream(radar_service.handle_mini_ticker)
            )
        )
        # Radar scoring loop
        self._tasks.append(
            asyncio.create_task(
                radar_service.run_scoring_loop(on_promotion_change)
            )
        )
        # Exit manager loop
        self._tasks.append(
            asyncio.create_task(exit_manager.run_exit_loop(execution))
        )

        # ─── 9. Main trading loop ───
        logger.info("main_loop_starting")

        try:
            while self._running:
                await asyncio.sleep(1)

                # Risk checks
                risk_engine.update_data_timestamp()
                risk_engine.check_risk_conditions(
                    execution.position_count,
                    ws_manager.last_message_time,
                )

                if not risk_engine.is_trading_allowed:
                    continue

                # Evaluate long signals
                promoted = radar_scorer.promoted_symbols
                if not promoted:
                    continue

                entry_signals = long_engine.evaluate(promoted)

                for symbol in entry_signals:
                    # Check risk budget
                    if not risk_engine.can_open_position(execution.position_count):
                        break

                    # Attempt entry
                    position = await execution.execute_entry(
                        symbol, reason="long_confirmed"
                    )
                    if position:
                        logger.info(
                            "trade_executed",
                            symbol=symbol,
                            qty=position.quantity,
                            entry=position.entry_price,
                        )

        except asyncio.CancelledError:
            pass
        finally:
            # ─── 10. Graceful shutdown ───
            logger.info("system_shutting_down")
            self._running = False
            radar_service.stop()

            # Close any open positions
            for symbol in list(execution.positions.keys()):
                await execution.close_position(symbol, "shutdown")

            # Cancel tasks
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

            # Shutdown WebSocket
            await ws_manager.shutdown()
            await dashboard.stop()
            await exchange.close()

            # Print final stats
            stats = risk_engine.get_status_summary()
            print("\n" + "=" * 60)
            print("  📊 Session Summary")
            print("=" * 60)
            print(f"  Trades:     {stats['daily']['trades']}")
            print(f"  Win Rate:   {stats['daily']['win_rate']}%")
            print(f"  PnL:        {stats['daily']['pnl']}")
            print(f"  Drawdown:   {stats['daily']['drawdown_pct']}%")
            print("=" * 60 + "\n")

            logger.info("system_stopped", final_stats=stats)


async def main():
    system = TinyCoinsSystem()

    # Handle Ctrl+C gracefully
    loop = asyncio.get_running_loop()

    def signal_handler():
        system._running = False

    try:
        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler
        pass

    await system.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete.")
