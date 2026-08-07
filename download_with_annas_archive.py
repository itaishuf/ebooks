import logging
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse

import aiohttp
from bs4 import BeautifulSoup

from config import settings
from exceptions import DownloadError
from utils import log_call

logger = logging.getLogger(__name__)

# Matches ISBN-13 (978/979 prefix) and ISBN-10 (9 digits + digit or X).
_ISBN_RE = re.compile(r'\b(97[89]\d{10}|\d{9}[\dX])\b')

# AA's countdown timer is ~60 s; 70 gives a buffer for slow page renders.
AA_COUNTDOWN_WAIT_S = 70
# Total FlareSolverr budget: DDoS-Guard JS challenge (~10 s) + countdown wait + network.
FLARESOLVERR_TIMEOUT_MS = 120_000
_PARTNER_HEALTH: dict[str, dict[str, float | int]] = {}
# Bounds in-memory partner bookkeeping. Partners change rarely, so this is only
# hit after many distinct partner paths have been seen.
_PARTNER_HEALTH_MAX_ENTRIES = 64
# Upper bound for a single partner response so a broken/abusive partner cannot
# exhaust process memory by streaming an unbounded body.
_MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


class AnnaPartnerError(DownloadError):
    """A safe partner failure code, intentionally without URLs or cookie values."""

    def __init__(self, outcome_code: str):
        super().__init__(outcome_code)
        self.outcome_code = outcome_code


def _page_isbns(html: str) -> list[str]:
    """Return all ISBN-10 / ISBN-13 strings found in an AA MD5 page."""
    return _ISBN_RE.findall(html)


def _extract_filename(content_disposition: str, url: str, md5: str) -> str:
    """Derive a filename from Content-Disposition, the URL, or the MD5 hash."""
    if "filename=" in content_disposition:
        part = content_disposition.split("filename=")[-1].strip().strip('"').strip("'")
        if part:
            return part
    url_path = url.split("?")[0].rstrip("/").split("/")[-1]
    return url_path if "." in url_path else f"{md5}.epub"


async def _fetch_md5_page(md5: str) -> str:
    """Fetch the AA MD5 detail page HTML (single shared fetch for all callers)."""
    md5_url = f"{settings.annas_archive_url}/md5/{md5}"
    logger.info(f"Fetching AA MD5 page for md5={md5}")
    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        async with session.get(md5_url, allow_redirects=True) as resp:
            logger.info(f"Anna MD5 decision response_status={resp.status}")
            return await resp.text()


async def _try_internet_archive(md5: str, html: str) -> Path | None:
    """Try downloading the book directly from Internet Archive.

    Parses the pre-fetched AA MD5 page HTML for an archive.org/details link,
    then downloads from archive.org/download/{item}/{item}.{ext}.
    Returns the saved Path on success, None if no IA source is found.
    Raises DownloadError only if an IA source is found but every download attempt fails.
    """
    soup = BeautifulSoup(html, "html.parser")
    ia_link = soup.find("a", href=re.compile(r"https://archive\.org/details/([^/?#]+)"))
    if not ia_link:
        logger.info(f"Anna MD5 decision internet_archive=absent md5={md5}")
        return None

    item_id = re.search(r"https://archive\.org/details/([^/?#]+)", ia_link["href"]).group(1)
    logger.info(f"Anna MD5 decision internet_archive=present md5={md5} next=epub_then_pdf")

    output_dir = Path(settings.download_dir) / f"aa-{md5[:8]}"
    output_dir.mkdir(parents=True, exist_ok=True)

    for ext in ("epub", "pdf"):
        ia_url = f"https://archive.org/download/{item_id}/{item_id}.{ext}"
        logger.info(f"Trying IA download: {ia_url}")
        try:
            async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
                async with session.get(
                    ia_url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=300)
                ) as resp:
                    if resp.status == 200:
                        file_path = output_dir / f"{item_id}.{ext}"
                        file_path.write_bytes(await resp.read())
                        size_kb = round(file_path.stat().st_size / 1000, 1)
                        logger.info(f"IA download complete: {file_path.name} ({size_kb} KB)")
                        return file_path
                    logger.info(f"Anna MD5 decision internet_archive format={ext} response_status={resp.status}")
        except Exception as e:
            logger.warning(f"IA download failed for {ia_url}: {e}")

    raise DownloadError(f"Internet Archive item {item_id} found but all format attempts failed")


def _get_slow_download_urls(md5: str, html: str) -> list[str]:
    """Extract unique slow-partner URLs without logging their signed parameters."""
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for link in soup.find_all("a", href=re.compile(r"/slow_download/")):
        href = link["href"]
        url = href if href.startswith("http") else f"{settings.annas_archive_url}{href}"
        if url not in urls:
            urls.append(url)
    if not urls:
        logger.warning(f"Anna partner decision md5={md5} outcome=no_partner_links")
        raise AnnaPartnerError("no_partner_links")
    logger.info(f"Anna partner decision md5={md5} available={len(urls)}")
    return urls


def _partner_key(slow_url: str) -> str:
    return urlparse(slow_url).path


def _rank_slow_partner_urls(urls: list[str], now: float | None = None) -> list[str]:
    now = now if now is not None else time.monotonic()

    def health(url: str) -> dict[str, float | int]:
        return _PARTNER_HEALTH.get(_partner_key(url), {})

    eligible = [url for url in urls if float(health(url).get("cooldown_until", 0.0)) <= now]
    return sorted(
        eligible,
        key=lambda url: (-int(health(url).get("successes", 0)), int(health(url).get("failures", 0))),
    )[: settings.anna_partner_attempt_limit]


def _prune_partner_health() -> None:
    """Evict stale partner entries so in-memory bookkeeping stays bounded.

    Called from the synchronous bookkeeping helpers; the event loop cannot
    preempt between calls, so no lock is needed.
    """
    if len(_PARTNER_HEALTH) < _PARTNER_HEALTH_MAX_ENTRIES:
        return
    now = time.monotonic()
    for key in list(_PARTNER_HEALTH):
        if float(_PARTNER_HEALTH[key].get("cooldown_until", 0.0)) <= now:
            _PARTNER_HEALTH.pop(key, None)
    while len(_PARTNER_HEALTH) >= _PARTNER_HEALTH_MAX_ENTRIES:
        _PARTNER_HEALTH.pop(next(iter(_PARTNER_HEALTH)), None)


def _record_partner_outcome(slow_url: str, *, success: bool) -> None:
    _prune_partner_health()
    state = _PARTNER_HEALTH.setdefault(_partner_key(slow_url), {"successes": 0, "failures": 0, "cooldown_until": 0.0})
    if success:
        state["successes"] = int(state["successes"]) + 1
        state["failures"] = 0
        state["cooldown_until"] = 0.0
        return
    state["failures"] = int(state["failures"]) + 1
    if int(state["failures"]) >= settings.anna_partner_failure_threshold:
        state["cooldown_until"] = time.monotonic() + settings.anna_partner_failure_cooldown_seconds


async def _solve_and_get_download_link(md5: str, slow_url: str) -> tuple[dict, str, str]:
    """Use FlareSolverr to bypass the DDoS-Guard JS challenge on the AA slow-download
    page and extract the actual download URL from the rendered HTML.

    Returns (all_cookies, user_agent, absolute_download_url).
    """
    logger.info(f"Anna partner decision md5={md5} stage=flaresolverr action=attempt")

    try:
        timeout = aiohttp.ClientTimeout(total=(FLARESOLVERR_TIMEOUT_MS // 1000) + 15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{settings.flaresolverr_url}/v1",
                json={
                    "cmd": "request.get",
                    "url": slow_url,
                    "maxTimeout": FLARESOLVERR_TIMEOUT_MS,
                    "waitInSeconds": AA_COUNTDOWN_WAIT_S,
                },
            ) as resp:
                if resp.status >= 500:
                    raise AnnaPartnerError("flaresolverr_unavailable")
                data = await resp.json()
    except TimeoutError as exc:
        raise AnnaPartnerError("flaresolverr_timeout") from exc
    except (aiohttp.ClientError, ValueError) as exc:
        raise AnnaPartnerError("flaresolverr_transport_failure") from exc

    status = data.get("status")
    if status != "ok":
        logger.warning(f"Anna partner decision md5={md5} stage=flaresolverr outcome=not_rendered")
        raise AnnaPartnerError("flaresolverr_not_rendered")

    solution = data["solution"]
    all_cookies = {c["name"]: c["value"] for c in solution.get("cookies", [])}
    user_agent = solution["userAgent"]

    html = solution["response"]
    soup = BeautifulSoup(html, "html.parser")

    btn = soup.find(id="download-button")
    if not btn or not btn.get("href"):
        # Fall back to any anchor whose href looks like a direct download path.
        btn = next(
            (
                a
                for a in soup.find_all("a", href=True)
                if "/dl/" in a["href"] or a["href"].endswith((".epub", ".pdf", ".mobi", ".azw3"))
            ),
            None,
        )

    if not btn or not btn.get("href"):
        raise AnnaPartnerError("no_rendered_link")

    href = btn["href"]
    download_url = href if href.startswith("http") else f"{settings.annas_archive_url}{href}"
    logger.info(f"Anna partner decision md5={md5} stage=flaresolverr outcome=success next=proxy")
    return all_cookies, user_agent, download_url


async def _download_via_slow_partners(md5: str, slow_urls: list[str], on_status=None) -> Path:
    from download_flow import _emit_status

    candidates = _rank_slow_partner_urls(slow_urls)
    if not candidates:
        logger.warning(f"Anna partner decision md5={md5} outcome=all_partners_in_cooldown")
        raise AnnaPartnerError("all_partners_in_cooldown")

    output_dir = Path(settings.download_dir) / f"aa-{md5[:8]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    last_error: AnnaPartnerError | None = None
    for index, slow_url in enumerate(candidates, start=1):
        try:
            _emit_status(on_status, "downloading", source="annas_archive", attempt=index)
            logger.info(f"Anna partner decision md5={md5} partner_attempt={index}/{len(candidates)}")
            all_cookies, user_agent, download_url = await _solve_and_get_download_link(md5, slow_url)
            proxy_url = f"{settings.download_proxy_url}/download?" + urlencode(
                {"url": download_url, "referer": f"{settings.annas_archive_url}/"}
            )
            cookie_str = "; ".join(f"{key}={value}" for key, value in all_cookies.items())
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    proxy_url,
                    headers={"User-Agent": user_agent, "X-Cookies": cookie_str},
                    timeout=aiohttp.ClientTimeout(total=360),
                ) as resp:
                    if resp.status != 200:
                        raise AnnaPartnerError("proxy_transport_failure")
                    content_length = resp.headers.get("Content-Length")
                    if content_length and content_length.isdigit() and int(content_length) > _MAX_DOWNLOAD_BYTES:
                        raise AnnaPartnerError("file_too_large")
                    content = await resp.read()
                    if not content or len(content) > _MAX_DOWNLOAD_BYTES:
                        raise AnnaPartnerError("file_validation_failed")
                    filename = Path(
                        _extract_filename(resp.headers.get("Content-Disposition", ""), download_url, md5)
                    ).name
                    if not filename:
                        raise AnnaPartnerError("file_validation_failed")
                    file_path = output_dir / filename
                    file_path.write_bytes(content)
            _record_partner_outcome(slow_url, success=True)
            logger.info(f"Anna partner decision md5={md5} partner_attempt={index} outcome=file_valid")
            return file_path
        except AnnaPartnerError as exc:
            last_error = exc
            _record_partner_outcome(slow_url, success=False)
            logger.warning(f"Anna partner decision md5={md5} partner_attempt={index} outcome={exc.outcome_code}")
        except (aiohttp.ClientError, TimeoutError, OSError):
            last_error = AnnaPartnerError("proxy_transport_failure")
            _record_partner_outcome(slow_url, success=False)
            logger.warning(f"Anna partner decision md5={md5} partner_attempt={index} outcome=proxy_transport_failure")
    raise last_error or AnnaPartnerError("all_partner_attempts_failed")


@log_call
async def download_book_from_annas_archive(md5: str, isbn: str = "", on_status=None) -> Path:
    """Download an ebook from Anna's Archive.

    Strategy:
    1. Fetch the AA MD5 page once to extract both the IA link (if any) and the
       slow_download URL.
    2. If *isbn* is provided, verify it appears on the MD5 page before proceeding
       (skips wrong-book results that slipped through the title search).
    3. Try Internet Archive directly (fast, no bot protection) if an IA source is
       linked from the page.
    4. Fall back to the FlareSolverr slow-download path + download-proxy sidecar
       for books not on IA.
    """
    html = await _fetch_md5_page(md5)

    if isbn:
        page_isbns = _page_isbns(html)
        if page_isbns and not any(isbn in p or p in isbn for p in page_isbns):
            logger.warning(
                f"AA MD5 page for {md5} has ISBNs {page_isbns} — none match {isbn}, skipping"
            )
            raise DownloadError(f"ISBN mismatch on AA MD5 page for {md5}")
        logger.info(
            f"Anna MD5 decision isbn_validation={'matched' if page_isbns else 'unavailable'} md5={md5}"
        )

    try:
        ia_path = await _try_internet_archive(md5, html)
    except DownloadError:
        ia_path = None
        logger.warning(f"Anna MD5 decision source=internet_archive outcome=failed md5={md5} next=slow_partner")
    if ia_path:
        logger.info(f"Anna MD5 decision source=internet_archive outcome=success md5={md5}")
        return ia_path

    logger.info(f"Anna MD5 decision source=slow_partner reason=internet_archive_unavailable md5={md5}")
    file_path = await _download_via_slow_partners(md5, _get_slow_download_urls(md5, html), on_status=on_status)
    size_kb = round(file_path.stat().st_size / 1000, 1)
    logger.info(f"Anna MD5 decision source=proxy outcome=success size_kb={size_kb}")
    return file_path
