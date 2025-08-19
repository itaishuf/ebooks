import asyncio
import logging
import os
import re
import smtplib
import sys
import time
import winreg
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

import aiohttp
import requests
import selenium.common
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.firefox.options import Options

from utils import log_function_call, log_coroutine_call, find_newest_file_in_downloads
from download_with_libgen import choose_libgen_mirror, get_libgen_link, download_book_using_selenium