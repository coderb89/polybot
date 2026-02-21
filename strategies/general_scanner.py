"""
General Market Scanner Strategy
Scans all active Polymarket markets for mispriced opportunities.
Looks for markets where the order book mid-price suggests an edge.
Uses simple momentum and value metrics to generate trades.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import List, Optional, Dict

from core.polymarket_client import PolymarketClient
from core.portfolio import Portfolio, Trade
from core.risk_manager import RiskManager

logger = logging.getLogger("polybot.scanner")


class GeneralScannerStrategy:
    """Scans Polymarket for any market with a favorable YES/NO price imbalance."""

    def __init__(self, settings, portfolio: Portfolio, risk_manager: RiskManager):
        self.settings = settings
        self.portfolio = portfolio
        self.risk_manager = risk_manager
        self.poly_client = PolymarketClient(settings)
        self.traded_markets: Dict[str, float] = {}  # condition_id -> last_trade_time

    async def run_once(self):
        """Single scan-and-trade cycle for GitHub Actions."""
        logger.info("GeneralScanner: scanning active markets")
        try:
            opportunities = await self._scan_markets()
            executed = 0
            for opp in opportunities[:3]:  # Max 3 trades per cycle
                success = await self._execute_trade(opp)
                if success:
                    executed += 1
            logger.info(f"GeneralScanner complete: {len(opportunities)} opportunities, {executed} trades executed")
        except Exception as e:
            logger.error(f"GeneralScanner error: {e}", exc_info=True)

    async def _scan_markets(self) -> List[Dict]:
        """Fetch markets and find mispriced opportunities."""
        markets = await self.poly_client.get_markets(active_only=True)
        logger.info(f"GeneralScanner: analyzing {len(markets)} active markets")

        opportunities = []
        analyzed = 0
        skipped_no_tokens = 0
        skipped_no_book = 0
        skipped_low_liq = 0

        for market in markets[:100]:  # Analyze top 100 markets by volume
            condition_id = market.get("condition_id", "")

            # Skip recently traded markets (1 hour cooldown)
            if condition_id in self.traded_markets:
                if time.time() - self.traded_markets[condition_id] < 3600:
                    continue

            # Must have YES and NO tokens
            tokens = market.get("tokens", [])
            if len(tokens) < 2:
                skipped_no_tokens += 1
                continue

            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_token = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)
            if not yes_token or not no_token:
                skipped_no_tokens += 1
                continue

            yes_id = yes_token.get("token_id")
            no_id = no_token.get("token_id")
            if not yes_id or not no_id:
                skipped_no_tokens += 1
                continue

            # Check resolution time — minimum 12 hours
            end_date = market.get("end_date_iso", "")
            if end_date:
                try:
                    resolution_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                    hours_until = (resolution_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_until < 12:
                        continue
                except Exception:
                    pass

            # Get order books for both sides
            analyzed += 1
            yes_book = await self.poly_client.get_order_book(yes_id)
            if not yes_book:
                skipped_no_book += 1
                continue

            no_book = await self.poly_client.get_order_book(no_id)
            if not no_book:
                skipped_no_book += 1
                continue

            # Minimum liquidity check ($25)
            min_liquidity = min(yes_book.liquidity_usd, no_book.liquidity_usd)
            if min_liquidity < 25:
                skipped_low_liq += 1
                continue

            yes_mid = yes_book.mid_price
            no_mid = no_book.mid_price
            total = yes_mid + no_mid

            # ── Opportunity Type 1: Arbitrage (YES + NO < $1.00) ──
            if total < 0.99:
                arb_edge = 1.0 - total - 0.004  # Subtract ~0.4% fees
                if arb_edge > 0.003:  # > 0.3% edge
                    opportunities.append({
                        "type": "arb",
                        "condition_id": condition_id,
                        "question": market.get("question", ""),
                        "yes_token_id": yes_id,
                        "no_token_id": no_id,
                        "yes_price": yes_mid,
                        "no_price": no_mid,
                        "edge": arb_edge,
                        "liquidity": min_liquidity,
                        "side": "BOTH",
                    })
                    continue

            # ── Opportunity Type 2: Value bet (strong lean on one side) ──
            # Buy the cheap side when one outcome is priced between 5¢-45¢
            # with tight spread. Avoid penny stocks (< 5¢) as they're almost
            # always losers despite seeming "high edge".
            spread = yes_book.spread

            if 0.05 <= yes_mid <= 0.45 and spread < 0.10 and min_liquidity > 50:
                # Cheap YES — potential value
                edge = 0.45 - yes_mid  # Implied edge to fair value
                if edge > 0.03:
                    opportunities.append({
                        "type": "value",
                        "condition_id": condition_id,
                        "question": market.get("question", ""),
                        "yes_token_id": yes_id,
                        "no_token_id": no_id,
                        "yes_price": yes_mid,
                        "no_price": no_mid,
                        "edge": edge,
                        "liquidity": min_liquidity,
                        "side": "BUY_YES",
                    })
            elif 0.05 <= no_mid <= 0.45 and spread < 0.10 and min_liquidity > 50:
                edge = 0.45 - no_mid
                if edge > 0.03:
                    opportunities.append({
                        "type": "value",
                        "condition_id": condition_id,
                        "question": market.get("question", ""),
                        "yes_token_id": yes_id,
                        "no_token_id": no_id,
                        "yes_price": yes_mid,
                        "no_price": no_mid,
                        "edge": edge,
                        "liquidity": min_liquidity,
                        "side": "BUY_NO",
                    })

            # Rate limit: don't hammer the API
            if analyzed % 10 == 0:
                await asyncio.sleep(0.3)

        logger.info(f"GeneralScanner stats: analyzed={analyzed}, no_tokens={skipped_no_tokens}, "
                   f"no_book={skipped_no_book}, low_liq={skipped_low_liq}")
        opportunities.sort(key=lambda x: x["edge"], reverse=True)
        if opportunities:
            logger.info(f"GeneralScanner: {len(opportunities)} opportunities, best edge: {opportunities[0]['edge']:.2%}")
        else:
            logger.info("GeneralScanner: no opportunities found this cycle")
        return opportunities

    async def _execute_trade(self, opp: Dict) -> bool:
        """Execute a paper/live trade for an opportunity."""
        portfolio_val = self.portfolio.get_portfolio_value()

        # Position sizing: max 5% of portfolio or $5, whichever is smaller
        max_size = min(
            portfolio_val * 0.05,
            5.0,
            opp["liquidity"] * 0.05  # Don't take more than 5% of liquidity
        )

        if max_size < 0.50:
            return False

        approved, reason = self.risk_manager.approve_trade(max_size, "general_scanner", opp["condition_id"])
        if not approved:
            logger.debug(f"Trade rejected: {reason}")
            return False

        if opp["type"] == "arb":
            # Buy both YES and NO
            logger.info(
                f"[SCANNER] ARB | {opp['question'][:55]} | "
                f"YES: {opp['yes_price']:.3f} + NO: {opp['no_price']:.3f} = {(opp['yes_price']+opp['no_price']):.3f} | "
                f"Edge: {opp['edge']:.2%} | Size: ${max_size:.2f}"
            )
            half_size = max_size / 2
            yes_result = await self.poly_client.place_market_order(
                opp["yes_token_id"], half_size, "BUY", self.settings.DRY_RUN)
            no_result = await self.poly_client.place_market_order(
                opp["no_token_id"], half_size, "BUY", self.settings.DRY_RUN)

            if yes_result.success and no_result.success:
                expected_pnl = max_size * opp["edge"]
                trade = Trade(
                    id=None, timestamp=datetime.utcnow().isoformat(),
                    strategy="general_scanner", market_id=opp["condition_id"],
                    market_question=opp["question"], side="BOTH",
                    token_id=f"{opp['yes_token_id'][:16]}|{opp['no_token_id'][:16]}",
                    price=(opp["yes_price"] + opp["no_price"]),
                    size_usd=max_size, edge_pct=opp["edge"],
                    dry_run=self.settings.DRY_RUN,
                    order_id=f"{yes_result.order_id}|{no_result.order_id}",
                    pnl=expected_pnl, status="open"
                )
                self.portfolio.log_trade(trade)
                self.traded_markets[opp["condition_id"]] = time.time()
                logger.info(f"ARB executed! Expected PnL: ${expected_pnl:.4f}")
                return True

        elif opp["type"] == "value":
            side = opp["side"]
            token_id = opp["yes_token_id"] if side == "BUY_YES" else opp["no_token_id"]
            price = opp["yes_price"] if side == "BUY_YES" else opp["no_price"]

            logger.info(
                f"[SCANNER] VALUE | {opp['question'][:55]} | "
                f"{side} @ {price:.3f} | Edge: {opp['edge']:.2%} | Size: ${max_size:.2f}"
            )

            result = await self.poly_client.place_market_order(
                token_id, max_size, "BUY", self.settings.DRY_RUN)

            if result.success:
                trade = Trade(
                    id=None, timestamp=datetime.utcnow().isoformat(),
                    strategy="general_scanner", market_id=opp["condition_id"],
                    market_question=opp["question"], side=side,
                    token_id=token_id,
                    price=price, size_usd=max_size, edge_pct=opp["edge"],
                    dry_run=self.settings.DRY_RUN,
                    order_id=result.order_id,
                    pnl=None, status="open"
                )
                self.portfolio.log_trade(trade)
                self.traded_markets[opp["condition_id"]] = time.time()
                logger.info(f"VALUE trade placed: ${max_size:.2f}")
                return True

        return False

    async def cleanup(self):
        logger.info(f"GeneralScanner cleanup: {len(self.traded_markets)} markets traded")
