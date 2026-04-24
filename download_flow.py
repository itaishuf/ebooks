import asyncio
import json
import logging
import os
import re
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, urlencode

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


@log_call
async def get_book_info(url: str) -> dict[str, str]:
    """Extract ISBN and title from a Goodreads book page.

    Returns ``{"isbn": "...", "title": "..."}``.
    Title may be empty if extraction fails.
    """
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
        match = re.search(r'isbn.{0,5}(\d{10,13})', text, re.IGNORECASE)
        if not match:
            raise BookNotFoundError(f"No ISBN found on page: {url}")
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
            with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
                smtp.login(settings.gmail_account, settings.gmail_password)
                smtp.send_message(msg)
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



@log_call
async def search_books(query: str) -> list[dict]:
    url = f'https://www.goodreads.com/search?q={quote(query)}'
    text = await _fetch_page_with_retry(url)
    soup = BeautifulSoup(text, 'html.parser')

    results = []
    for row in soup.find_all('tr', attrs={'itemtype': 'http://schema.org/Book'}):
        title_tag = row.find('a', class_='bookTitle')
        author_tag = row.find('a', class_='authorName')
        img_tag = row.find('img')

        if not title_tag:
            continue

        goodreads_url = f'https://www.goodreads.com{title_tag["href"]}'
        results.append({
            "title": title_tag.text.strip(),
            "author": author_tag.text.strip() if author_tag else "",
            "goodreads_url": goodreads_url,
            "cover_url": re.sub(r'\._S[XY]\d+_', '._SY475_', img_tag["src"]) if img_tag else "",
        })

    return results


async def _download_via_libgen(isbn: str, md5_list: list[str]) -> Path:
    libgen_mirror = await choose_libgen_mirror()
    url = await get_libgen_link(isbn, md5_list, libgen_mirror)
    return await asyncio.to_thread(download_book_using_selenium, url)


async def _download_via_annas_archive(md5_list: list[str], on_status=None) -> Path:
    if on_status:
        on_status("trying_alternative")
    last_error: Exception | None = None
    for md5 in md5_list:
        try:
            return await download_book_from_annas_archive(md5)
        except (DownloadError, Exception) as e:
            logger.warning(f"AA download failed for md5={md5}: {e}")
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

    _emit("downloading")
    book_path = await _download_via_libgen(md5, [md5])

    _emit("sending")
    await asyncio.to_thread(send_to_kindle, kindle_mail, book_path)

    _emit("done")


async def ebook_download(goodreads_url: str, kindle_mail: str, on_status=None) -> None:
    def _emit(status):
        if on_status:
            on_status(status)

    _emit("fetching_isbn")
    book_info = await get_book_info(goodreads_url)
    isbn = book_info["isbn"]
    title = book_info["title"]

    _emit("searching")
    all_hashes = await search_aa_all_formats(isbn, title=title)
    epub_hashes = all_hashes.get("epub", [])
    pdf_hashes = all_hashes.get("pdf", [])
    mobi_hashes = all_hashes.get("mobi", [])

    if not epub_hashes and not pdf_hashes and not mobi_hashes:
        raise BookNotFoundError(f"No book found for ISBN {isbn}")

    _emit("downloading")

    last_error: Exception | None = None
    fallback_error: ManualDownloadRequiredError | None = None
    book_path: Path | None = None

    # -- LibGen: epub --------------------------------------------------------
    if epub_hashes:
        try:
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
            book_path = await _download_via_annas_archive(epub_hashes, on_status=_emit)
            logger.info(f"Downloaded via Anna's Archive (epub): {book_path.name}")
            last_error = None
            fallback_error = None
        except DownloadError as e:
            logger.warning(f"Anna's Archive download (epub) failed: {e}")
            last_error = e

    # -- Anna's Archive: pdf -------------------------------------------------
    if book_path is None and pdf_hashes:
        try:
            book_path = await _download_via_annas_archive(pdf_hashes, on_status=_emit)
            logger.info(f"Downloaded via Anna's Archive (pdf): {book_path.name}")
            last_error = None
            fallback_error = None
        except DownloadError as e:
            logger.warning(f"Anna's Archive download (pdf) failed: {e}")
            last_error = e

    # -- Anna's Archive: mobi (convert to epub/pdf) --------------------------
    if book_path is None and mobi_hashes:
        try:
            book_path = await _download_via_annas_archive(mobi_hashes, on_status=_emit)
            book_path = await _try_convert_mobi(book_path)
            logger.info(f"Downloaded via Anna's Archive (mobi→{book_path.suffix.lstrip('.')}): {book_path.name}")
            last_error = None
            fallback_error = None
        except DownloadError as e:
            logger.warning(f"Anna's Archive download (mobi) failed: {e}")
            last_error = e

    if book_path is None:
        if fallback_error is not None:
            raise ManualDownloadRequiredError(
                f"All download attempts failed for ISBN {isbn}",
                fallback_url=fallback_error.fallback_url,
                fallback_message=fallback_error.fallback_message,
            ) from last_error
        raise DownloadError(f"All download attempts failed for ISBN {isbn}") from last_error

    _emit("sending")
    await asyncio.to_thread(send_to_kindle, kindle_mail, book_path)

    _emit("done")
