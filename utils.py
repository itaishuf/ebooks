import logging
import os
import time
import winreg
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def log_coroutine_call(func):
    async def async_wrapper(*args, **kwargs):
        logger.info(f"function name:{func.__name__}, function arguments: {args}, function keyword arguments: {kwargs}")
        start_time = time.perf_counter()
        result = await func(*args, **kwargs)
        end_time = time.perf_counter()
        logger.info(
            f"function name:{func.__name__}, return value: {result}, duration: {end_time - start_time:.4f}s")
        return result

    return async_wrapper


def log_function_call(func):
    def sync_wrapper(*args, **kwargs):
        logger.info(f"function name:{func.__name__}, function arguments: {args}, function keyword arguments: {kwargs}")
        start_time = time.perf_counter()
        result = func(*args, **kwargs)
        end_time = time.perf_counter()
        try:
            length = len(result)
            if length > 8192:
                logger.info(
                    f"function name:{func.__name__}, return value: {result[:8192]}, duration: {end_time - start_time:.4f}s")
            else:
                logger.info(
                    f"function name:{func.__name__}, return value: {result}, duration: {end_time - start_time:.4f}s")
        except TypeError:
            logger.info(
                f"function name:{func.__name__}, return value: {result}, duration: {end_time - start_time:.4f}s")
        return result

    return sync_wrapper


@log_function_call
def find_newest_file_in_downloads() -> Path:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as key:
        downloads_dir = Path(os.path.expandvars(
            winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]))
    try:
        files = [f for f in downloads_dir.iterdir() if f.is_file()]

        newest_file = max(files, key=os.path.getmtime)
        last_modified = datetime.fromtimestamp(
            os.path.getmtime(newest_file))
        if datetime.now() - last_modified > timedelta(minutes=10):
            raise FileNotFoundError()
        last_modified = last_modified.strftime('%Y-%m-%d %H:%M:%S')

        logger.info({"file name": newest_file.name, "time": last_modified})
        return newest_file.absolute()
    except Exception as e:
        raise FileNotFoundError("Error locating the file downloaded with Selenium")
