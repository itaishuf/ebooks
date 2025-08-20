import logging
import os
import re
import smtplib
import sys
from email.message import EmailMessage

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


async def ebook_download(goodreads_url, kindle_mail):
    isbn = get_isbn(goodreads_url)
    book_md5_list = get_book_md5(isbn)
    try:
        libgen_mirror = await choose_libgen_mirror()
        url = await get_libgen_link(isbn, book_md5_list, libgen_mirror)
        book_path = download_book_using_selenium(url)
        send_to_kindle(kindle_mail, book_path=book_path)
    except ConnectionError:
        """
        if there is a connection error, that means libgen is down.
        in that case we use annas archive api which costs money
        """
        uri = f'/dyn/api/fast_download.json?md5={book_md5_list[0]}&key={os.getenv("ANNAS_ARCHIVE_API_KEY")}'
        domain = 'annas-archive.org'
        response = requests.get(f'https://{domain}/{uri}')
        download_url = response.json()['download_url']
        file_data = requests.get(download_url).content
        send_to_kindle(kindle_mail, book_data=file_data, filename=f'{book_md5_list[0]}.epub')
