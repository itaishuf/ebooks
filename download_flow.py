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
async def get_isbn(url: str) -> str:
    try:
        text = await _fetch_page_with_retry(url)
    except aiohttp.InvalidUrlClientError as e:
        raise InvalidURLError(f"Goodreads URL isn't valid: {url}") from e
    soup = BeautifulSoup(text, 'html.parser')

    # Try JSON-LD structured data first (most reliable)
    for script in soup.find_all('script', type='application/ld+json'):
        try:
            data = json.loads(script.string)
            isbn = data.get('isbn')
            if isbn:
                return isbn
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue

    # Fallback: search page text for ISBN patterns
    match = re.search(r'isbn.{0,5}(\d{10,13})', text, re.IGNORECASE)
    if not match:
        raise BookNotFoundError(f"No ISBN found on page: {url}")
    return match.group(1)


@log_call
def send_to_kindle(email: str, book_path: Path | None = None,
                   book_data: bytes = b'', filename: str = ''):
    msg = EmailMessage()
    msg['From'] = settings.gmail_account
    msg['To'] = email
    msg['Subject'] = 'book'

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
async def get_book_md5(isbn: str, ext: str = "epub") -> list[str]:
    params = urlencode({"q": isbn, "ext": ext, "lang": ["en", "he"]}, doseq=True)
    query = f'{settings.annas_archive_url}/search?{params}'
    text = await _fetch_page_with_retry(query)
    soup = BeautifulSoup(text, 'html.parser')
    hashes = []
    seen_hashes = set()
    for a_tag in soup.find_all('a', href=re.compile(r'/md5/[0-9a-f]+')):
        match = re.search(r'/md5/([0-9a-f]+)', a_tag['href'])
        if match and match.group(1) not in seen_hashes:
            seen_hashes.add(match.group(1))
            hashes.append(match.group(1))
    return hashes


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


async def ebook_download_by_md5(md5: str, ext: str, kindle_mail: str, on_status=None) -> None:
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
    isbn = await get_isbn(goodreads_url)

    _emit("searching")
    epub_hashes = await get_book_md5(isbn, ext="epub")
    if not epub_hashes:
        logger.warning(f"No epub results for ISBN {isbn}, falling back to pdf")

    _emit("downloading")

    last_error: Exception | None = None
    fallback_error: ManualDownloadRequiredError | None = None
    book_path: Path | None = None

    if epub_hashes:
        try:
            book_path = await _download_via_libgen(isbn, epub_hashes)
        except (ConnectionError, DownloadError, BookNotFoundError) as e:
            logger.warning(f"LibGen download (epub) failed: {e}")
            if isinstance(e, ManualDownloadRequiredError):
                fallback_error = e
            last_error = e

    if not epub_hashes or last_error:
        pdf_hashes = await get_book_md5(isbn, ext="pdf")
        if pdf_hashes:
            try:
                book_path = await _download_via_libgen(isbn, pdf_hashes)
                last_error = None
                fallback_error = None
            except (ConnectionError, DownloadError, BookNotFoundError) as e:
                logger.warning(f"LibGen download (pdf) failed: {e}")
                if isinstance(e, ManualDownloadRequiredError):
                    fallback_error = e
                last_error = e
        elif not epub_hashes:
            raise BookNotFoundError(f"No book found for ISBN {isbn}")

    if last_error or book_path is None:
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
