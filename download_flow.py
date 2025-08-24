import logging
import os
import re
import smtplib
import sys
import asyncio
from email.message import EmailMessage
from typing import Callable, Optional, Awaitable

import requests

from download_with_libgen import choose_libgen_mirror, get_libgen_link, \
    download_book_using_selenium
from utils import log_function_call

# redirect console output so script will run with pythonw
sys.stdout = open(os.devnull, 'w')
sys.stderr = open(os.devnull, 'w')

logger = logging.getLogger(__name__)


@log_function_call
def get_isbn(url: str) -> str:
    try:
        response = requests.get(url)
        text = response.text
        text = re.search(r'isbn...(\d+)', text).groups()[0]
        return text
    except requests.exceptions.MissingSchema:
        raise ConnectionRefusedError("Goodreads URL isn't valid")


@log_function_call
def send_to_kindle(email, book_path=None, book_data=b'', filename=''):
    msg = EmailMessage()
    msg['From'] = os.getenv('GMAIL_ACCOUNT')
    msg['To'] = email
    msg['Subject'] = 'book'

    # Attach file
    if book_path:
        with open(book_path, 'rb') as f:
            file_data = f.read()
            file_name = f.name
        os.remove(book_path)
    else:
        file_data = book_data
        file_name = filename
    logger.info(f"file size: {round(len(file_data) / 1000, 1)}KB")
    msg.add_attachment(file_data, maintype='application', subtype='octet-stream',
                       filename=file_name)
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(os.getenv('GMAIL_ACCOUNT'), os.getenv('GMAIL_PASSWORD'))
            smtp.send_message(msg)
    except smtplib.SMTPException:
        raise ConnectionRefusedError("Kindle mail isn't valid")


def get_book_md5(isbn):
    query = f'https://annas-archive.org/search?q={isbn}&ext=epub&lang=en&lang=he'
    response = requests.get(query)
    text = response.text
    hashes = re.findall('href...md5.([0-9a-f]+)', text)
    return hashes


async def ebook_download(
    goodreads_url: str,
    kindle_mail: str,
    progress_cb: Optional[Callable[[int, str], Optional[Awaitable[None]]]] = None,
):
    def _set_progress(percent: int, message: str) -> None:
        if not progress_cb:
            return
        try:
            res = progress_cb(percent, message)
            if asyncio.iscoroutine(res):
                asyncio.create_task(res)  # fire and forget
        except Exception:
            # Ignore progress errors to not break main flow
            pass

    # Main flow with non-blocking thread offloading
    _set_progress(5, "Fetching ISBN")
    isbn = await asyncio.to_thread(get_isbn, goodreads_url)

    _set_progress(20, "Searching sources")
    book_md5_list = await asyncio.to_thread(get_book_md5, isbn)

    try:
        _set_progress(40, "Choosing mirror")
        libgen_mirror = await choose_libgen_mirror()

        _set_progress(60, "Getting download link")
        url = await get_libgen_link(isbn, book_md5_list, libgen_mirror)

        _set_progress(85, "Downloading")
        book_path = await asyncio.to_thread(download_book_using_selenium, url)

        _set_progress(95, "Sending to Kindle")
        await asyncio.to_thread(send_to_kindle, kindle_mail, book_path=book_path)

        _set_progress(100, "Done")
    except ConnectionError:
        """
        if there is a connection error, that means libgen is down.
        in that case we use annas archive api which costs money
        """
        _set_progress(70, "Falling back to Anna's Archive")
        uri = f'/dyn/api/fast_download.json?md5={book_md5_list[0]}&key={os.getenv("ANNAS_ARCHIVE_API_KEY")}'
        domain = 'annas-archive.org'
        response = await asyncio.to_thread(requests.get, f'https://{domain}/{uri}')
        download_url = response.json()['download_url']
        file_resp = await asyncio.to_thread(requests.get, download_url)
        file_data = file_resp.content
        _set_progress(95, "Sending to Kindle")
        await asyncio.to_thread(send_to_kindle, kindle_mail, book_data=file_data, filename=f'{book_md5_list[0]}.epub')
        _set_progress(100, "Done")
