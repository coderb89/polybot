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

import httpx

from config.settings import Settings
from core.portfolio import Portfolio
from core.risk_manager import RiskManager
from strategies.weather_arb import WeatherArbStrategy
from strategies.market_maker import MarketMakerStrategy
from strategies.cross_platform_arb import CrossPlatformArbStrategy
from utils.logger import setup_logger
from utils.telegram_alerts import TelegramAlerter

logger = setup_logger("polybot.main")


POLYGON_RPC_URLS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
    "https://1rpc.io/matic",
]


async def check_wallet_balance(address: str) -> dict:
    """Check MATIC and USDC balances on Polygon via public RPC (tries multiple providers)."""
    balances = {"matic": 0.0, "usdc": 0.0, "error": None}
    if not address:
        balances["error"] = "No wallet address configured"
        return balances

    # USDC contracts on Polygon
    usdc_native = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"   # Native USDC
    usdc_bridged = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"   # USDC.e (bridged)
    padded_addr = address.lower().replace("0x", "").zfill(64)

    for rpc_url in POLYGON_RPC_URLS:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                # 1. MATIC balance
                resp = await client.post(rpc_url, json={
                    "jsonrpc": "2.0", "method": "eth_getBalance",
                    "params": [address, "latest"], "id": 1
                })
                data = resp.json()
                if "result" in data:
                    balances["matic"] = int(data["result"], 16) / 1e18

                # 2. Native USDC balance
                resp2 = await client.post(rpc_url, json={
                    "jsonrpc": "2.0", "method": "eth_call",
                    "params": [{"to": usdc_native, "data": f"0x70a08231{padded_addr}"}, "latest"],
                    "id": 2
                })
                data2 = resp2.json()
                if "result" in data2 and data2["result"] not in ("0x", "0x0", ""):
                    balances["usdc"] = int(data2["result"], 16) / 1e6

                # 3. Bridged USDC.e balance
                resp3 = await client.post(rpc_url, json={
                    "jsonrpc": "2.0", "method": "eth_call",
                    "params": [{"to": usdc_bridged, "data": f"0x70a08231{padded_addr}"}, "latest"],
                    "id": 3
                })
                data3 = resp3.json()
                if "result" in data3 and data3["result"] not in ("0x", "0x0", ""):
                    balances["usdc"] += int(data3["result"], 16) / 1e6

                logger.info(f"Wallet balance via {rpc_url.split('/')[2]}: "
                          f"{balances['matic']:.4f} MATIC | ${balances['usdc']:.2f} USDC")
                return balances  # Success — stop trying other RPCs

        except Exception as e:
            logger.debug(f"RPC {rpc_url} failed: {e}")
            continue

    balances["error"] = "All RPC providers failed"
    logger.warning("Wallet balance check: all RPC providers failed")
    return balances


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

        # ── Check on-chain wallet balance ──
        wallet_addr = self.settings.FUNDER_ADDRESS
        balances = await check_wallet_balance(wallet_addr)
        if balances["error"]:
            logger.warning(f"Wallet balance check: {balances['error']}")
        else:
            logger.info(f"Wallet Balance: {balances['matic']:.4f} MATIC | ${balances['usdc']:.2f} USDC")
        self.portfolio.set_wallet_balances(balances)

        await self.alerter.send(
            f"PolyBot scan started\n"
            f"Mode: {'DRY RUN' if self.settings.DRY_RUN else 'LIVE'}\n"
            f"Portfolio: ${self.portfolio.get_portfolio_value():.2f}\n"
            f"Wallet: {balances['matic']:.4f} MATIC | ${balances['usdc']:.2f} USDC"
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

        # Log open positions summary
        open_positions = self.portfolio.get_open_positions()
        if open_positions:
            logger.info(f"Open Positions ({len(open_positions)}):")
            for pos in open_positions:
                logger.info(f"  - {pos.get('market_question', 'N/A')[:60]} | "
                          f"${pos.get('size_usd', 0):.2f} invested | "
                          f"Edge: {pos.get('edge_pct', 0):.1%}")
        else:
            logger.info("No open positions")

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
