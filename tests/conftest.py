"""Shared pytest configuration."""

from __future__ import annotations

from loguru import logger


def _discard_loguru_message(message: str) -> None:
    """Drop Loguru output during tests.

    Background orchestrator threads can continue logging briefly during pytest
    teardown, after stderr has already been closed. Using a null sink keeps
    those late log writes from surfacing as ``ValueError: I/O operation on
    closed file`` noise in otherwise passing test runs.
    """


logger.remove()
logger.add(_discard_loguru_message, level="INFO", catch=False)
