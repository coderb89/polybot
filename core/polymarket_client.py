"""
Polymarket CLOB Client Wrapper
Handles all API calls to Polymarket's Central Limit Order Book.
"""

import asyncio
import logging
import time
from typing import Optional, Dict, List
from dataclasses import dataclass

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs, MarketOrderArgs, OrderType, OrderBookSummary
)
from py_clob_client.order_builder.constants import BUY, SELL

logger = logging.getLogger("polybot.clob")


@dataclass
class OrderBook:
    token_id: str
    bids: List[Dict]  # [{price, size}]
    asks: List[Dict]
    mid_price: float
    spread: float
    liquidity_usd: float


@dataclass
class TradeResult:
    success: bool
    order_id: Optional[str]
    filled_price: Optional[float]
    filled_size: Optional[float]
    error: Optional[str] = None


class PolymarketClient:
    """Async wrapper around Polymarket's py-clob-client."""

    def __init__(self, settings):
        self.settings = settings
        self._client: Optional[ClobClient] = None
        self._rate_limit_delay = 1.0 / 60  # 60 orders/min limit
        self._last_request_time = 0

    def _get_client(self) -> ClobClient:
        """Lazy-init the CLOB client (creates API creds on first call)."""
        if self._client is None:
            if self.settings.PRIVATE_KEY:
                self._client = ClobClient(
                    self.settings.CLOB_HOST,
                    key=self.settings.PRIVATE_KEY,
                    chain_id=self.settings.CHAIN_ID,
                    signature_type=0,  # EOA wallet
                )
                self._client.set_api_creds(self._client.create_or_derive_api_creds())
                logger.info("Polymarket CLOB client authenticated")
            else:
                # Read-only mode
                self._client = ClobClient(self.settings.CLOB_HOST)
                logger.info("Polymarket CLOB client in read-only mode")
        return self._client

    async def _rate_limit(self):
        """Enforce API rate limits."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self._rate_limit_delay:
            await asyncio.sleep(self._rate_limit_delay - elapsed)
        self._last_request_time = time.monotonic()

    async def get_markets(self, tag: Optional[str] = None, active_only: bool = True) -> List[Dict]:
        """Fetch active markets, optionally filtered by tag."""
        await self._rate_limit()
        try:
            client = self._get_client()
            markets = []
            next_cursor = ""
            page = 0
            max_pages = 3  # Limit pages to avoid long runs in GH Actions

            while page < max_pages:
                page += 1
                try:
                    if next_cursor:
                        resp = client.get_markets(next_cursor=next_cursor)
                    else:
                        resp = client.get_markets()
                except Exception as e:
                    logger.warning(f"get_markets page {page} failed: {e}")
                    break

                # Handle response — could be list or dict depending on client version
                if isinstance(resp, list):
                    data = resp
                    next_cursor = ""
                elif isinstance(resp, dict):
                    data = resp.get("data", resp.get("markets", []))
                    next_cursor = resp.get("next_cursor", "")
                else:
                    break

                if not data:
                    break

                for m in data:
                    if not isinstance(m, dict):
                        continue
                    if active_only and not m.get("active", False):
                        continue
                    if tag and tag.lower() not in [t.lower() for t in m.get("tags", [])]:
                        continue
                    markets.append(m)

                # Stop if no valid cursor or cursor is "LTE0" / empty
                if not next_cursor or next_cursor == "LTE0":
                    break

            logger.info(f"Fetched {len(markets)} markets across {page} pages")
            return markets
        except Exception as e:
            logger.error(f"Failed to fetch markets: {e}")
            return []

    async def get_order_book(self, token_id: str) -> Optional[OrderBook]:
        """Get current order book for a market token."""
        await self._rate_limit()
        try:
            client = self._get_client()
            book = client.get_order_book(token_id)
            bids = [{"price": float(b.price), "size": float(b.size)} for b in (book.bids or [])]
            asks = [{"price": float(a.price), "size": float(a.size)} for a in (book.asks or [])]

            if not bids or not asks:
                return None

            best_bid = max(bids, key=lambda x: x["price"])["price"]
            best_ask = min(asks, key=lambda x: x["price"])["price"]
            mid = (best_bid + best_ask) / 2
            spread = best_ask - best_bid
            liquidity = sum(b["price"] * b["size"] for b in bids) + sum(a["price"] * a["size"] for a in asks)

            return OrderBook(
                token_id=token_id,
                bids=bids,
                asks=asks,
                mid_price=mid,
                spread=spread,
                liquidity_usd=liquidity
            )
        except Exception as e:
            logger.debug(f"Order book fetch failed for {token_id}: {e}")
            return None

    async def get_market_price(self, token_id: str, side: str = "MID") -> Optional[float]:
        """Get current mid-price for a token."""
        try:
            client = self._get_client()
            await self._rate_limit()
            if side == "MID":
                result = client.get_midpoint(token_id)
                return float(result.get("mid", 0))
            else:
                result = client.get_price(token_id, side=side)
                return float(result.get("price", 0))
        except Exception as e:
            logger.debug(f"Price fetch failed for {token_id}: {e}")
            return None

    async def search_markets(self, query: str) -> List[Dict]:
        """Search markets by keyword (uses Gamma API for text search)."""
        try:
            async with httpx.AsyncClient() as http:
                resp = await http.get(
                    f"{self.settings.GAMMA_HOST}/markets",
                    params={"search": query, "active": "true", "limit": 50},
                    timeout=10
                )
                resp.raise_for_status()
                result = resp.json()
                # Gamma API returns a list directly, not {"markets": [...]}
                if isinstance(result, list):
                    return result
                elif isinstance(result, dict):
                    return result.get("markets", result.get("data", []))
                return []
        except Exception as e:
            logger.error(f"Market search failed: {e}")
            return []

    async def place_limit_order(
        self, token_id: str, price: float, size: float, side: str, dry_run: bool = True
    ) -> TradeResult:
        """Place a limit order on the CLOB."""
        side_const = BUY if side.upper() == "BUY" else SELL
        logger.info(f"{'[DRY RUN] ' if dry_run else ''}LIMIT {side} {size:.2f} @ ${price:.4f} token={token_id[:8]}...")

        if dry_run:
            return TradeResult(success=True, order_id="dry_run_" + str(int(time.time())),
                               filled_price=price, filled_size=size)

        await self._rate_limit()
        try:
            client = self._get_client()
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=side_const,
            )
            signed = client.create_order(order_args)
            resp = client.post_order(signed, OrderType.GTC)

            if resp.get("success"):
                order_id = resp.get("orderID", "unknown")
                logger.info(f"Order placed: {order_id}")
                return TradeResult(success=True, order_id=order_id, filled_price=price, filled_size=size)
            else:
                err = resp.get("errorMsg", "Unknown error")
                logger.warning(f"Order failed: {err}")
                return TradeResult(success=False, order_id=None, filled_price=None, filled_size=None, error=err)
        except Exception as e:
            logger.error(f"Order placement exception: {e}")
            return TradeResult(success=False, order_id=None, filled_price=None, filled_size=None, error=str(e))

    async def place_market_order(
        self, token_id: str, amount_usd: float, side: str, dry_run: bool = True
    ) -> TradeResult:
        """Place a market order."""
        logger.info(f"{'[DRY RUN] ' if dry_run else ''}MARKET {side} ${amount_usd:.2f} token={token_id[:8]}...")

        if dry_run:
            return TradeResult(success=True, order_id="dry_run_mkt_" + str(int(time.time())),
                               filled_price=None, filled_size=amount_usd)

        await self._rate_limit()
        try:
            client = self._get_client()
            side_const = BUY if side.upper() == "BUY" else SELL
            order_args = MarketOrderArgs(token_id=token_id, amount=amount_usd, side=side_const)
            signed = client.create_market_order(order_args)
            resp = client.post_order(signed, OrderType.FOK)

            if resp.get("success"):
                return TradeResult(success=True, order_id=resp.get("orderID"), filled_price=None, filled_size=amount_usd)
            else:
                return TradeResult(success=False, order_id=None, filled_price=None, filled_size=None,
                                   error=resp.get("errorMsg"))
        except Exception as e:
            return TradeResult(success=False, order_id=None, filled_price=None, filled_size=None, error=str(e))

    async def cancel_order(self, order_id: str, dry_run: bool = True) -> bool:
        """Cancel an open order."""
        if dry_run:
            logger.info(f"[DRY RUN] Cancel order {order_id}")
            return True
        try:
            client = self._get_client()
            resp = client.cancel(order_id)
            return resp.get("canceled", False)
        except Exception as e:
            logger.error(f"Cancel failed for {order_id}: {e}")
            return False

    async def get_open_orders(self) -> List[Dict]:
        """Get all open orders for authenticated wallet."""
        try:
            client = self._get_client()
            resp = client.get_orders()
            return resp if isinstance(resp, list) else []
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []
