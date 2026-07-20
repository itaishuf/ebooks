import asyncio
import json
import logging
import os
import re
import smtplib
import time
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


async def _fetch_goodreads_page_with_flaresolverr(url: str) -> str:
    """Retrieve a Goodreads page through FlareSolverr when direct access is blocked."""
    hostname = urlsplit(url).hostname
    if hostname not in {"goodreads.com", "www.goodreads.com"}:
        logger.warning(f"Goodreads metadata fallback skipped for untrusted host={hostname!r}")
        return ""

    logger.info(f"Metadata decision source=goodreads fallback=flaresolverr url={url}")
    payload = {"cmd": "request.get", "url": url, "maxTimeout": 60_000}
    timeout = aiohttp.ClientTimeout(total=70)
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


@log_call
async def get_book_info(url: str) -> dict[str, str]:
    """Extract ISBN and title from a Goodreads book page.

    Returns ``{"isbn": "...", "title": "..."}``.
    Title may be empty if extraction fails.
    """
    logger.info(f"Metadata decision source=goodreads url={url} retrieval=direct")
    try:
        text = await _fetch_page_with_retry(url)
    except aiohttp.InvalidUrlClientError as e:
        raise InvalidURLError(f"Goodreads URL isn't valid: {url}") from e
    soup = BeautifulSoup(text, 'html.parser')

    isbn = ""
    title = ""

    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string)
            if not isbn:
                isbn = data.get('isbn', '')
            if not title:
                title = data.get('name', '')
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

    logger.info(f"Extracted book info: isbn={isbn}, title={title!r}")
    return {"isbn": isbn, "title": title}



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
async def search_aa_all_formats(isbn: str, title: str = "") -> dict[str, list[str]]:
    """Search Anna's Archive for all formats of a book.

    Searches by *title* (broad, finds all formats) rather than ISBN, because
    many AA records lack ISBN metadata.  Falls back to ISBN when no title is
    available.

    The ``ext`` search filter is intentionally omitted because AA's backend
    does not reliably honour it (confirmed by SearXNG maintainers).

    Returns ``{"epub": [md5, ...], "pdf": [...], "mobi": [...]}``.
    """
    query = title if title else isbn
    params = urlencode({"q": query, "lang": ["en", "he"]}, doseq=True)
    search_url = f"{settings.annas_archive_url}/search?{params}"

    logger.info(f"Searching AA for {query!r} (isbn={isbn})")

    html = await _fetch_page_with_retry(search_url)
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

    for outer in soup.find_all("div", class_="js-aarecord-list-outer"):
        record = next(
            (
                item
                for item in outer.find_all("div", class_="flex", recursive=False)
                if item.find("a", href=re.compile(r"/md5/[0-9a-f]{32}"))
            ),
            None,
        )
        if record is None:
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
        seen_md5s.add(md5)
        results.append(
            {
                "title": title,
                "author": author,
                "isbn": "",
                "cover_url": "",
                "md5": md5,
                "format": format_match.group(1).lower(),
                "language": language_match.group(1).lower() if language_match else "",
                "source": "annas_archive",
            }
        )

    logger.info(f"Anna metadata decisions selected={len(results)} unique_md5s={len(seen_md5s)}")
    return results


async def _search_aa_metadata(query: str) -> list[dict]:
    if not settings.annas_archive_url:
        raise RuntimeError("Anna's Archive mirror is not configured")
    params = urlencode({"q": query, "lang": ["en", "he"]}, doseq=True)
    search_url = f"{settings.annas_archive_url}/search?{params}"
    logger.info(f"Metadata decision source=annas_archive query={query!r} language_filter=['en', 'he']")
    html = await _fetch_page_with_retry(search_url)
    return _parse_aa_metadata_results(html)[:20]


@log_call
async def _fetch_google_books_search(query: str) -> list[dict]:
    if not settings.google_books_api_key:
        raise RuntimeError("Google Books API key is not configured")
    params = {
        "q": query,
        "maxResults": "20",
        "printType": "books",
        "key": settings.google_books_api_key,
    }
    timeout = aiohttp.ClientTimeout(total=10)
    logger.info(
        f"Metadata decision source=google_books query={query!r} api_host=www.googleapis.com "
        "language_filter=provider_ranked limit=20 timeout_seconds=10"
    )
    for attempt in range(3):
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session, session.get(
                _GOOGLE_BOOKS_VOLUMES_URL, params=params
            ) as response:
                logger.info(f"Google Books API response status={response.status}")
                if response.status >= 500:
                    raise RuntimeError(f"Google Books API returned HTTP {response.status}")
                response.raise_for_status()
                payload = await response.json()
            volumes = payload.get("items", [])
            if not isinstance(volumes, list):
                logger.warning("Google Books API response decision=invalid_items_payload")
                return []
            logger.info(f"Google Books API response volumes={len(volumes)}")
            return volumes
        except (aiohttp.ClientError, RuntimeError, TimeoutError, ValueError) as exc:
            logger.warning(
                f"Google Books API request failed attempt={attempt + 1}/3 error={exc.__class__.__name__}"
            )
            if attempt == 2:
                raise RuntimeError("Google Books metadata service is unavailable") from None
            await asyncio.sleep(2 ** (attempt + 1))
    raise RuntimeError("Google Books metadata service is unavailable")


def _normalize_isbn(value: object) -> str:
    isbn = re.sub(r"[\s-]", "", str(value)).upper()
    return isbn if _ISBN_RE.fullmatch(isbn) else ""


def _parse_google_books_results(volumes: list[dict]) -> list[dict]:
    results = []
    seen_isbns = set()
    skipped_missing_title = 0
    skipped_missing_isbn = 0
    skipped_duplicate_isbn = 0
    language_codes = set()
    for volume in volumes:
        volume_info = volume.get("volumeInfo", {})
        if not isinstance(volume_info, dict):
            skipped_missing_title += 1
            continue
        language = volume_info.get("language")
        if language:
            language_codes.add(str(language))
        identifiers = volume_info.get("industryIdentifiers", [])
        isbn = next(
            (
                normalized
                for identifier in identifiers
                if isinstance(identifier, dict)
                and identifier.get("type") in {"ISBN_13", "ISBN_10"}
                and (normalized := _normalize_isbn(identifier.get("identifier", ""))) not in seen_isbns
                and normalized
            ),
            "",
        )
        title = str(volume_info.get("title", "")).strip()
        if not title:
            skipped_missing_title += 1
            continue
        if not isbn:
            skipped_missing_isbn += 1
            continue
        if isbn in seen_isbns:
            skipped_duplicate_isbn += 1
            continue
        seen_isbns.add(isbn)
        authors = volume_info.get("authors", [])
        author = ", ".join(str(name) for name in authors if name) if isinstance(authors, list) else ""
        image_links = volume_info.get("imageLinks", {})
        thumbnail = image_links.get("thumbnail", "") if isinstance(image_links, dict) else ""
        cover_url = re.sub(r"^http://", "https://", str(thumbnail))
        results.append(
            {
                "title": title,
                "author": author,
                "isbn": isbn,
                "cover_url": cover_url,
                "md5": "",
                "format": "",
                "language": str(language or ""),
                "source": "google_books",
            }
        )
    logger.info(
        f"Google Books ISBN decisions volumes={len(volumes)} selected={len(results)} "
        f"skipped_missing_title={skipped_missing_title} skipped_missing_isbn={skipped_missing_isbn} "
        f"skipped_duplicate_isbn={skipped_duplicate_isbn} language_codes={sorted(language_codes)}"
    )
    return results


@log_call
async def search_books(query: str) -> list[dict]:
    try:
        google_results = _parse_google_books_results(await _fetch_google_books_search(query))
    except RuntimeError:
        logger.warning("Metadata fallback decision google_books=unavailable next=annas_archive")
        google_results = []
    if google_results:
        logger.info(f"Metadata provider decision selected=google_books results={len(google_results)}")
        return google_results

    logger.info("Metadata fallback decision google_books=no_selectable_isbn next=annas_archive")
    return await _search_aa_metadata(query)


async def _download_via_libgen(isbn: str, md5_list: list[str]) -> Path:
    logger.info(f"Download decision source=libgen identifier={isbn} candidates={len(md5_list)}")
    libgen_mirror = await choose_libgen_mirror()
    logger.info("Download decision source=libgen mirror=selected next=get_link")
    url = await get_libgen_link(isbn, md5_list, libgen_mirror)
    logger.info("Download decision source=libgen link=selected next=selenium")
    return await asyncio.to_thread(download_book_using_selenium, url)


async def _download_via_annas_archive(
    md5_list: list[str], isbn: str = "", on_status=None
) -> Path:
    if on_status:
        on_status("trying_alternative")
    last_error: Exception | None = None
    for md5 in md5_list:
        try:
            logger.info(f"Download decision source=annas_archive md5={md5} action=attempt")
            return await download_book_from_annas_archive(md5, isbn=isbn)
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
    def _emit(status):
        if on_status:
            on_status(status)

    logger.info(f"Download decision source=libgen_md5 md5={md5}")
    _emit("downloading")
    book_path = await _download_via_libgen(md5, [md5])

    logger.info("Download decision source=libgen_md5 file=ready next=kindle_delivery")
    _emit("sending")
    await asyncio.to_thread(send_to_kindle, kindle_mail, book_path)

    _emit("done")


async def ebook_download_from_annas_md5(md5: str, kindle_mail: str, on_status=None) -> None:
    def _emit(status):
        if on_status:
            on_status(status)

    logger.info(f"Download decision source=annas_archive md5={md5}")
    _emit("downloading")
    last_error: DownloadError | None = None
    book_path: Path | None = None
    for attempt in range(2):
        try:
            book_path = await download_book_from_annas_archive(md5)
            break
        except DownloadError as exc:
            last_error = exc
            logger.warning(f"Anna direct download failed attempt={attempt + 1}/2 md5={md5}")
            if attempt == 0:
                await asyncio.sleep(2)
    if book_path is None:
        logger.warning(f"Anna direct download exhausted; trying LibGen mirror for md5={md5}")
        try:
            book_path = await _download_via_libgen(md5, [md5])
        except (ConnectionError, DownloadError, BookNotFoundError) as exc:
            raise DownloadError(f"Anna direct download failed for md5={md5}") from exc

    logger.info("Download decision source=annas_archive file=ready next=kindle_delivery")
    _emit("sending")
    await asyncio.to_thread(send_to_kindle, kindle_mail, book_path)

    _emit("done")


async def ebook_download(goodreads_url: str, kindle_mail: str, on_status=None) -> None:
    def _emit(status):
        if on_status:
            on_status(status)

    _emit("fetching_isbn")
    book_info = await get_book_info(goodreads_url)
    await ebook_download_from_metadata(
        book_info["isbn"],
        book_info["title"],
        kindle_mail,
        on_status=on_status,
    )


async def ebook_download_from_metadata(isbn: str, title: str, kindle_mail: str, on_status=None) -> None:
    def _emit(status):
        if on_status:
            on_status(status)

    logger.info(f"Download decision source=metadata isbn={isbn} title={title!r} next=archive_search")
    _emit("searching")
    all_hashes = await search_aa_all_formats(isbn, title=title)
    epub_hashes = all_hashes.get("epub", [])
    pdf_hashes = all_hashes.get("pdf", [])
    mobi_hashes = all_hashes.get("mobi", [])

    if not epub_hashes and not pdf_hashes and not mobi_hashes:
        logger.info("Download decision metadata_search=empty terminal=book_not_found")
        raise BookNotFoundError(f"No book found for ISBN {isbn}")

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
            book_path = await _download_via_libgen(isbn, epub_hashes)
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
            book_path = await _download_via_libgen(isbn, pdf_hashes)
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
            book_path = await _download_via_libgen(isbn, mobi_hashes)
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
            book_path = await _download_via_annas_archive(epub_hashes, isbn=isbn, on_status=_emit)
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
            book_path = await _download_via_annas_archive(pdf_hashes, isbn=isbn, on_status=_emit)
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
            book_path = await _download_via_annas_archive(mobi_hashes, isbn=isbn, on_status=_emit)
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
                f"All download attempts failed for ISBN {isbn}",
                fallback_url=fallback_error.fallback_url,
                fallback_message=fallback_error.fallback_message,
            ) from last_error
        logger.info("Download decision terminal=all_sources_failed")
        raise DownloadError(f"All download attempts failed for ISBN {isbn}") from last_error

    logger.info(f"Download decision file=ready format={book_path.suffix.lower()} next=kindle_delivery")
    _emit("sending")
    await asyncio.to_thread(send_to_kindle, kindle_mail, book_path)

    _emit("done")
