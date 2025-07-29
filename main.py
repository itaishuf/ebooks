import selenium
import requests
import re
import smtplib
from email.message import EmailMessage
import sys
import logging

logger = logging.getLogger()
logging.basicConfig(filename="D:\documents\RandomScripts\ebookarr\log", format='%(asctime)s %(message)s', level=logging.DEBUG)


def get_goodreads_link():
    link = sys.argv[1]
    email = sys.argv[2]
    logger.debug(link)
    return link, email


def get_isbn(url):
    logger.debug(url)
    response = requests.get(url)
    text = response.text
    text = re.search('isbn...(\d+)', text).groups()[0]
    return text


def get_libgen_link(isbn):
    query = f'https://annas-archive.org/search?q={isbn}&ext=epub'
    logger.debug(query)
    response = requests.get(query)
    text = response.text
    md5 = re.search('href...md5.([0-9a-f]+)', text).groups()[0]
    link = f'https://libgen.li/get.php?md5={md5}'
    return link


def download_book(url):
    logger.debug(url)
    response = requests.get(url)
    text = response.text
    href = re.search('get.php.md5=.+?>', text).group()
    url = f'https://libgen.li/{href}'

    logger.debug(url)
    response = requests.get(url)
    book = response.content
    with open('book.epub', 'wb') as f:
        f.write(book)
    return 'book.epub'


def send_to_kindle(book_path, email):
    msg = EmailMessage()
    msg['From'] = 'itaishuf@gmail.com'
    msg['To'] = email
    msg['Subject'] = 'book'

    # Attach file
    with open(book_path, 'rb') as f:
        file_data = f.read()
        file_name = f.name
    msg.add_attachment(file_data, maintype='application', subtype='octet-stream',
                       filename=file_name)

    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as smtp:
        smtp.login('itaishuf@gmail.com', 'gftq pksv sogx hlns')
        smtp.send_message(msg)


def main():
    url, email = get_goodreads_link()
    isbn = get_isbn(url)
    url = get_libgen_link(isbn)
    book_path = download_book(url)
    send_to_kindle(book_path, email)


main()
