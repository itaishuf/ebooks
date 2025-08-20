import logging
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from pathlib import Path

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
def send_to_kindle(book_path: Path, email: str):
    msg = EmailMessage()
    msg['From'] = os.getenv('GMAIL_ACCOUNT')
    msg['To'] = email
    msg['Subject'] = 'book'

    # Attach file
    with open(book_path, 'rb') as f:
        file_data = f.read()
        file_name = f.name
        logger.info(f"file size: {round(len(file_data) / 1000, 1)}KB")
    msg.add_attachment(file_data, maintype='application', subtype='octet-stream',
                       filename=file_name)
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
            smtp.login(os.getenv('GMAIL_ACCOUNT'), os.getenv('GMAIL_PASSWORD'))
            smtp.send_message(msg)
    except smtplib.SMTPException:
        raise ConnectionRefusedError("Kindle mail isn't valid")

    os.remove(book_path)


async def ebook_download(goodreads_url, kindle_mail):
    # libgen_mirror = await choose_libgen_mirror()
    # isbn = get_isbn(goodreads_url)
    # url = await get_libgen_link(isbn, libgen_mirror)
    url = 'https://libgen.la/get.php?md5=584eaab7bcd4eddd760270deaff87c37'
    book_path = download_book_using_selenium(url)
    send_to_kindle(book_path, kindle_mail)
