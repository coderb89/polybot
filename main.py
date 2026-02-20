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


async def check_wallet_balance(address: str) -> dict:
    """Check MATIC and USDC balances on Polygon via public RPC."""
    balances = {"matic": 0.0, "usdc": 0.0, "error": None}
    if not address:
        balances["error"] = "No wallet address configured"
        return balances

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # Check MATIC balance via Polygon RPC
            rpc_url = "https://polygon-mainnet.g.alchemy.com/v2/demo"
            payload = {
                "jsonrpc": "2.0",
                "method": "eth_getBalance",
                "params": [address, "latest"],
                "id": 1
            }
            resp = await client.post(rpc_url, json=payload)
            data = resp.json()
            if "result" in data:
                matic_wei = int(data["result"], 16)
                balances["matic"] = matic_wei / 1e18

            # Check USDC balance (Polygon USDC contract: 0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359)
            usdc_contract = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
            # balanceOf(address) selector = 0x70a08231 + padded address
            padded_addr = address.lower().replace("0x", "").zfill(64)
            call_data = f"0x70a08231{padded_addr}"
            payload_usdc = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{"to": usdc_contract, "data": call_data}, "latest"],
                "id": 2
            }
            resp2 = await client.post(rpc_url, json=payload_usdc)
            data2 = resp2.json()
            if "result" in data2 and data2["result"] != "0x":
                usdc_raw = int(data2["result"], 16)
                balances["usdc"] = usdc_raw / 1e6  # USDC has 6 decimals

            # Also check USDC.e (bridged USDC: 0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
            usdc_e_contract = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
            padded_addr_e = address.lower().replace("0x", "").zfill(64)
            call_data_e = f"0x70a08231{padded_addr_e}"
            payload_usdc_e = {
                "jsonrpc": "2.0",
                "method": "eth_call",
                "params": [{"to": usdc_e_contract, "data": call_data_e}, "latest"],
                "id": 3
            }
            resp3 = await client.post(rpc_url, json=payload_usdc_e)
            data3 = resp3.json()
            if "result" in data3 and data3["result"] != "0x":
                usdc_e_raw = int(data3["result"], 16)
                balances["usdc"] += usdc_e_raw / 1e6  # Add bridged USDC

    except Exception as e:
        balances["error"] = str(e)
        logger.warning(f"Wallet balance check failed: {e}")

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
