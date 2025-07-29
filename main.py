import logging
import os
import re
import smtplib
import sys
import winreg
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import requests
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options

logger = logging.getLogger()
logging.basicConfig(filename="D:\documents\RandomScripts\ebookarr\log",
                    format='%(asctime)s %(message)s', level=logging.DEBUG)


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
    link = f'https://libgen.bz/get.php?md5={md5}'
    return link


def download_book(url):
    logger.debug(url)
    response = requests.get(url)
    text = response.text
    href = re.search('get.php.md5=.+?>', text).group()
    url = f'https://libgen.bz/{href}'

    logger.debug(url)
    ad_page = response.text

    href = re.search('get.php.md5=.+?>', ad_page).group()
    url = f'https://libgen.bz/{href}'
    logger.debug(url)

    response = requests.get(url)
    book = response.content
    logger.debug(book[0:20])
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
        smtp.login('itaishuf@gmail.com', os.getenv('GMAIL_API_PASSWORD'))
        smtp.send_message(msg)

    os.remove(book_path)


def download_book_using_selenium(url):
    options = Options()
    options.add_argument("--headless")
    driver = webdriver.Firefox(options=options)
    driver.get(url)
    elem = driver.find_element(By.XPATH, "/html/body/table/tbody/tr[1]/td[2]/a")
    elem.click()
    driver.close()
    book_path = find_newest_file_in_downloads()
    return book_path



def find_newest_file_in_downloads():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
        downloads_dir = Path(os.path.expandvars(
            winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]))
    try:
        files = [f for f in downloads_dir.iterdir() if f.is_file()]

        newest_file = max(files, key=os.path.getmtime)
        last_modified = datetime.fromtimestamp(
            os.path.getmtime(newest_file)).strftime('%Y-%m-%d %H:%M:%S')

        logger.debug({"file name": newest_file.name, "time": last_modified})
        return newest_file.absolute()
    except Exception as e:
        return f"Error processing directory '{downloads_dir}': {e}"


def main():
    url, email = get_goodreads_link()
    isbn = get_isbn(url)
    url = get_libgen_link(isbn)
    book_path = download_book_using_selenium(url)
    send_to_kindle(book_path, email)


main()
