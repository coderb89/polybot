#!/usr/bin/env python3
"""
PolyBot - Polymarket Multi-Strategy Trading Bot
Strategies: Weather Arbitrage | Cross-Platform Arbitrage
Starting capital: $100 USDC on Polygon

Runs as a single scan-and-trade cycle (designed for GitHub Actions cron).
"""

import argparse
import asyncio
import logging
import sys

from config.settings import Settings
from core.portfolio import Portfolio
from core.risk_manager import RiskManager
from strategies.weather_arb import WeatherArbStrategy
from strategies.market_maker import MarketMakerStrategy
from strategies.cross_platform_arb import CrossPlatformArbStrategy
from utils.logger import setup_logger
from utils.telegram_alerts import TelegramAlerter

logger = setup_logger("polybot.main")


class PolyBot:
    def __init__(self, export_dashboard: bool = False):
        self.settings = Settings()
        self.portfolio = Portfolio(self.settings)
        self.risk_manager = RiskManager(self.settings, self.portfolio)
        self.alerter = TelegramAlerter(self.settings)
        self.export_dashboard = export_dashboard

        self.strategies = []
        if self.settings.ENABLE_WEATHER_ARB:
            self.strategies.append(WeatherArbStrategy(self.settings, self.portfolio, self.risk_manager))
        if self.settings.ENABLE_MARKET_MAKER:
            self.strategies.append(MarketMakerStrategy(self.settings, self.portfolio, self.risk_manager))
        if self.settings.ENABLE_CROSS_PLATFORM_ARB:
            self.strategies.append(CrossPlatformArbStrategy(self.settings, self.portfolio, self.risk_manager))

        logger.info(f"PolyBot initialized with {len(self.strategies)} strategies")
        logger.info(f"Mode: {'DRY RUN' if self.settings.DRY_RUN else 'LIVE TRADING'}")

    async def run_once(self):
        """Single scan-and-trade cycle (for GitHub Actions cron)."""
        logger.info("=" * 60)
        logger.info("  PolyBot - Single Cycle")
        logger.info(f"  Capital: ${self.settings.INITIAL_CAPITAL}")
        logger.info(f"  Strategies: {[s.__class__.__name__ for s in self.strategies]}")
        logger.info(f"  DRY_RUN: {self.settings.DRY_RUN}")
        logger.info("=" * 60)

        await self.alerter.send(
            f"PolyBot scan started\n"
            f"Mode: {'DRY RUN' if self.settings.DRY_RUN else 'LIVE'}\n"
            f"Portfolio: ${self.portfolio.get_portfolio_value():.2f}"
        )

        # Check if risk manager needs daily reset
        self.risk_manager.check_daily_reset()

        # Run each strategy's single scan
        for strategy in self.strategies:
            try:
                await strategy.run_once()
            except Exception as e:
                logger.error(f"Strategy {strategy.__class__.__name__} failed: {e}", exc_info=True)

        # Take portfolio snapshot
        self.portfolio.snapshot()

        # Report results
        summary = self.portfolio.get_summary()
        logger.info(f"Scan complete:\n{summary}")
        await self.alerter.send(f"Scan complete\n{summary}")

        # Export dashboard data for GitHub Pages
        if self.export_dashboard:
            self.portfolio.export_dashboard_json("dashboard/dashboard_data.json")
            logger.info("Dashboard data exported")

        # Cleanup strategies
        for strategy in self.strategies:
            await strategy.cleanup()


def main():
    parser = argparse.ArgumentParser(description="PolyBot - Polymarket Trading Bot")
    parser.add_argument(
        "--export-dashboard",
        action="store_true",
        help="Export dashboard data to JSON for GitHub Pages"
    )
    args = parser.parse_args()

    bot = PolyBot(export_dashboard=args.export_dashboard)
    asyncio.run(bot.run_once())


if __name__ == "__main__":
    main()
