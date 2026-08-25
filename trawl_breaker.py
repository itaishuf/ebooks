"""Circuit breaker for the trawl dependency (AA slow-partner downloads).

When trawl returns 5xx, times out, or reports pool exhaustion, the breaker
opens: subsequent slow-partner attempts fail fast (routing jobs to LibGen /
Internet Archive paths that do not need a browser). A background probe hits
trawl's ``/health`` and closes the breaker on recovery. All state is
in-process by design.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp

from config import settings

logger = logging.getLogger(__name__)

_FAILURE_THRESHOLD = 2
_OPEN_SECONDS = 5 * 60
_PROBE_INTERVAL_SECONDS = 60

_state = {"consecutive_failures": 0, "open_until": 0.0}
_probe_task: asyncio.Task | None = None


def is_trawl_down(now: float | None = None) -> bool:
    now = now if now is not None else time.monotonic()
    return now < _state["open_until"]


def record_trawl_success() -> None:
    if _state["consecutive_failures"] or _state["open_until"]:
        logger.info("Trawl breaker outcome=closed reason=success")
    _state["consecutive_failures"] = 0
    _state["open_until"] = 0.0


def record_trawl_failure(outcome_code: str) -> None:
    """Count a trawl failure; open the breaker past the threshold."""
    if is_trawl_down():
        return
    _state["consecutive_failures"] += 1
    if _state["consecutive_failures"] >= _FAILURE_THRESHOLD:
        _state["open_until"] = time.monotonic() + _OPEN_SECONDS
        logger.warning(
            f"Trawl breaker outcome=open reason={outcome_code} "
            f"open_seconds={_OPEN_SECONDS} downloads=degraded_to_no_browser_paths"
        )


def reset_for_tests() -> None:
    _state["consecutive_failures"] = 0
    _state["open_until"] = 0.0


async def _probe_once(session: aiohttp.ClientSession) -> bool:
    try:
        async with session.get(
            f"{settings.trawl_url}/health", timeout=aiohttp.ClientTimeout(total=10)
        ) as response:
            await response.read()
            return response.status < 500
    except (aiohttp.ClientError, TimeoutError, OSError):
        return False


async def _probe_loop() -> None:
    """While open, poll /health; close the breaker on first healthy reply.

    Closing mid-cooldown lets traffic return early; if trawl is still sick,
    the next two failures re-open it. Loop exits when the breaker is closed.
    """
    while is_trawl_down():
        await asyncio.sleep(_PROBE_INTERVAL_SECONDS)
        healthy = await _probe_once(aiohttp.ClientSession())
        if healthy:
            record_trawl_success()
            logger.info("Trawl breaker outcome=closed reason=health_probe_ok")
            return


def ensure_probe_running() -> None:
    """Start the health-probe loop when the breaker opens (idempotent)."""
    global _probe_task
    if not is_trawl_down() or (_probe_task and not _probe_task.done()):
        return
    _probe_task = asyncio.create_task(_probe_loop())


async def probe_now() -> bool:
    """One-shot health check (also used by tests and ops debugging)."""
    async with aiohttp.ClientSession() as session:
        return await _probe_once(session)
