import io
import logging
import re
import time
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from config import settings
from exceptions import DownloadError
from mirror_selector import fetch_aa_html
from trawl_breaker import ensure_probe_running, is_trawl_down, record_trawl_failure, record_trawl_success
from utils import log_call

logger = logging.getLogger(__name__)

# Matches ISBN-13 (978/979 prefix) and ISBN-10 (9 digits + digit or X).
_ISBN_RE = re.compile(r'\b(97[89]\d{10}|\d{9}[\dX])\b')

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


_CORRUPT_ISBN = frozenset({"4294967295"})

def _page_isbns(html: str) -> list[str]:
    """Return all valid ISBN-10 / ISBN-13 strings found in an AA MD5 page."""
    return [isbn for isbn in _ISBN_RE.findall(html) if isbn not in _CORRUPT_ISBN]


async def _fetch_md5_page(md5: str) -> str:
    """Fetch the AA MD5 detail page HTML (single shared fetch for all callers).

    Goes through the mirror selector so a dead mirror is skipped and demoted
    automatically instead of poisoning every request until restart.
    """
    logger.info(f"Fetching AA MD5 page for md5={md5}")
    return await fetch_aa_html(f"/md5/{md5}")


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
    )


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


async def _download_via_trawl_browser(md5: str, slow_url: str) -> tuple[bytes, str]:
    """Download a book from an AA slow partner via trawl's /aa/download endpoint.

    Trawl runs patchright + headless Chromium end-to-end: it clears the
    DDoS-Guard browser verification on the /slow_download/ page (the only
    engine verified to do so — Camoufox/Firefox gets re-challenged), waits
    for the partner countdown, then clicks the d3 anchor so the REAL browser
    download carries the session cookies. The d3 URL alone is useless outside
    the browser session (plain fetches fail), so trawl returns the file bytes.

    Returns (content, filename). Filename comes from Chromium's suggested
    download name (Content-Disposition of the d3 CDN response).
    """
    logger.info(f"Anna partner decision md5={md5} stage=trawl_aa action=attempt")
    if is_trawl_down():
        raise AnnaPartnerError("trawl_breaker_open")

    try:
        timeout = aiohttp.ClientTimeout(total=300)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f"{settings.trawl_url}/aa/download",
                json={"url": slow_url},
            ) as resp:
                if resp.status != 200:
                    try:
                        err = await resp.json()
                        code = err.get("error", f"http_{resp.status}")
                    except (aiohttp.ClientError, ValueError):
                        code = f"http_{resp.status}"
                    logger.warning(f"Anna partner decision md5={md5} stage=trawl_aa outcome={code}")
                    raise AnnaPartnerError(f"trawl_aa_{code}")
                content = await resp.read()
                filename = resp.headers.get("X-AA-Filename", "")
    except TimeoutError as exc:
        record_trawl_failure("timeout")
        ensure_probe_running()
        raise AnnaPartnerError("trawl_aa_timeout") from exc
    except (aiohttp.ClientError, ValueError) as exc:
        record_trawl_failure("transport_failure")
        ensure_probe_running()
        raise AnnaPartnerError("trawl_aa_transport_failure") from exc

    record_trawl_success()
    if not content or len(content) > _MAX_DOWNLOAD_BYTES:
        raise AnnaPartnerError("file_validation_failed")
    logger.info(f"Anna partner decision md5={md5} stage=trawl_aa outcome=success bytes={len(content)}")
    return content, filename


_EBOK_EXTS = (
    ".pdf", ".epub", ".mobi", ".azw", ".azw3", ".djvu", ".txt",
    ".cbz", ".cbr", ".doc", ".docx", ".rtf", ".html", ".htm",
)


def _ebook_extension(content: bytes) -> str:
    """Best-effort ebook extension from content magic ('' if unknown).

    zlib3/oceanofpdf d3 URLs carry NO extension in the filename, and
    Kindle email delivery needs a real extension to pick up the
    attachment, so sniff the content when the name is bare.
    """
    if content[:5] == b"%PDF-":
        return ".pdf"
    if content[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                if "mimetype" in z.namelist() and b"epub" in z.read("mimetype").lower():
                    return ".epub"
        except (zipfile.BadZipFile, KeyError, OSError):
            pass
    return ""


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
            _emit_status(on_status, "downloading", source="annas_archive", attempt=index, total=len(candidates))
            logger.info(f"Anna partner decision md5={md5} partner_attempt={index}/{len(candidates)}")
            content, filename = await _download_via_trawl_browser(md5, slow_url)
            from download_flow import _validate_book_file
            _validate_book_file(content, "trawl")
            safe_name = Path(filename).name if filename else md5
            if not safe_name.lower().endswith(_EBOK_EXTS):
                safe_name += _ebook_extension(content)
            file_path = output_dir / safe_name
            file_path.write_bytes(content)
            _record_partner_outcome(slow_url, success=True)
            logger.info(f"Anna partner decision md5={md5} partner_attempt={index} outcome=file_valid bytes={len(content)}")
            return file_path
        except AnnaPartnerError as exc:
            last_error = exc
            _record_partner_outcome(slow_url, success=False)
            logger.warning(f"Anna partner decision md5={md5} partner_attempt={index} outcome={exc.outcome_code}")
        except (aiohttp.ClientError, TimeoutError, OSError):
            last_error = AnnaPartnerError("trawl_aa_transport_failure")
            _record_partner_outcome(slow_url, success=False)
            logger.warning(f"Anna partner decision md5={md5} partner_attempt={index} outcome=trawl_aa_transport_failure")
    raise last_error or AnnaPartnerError("all_partner_attempts_failed")


@log_call
async def download_book_from_annas_archive(md5: str, isbns: set[str] | None = None, on_status=None) -> Path:
    """Download an ebook from Anna's Archive.

    Strategy:
    1. Fetch the AA MD5 page once to extract both the IA link (if any) and the
       slow_download URL.
    2. If *isbns* is provided, verify at least one appears on the MD5 page before
       proceeding (skips wrong-book results that slipped through the title search,
       while accepting different editions when Google Books returns additional
       ISBNs from other same-language editions).
    3. Try Internet Archive directly (fast, no bot protection) if an IA source is
       linked from the page.
    4. Fall back to trawl's /aa/download endpoint (patchright + headless
       Chromium) which clears DDoS-Guard on the slow-download page and returns
       the book bytes — the only verified automated path for AA downloads.

    When the MD5 page itself is unreachable (all mirrors return 403/DDG
    challenge), constructs the slow_download URL directly from the md5
    (deterministic format) and proceeds to step 4 — skipping IA and ISBN
    validation, but still attempting the download.
    """
    from mirror_selector import current_annas_archive_url

    html: str | None = None
    try:
        html = await _fetch_md5_page(md5)
    except Exception as exc:
        logger.warning(
            f"Anna MD5 page fetch failed for {md5} ({exc.__class__.__name__}); "
            f"constructing slow_download URL directly"
        )

    if html:
        if isbns:
            page_isbns = _page_isbns(html)
            if page_isbns and not any(
                any(isbn in p or p in isbn for isbn in isbns) for p in page_isbns
            ):
                logger.warning(
                    f"AA MD5 page for {md5} has ISBNs {page_isbns} — none match {sorted(isbns)}, continuing anyway (different edition)"
                )
            isbn_match_str = (
                'matched' if page_isbns and any(
                    any(isbn in p or p in isbn for isbn in isbns) for p in page_isbns
                ) else 'unavailable' if not page_isbns else 'different_edition'
            )
            logger.info(f"Anna MD5 decision isbn_validation={isbn_match_str} md5={md5}")

        try:
            ia_path = await _try_internet_archive(md5, html)
        except DownloadError:
            ia_path = None
            logger.warning(f"Anna MD5 decision source=internet_archive outcome=failed md5={md5} next=slow_partner")
        if ia_path:
            logger.info(f"Anna MD5 decision source=internet_archive outcome=success md5={md5}")
            return ia_path

        slow_urls = _get_slow_download_urls(md5, html)
    else:
        # MD5 page unreachable — construct slow_download URL directly.
        # The URL format is deterministic: /slow_download/{md5}/0/0
        mirror = current_annas_archive_url()
        slow_urls = [f"{mirror}/slow_download/{md5}/0/0"]
        logger.info(f"Anna MD5 decision md5={md5} source=constructed_url mirror={mirror}")

    logger.info(f"Anna MD5 decision source=slow_partner reason={'md5_page_unreachable' if not html else 'internet_archive_unavailable'} md5={md5}")
    file_path = await _download_via_slow_partners(md5, slow_urls, on_status=on_status)
    size_kb = round(file_path.stat().st_size / 1000, 1)
    logger.info(f"Anna MD5 decision source=proxy outcome=success size_kb={size_kb}")
    return file_path
