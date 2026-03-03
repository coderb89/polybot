"""
Lightweight OpenAI API client using httpx.
No langchain/chromadb dependencies — just direct API calls.
Adapted from Polymarket/agents superforecaster approach.
"""

import json
import logging
import os
import re
from typing import Optional, Dict, List

import httpx

logger = logging.getLogger("polybot.ai_client")

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# ─── Superforecaster System Prompt ────────────────────────────────────
# Adapted from Polymarket/agents official repo
SUPERFORECASTER_SYSTEM = """You are an elite Superforecaster on Polymarket — the world's largest prediction market.
You have a proven track record of calibrated probability estimates. You think in probabilities, not certainties.

Your systematic forecasting process:

1. DECOMPOSE: Break the question into key sub-questions and components.
2. BASE RATES: Identify relevant historical base rates and statistical benchmarks.
3. EVIDENCE: Weigh current evidence — news, trends, expert opinion, data.
4. FACTORS: List factors that push the probability UP vs DOWN.
5. CALIBRATE: Adjust for known biases (anchoring, availability, overconfidence).
6. ESTIMATE: Produce a final probability estimate between 0.01 and 0.99.

CRITICAL RULES:
- Always give a numeric probability. Never say "I don't know" — estimate under uncertainty.
- Your probability must reflect YOUR independent assessment, NOT the current market price.
- Think contrarian: markets are often wrong, especially on complex geopolitical/tech events.
- Be precise: 0.73 is better than "around 70%".
"""

TRADE_DECISION_SYSTEM = """You are the top trader on Polymarket. You identify mispriced markets and exploit edges.

Given a market question, current prices, and a probability estimate from a superforecaster:
1. Compare the forecaster's probability to the current market price
2. Calculate the edge: |forecaster_prob - market_price|
3. Decide: BUY YES (if forecaster thinks YES is underpriced) or BUY NO (if overpriced)
4. Only trade if edge > 10%

Respond ONLY in this exact JSON format (no other text):
{"action": "BUY_YES" or "BUY_NO" or "SKIP", "confidence": 0.0-1.0, "edge_pct": 0.0-100.0, "reasoning": "brief reason"}
"""


class AIClient:
    """Lightweight async OpenAI API client for superforecasting."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gpt-4o-mini"):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.model = model
        self.enabled = bool(self.api_key)
        if not self.enabled:
            logger.info("AIClient: No OPENAI_API_KEY — AI forecasting disabled")
        else:
            logger.info(f"AIClient: initialized with model={model}")

    async def _chat(self, system: str, user: str, temperature: float = 0.3) -> Optional[str]:
        """Send a chat completion request to OpenAI."""
        if not self.enabled:
            return None

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    OPENAI_API_URL,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "temperature": temperature,
                        "max_tokens": 800,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return content.strip()
        except httpx.HTTPStatusError as e:
            logger.error(f"OpenAI API error {e.response.status_code}: {e.response.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"OpenAI API call failed: {e}")
            return None

    async def get_probability(
        self,
        question: str,
        description: str = "",
        current_yes_price: float = 0.0,
        current_no_price: float = 0.0,
        hours_until_resolution: Optional[float] = None,
    ) -> Optional[float]:
        """
        Ask the superforecaster to estimate the probability of YES for a market.
        Returns a float between 0.01 and 0.99, or None on failure.
        """
        time_context = ""
        if hours_until_resolution:
            days = hours_until_resolution / 24
            if days < 1:
                time_context = f"\nThis market resolves in {hours_until_resolution:.0f} hours."
            else:
                time_context = f"\nThis market resolves in {days:.1f} days."

        user_prompt = f"""Prediction Market Question: "{question}"

Description: {description or 'No additional description available.'}
{time_context}

Current market prices (for context only — form YOUR OWN independent estimate):
- YES token: ${current_yes_price:.3f}
- NO token: ${current_no_price:.3f}

What is the probability that the answer is YES?

Think step by step, then on the LAST line write EXACTLY:
PROBABILITY: 0.XX
"""

        response = await self._chat(SUPERFORECASTER_SYSTEM, user_prompt, temperature=0.3)
        if not response:
            return None

        # Parse probability from response
        prob = self._extract_probability(response)
        if prob is not None:
            logger.info(f"AI forecast: '{question[:60]}' → P(YES)={prob:.2f}")
        else:
            logger.warning(f"AI forecast parse failed for: '{question[:60]}'")
            logger.debug(f"Raw response: {response[:300]}")

        return prob

    async def get_trade_decision(
        self,
        question: str,
        yes_price: float,
        no_price: float,
        forecaster_probability: float,
    ) -> Optional[Dict]:
        """
        Given a forecaster probability and market prices, decide whether to trade.
        Returns dict with action, confidence, edge_pct, reasoning — or None.
        """
        user_prompt = f"""Market: "{question}"
Current YES price: ${yes_price:.3f}
Current NO price: ${no_price:.3f}
Superforecaster's P(YES): {forecaster_probability:.2f}

Edge on YES side: {(forecaster_probability - yes_price)*100:.1f}%
Edge on NO side: {((1-forecaster_probability) - no_price)*100:.1f}%

Should we trade? Respond in JSON only."""

        response = await self._chat(TRADE_DECISION_SYSTEM, user_prompt, temperature=0.1)
        if not response:
            return None

        return self._parse_trade_decision(response)

    def _extract_probability(self, text: str) -> Optional[float]:
        """Extract probability from LLM response."""
        # Look for "PROBABILITY: 0.XX" pattern
        patterns = [
            r'PROBABILITY:\s*(0\.\d+)',
            r'PROBABILITY:\s*(\d+(?:\.\d+)?)\s*%',
            r'probability[:\s]+(?:is\s+)?(?:approximately\s+)?(0\.\d+)',
            r'(\d+(?:\.\d+)?)\s*%\s*(?:probability|likelihood|chance)',
            r'P\(YES\)\s*[=:≈]\s*(0\.\d+)',
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                val = float(match.group(1))
                # Convert percentage to decimal if needed
                if val > 1.0:
                    val = val / 100.0
                # Clamp to valid range
                return max(0.01, min(0.99, val))

        # Fallback: look for any standalone decimal between 0 and 1 on the last few lines
        last_lines = text.strip().split('\n')[-5:]
        for line in reversed(last_lines):
            decimals = re.findall(r'\b(0\.\d{1,3})\b', line)
            if decimals:
                val = float(decimals[-1])
                if 0.01 <= val <= 0.99:
                    return val

        return None

    def _parse_trade_decision(self, text: str) -> Optional[Dict]:
        """Parse trade decision JSON from LLM response."""
        try:
            # Try to extract JSON from the response
            # Handle markdown code blocks
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(1))
            else:
                # Try parsing the whole thing as JSON
                # Find first { and last }
                start = text.find('{')
                end = text.rfind('}')
                if start >= 0 and end > start:
                    data = json.loads(text[start:end+1])
                else:
                    return None

            # Validate required fields
            action = data.get("action", "SKIP").upper()
            if action not in ("BUY_YES", "BUY_NO", "SKIP"):
                action = "SKIP"

            return {
                "action": action,
                "confidence": float(data.get("confidence", 0.5)),
                "edge_pct": float(data.get("edge_pct", 0)),
                "reasoning": str(data.get("reasoning", ""))[:200],
            }
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            logger.debug(f"Trade decision parse failed: {e}, text: {text[:200]}")
            return None
