"""
PolyBot Configuration
Reads from environment variables (GitHub Actions secrets) or .env file.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass  # python-dotenv not required when env vars are set directly (GH Actions)


@dataclass
class Settings:
    # ─── Environment Detection ─────────────────────────────────────
    GH_ACTIONS: bool = field(default_factory=lambda: os.getenv("GITHUB_ACTIONS", "").lower() == "true")

    # ─── Wallet / Auth ─────────────────────────────────────────────
    PRIVATE_KEY: str = field(default_factory=lambda: os.getenv("PRIVATE_KEY", ""))
    FUNDER_ADDRESS: str = field(default_factory=lambda: os.getenv("FUNDER_ADDRESS", ""))
    CHAIN_ID: int = 137  # Polygon mainnet

    # ─── Polymarket API ─────────────────────────────────────────────
    CLOB_HOST: str = "https://clob.polymarket.com"
    GAMMA_HOST: str = "https://gamma-api.polymarket.com"

    # ─── Kalshi API (cross-platform arb) ────────────────────────────
    KALSHI_API_KEY: str = field(default_factory=lambda: os.getenv("KALSHI_API_KEY", ""))
    KALSHI_API_SECRET: str = field(default_factory=lambda: os.getenv("KALSHI_API_SECRET", ""))
    KALSHI_BASE_URL: str = "https://api.elections.kalshi.com/trade-api/v2"

    # ─── Weather Data ───────────────────────────────────────────────
    NOAA_API_TOKEN: str = field(default_factory=lambda: os.getenv("NOAA_API_TOKEN", ""))
    OPENMETEO_BASE_URL: str = "https://api.open-meteo.com/v1/forecast"

    # ─── Telegram Alerts ────────────────────────────────────────────
    TELEGRAM_BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    TELEGRAM_CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    TELEGRAM_REPORT_ENABLED: bool = True

    # ─── Capital & Risk ─────────────────────────────────────────────
    INITIAL_CAPITAL: float = field(default_factory=lambda: float(os.getenv("INITIAL_CAPITAL", "100")))
    MAX_POSITION_PCT: float = 0.08            # 8% per trade (was 5%)
    MAX_GLOBAL_EXPOSURE_PCT: float = 0.70     # 70% deployed (was 40%)
    DAILY_LOSS_LIMIT_PCT: float = 0.15        # 15% daily loss limit (was 10%)
    MAX_SIMULTANEOUS_POSITIONS: int = 20      # Up to 20 positions (was 10)
    KELLY_FRACTION: float = 0.30              # 30% Kelly (was 25%)

    # ─── Strategy Toggles ───────────────────────────────────────────
    ENABLE_WEATHER_ARB: bool = True
    ENABLE_MARKET_MAKER: bool = False  # Disabled - requires continuous quoting
    ENABLE_CROSS_PLATFORM_ARB: bool = True
    ENABLE_GENERAL_SCANNER: bool = True
    ENABLE_MOMENTUM_SCALPER: bool = True
    ENABLE_SPREAD_CAPTURE: bool = True
    DRY_RUN: bool = field(default_factory=lambda: os.getenv("DRY_RUN", "true").lower() == "true")

    # ─── Weather Strategy Config ─────────────────────────────────────
    WEATHER_CITIES: list = field(default_factory=lambda: [
        {"name": "New York", "lat": 40.7128, "lon": -74.0060, "station": "KNYC"},
        {"name": "London", "lat": 51.5074, "lon": -0.1278, "station": "EGLC"},
        {"name": "Chicago", "lat": 41.8781, "lon": -87.6298, "station": "KORD"},
        {"name": "Seoul", "lat": 37.5665, "lon": 126.9780, "station": "RKSS"},
        {"name": "Sydney", "lat": -33.8688, "lon": 151.2093, "station": "YSSY"},
        {"name": "Dallas", "lat": 32.7767, "lon": -96.7970, "station": "KDFW"},
    ])
    WEATHER_MIN_EDGE: float = 0.15
    WEATHER_MIN_LIQUIDITY: float = 500.0
    WEATHER_MAX_HOURS_OUT: int = 336  # 14 days max
    WEATHER_MIN_HOURS_OUT: int = 12  # 12 hours minimum
    WEATHER_SCAN_INTERVAL: int = 300
    WEATHER_MAX_BET_USD: float = 5.0

    # ─── Market Maker Config ─────────────────────────────────────────
    MM_MIN_SPREAD: float = 0.03
    MM_MAX_SPREAD: float = 0.10
    MM_ORDER_SIZE_USD: float = 10.0
    MM_UPDATE_INTERVAL: int = 30
    MM_MIN_VOLUME_24H: float = 1000.0
    MM_MAX_POSITION_SIZE: float = 50.0
    MM_INVENTORY_SKEW: float = 0.3

    # ─── Cross-Platform Arb Config ───────────────────────────────────
    ARB_MIN_EDGE_PCT: float = 0.015
    ARB_SCAN_INTERVAL: int = 10
    ARB_MAX_POSITION_USD: float = 20.0
    ARB_POLY_FEE: float = 0.001
    ARB_KALSHI_FEE: float = 0.007
    ARB_MIN_HOURS_TO_RESOLUTION: int = 24  # 24 hours minimum

    # ─── Portfolio / Reporting ──────────────────────────────────────
    REPORT_INTERVAL_SECONDS: int = 3600
    DATA_DIR: Path = field(default_factory=lambda: Path("data"))
    LOG_DIR: Path = field(default_factory=lambda: Path("logs"))
    DB_PATH: str = "data/polybot.db"

    def __post_init__(self):
        self.DATA_DIR.mkdir(exist_ok=True)
        self.LOG_DIR.mkdir(exist_ok=True)

        # Auto-disable market maker in GH Actions (needs continuous quoting)
        if self.GH_ACTIONS:
            self.ENABLE_MARKET_MAKER = False

        if not self.DRY_RUN and not self.PRIVATE_KEY:
            raise ValueError("PRIVATE_KEY is required for live trading. Set DRY_RUN=true for paper trading.")
