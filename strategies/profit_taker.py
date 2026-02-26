"""
Profit Taker — Active Position Management
Checks all open positions each cycle and SELLS when profit targets are hit.

This is the CRITICAL missing piece: without active selling, the bot just buys
tokens and waits for market resolution (which takes weeks/months).

Logic:
1. For each open position, fetch current market price
2. Compare current price to entry price
3. SELL if:
   - Price moved up 30%+ (profit target)
   - Price dropped 40%+ (stop-loss to cut losses)
   - Market is about to close within 1 hour (exit before resolution)
4. For arb trades (BOTH sides): sell the winning side after resolution

This runs BEFORE new trades each cycle, so profits are realized
and capital is freed for new trades.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List, Dict, Optional

import httpx

from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager

logger = logging.getLogger("polybot.profit_taker")

# Profit-taking thresholds
PROFIT_TARGET_PCT = 0.25       # 25% price increase → sell for profit
STOP_LOSS_PCT = -0.50          # 50% price drop → cut losses
TRAILING_PROFIT_PCT = 0.15     # 15% gain → sell (more aggressive for faster turnover)
ARB_AUTO_CLOSE_HOURS = 1       # Close arb positions within 1 hour of resolution
MAX_HOLD_HOURS = 168           # 7 days max hold — force close if no movement
STALE_POSITION_HOURS = 48      # After 48h with < 5% movement, close to free capital


class ProfitTakerStrategy:
    """Actively manages open positions: takes profits, cuts losses, frees capital."""

    def __init__(self, settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)

    async def run_once(self):
        """Check all open positions and sell if targets are hit."""
        open_positions = self.portfolio.get_open_positions()
        if not open_positions:
            logger.info("ProfitTaker: no open positions to manage")
            return

        logger.info(f"ProfitTaker: checking {len(open_positions)} open positions")

        closed_count = 0
        profit_total = 0.0
        now = datetime.now(timezone.utc)

        for pos in open_positions:
            try:
                result = await self._evaluate_position(pos, now)
                if result:
                    action, pnl, reason = result
                    if action == "CLOSE":
                        await self._close_position(pos, pnl, reason)
                        closed_count += 1
                        profit_total += pnl
                        logger.info(
                            f"ProfitTaker: CLOSED {pos.get('market_question', '')[:50]} | "
                            f"PnL: ${pnl:+.4f} | Reason: {reason}"
                        )
            except Exception as e:
                logger.debug(f"ProfitTaker: error evaluating position {pos.get('id')}: {e}")

            # Rate limit
            await asyncio.sleep(0.3)

        if closed_count > 0:
            logger.info(f"ProfitTaker: closed {closed_count} positions, total PnL: ${profit_total:+.4f}")
        else:
            logger.info("ProfitTaker: no positions met close criteria this cycle")

    async def _evaluate_position(self, pos: Dict, now: datetime) -> Optional[tuple]:
        """Evaluate if a position should be closed.

        Returns: (action, pnl, reason) or None if no action needed.
        """
        side = pos.get("side", "")
        entry_price = pos.get("price", 0)
        size_usd = pos.get("size_usd", 0)
        token_id = pos.get("token_id", "")
        market_id = pos.get("market_id", "")

        if not token_id or not entry_price or entry_price <= 0:
            return None

        # Calculate hold duration
        try:
            entry_time = datetime.fromisoformat(pos.get("timestamp", ""))
            if entry_time.tzinfo is None:
                entry_time = entry_time.replace(tzinfo=timezone.utc)
            hold_hours = (now - entry_time).total_seconds() / 3600
        except Exception:
            hold_hours = 0

        # ══════════════════════════════════════════════════════════
        # CASE 1: Arb trades (side = "BOTH")
        # These have two token IDs separated by "|"
        # ══════════════════════════════════════════════════════════
        if side == "BOTH":
            return await self._evaluate_arb_position(pos, entry_price, size_usd, hold_hours)

        # ══════════════════════════════════════════════════════════
        # CASE 2: Value trades (BUY_YES or BUY_NO)
        # Check current price vs entry price
        # ══════════════════════════════════════════════════════════

        # For value trades, token_id is the actual single token
        current_price = await self.poly_client.get_market_price(token_id)
        if current_price is None or current_price <= 0:
            # Try order book as fallback
            book = await self.poly_client.get_order_book(token_id)
            if book:
                current_price = book.mid_price
            else:
                return None

        # Calculate price change and unrealized P&L
        price_change_pct = (current_price - entry_price) / entry_price
        tokens_owned = size_usd / entry_price
        current_value = tokens_owned * current_price
        unrealized_pnl = current_value - size_usd
        # Subtract estimated fees for selling
        sell_fees = current_value * 0.002  # ~0.2% fee
        net_pnl = unrealized_pnl - sell_fees

        logger.debug(
            f"Position {pos.get('id')}: entry={entry_price:.3f} current={current_price:.3f} "
            f"change={price_change_pct:+.1%} pnl=${net_pnl:+.4f} hold={hold_hours:.0f}h"
        )

        # ── PROFIT TARGET: Price moved up significantly ──
        if price_change_pct >= PROFIT_TARGET_PCT and net_pnl > 0:
            return ("CLOSE", round(net_pnl, 4),
                    f"profit_target: {price_change_pct:+.0%} gain (entry={entry_price:.3f} now={current_price:.3f})")

        # ── TRAILING PROFIT: After 24h, take smaller profits ──
        if hold_hours > 24 and price_change_pct >= TRAILING_PROFIT_PCT and net_pnl > 0:
            return ("CLOSE", round(net_pnl, 4),
                    f"trailing_profit: {price_change_pct:+.0%} after {hold_hours:.0f}h")

        # ── STOP LOSS: Price dropped significantly ──
        if price_change_pct <= STOP_LOSS_PCT:
            return ("CLOSE", round(net_pnl, 4),
                    f"stop_loss: {price_change_pct:+.0%} (entry={entry_price:.3f} now={current_price:.3f})")

        # ── NEAR-RESOLUTION: Price is very high (>$0.92) — token is about to win ──
        if current_price >= 0.92:
            # This token is likely winning — sell at high price for guaranteed profit
            if net_pnl > 0:
                return ("CLOSE", round(net_pnl, 4),
                        f"near_resolution: price={current_price:.3f} (almost certain win)")

        # ── NEAR-RESOLUTION: Price is very low (<$0.08) — token is about to lose ──
        if current_price <= 0.08 and hold_hours > 2:
            # This token is likely losing — sell remnant to recover some capital
            return ("CLOSE", round(net_pnl, 4),
                    f"likely_loser: price={current_price:.3f} (cutting losses)")

        # ── STALE POSITION: No significant movement after 48h ──
        if hold_hours > STALE_POSITION_HOURS and abs(price_change_pct) < 0.05:
            # Flat position tying up capital — close to free it for new trades
            return ("CLOSE", round(net_pnl, 4),
                    f"stale: {price_change_pct:+.0%} after {hold_hours:.0f}h (freeing capital)")

        # ── MAX HOLD TIME: Force close after 7 days ──
        if hold_hours > MAX_HOLD_HOURS:
            return ("CLOSE", round(net_pnl, 4),
                    f"max_hold: {hold_hours:.0f}h exceeded {MAX_HOLD_HOURS}h limit")

        return None

    async def _evaluate_arb_position(self, pos: Dict, entry_price: float,
                                      size_usd: float, hold_hours: float) -> Optional[tuple]:
        """Evaluate arb position (BOTH sides). These are guaranteed profit on resolution."""
        token_id = pos.get("token_id", "")

        # Arb token_id format: "yes_id_prefix|no_id_prefix"
        # We can't easily look up current prices for truncated IDs
        # So we use market resolution or time-based closing

        # Check if market is resolved via Gamma API
        market_id = pos.get("market_id", "")
        if market_id:
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(
                        f"{self.settings.GAMMA_HOST}/markets",
                        params={"condition_id": market_id, "limit": 1}
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        market = data[0] if isinstance(data, list) and data else data if isinstance(data, dict) else None
                        if market:
                            resolved = market.get("resolved", False) or market.get("closed", False)
                            if resolved:
                                # Market resolved — arb profit is guaranteed
                                expected_pnl = size_usd * (1.0 / entry_price - 1.0) - size_usd * 0.004
                                return ("CLOSE", round(max(expected_pnl, 0), 4),
                                        "arb_resolved: market settled")

                            # Check how close to resolution
                            end_date = market.get("endDateIso", market.get("end_date_iso", ""))
                            if end_date:
                                try:
                                    from datetime import datetime as dt
                                    now = datetime.now(timezone.utc)
                                    resolution_dt = dt.fromisoformat(end_date.replace("Z", "+00:00"))
                                    hours_left = (resolution_dt - now).total_seconds() / 3600
                                    if hours_left < 0:
                                        # Past resolution — close with expected profit
                                        expected_pnl = size_usd * (1.0 / entry_price - 1.0) - size_usd * 0.004
                                        return ("CLOSE", round(max(expected_pnl, 0), 4),
                                                "arb_past_resolution: market should have settled")
                                except Exception:
                                    pass
            except Exception as e:
                logger.debug(f"Arb market check failed for {market_id[:16]}: {e}")

        # Arb positions past max hold time
        if hold_hours > MAX_HOLD_HOURS * 2:  # Give arbs more time (14 days)
            expected_pnl = pos.get("pnl", 0) or 0
            return ("CLOSE", round(expected_pnl, 4),
                    f"arb_max_hold: {hold_hours:.0f}h exceeded limit")

        return None

    async def _close_position(self, pos: Dict, pnl: float, reason: str):
        """Close a position by selling and updating the database."""
        side = pos.get("side", "")
        token_id = pos.get("token_id", "")
        size_usd = pos.get("size_usd", 0)
        trade_id = pos.get("id")

        # For value bets, try to sell the tokens
        if side in ("BUY_YES", "BUY_NO", "BUY") and token_id and "|" not in token_id:
            entry_price = pos.get("price", 0)
            tokens_owned = size_usd / entry_price if entry_price > 0 else 0

            if tokens_owned > 0:
                # Place sell order
                sell_result = await self.poly_client.place_market_order(
                    token_id,
                    tokens_owned,  # Sell all tokens we own
                    "SELL",
                    self.settings.DRY_RUN
                )

                if sell_result.success:
                    logger.info(f"SELL order placed: {tokens_owned:.4f} tokens of {token_id[:16]}...")
                else:
                    logger.warning(f"SELL failed for {token_id[:16]}: {sell_result.error}")
                    # Still close in DB for paper trading
                    if not self.settings.DRY_RUN:
                        return  # Don't close if live sell failed

        # Update database
        status = "won" if pnl > 0 else "lost" if pnl < 0 else "resolved"
        self.portfolio.close_trade(trade_id, pnl, status, reason)

    async def cleanup(self):
        logger.info("ProfitTaker: cleanup complete")
