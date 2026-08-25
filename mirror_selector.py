"""Anna's Archive mirror selection with continuous health scoring.

Replaces the boot-time "pick one mirror and ride it" bootstrap. AA page
fetches go through :func:`fetch_aa_html`, which serves the best-scoring
mirror and demotes mirrors that fail. URL-building paths that cannot use
the helper (Selenium search) call :func:`current_annas_archive_url`.
Scores persist to a small JSON state file so restarts keep their knowledge.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import aiohttp

from config import settings
from exceptions import DownloadError

logger = logging.getLogger(__name__)

# A mirror earns credit for every success and decays credit on failure.
# Demotion (cooldown) triggers after consecutive failures reach the
# threshold; cooldown expires so a recovered mirror rejoins rotation.
_MIRROR_STATE_FILE = Path("/data/mirror_state.json")
_MIRROR_MAX_FAILURES = 2
_MIRROR_COOLDOWN_SECONDS = 15 * 60
_MIRROR_FETCH_TIMEOUT = aiohttp.ClientTimeout(total=20)
_MIRROR_MAX_ATTEMPTS = 3


class AnnasArchiveUnreachableError(DownloadError):
    """Every configured mirror failed for this request."""


_mirror_state: dict[str, dict[str, float]] = {}
_state_loaded = False


def _load_state() -> None:
    global _state_loaded
    if _state_loaded:
        return
    _state_loaded = True
    try:
        raw = json.loads(_MIRROR_STATE_FILE.read_text())
        if isinstance(raw, dict):
            for url, entry in raw.items():
                if (
                    url in settings.annas_archive_mirrors
                    and isinstance(entry, dict)
                    and {"successes", "failures", "cooldown_until"} <= set(entry)
                ):
                    _mirror_state[url] = {
                        "successes": float(entry["successes"]),
                        "failures": float(entry["failures"]),
                        "cooldown_until": float(entry["cooldown_until"]),
                    }
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        logger.warning(f"AA mirror state load failed: {exc.__class__.__name__}")


def _save_state() -> None:
    try:
        _MIRROR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MIRROR_STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_mirror_state))
        tmp.replace(_MIRROR_STATE_FILE)
    except OSError as exc:
        logger.warning(f"AA mirror state save failed: {exc.__class__.__name__}")


def _entry(url: str) -> dict[str, float]:
    return _mirror_state.setdefault(url, {"successes": 0.0, "failures": 0.0, "cooldown_until": 0.0})


def record_mirror_success(url: str) -> None:
    _entry(url)
    entry = _mirror_state[url]
    entry["successes"] += 1.0
    entry["failures"] = 0.0
    entry["cooldown_until"] = 0.0
    _save_state()


def record_mirror_failure(url: str) -> None:
    """Decay credit on every failure; open a cooldown after repeated ones."""
    entry = _entry(url)
    entry["successes"] = max(0.0, entry["successes"] - 1.0)
    entry["failures"] += 1.0
    if entry["failures"] >= _MIRROR_MAX_FAILURES:
        entry["cooldown_until"] = time.monotonic() + _MIRROR_COOLDOWN_SECONDS
        logger.warning(
            f"AA mirror decision mirror={url} outcome=cooldown failures={int(entry['failures'])}"
        )
    _save_state()


def reset_mirror_state_for_tests() -> None:
    """Test isolation hook: clear in-memory scores without touching disk."""
    _mirror_state.clear()
    globals()["_state_loaded"] = True


def _eligible(now: float | None = None) -> list[str]:
    now = now if now is not None else time.monotonic()
    eligible = [
        url
        for url in settings.annas_archive_mirrors
        if _mirror_state.get(url, {}).get("cooldown_until", 0.0) <= now
    ]
    # Total-outage behaviour: serve the full list ordered by score rather
    # than refusing before any attempt was made.
    pool = eligible or list(settings.annas_archive_mirrors)
    return sorted(pool, key=lambda url: (-_entry(url)["successes"], _entry(url)["failures"], url))


def current_annas_archive_url() -> str:
    """Best available mirror right now (no network I/O)."""
    _load_state()
    return _eligible()[0]


async def fetch_aa_html(path: str, *, timeout: aiohttp.ClientTimeout | None = None) -> str:
    """Fetch an AA page from the best mirror, demoting mirrors on failure.

    Tries up to ``_MIRROR_MAX_ATTEMPTS`` distinct mirrors. Returns the page
    HTML. Raises :class:`AnnasArchiveUnreachableError` when every attempt
    fails — callers translate that into their own error taxonomy.
    """
    _load_state()
    last_error: Exception | None = None
    tried: set[str] = set()

    for mirror in _eligible():
        if len(tried) >= _MIRROR_MAX_ATTEMPTS:
            break
        tried.add(mirror)
        url = f"{mirror}{path}"
        try:
            async with (
                aiohttp.ClientSession(
                    headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout or _MIRROR_FETCH_TIMEOUT
                ) as session,
                session.get(url, allow_redirects=True) as response,
            ):
                if response.status >= 400:
                    raise aiohttp.ClientResponseError(
                        response.request_info,
                        response.history,
                        status=response.status,
                        message=f"http_{response.status}",
                    )
                html = await response.text()
            record_mirror_success(mirror)
            logger.info(f"AA mirror decision mirror={mirror} path={path} outcome=ok bytes={len(html)}")
            return html
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            last_error = exc
            record_mirror_failure(mirror)
            logger.warning(
                f"AA mirror decision mirror={mirror} path={path} outcome=fail type={exc.__class__.__name__}"
            )
    raise AnnasArchiveUnreachableError(f"All Anna's Archive mirrors failed for {path}") from last_error
