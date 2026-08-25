import asyncio
import ipaddress
import io
import json
import logging
import os
import re
import smtplib
import time
import unicodedata
import zipfile
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import aiohttp
from bs4 import BeautifulSoup

from config import settings
from download_with_annas_archive import download_book_from_annas_archive
from download_with_libgen import (
    choose_libgen_mirror,
    download_book_using_selenium,
    get_libgen_link,
)
from exceptions import (
    BookNotFoundError,
    DownloadError,
    EmailDeliveryError,
    InvalidURLError,
    ManualDownloadRequiredError,
)
from utils import log_call

logger = logging.getLogger(__name__)
_GOOGLE_BOOKS_VOLUMES_URL = "https://www.googleapis.com/books/v1/volumes"
_ISBN_RE = re.compile(r"^(?:97[89]\d{10}|\d{9}[\dX])$")
_GOOGLE_COVER_SIZES = ("extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail")
_AA_ENGLISH_STOP_WORDS = frozenset(
    {"a", "an", "and", "author", "book", "by", "for", "from", "in", "of", "on", "the", "to", "volume", "with"}
)
_AA_HEBREW_STOP_WORDS = frozenset({"את", "ב", "ה", "ו", "כ", "ל", "מ", "מן", "עם", "על", "של"})
_GOOGLE_CACHE: dict[str, tuple[float, list[dict], list[dict]]] = {}
_GOOGLE_COVER_CACHE: dict[tuple[str, str, str], tuple[float, str]] = {}
# Bounds the primary Google cache to a fixed number of distinct queries; stale
# entries for less-recently-seen queries are dropped first.
_GOOGLE_CACHE_MAX_ENTRIES = 64
_GOOGLE_BREAKER = {"consecutive_failures": 0, "open_until": 0.0}
_STRUCTURED_GOOGLE_QUERIES = ("isbn:", "intitle:", "inauthor:")


class GoogleBooksProviderError(RuntimeError):
    """A provider-safe failure that intentionally excludes request URLs and keys."""

    def __init__(self, outcome_code: str):
        super().__init__(outcome_code)
        self.outcome_code = outcome_code


def _normalize_query(query: str) -> str:
    return " ".join(query.casefold().split())


def _aa_meaningful_token_sequence(value: object) -> list[str]:
    """Return normalized title/author words suitable for Anna result matching."""
    normalized = unicodedata.normalize("NFKD", str(value).casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    tokens: list[str] = []
    current: list[str] = []
    for char in normalized:
        if char.isalpha():
            current.append(char)
            continue
        if current:
            token = "".join(current)
            current.clear()
            if _aa_token_is_meaningful(token):
                tokens.append(token)
    if current:
        token = "".join(current)
        if _aa_token_is_meaningful(token):
            tokens.append(token)
    return tokens


def _aa_meaningful_tokens(value: object) -> set[str]:
    return set(_aa_meaningful_token_sequence(value))


def _aa_token_is_meaningful(token: str) -> bool:
    if token in _AA_ENGLISH_STOP_WORDS or token in _AA_HEBREW_STOP_WORDS:
        return False
    is_hebrew = any("\u0590" <= char <= "\u05ff" for char in token)
    return len(token) >= (2 if is_hebrew else 3)


def _aa_title_phrase_matches(query: str, candidate: str) -> bool:
    """True when the full meaningful-token phrase of *query* appears in *candidate*."""
    query_tokens = _aa_meaningful_token_sequence(query)
    if not query_tokens:
        return False
    candidate_tokens = _aa_meaningful_token_sequence(candidate)
    phrase_length = len(query_tokens)
    return any(
        candidate_tokens[index:index + phrase_length] == query_tokens
        for index in range(len(candidate_tokens) - phrase_length + 1)
    )


def _aa_result_is_relevant(query: str, title: str, author: str) -> bool:
    """True when the query shares any meaningful token with the title or author.

    Searches often combine a title and author (e.g. "Dune Frank Herbert"), so a
    full contiguous-phrase match would reject valid results. Matching any token
    keeps the filter broad for search ranking while still dropping unrelated
    volumes.
    """
    query_tokens = set(_aa_meaningful_token_sequence(query))
    if not query_tokens:
        return False
    return bool(
        query_tokens & _aa_meaningful_tokens(title)
        or query_tokens & _aa_meaningful_tokens(author)
    )


def _google_circuit_is_open(now: float | None = None) -> bool:
    return (now if now is not None else time.monotonic()) < _GOOGLE_BREAKER["open_until"]


def _record_google_provider_success() -> None:
    _GOOGLE_BREAKER["consecutive_failures"] = 0
    _GOOGLE_BREAKER["open_until"] = 0.0


def _record_google_provider_failure(outcome_code: str) -> None:
    if outcome_code not in {"transient_server_error", "timeout", "rate_limited"}:
        return
    _GOOGLE_BREAKER["consecutive_failures"] += 1
    if _GOOGLE_BREAKER["consecutive_failures"] >= settings.google_books_circuit_failure_threshold:
        _GOOGLE_BREAKER["open_until"] = time.monotonic() + settings.google_books_circuit_cooldown_seconds
        logger.warning(
            f"Metadata provider=google_books outcome=circuit_open reason={outcome_code} "
            f"cooldown_seconds={settings.google_books_circuit_cooldown_seconds}"
        )


def _google_result_is_relevant(query: str, title: str, author: str) -> bool:
    normalized_query = _normalize_query(query)
    candidate = _normalize_query(f"{title} {author}")
    normalized_title = _normalize_query(title)
    if normalized_query and normalized_query in candidate:
        return True

    query_tokens = {token for token in re.findall(r"\w+", normalized_query) if len(token) > 1}
    if not query_tokens:
        return False
    title_tokens = set(re.findall(r"\w+", normalized_title))
    candidate_tokens = set(re.findall(r"\w+", candidate))
    title_matches = len(query_tokens & title_tokens)
    candidate_matches = len(query_tokens & candidate_tokens)
    return title_matches >= 1 and (title_matches * 2 + candidate_matches) >= 2


def _emit_status(on_status, status: str, **details) -> None:
    if on_status is None:
        return
    try:
        on_status(status, **details)
    except TypeError:
        # Older callbacks only receive coarse stage updates. They must not see
        # duplicate stage events that differ only by source/format telemetry.
        if not details:
            on_status(status)


async def _fetch_page_with_retry(url: str, max_retries: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with aiohttp.ClientSession() as session, session.get(url) as response:
                return await response.text()
        except (aiohttp.ClientError, OSError) as e:
            last_error = e
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(f"HTTP fetch attempt {attempt + 1} for {url} failed: {e}, retrying in {wait}s")
                await asyncio.sleep(wait)
    raise last_error


async def _fetch_aa_via_trawl(query: str) -> str:
    """Search Anna's Archive via Selenium+Firefox to bypass DDoS-Guard.

    Uses the mirror selector's best mirror and records the outcome so the
    selector learns even from Selenium-path failures.
    """
    import asyncio

    from mirror_selector import (
        current_annas_archive_url,
        record_mirror_failure,
        record_mirror_success,
    )

    def _fetch_sync(q: str, mirror: str) -> str:
        from selenium import webdriver
        from selenium.webdriver.firefox.options import Options as FirefoxOptions

        url = f"{mirror}/search?q={q}"
        options = FirefoxOptions()
        options.add_argument("--headless")

        driver = webdriver.Firefox(options=options)
        try:
            driver.set_page_load_timeout(30)
            driver.get(url)
            # Wait for DDoS-Guard to clear (title changes from challenge page)
            import time
            for _ in range(20):
                time.sleep(1)
                title = driver.title.lower()
                if "checking" not in title and "ddos" not in title and "challenge" not in title:
                    break
            html = driver.page_source
            logger.info(f"AA Selenium search OK query={q!r} length={len(html)}")
            return html
        finally:
            try:
                driver.quit()
            except Exception:
                pass

    mirror = current_annas_archive_url()
    try:
        html = await asyncio.to_thread(_fetch_sync, query, mirror)
    except Exception:
        record_mirror_failure(mirror)
        raise
    record_mirror_success(mirror)
    return html


async def _fetch_goodreads_page_with_flaresolverr(url: str) -> str:
    """Retrieve a Goodreads page through FlareSolverr when direct access is blocked."""
    hostname = urlsplit(url).hostname
    if hostname not in {"goodreads.com", "www.goodreads.com"}:
        logger.warning(f"Goodreads metadata fallback skipped for untrusted host={hostname!r}")
        return ""

    logger.info(f"Metadata decision source=goodreads fallback=flaresolverr url={url}")
    payload = {"cmd": "request.get", "url": url, "maxTimeout": settings.flaresolverr_timeout_ms}
    timeout = aiohttp.ClientTimeout(total=(settings.flaresolverr_timeout_ms // 1000) + 15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session, session.post(
            f"{settings.flaresolverr_url}/v1", json=payload
        ) as response:
            response.raise_for_status()
            data = await response.json()
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        logger.warning(f"Goodreads metadata fallback failed via flaresolverr: {exc.__class__.__name__}")
        return ""

    page = data.get("solution", {}).get("response", "")
    logger.info(
        f"Goodreads metadata fallback response status={data.get('status')!r} "
        f"body_bytes={len(page) if isinstance(page, str) else 0}"
    )
    return page if data.get("status") == "ok" and isinstance(page, str) else ""


def _extract_author_name(value: object) -> str:
    """Return a display name from a JSON-LD author value of any shape."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        name = value.get("name")
        return str(name).strip() if name else ""
    if isinstance(value, list):
        names = [_extract_author_name(item) for item in value]
        return ", ".join(name for name in names if name)
    return ""


@log_call
async def get_book_info(url: str) -> dict[str, str]:
    """Extract ISBN, title, and author from a Goodreads book page.

    Returns ``{"isbn": "...", "title": "...", "author": "..."}``.
    ISBN may be empty when the page carries none but title/author were
    extracted — the download pipeline searches by title in that case.
    Raises :class:`BookNotFoundError` only when nothing usable was found.
    """
    logger.info(f"Metadata decision source=goodreads url={url} retrieval=direct")
    try:
        text = await _fetch_page_with_retry(url)
    except aiohttp.InvalidUrlClientError as e:
        raise InvalidURLError(f"Goodreads URL isn't valid: {url}") from e
    soup = BeautifulSoup(text, 'html.parser')

    isbn = ""
    title = ""
    author = ""

    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string)
            if not isbn:
                isbn = data.get('isbn', '')
            if not title:
                title = data.get('name', '')
            if not author:
                author = _extract_author_name(data.get('author'))
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue

    if not isbn:
        match = re.search(r"isbn\D{0,200}(\d{10,13})", text, re.IGNORECASE)
        if not match:
            logger.info("Goodreads metadata decision isbn=missing direct_response; fallback=flaresolverr")
            text = await _fetch_goodreads_page_with_flaresolverr(url)
            soup = BeautifulSoup(text, 'html.parser')
            for script in soup.find_all('script', type='application/ld+json'):
                try:
                    data = json.loads(script.string)
                    if not isbn:
                        isbn = data.get('isbn', '')
                    if not title:
                        title = data.get('name', '')
                    if not author:
                        author = _extract_author_name(data.get('author'))
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
            match = re.search(r"isbn\D{0,200}(\d{10,13})", text, re.IGNORECASE)
            if not isbn and not match:
                logger.warning("Goodreads metadata decision outcome=no_isbn_after_fallback")
                raise BookNotFoundError(f"No ISBN found on page: {url}")
        if not isbn:
            isbn = match.group(1)

    if not title:
        og_title = soup.find('meta', property='og:title')
        if og_title and og_title.get('content'):
            title = og_title['content']

    logger.info(f"Extracted book info: isbn={isbn}, title={title!r}, author={author!r}")
    return {"isbn": isbn, "title": title, "author": author}


def _sanitize_epub_bytes(data: bytes) -> bytes:
    """Rebuild an EPUB zip, dropping non-standard entries.

    Shadow-library rips (zlib3/oceanofpdf, libgen, ...) embed root-level
    watermark files (e.g. ``oceanofpdf.com``). Gmail's content scanner
    blocks messages carrying zips with such marker files
    (``552 5.7.0 ... content presents a potential security issue``), which
    breaks Kindle email delivery even though the book itself is fine.
    Keep only the standard EPUB entries (``mimetype``, ``META-INF/``,
    ``OEBPS/``); the result is a valid EPUB that passes Gmail.

    Non-EPUB input (PDF, MOBI, plain files) is returned unchanged.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zin:
            names = zin.namelist()
            if "mimetype" not in names:
                return data
            mimetype = zin.read("mimetype")
            if b"epub" not in mimetype.lower():
                return data
            out = io.BytesIO()
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
                # EPUB spec: mimetype must be first and stored (uncompressed).
                info = zipfile.ZipInfo("mimetype")
                zout.writestr(info, mimetype, compress_type=zipfile.ZIP_STORED)
                for name in names:
                    if name == "mimetype":
                        continue
                    if name.startswith(("META-INF/", "OEBPS/")):
                        zout.writestr(name, zin.read(name))
            return out.getvalue()
    except (zipfile.BadZipFile, KeyError, OSError):
        return data


@log_call
def send_to_kindle(email: str, book_path: Path | None = None,
                   book_data: bytes = b'', filename: str = ''):
    msg = EmailMessage()
    msg['From'] = settings.gmail_account
    msg['To'] = email
    msg['Subject'] = 'book'
    logger.info(f"Sending ebook to Kindle email {email}", extra={"allow_email_log": True})

    if book_path:
        with open(book_path, 'rb') as f:
            file_data = f.read()
            file_name = book_path.name
    else:
        file_data = book_data
        file_name = filename
    file_data = _sanitize_epub_bytes(file_data)
    logger.info(f"file size: {round(len(file_data) / 1000, 1)}KB")
    msg.add_attachment(file_data, maintype='application', subtype='octet-stream',
                       filename=file_name)
    max_retries = 3
    for attempt in range(max_retries):
        try:
            logger.info(f"Delivery decision transport=smtp_ssl attempt={attempt + 1}/{max_retries}")
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
                smtp.login(settings.gmail_account, settings.gmail_password)
                smtp.send_message(msg)
            logger.info("Delivery decision transport=smtp_ssl outcome=accepted")
            break
        except (smtplib.SMTPException, OSError) as e:
            if attempt < max_retries - 1:
                wait = 2 ** (attempt + 1)
                logger.warning(f"SMTP attempt {attempt + 1} failed: {e}, retrying in {wait}s")
                time.sleep(wait)
            else:
                raise EmailDeliveryError(f"Failed to send email to {email}") from e
    if book_path:
        os.remove(book_path)
        logger.info("Delivery decision local_artifact=removed")


@log_call
async def search_aa_all_formats(isbn: str, title: str = "", author: str = "") -> dict[str, list[str]]:
    """Search Anna's Archive for all formats of a book.

    Searches by *title* (broad, finds all formats) rather than ISBN, because
    many AA records lack ISBN metadata.  Falls back to ISBN when no title is
    available.

    The ``ext`` search filter is intentionally omitted because AA's backend
    does not reliably honour it (confirmed by SearXNG maintainers).

    Returns ``{"epub": [md5, ...], "pdf": [...], "mobi": [...]}``.
    """
    query = f"{title} {author}".strip() if title else isbn

    logger.info(f"Searching AA for {query!r} (isbn={isbn})")

    html = await _fetch_aa_via_trawl(query)
    return _parse_aa_search_results(html)


def _parse_aa_search_results(html: str) -> dict[str, list[str]]:
    """Extract per-format MD5 hashes from a rendered AA search page."""
    soup = BeautifulSoup(html, "html.parser")
    results: dict[str, list[str]] = {"epub": [], "pdf": [], "mobi": []}
    seen: set[str] = set()

    for outer in soup.find_all("div", class_="js-aarecord-list-outer"):
        for item in outer.find_all("div", class_="flex", recursive=False):
            link = item.find("a", href=re.compile(r"/md5/"))
            if not link:
                continue
            md5_match = re.search(r"/md5/([0-9a-f]+)", link["href"])
            if not md5_match:
                continue
            md5 = md5_match.group(1)
            if md5 in seen:
                continue
            seen.add(md5)

            tag_div = item.find("div", class_=re.compile(r"font-semibold"))
            fmt_assigned = False
            if tag_div:
                tag_text = tag_div.get_text().lower()
                for fmt in ("epub", "pdf", "mobi"):
                    if fmt in tag_text:
                        results[fmt].append(md5)
                        fmt_assigned = True
                        break
            if not fmt_assigned:
                logger.info(f"Unknown format for AA result md5={md5}")

    total = sum(len(v) for v in results.values())
    logger.info(f"AA search found {total} results: " +
                ", ".join(f"{k}={len(v)}" for k, v in results.items()))
    return results



def _parse_aa_metadata_results(html: str) -> list[dict]:
    """Extract selectable book metadata from Anna's Archive search result cards."""
    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen_md5s = set()

    for record in soup.select("div.js-aarecord-list-outer div.flex"):
        if not record.find("a", href=re.compile(r"/md5/[0-9a-f]{32}")):
            continue
        md5_link = record.find("a", href=re.compile(r"/md5/([0-9a-f]{32})"))
        md5_match = re.search(r"/md5/([0-9a-f]{32})", md5_link["href"]) if md5_link else None
        if not md5_match:
            continue
        md5 = md5_match.group(1)
        if md5 in seen_md5s:
            continue

        title_link = record.select_one("a.text-lg")
        title = title_link.get_text(" ", strip=True) if title_link else ""
        if not title:
            logger.info(f"Anna metadata decision md5={md5} skipped=missing_title")
            continue
        author_link = title_link.find_next("a", class_="text-sm") if title_link else None
        author = author_link.get_text(" ", strip=True) if author_link else ""
        details = record.select_one("div.text-gray-800.font-semibold.text-sm")
        details_text = details.get_text(" ", strip=True) if details else ""
        format_match = re.search(r"\b(EPUB|PDF|MOBI)\b", details_text, re.IGNORECASE)
        if not format_match:
            logger.info(f"Anna metadata decision md5={md5} skipped=unsupported_format")
            continue
        language_match = re.search(r"\[([a-z]{2,3})\]", details_text, re.IGNORECASE)
        cover_image = record.find("img", src=True)
        cover_url = _public_cover_url(cover_image.get("src") if cover_image else "")
        seen_md5s.add(md5)
        results.append(
            {
                "title": title,
                "author": author,
                "isbn": "",
                "cover_url": cover_url,
                "md5": md5,
                "format": format_match.group(1).lower(),
                "language": language_match.group(1).lower() if language_match else "",
                "source": "annas_archive",
            }
        )

    logger.info(f"Anna metadata decisions selected={len(results)} unique_md5s={len(seen_md5s)}")
    return results


async def _search_aa_metadata(query: str) -> list[dict]:
    if not settings.annas_archive_url and not settings.annas_archive_mirrors:
        raise RuntimeError("Anna's Archive mirror is not configured")
    logger.info(f"Metadata decision source=annas_archive query={query!r}")
    from mirror_selector import fetch_aa_html

    html = await fetch_aa_html(f"/search?{urlencode({'q': query})}")
    parsed_results = _parse_aa_metadata_results(html)
    relevant_results = [
        result
        for result in parsed_results
        if _aa_result_is_relevant(query, result["title"], result["author"])
    ]
    logger.info(
        f"Anna metadata relevance parsed={len(parsed_results)} "
        f"rejected={len(parsed_results) - len(relevant_results)} "
        f"retained={len(relevant_results)} returned={min(len(relevant_results), 20)}"
    )
    return relevant_results[:20]


@log_call
async def _fetch_google_books_search(query: str) -> list[dict]:
    if not settings.google_books_api_key:
        logger.warning("Metadata provider=google_books outcome=invalid_configuration")
        raise GoogleBooksProviderError("invalid_configuration")
    params = {
        "q": query,
        "maxResults": "40",
        "printType": "books",
        "key": settings.google_books_api_key,
    }
    timeout = aiohttp.ClientTimeout(total=10)
    logger.info(
        f"Metadata decision source=google_books query={query!r} api_host=www.googleapis.com "
                "language_filter=provider_ranked limit=40 timeout_seconds=10"
    )
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
                _GOOGLE_BOOKS_VOLUMES_URL, params=params
            ) as response:
                logger.info(f"Metadata provider=google_books outcome=http_response status={response.status}")
                if response.status >= 500:
                    raise GoogleBooksProviderError("transient_server_error")
                if response.status == 429:
                    raise GoogleBooksProviderError("rate_limited")
                if response.status in {401, 403}:
                    raise GoogleBooksProviderError("quota_or_authorization")
                if response.status >= 400:
                    raise GoogleBooksProviderError("invalid_request")
                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    raise GoogleBooksProviderError("malformed_response") from exc
            if not isinstance(payload, dict):
                logger.warning("Metadata provider=google_books outcome=malformed_response reason=payload_not_object")
                raise GoogleBooksProviderError("malformed_response")
            volumes = payload.get("items", [])
            if not isinstance(volumes, list):
                logger.warning("Metadata provider=google_books outcome=malformed_response reason=items_not_list")
                raise GoogleBooksProviderError("malformed_response")
            logger.info(f"Metadata provider=google_books outcome=success volumes={len(volumes)}")
            return volumes
        except GoogleBooksProviderError as exc:
            logger.warning(f"Metadata provider=google_books outcome={exc.outcome_code} attempt={attempt + 1}/3")
            if exc.outcome_code not in {"transient_server_error", "rate_limited"} or attempt == 2:
                raise
            await asyncio.sleep(2 ** (attempt + 1))
        except TimeoutError:
            outcome_code = "timeout"
            logger.warning(f"Metadata provider=google_books outcome={outcome_code} attempt={attempt + 1}/3")
            if attempt == 2:
                raise GoogleBooksProviderError(outcome_code) from None
            await asyncio.sleep(2 ** (attempt + 1))
        except aiohttp.ClientError:
            outcome_code = "transport_error"
            logger.warning(f"Metadata provider=google_books outcome={outcome_code} attempt={attempt + 1}/3")
            if attempt == 2:
                raise GoogleBooksProviderError(outcome_code) from None
            await asyncio.sleep(2 ** (attempt + 1))
    raise GoogleBooksProviderError("unavailable")


def _normalize_isbn(value: object) -> str:
    isbn = re.sub(r"[\s-]", "", str(value)).upper()
    return isbn if _ISBN_RE.fullmatch(isbn) else ""


def _public_cover_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    parsed = urlsplit(value.strip())
    hostname = parsed.hostname
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
        or hostname.lower() == "localhost"
        or hostname.lower().endswith(".local")
    ):
        return ""
    try:
        if not ipaddress.ip_address(hostname).is_global:
            return ""
    except ValueError:
        pass
    return parsed._replace(scheme="https", fragment="").geturl()


def _google_cover_url(image_links: object) -> str:
    if not isinstance(image_links, dict):
        return ""
    for size in _GOOGLE_COVER_SIZES:
        cover_url = _public_cover_url(image_links.get(size))
        if cover_url:
            return cover_url
    return ""


def _parse_google_books_results(volumes: list[dict], query: str = "") -> list[dict]:
    results: dict[tuple[str, str], dict] = {}
    skipped_missing_title = 0
    skipped_missing_isbn = 0
    skipped_duplicate_isbn = 0
    skipped_irrelevant = 0
    language_codes = set()
    for volume in volumes:
        if not isinstance(volume, dict):
            skipped_missing_title += 1
            continue
        volume_info = volume.get("volumeInfo", {})
        if not isinstance(volume_info, dict):
            skipped_missing_title += 1
            continue
        language = volume_info.get("language")
        if language:
            language_codes.add(str(language))
        title = str(volume_info.get("title", "")).strip()
        if not title:
            skipped_missing_title += 1
            continue
        authors = volume_info.get("authors", [])
        author = ", ".join(str(name) for name in authors if name) if isinstance(authors, list) else ""
        if query and not query.casefold().startswith(_STRUCTURED_GOOGLE_QUERIES) and not _google_result_is_relevant(query, title, author):
            skipped_irrelevant += 1
            continue
        identifiers = volume_info.get("industryIdentifiers", [])
        isbns = [
            normalized
            for identifier in identifiers
            if isinstance(identifier, dict)
            and identifier.get("type") in {"ISBN_13", "ISBN_10"}
            and (normalized := _normalize_isbn(identifier.get("identifier", "")))
        ]
        if not isbns:
            skipped_missing_isbn += 1
            continue
        key = (_normalize_query(title), _normalize_query(author) if author else "")
        if key in results:
            existing = results[key]
            for isbn in isbns:
                if isbn not in existing["isbns"]:
                    existing["isbns"].append(isbn)
                    existing["isbn"] = existing["isbns"][0]
            if not existing.get("cover_url") and not existing.get("language"):
                cover_url = _google_cover_url(volume_info.get("imageLinks"))
                if cover_url:
                    existing["cover_url"] = cover_url
                if language:
                    existing["language"] = str(language)
                if author:
                    existing["author"] = author
        else:
            cover_url = _google_cover_url(volume_info.get("imageLinks"))
            results[key] = {
                "title": title,
                "author": author,
                "isbn": isbns[0],
                "isbns": isbns,
                "cover_url": cover_url,
                "md5": "",
                "format": "",
                "language": str(language or ""),
                "source": "google_books",
            }
    logger.info(
        f"Google Books ISBN decisions volumes={len(volumes)} selected={len(results)} "
        f"skipped_missing_title={skipped_missing_title} skipped_missing_isbn={skipped_missing_isbn} "
        f"skipped_duplicate_isbn={skipped_duplicate_isbn} skipped_irrelevant={skipped_irrelevant} "
        f"language_codes={sorted(language_codes)}"
    )
    return list(results.values())


async def _google_metadata_results(query: str) -> list[dict]:
    normalized_query = _normalize_query(query)
    now = time.monotonic()
    cached = _GOOGLE_CACHE.get(normalized_query)
    if cached and cached[0] > now:
        logger.info("Metadata provider=google_books outcome=cache_hit")
        return cached[1]
    if cached:
        _GOOGLE_CACHE.pop(normalized_query, None)

    if _google_circuit_is_open(now):
        logger.warning("Metadata provider=google_books outcome=circuit_open action=skip")
        raise GoogleBooksProviderError("circuit_open")

    try:
        volumes = await _fetch_google_books_search(query)
        results = _parse_google_books_results(volumes, query)
    except GoogleBooksProviderError as exc:
        _record_google_provider_failure(exc.outcome_code)
        raise
    except RuntimeError:
        # Kept for compatibility with callers and tests that replace the provider.
        _record_google_provider_failure("transport_error")
        raise GoogleBooksProviderError("transport_error") from None

    _record_google_provider_success()
    if volumes:
        _prune_google_cache(now)
        _GOOGLE_CACHE[normalized_query] = (
            now + settings.google_books_cache_ttl_seconds,
            results,
            volumes,
        )
        logger.info(f"Metadata provider=google_books outcome=cache_store results={len(results)}")
    return results


def _prune_google_cache(now: float | None = None) -> None:
    """Evict expired entries first, then the oldest, to keep the cache bounded."""
    now = now if now is not None else time.monotonic()
    for key in list(_GOOGLE_CACHE):
        if _GOOGLE_CACHE[key][0] <= now:
            _GOOGLE_CACHE.pop(key, None)
    while len(_GOOGLE_CACHE) >= _GOOGLE_CACHE_MAX_ENTRIES:
        _GOOGLE_CACHE.pop(next(iter(_GOOGLE_CACHE)), None)


def _google_cached_volumes(query: str) -> list[dict]:
    cached = _GOOGLE_CACHE.get(_normalize_query(query))
    if not cached:
        return []
    if cached[0] <= time.monotonic():
        _GOOGLE_CACHE.pop(_normalize_query(query), None)
        return []
    return cached[2]


async def _enrich_google_cache_for_book(isbn: str, title: str, author: str) -> None:
    """Populate the Google Books cache with all known editions of a book.

    Fires targeted API calls that surface different editions than the bare
    keyword search used during initial book discovery.
    """
    if not settings.google_books_api_key:
        return
    queries: list[str] = []
    if isbn:
        queries.append(f"isbn:{isbn}")
    if title and author:
        queries.append(f'intitle:"{title}" inauthor:"{author}"')
    elif title:
        queries.append(f'intitle:"{title}"')
    elif author:
        queries.append(f'inauthor:"{author}"')
    for query in queries:
        try:
            await _google_metadata_results(query)
        except GoogleBooksProviderError:
            pass


def _google_edition_isbns(title: str, author: str = "", *, language: str = "") -> set[str]:
    """Return all ISBNs from cached Google Books volumes whose title and language match.

    Iterates the reasonably-small cache (max ~64 entries) to find volumes matching
    the requested book. Only ISBNs from volumes with the same language as the
    user-selected edition are included, so unrelated foreign-language editions are
    excluded from the LibGen / Anna's Archive identity checks.
    """
    normalized_title = _normalize_query(title)
    normalized_author = _normalize_query(author) if author else ""
    isbns: set[str] = set()
    now = time.monotonic()
    for _normalized_query_key, (expiry, _results, volumes) in list(_GOOGLE_CACHE.items()):
        if expiry <= now:
            continue
        for result in _results:
            result_title = _normalize_query(result.get("title", ""))
            if normalized_title not in result_title:
                continue
            if normalized_author and normalized_author not in _normalize_query(result.get("author", "")):
                continue
            for volume in volumes:
                if not isinstance(volume, dict):
                    continue
                vol_info = volume.get("volumeInfo", {})
                if not isinstance(vol_info, dict):
                    continue
                vol_lang = str(vol_info.get("language", ""))
                if language and vol_lang != language:
                    continue
                identifiers = vol_info.get("industryIdentifiers", [])
                for identifier in identifiers:
                    if not isinstance(identifier, dict):
                        continue
                    if identifier.get("type") not in {"ISBN_13", "ISBN_10"}:
                        continue
                    raw = identifier.get("identifier", "")
                    normalized = _normalize_isbn(raw)
                    if normalized:
                        isbns.add(normalized)
            return isbns
    return isbns


def _normalized_metadata_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value).casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return " ".join("".join(
        char if char.isalnum() else " "
        for char in normalized
    ).split())


def _normalized_language(value: object) -> str:
    language = str(value or "").casefold().split("-", maxsplit=1)[0]
    return {"iw": "he", "heb": "he", "eng": "en"}.get(language, language)


def _google_enrichment_cover_url(image_links: object) -> str:
    cover_url = _google_cover_url(image_links)
    hostname = urlsplit(cover_url).hostname
    if not hostname:
        return ""
    hostname = hostname.lower()
    if hostname == "books.google.com" or hostname.endswith(".books.googleusercontent.com"):
        return cover_url
    return ""


def _google_volume_cover_for_anna_result(result: dict, volumes: list[dict]) -> str:
    expected_title = _normalized_metadata_text(result.get("title", ""))
    expected_author_tokens = _aa_meaningful_tokens(result.get("author", ""))
    expected_language = _normalized_language(result.get("language", ""))
    if not expected_title:
        return ""

    matching_covers: set[str] = set()
    for volume in volumes:
        if not isinstance(volume, dict):
            continue
        volume_info = volume.get("volumeInfo")
        if not isinstance(volume_info, dict):
            continue
        if _normalized_metadata_text(volume_info.get("title", "")) != expected_title:
            continue

        authors = volume_info.get("authors", [])
        author = " ".join(str(name) for name in authors if name) if isinstance(authors, list) else ""
        if expected_author_tokens and not (expected_author_tokens & _aa_meaningful_tokens(author)):
            continue

        candidate_language = _normalized_language(volume_info.get("language", ""))
        if expected_language and candidate_language and expected_language != candidate_language:
            continue

        cover_url = _google_enrichment_cover_url(volume_info.get("imageLinks"))
        if cover_url:
            matching_covers.add(cover_url)

    return matching_covers.pop() if len(matching_covers) == 1 else ""


def _google_cover_cache_key(result: dict) -> tuple[str, str, str]:
    return (
        _normalized_metadata_text(result.get("title", "")),
        " ".join(sorted(_aa_meaningful_tokens(result.get("author", "")))),
        _normalized_language(result.get("language", "")),
    )


def _get_cached_google_cover(key: tuple[str, str, str], now: float) -> str | None:
    cached = _GOOGLE_COVER_CACHE.get(key)
    if not cached:
        return None
    if cached[0] <= now:
        _GOOGLE_COVER_CACHE.pop(key, None)
        return None
    return cached[1]


def _cache_google_cover(key: tuple[str, str, str], cover_url: str, now: float) -> None:
    max_entries = max(1, settings.google_books_cover_cache_max_entries)
    while len(_GOOGLE_COVER_CACHE) >= max_entries and key not in _GOOGLE_COVER_CACHE:
        oldest_key = min(_GOOGLE_COVER_CACHE, key=lambda cached_key: _GOOGLE_COVER_CACHE[cached_key][0])
        _GOOGLE_COVER_CACHE.pop(oldest_key, None)
    _GOOGLE_COVER_CACHE[key] = (
        now + settings.google_books_cover_cache_ttl_seconds,
        cover_url,
    )


async def _fetch_google_books_cover_search(title: str, author: str) -> list[dict]:
    if not settings.google_books_api_key:
        raise GoogleBooksProviderError("invalid_configuration")

    query = f'intitle:"{title}"'
    if author:
        query = f'{query} inauthor:"{author}"'
    params = {
        "q": query,
        "maxResults": "5",
        "printType": "books",
        "key": settings.google_books_api_key,
    }
    timeout = aiohttp.ClientTimeout(total=settings.google_books_cover_timeout_seconds)
    attempts = max(1, settings.google_books_cover_request_attempts)
    for attempt in range(attempts):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
                _GOOGLE_BOOKS_VOLUMES_URL, params=params
            ) as response:
                if response.status >= 500:
                    raise GoogleBooksProviderError("transient_server_error")
                if response.status == 429:
                    raise GoogleBooksProviderError("rate_limited")
                if response.status in {401, 403}:
                    raise GoogleBooksProviderError("quota_or_authorization")
                if response.status >= 400:
                    raise GoogleBooksProviderError("invalid_request")
                try:
                    payload = await response.json()
                except (aiohttp.ContentTypeError, ValueError) as exc:
                    raise GoogleBooksProviderError("malformed_response") from exc
        except GoogleBooksProviderError as exc:
            outcome_code = exc.outcome_code
        except TimeoutError:
            outcome_code = "timeout"
        except aiohttp.ClientError:
            outcome_code = "transport_error"
        else:
            volumes = payload.get("items", []) if isinstance(payload, dict) else []
            if not isinstance(volumes, list):
                raise GoogleBooksProviderError("malformed_response")
            return volumes

        if outcome_code not in {"transient_server_error", "rate_limited", "timeout", "transport_error"}:
            raise GoogleBooksProviderError(outcome_code)
        if attempt == attempts - 1:
            raise GoogleBooksProviderError(outcome_code)
        logger.warning(
            f"Metadata provider=google_books_cover outcome={outcome_code} "
            f"attempt={attempt + 1}/{attempts} action=retry"
        )
        await asyncio.sleep(attempt + 1)

    raise GoogleBooksProviderError("unavailable")


async def _enrich_anna_missing_covers(results: list[dict], query: str) -> list[dict]:
    shared_volumes = _google_cached_volumes(query)
    enriched_results: list[dict] = []
    already_covered = 0
    enriched = 0
    cache_hits = 0
    unmatched = 0
    provider_failures = 0
    provider_outcomes: dict[str, int] = {}
    lookup_attempts = 0
    lookup_limit = max(0, settings.google_books_cover_lookup_limit)

    for result in results:
        enriched_result = dict(result)
        if enriched_result.get("cover_url"):
            already_covered += 1
            enriched_results.append(enriched_result)
            continue

        cache_key = _google_cover_cache_key(enriched_result)
        now = time.monotonic()
        cover_url = _get_cached_google_cover(cache_key, now)
        if cover_url is not None:
            cache_hits += 1
        else:
            cover_url = _google_volume_cover_for_anna_result(enriched_result, shared_volumes)
            if not cover_url and lookup_attempts < lookup_limit:
                lookup_attempts += 1
                try:
                    volumes = await _fetch_google_books_cover_search(
                        str(enriched_result.get("title", "")),
                        str(enriched_result.get("author", "")),
                    )
                except GoogleBooksProviderError as exc:
                    provider_failures += 1
                    provider_outcomes[exc.outcome_code] = provider_outcomes.get(exc.outcome_code, 0) + 1
                    _record_google_provider_failure(exc.outcome_code)
                else:
                    _record_google_provider_success()
                    cover_url = _google_volume_cover_for_anna_result(enriched_result, volumes)
                    _cache_google_cover(cache_key, cover_url, now)
                    if (
                        not cover_url
                        and enriched_result.get("author")
                        and lookup_attempts < lookup_limit
                    ):
                        lookup_attempts += 1
                        try:
                            title_only_volumes = await _fetch_google_books_cover_search(
                                str(enriched_result.get("title", "")),
                                "",
                            )
                        except GoogleBooksProviderError as exc:
                            provider_failures += 1
                            provider_outcomes[exc.outcome_code] = provider_outcomes.get(exc.outcome_code, 0) + 1
                            _record_google_provider_failure(exc.outcome_code)
                        else:
                            _record_google_provider_success()
                            cover_url = _google_volume_cover_for_anna_result(
                                enriched_result,
                                title_only_volumes,
                            )
                            _cache_google_cover(cache_key, cover_url, now)
            elif not cover_url:
                unmatched += 1

        if cover_url:
            enriched_result["cover_url"] = cover_url
            enriched += 1
        elif cache_key in _GOOGLE_COVER_CACHE:
            unmatched += 1
        enriched_results.append(enriched_result)

    logger.info(
        f"Anna cover enrichment results={len(results)} already_covered={already_covered} "
        f"enriched={enriched} cache_hits={cache_hits} unmatched={unmatched} "
        f"lookup_attempts={lookup_attempts} provider_failures={provider_failures} "
        f"provider_outcomes={provider_outcomes}"
    )
    return enriched_results


@log_call
async def search_books(query: str) -> list[dict]:
    try:
        google_results = await _google_metadata_results(query)
    except GoogleBooksProviderError as exc:
        logger.warning(f"Metadata fallback decision google_books={exc.outcome_code} next=annas_archive")
        google_results = []
    if google_results:
        logger.info(f"Metadata provider decision selected=google_books results={len(google_results)}")
        return google_results

    logger.info("Metadata fallback decision google_books=no_selectable_isbn next=annas_archive")
    anna_results = await _search_aa_metadata(query)
    return await _enrich_anna_missing_covers(anna_results, query)


async def _download_via_libgen(
    isbns: set[str],
    md5_list: list[str],
    *,
    require_confirmation: bool = True,
    title: str = "",
    author: str = "",
) -> Path:
    primary_isbn = next(iter(isbns), "")
    logger.info(f"Download decision source=libgen identifier={primary_isbn} isbns={len(isbns)} candidates={len(md5_list)}")
    libgen_mirror = await choose_libgen_mirror()
    logger.info("Download decision source=libgen mirror=selected next=get_link")
    url = await get_libgen_link(
        isbns,
        md5_list,
        libgen_mirror,
        require_confirmation=require_confirmation,
        title=title,
        author=author,
    )
    logger.info("Download decision source=libgen link=selected next=selenium")
    return await asyncio.to_thread(download_book_using_selenium, url)


async def _download_via_annas_archive(
    md5_list: list[str], isbns: set[str] | None = None, on_status=None
) -> Path:
    _emit_status(on_status, "trying_alternative", source="annas_archive")
    last_error: Exception | None = None
    for md5 in md5_list:
        try:
            logger.info(f"Download decision source=annas_archive md5={md5} action=attempt")
            return await download_book_from_annas_archive(md5, isbns=isbns, on_status=on_status)
        except (DownloadError, Exception) as e:
            logger.warning(f"Download decision source=annas_archive md5={md5} outcome=failed type={e.__class__.__name__}")
            last_error = e
    raise DownloadError(
        f"All Anna's Archive download attempts failed for {md5_list}"
    ) from last_error


async def _convert_mobi(mobi_path: Path, target_ext: str) -> Path:
    """Convert a .mobi file to *target_ext* using Calibre's ebook-convert CLI."""
    out_path = mobi_path.with_suffix(f".{target_ext}")
    proc = await asyncio.create_subprocess_exec(
        "ebook-convert", str(mobi_path), str(out_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise DownloadError(f"ebook-convert mobi→{target_ext} failed: {stderr.decode()}")
    mobi_path.unlink(missing_ok=True)
    return out_path


async def _try_convert_mobi(mobi_path: Path) -> Path:
    """Attempt mobi→epub, then mobi→pdf.  Returns raw mobi as last resort."""
    for target in ("epub", "pdf"):
        try:
            result = await _convert_mobi(mobi_path, target)
            logger.info(f"mobi→{target} conversion succeeded: {result.name}")
            return result
        except DownloadError as e:
            logger.warning(f"mobi→{target} conversion failed: {e}")
    logger.warning("All mobi conversions failed, sending raw .mobi")
    return mobi_path


async def ebook_download_by_md5(md5: str, kindle_mail: str, on_status=None) -> None:
    def _emit(status, **details):
        _emit_status(on_status, status, **details)

    logger.info(f"Download decision source=libgen_md5 md5={md5}")
    _emit("downloading", source="libgen")
    book_path = await _download_via_libgen(md5, [md5], require_confirmation=False)

    logger.info("Download decision source=libgen_md5 file=ready next=kindle_delivery")
    _emit("sending")
    await asyncio.to_thread(send_to_kindle, kindle_mail, book_path)

    _emit("done")


async def ebook_download_from_annas_md5(md5: str, kindle_mail: str, on_status=None) -> None:
    def _emit(status, **details):
        _emit_status(on_status, status, **details)

    logger.info(f"Download decision source=annas_archive md5={md5}")
    book_path: Path | None = None
    for attempt in range(2):
        try:
            if attempt == 0:
                _emit("downloading")
            _emit("downloading", source="annas_archive", attempt=attempt + 1)
            book_path = await download_book_from_annas_archive(md5, isbns=None, on_status=_emit)
            break
        except DownloadError:
            logger.warning(f"Anna direct download failed attempt={attempt + 1}/2 md5={md5}")
            if attempt == 0:
                await asyncio.sleep(2)
    if book_path is None:
        logger.warning(f"Anna direct download exhausted; trying LibGen mirror for md5={md5}")
        try:
            book_path = await _download_via_libgen({md5}, [md5], require_confirmation=False)
        except (ConnectionError, DownloadError, BookNotFoundError) as exc:
            raise DownloadError(f"Anna direct download failed for md5={md5}") from exc

    logger.info("Download decision source=annas_archive file=ready next=kindle_delivery")
    _emit("sending")
    await asyncio.to_thread(send_to_kindle, kindle_mail, book_path)

    _emit("done")


async def ebook_download(goodreads_url: str, kindle_mail: str, on_status=None) -> None:
    def _emit(status, **details):
        _emit_status(on_status, status, **details)

    _emit("fetching_isbn")
    _emit("fetching_isbn", source="goodreads")
    book_info = await get_book_info(goodreads_url)
    await ebook_download_from_metadata(
        book_info["isbn"],
        book_info["title"],
        kindle_mail,
        on_status=on_status,
        author=book_info.get("author", ""),
    )


async def _resolve_book_metadata(
    isbn: str, title: str, author: str, *, language: str = ""
) -> tuple[str, set[str]]:
    """Gather edition ISBNs from Google Books for the requested book.

    Returns ``(primary_isbn, all_isbns)``. The primary is the caller's ISBN
    when present, else the first Google-sourced one. Never raises: Google
    Books unavailability degrades to the caller-provided ISBN alone.
    """
    await _enrich_google_cache_for_book(isbn, title, author)
    edition_isbns = _google_edition_isbns(title, author, language=language)
    isbns = ({isbn} | edition_isbns) if isbn else set(edition_isbns)
    primary = isbn or (sorted(isbns)[0] if isbns else "")
    logger.info(
        f"Download decision edition_isbns primary={primary} total={len(isbns)} "
        f"google_editions={len(edition_isbns)}"
    )
    return primary, isbns


async def ebook_download_from_metadata(
    isbn: str, title: str, kindle_mail: str, on_status=None, author: str = "", language: str = ""
) -> None:
    def _emit(status, **details):
        _emit_status(on_status, status, **details)

    primary, isbns = await _resolve_book_metadata(isbn, title, author, language=language)
    logger.info(f"Download decision source=metadata isbn={primary} title={title!r} next=archive_search")
    _emit("searching")
    _emit("searching", source="annas_archive")
    all_hashes = await search_aa_all_formats(primary, title=title, author=author)
    epub_hashes = all_hashes.get("epub", [])
    pdf_hashes = all_hashes.get("pdf", [])
    mobi_hashes = all_hashes.get("mobi", [])

    if not epub_hashes and not pdf_hashes and not mobi_hashes:
        # Structured not-found record (W5): lets us later separate real
        # catalog gaps from search-strategy gaps. No PII beyond the query.
        logger.info(
            "Not-found record "
            f"query_title={title!r} query_author={author!r} isbn={isbn or '-'} "
            f"sources_tried=annas_archive search_query={(title + ' ' + author).strip() or isbn!r}"
        )
        logger.info("Download decision metadata_search=empty terminal=book_not_found")
        identifier = f"ISBN {isbn}" if isbn else f"title {title!r}"
        raise BookNotFoundError(f"No book found for {identifier}")

    logger.info(
        f"Download decision format_candidates epub={len(epub_hashes)} pdf={len(pdf_hashes)} mobi={len(mobi_hashes)}"
    )
    _emit("downloading")
    last_error: Exception | None = None
    fallback_error: ManualDownloadRequiredError | None = None
    book_path: Path | None = None

    # -- LibGen: epub --------------------------------------------------------
    if epub_hashes:
        try:
            logger.info("Download decision branch=libgen_epub")
            _emit("downloading", source="libgen", file_format="epub", attempt=1)
            book_path = await _download_via_libgen(isbns, epub_hashes, title=title, author=author)
            logger.info(f"Downloaded via LibGen (epub): {book_path.name}")
        except (ConnectionError, DownloadError, BookNotFoundError) as e:
            logger.warning(f"LibGen download (epub) failed: {e}")
            if isinstance(e, ManualDownloadRequiredError):
                fallback_error = e
            last_error = e

    # -- LibGen: pdf ---------------------------------------------------------
    if book_path is None and pdf_hashes:
        try:
            logger.info("Download decision branch=libgen_pdf reason=epub_unavailable_or_failed")
            _emit("downloading", source="libgen", file_format="pdf", attempt=1)
            book_path = await _download_via_libgen(isbns, pdf_hashes, title=title, author=author)
            logger.info(f"Downloaded via LibGen (pdf): {book_path.name}")
            last_error = None
            fallback_error = None
        except (ConnectionError, DownloadError, BookNotFoundError) as e:
            logger.warning(f"LibGen download (pdf) failed: {e}")
            if isinstance(e, ManualDownloadRequiredError):
                fallback_error = e
            last_error = e

    # -- LibGen: mobi (convert to epub/pdf) ----------------------------------
    if book_path is None and mobi_hashes:
        try:
            logger.info("Download decision branch=libgen_mobi reason=preferred_formats_unavailable_or_failed")
            _emit("downloading", source="libgen", file_format="mobi", attempt=1)
            book_path = await _download_via_libgen(isbns, mobi_hashes, title=title, author=author)
            book_path = await _try_convert_mobi(book_path)
            logger.info(f"Downloaded via LibGen (mobi→{book_path.suffix.lstrip('.')}): {book_path.name}")
            last_error = None
            fallback_error = None
        except (ConnectionError, DownloadError, BookNotFoundError) as e:
            logger.warning(f"LibGen download (mobi) failed: {e}")
            if isinstance(e, ManualDownloadRequiredError):
                fallback_error = e
            last_error = e

    # -- Anna's Archive: epub ------------------------------------------------
    if book_path is None and epub_hashes:
        try:
            logger.info("Download decision branch=annas_epub reason=libgen_paths_failed")
            _emit("trying_alternative", source="annas_archive", file_format="epub", attempt=1)
            book_path = await _download_via_annas_archive(epub_hashes, isbns=isbns, on_status=_emit)
            logger.info(f"Downloaded via Anna's Archive (epub): {book_path.name}")
            last_error = None
            fallback_error = None
        except DownloadError as e:
            logger.warning(f"Anna's Archive download (epub) failed: {e}")
            last_error = e

    # -- Anna's Archive: pdf -------------------------------------------------
    if book_path is None and pdf_hashes:
        try:
            logger.info("Download decision branch=annas_pdf reason=prior_paths_failed")
            _emit("trying_alternative", source="annas_archive", file_format="pdf", attempt=1)
            book_path = await _download_via_annas_archive(pdf_hashes, isbns=isbns, on_status=_emit)
            logger.info(f"Downloaded via Anna's Archive (pdf): {book_path.name}")
            last_error = None
            fallback_error = None
        except DownloadError as e:
            logger.warning(f"Anna's Archive download (pdf) failed: {e}")
            last_error = e

    # -- Anna's Archive: mobi (convert to epub/pdf) --------------------------
    if book_path is None and mobi_hashes:
        try:
            logger.info("Download decision branch=annas_mobi reason=prior_paths_failed")
            _emit("trying_alternative", source="annas_archive", file_format="mobi", attempt=1)
            book_path = await _download_via_annas_archive(mobi_hashes, isbns=isbns, on_status=_emit)
            book_path = await _try_convert_mobi(book_path)
            logger.info(f"Downloaded via Anna's Archive (mobi→{book_path.suffix.lstrip('.')}): {book_path.name}")
            last_error = None
            fallback_error = None
        except DownloadError as e:
            logger.warning(f"Anna's Archive download (mobi) failed: {e}")
            last_error = e

    if book_path is None:
        if fallback_error is not None:
            logger.info("Download decision terminal=manual_libgen_fallback")
            raise ManualDownloadRequiredError(
                f"All download attempts failed for {'ISBN ' + isbn if isbn else 'title ' + title!r}",
                fallback_url=fallback_error.fallback_url,
                fallback_message=fallback_error.fallback_message,
            ) from last_error
        logger.info("Download decision terminal=all_sources_failed")
        identifier = f"ISBN {isbn}" if isbn else f"title {title!r}"
        raise DownloadError(f"All download attempts failed for {identifier}") from last_error

    logger.info(f"Download decision file=ready format={book_path.suffix.lower()} next=kindle_delivery")
    _emit("sending")
    await asyncio.to_thread(send_to_kindle, kindle_mail, book_path)

    _emit("done")
