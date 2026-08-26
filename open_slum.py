"""Dynamic mirror discovery from open-slum.org.

Fetches the current mirror list for Anna's Archive and LibGen from the
Shadow Library Uptime Monitor. Results are cached in-memory for
``CACHE_TTL_SECONDS`` to avoid hammering the status site on every request.

Only mirrors with status ``PROTECTED`` or ``UP`` and HTTP 200 are returned.
``PROTECTED`` mirrors (behind DDoS-Guard/Cloudflare) are preferred — they
tend to serve real content more reliably than bare ``UP`` mirrors which
sometimes return nginx stubs or parked pages.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 300  # 5 minutes

_SLUM_PAGES = {
    "annas": "https://open-slum.org/annas.html",
    "libgen": "https://open-slum.org/libgen.html",
}

# Regex to extract mirror entries from open-slum's HTML.
# Each mirror block has: <a href="https://domain..."> ... data-status="..." data-http-status="..." ...
_MIRROR_RE = re.compile(
    r'href="(https://[^"]+)"[^>]*>.*?data-status="([^"]+)".*?data-http-status="(\d+)"',
    re.DOTALL,
)


@dataclass
class _Cache:
    mirrors: dict[str, list[str]] = field(default_factory=dict)
    fetched_at: float = 0.0


_cache = _Cache()


def _parse_mirrors(html: str) -> list[str]:
    """Extract unique mirror base URLs with PROTECTED or UP status and HTTP 200."""
    seen: set[str] = set()
    result: list[str] = []
    for match in _MIRROR_RE.finditer(html):
        url, status, http_code = match.groups()
        domain = url.split("//")[1].split("/")[0]
        if domain in seen:
            continue
        seen.add(domain)
        if http_code != "200":
            continue
        if status not in ("PROTECTED", "UP"):
            continue
        # Prefer PROTECTED mirrors (behind DDG/CF) — put them first
        base = f"https://{domain}"
        if status == "PROTECTED":
            result.insert(0, base)
        else:
            result.append(base)
    return result


async def get_mirrors(library: str) -> list[str]:
    """Return cached mirror list for *library* (``"annas"`` or ``"libgen"``).

    Fetches from open-slum.org if the cache is stale.  Returns an empty
    list on failure (callers should fall back to their hardcoded defaults).
    """
    now = time.monotonic()
    if _cache.mirrors.get(library) and (now - _cache.fetched_at) < _CACHE_TTL_SECONDS:
        return _cache.mirrors[library]

    page_url = _SLUM_PAGES.get(library)
    if not page_url:
        return []

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "Mozilla/5.0"},
        ) as session, session.get(page_url) as response:
            if response.status != 200:
                logger.warning(f"open-slum.org returned HTTP {response.status} for {library}")
                return _cache.mirrors.get(library, [])
            html = await response.text()
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        logger.warning(f"open-slum.org fetch failed for {library}: {exc.__class__.__name__}")
        return _cache.mirrors.get(library, [])

    mirrors = _parse_mirrors(html)
    if mirrors:
        _cache.mirrors[library] = mirrors
        _cache.fetched_at = now
        logger.info(f"open-slum.org {library} mirrors: {mirrors}")
    else:
        logger.warning(f"open-slum.org returned no usable mirrors for {library}")

    return mirrors
