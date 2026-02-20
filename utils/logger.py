"""
Logging setup for PolyBot.
"""

import logging
import sys
from pathlib import Path


def setup_logger(name: str, log_dir: str = "logs") -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # Also configure the root "polybot" logger so child loggers propagate correctly
    root_polybot = logging.getLogger("polybot")
    root_polybot.setLevel(logging.DEBUG)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File handler
    log_path = Path(log_dir)
    log_path.mkdir(exist_ok=True)
    file_handler = logging.FileHandler(log_path / "polybot.log")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
