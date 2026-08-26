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
from urllib.parse import parse_qs, urlsplit

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


# DDoS-Guard serves a tiny JS-challenge page with HTTP 200; hijacked/parked
# mirrors (.gs, 2026-08-26) serve a similar tiny "Antibot solution" click
# shell redirecting to adware. All count as failures — they were poisoning
# mirror scores as successes. Belt-and-braces: every genuinely useful AA
# page (search results, md5 record) is tens of KB, so anything smaller than
# _MIN_USEFUL_HTML_BYTES is a challenge/parked shell by definition.
_CHALLENGE_MARKERS = (
    "ddos-guard",
    "checking your browser",
    "js-challenge",
    "forsale.min.js",  # parked domain
    "antibot solution",
    "click for continue",
    "loading...</title>",
    "just a moment",  # cloudflare
    "attention required",
    "one more step",
)
_MIN_USEFUL_HTML_BYTES = 5000


def _looks_like_challenge(html: str) -> bool:
    """True when *html* is a challenge/parked/adware shell, not a real page.

    Primary signal is SIZE: real AA search/record pages are tens of KB and
    legitimately embed 'ddos-guard' client-script references (21-48x on
    genuine pages), so marker matching must only apply to small responses
    where a marker distinguishes the shell kind.
    """
    if len(html) >= _MIN_USEFUL_HTML_BYTES:
        return False
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS) or len(html) > 0


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


def _extract_search_query(path: str) -> str:
    """Return the ``q`` parameter when *path* is an AA /search URL, else ''."""
    parts = urlsplit(path)
    if parts.path != "/search":
        return ""
    return (parse_qs(parts.query).get("q") or [""])[0]


async def _fetch_aa_search_via_trawl(query: str) -> str:
    """Fetch AA search results through trawl's browser-backed /aa/search.

    Trawl runs patchright+Chromium, clears the DDoS-Guard JS challenge on the
    mirror itself, and returns the rendered results HTML. Uses the same
    circuit breaker as the slow_download path.
    """
    from trawl_breaker import ensure_probe_running, is_trawl_down, record_trawl_failure, record_trawl_success

    if is_trawl_down():
        raise AnnasArchiveUnreachableError("trawl_breaker_open")

    # Worst case: trawl rotates through several mirrors × ~30s DDG wait each.
    # 120s timed out mid-rotation on 2026-08-26; give it headroom.
    timeout = aiohttp.ClientTimeout(total=180)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session, session.post(
            f"{settings.trawl_url}/aa/search", json={"query": query}
        ) as response:
            body = await response.json(content_type=None)
            if response.status >= 400:
                code = body.get("error", f"http_{response.status}") if isinstance(body, dict) else "bad_body"
                raise RuntimeError(str(code))
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        record_trawl_failure("transport_failure")
        ensure_probe_running()
        raise AnnasArchiveUnreachableError(f"trawl /aa/search failed: {exc.__class__.__name__}") from exc
    except Exception as exc:
        record_trawl_failure("aa_error")
        ensure_probe_running()
        raise AnnasArchiveUnreachableError(f"trawl /aa/search failed: {exc}") from exc

    html = body.get("html") if isinstance(body, dict) else None
    if not html or len(html) < 5000 or _looks_like_challenge(html):
        record_trawl_failure("empty_or_challenged")
        ensure_probe_running()
        raise AnnasArchiveUnreachableError("trawl /aa/search returned no usable results HTML")

    record_trawl_success()
    logger.info(f"AA mirror decision mirror={body.get('mirror')} path=/search?q={query!r} outcome=ok_via_trawl bytes={len(html)}")
    return html


async def fetch_aa_html(path: str, *, timeout: aiohttp.ClientTimeout | None = None) -> str:
    """Fetch an AA page from the best mirror, demoting mirrors on failure.

    Tries up to ``_MIRROR_MAX_ATTEMPTS`` distinct mirrors. A DDoS-Guard
    challenge page (HTTP 200 but tiny JS-challenge HTML) counts as a failure,
    never as a success. When every plain-HTTP attempt fails and the request
    is a ``/search`` page, falls back to trawl's browser-backed
    ``POST /aa/search`` — the only path that reliably clears AA protection.
    Raises :class:`AnnasArchiveUnreachableError` when everything fails —
    callers translate that into their own error taxonomy.
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
            if _looks_like_challenge(html):
                # 200 + challenge shell: demote, keep rotating.
                raise aiohttp.ClientResponseError(
                    response.request_info,
                    response.history,
                    status=503,
                    message="ddos_guard_challenge_page",
                )
            record_mirror_success(mirror)
            logger.info(f"AA mirror decision mirror={mirror} path={path} outcome=ok bytes={len(html)}")
            return html
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            last_error = exc
            record_mirror_failure(mirror)
            logger.warning(
                f"AA mirror decision mirror={mirror} path={path} outcome=fail type={exc.__class__.__name__}"
            )

    query = _extract_search_query(path)
    if query:
        logger.warning(f"AA mirror decision path={path} outcome=all_plain_mirrors_failed fallback=trawl_aa_search")
        return await _fetch_aa_search_via_trawl(query)

    raise AnnasArchiveUnreachableError(f"All Anna's Archive mirrors failed for {path}") from last_error
