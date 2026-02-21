import asyncio
import functools
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from config import settings

logger = logging.getLogger(__name__)

_REDACTED_KEYWORDS = {"password", "key", "secret", "token", "mail", "email"}


def _redact(value: str) -> str:
    return f"{value[:3]}***" if len(value) > 3 else "***"


def _redact_args(args: tuple, kwargs: dict) -> tuple[tuple, dict]:
    """Redact argument values whose keyword names contain sensitive words."""
    safe_kwargs = {}
    for k, v in kwargs.items():
        if any(word in k.lower() for word in _REDACTED_KEYWORDS) and isinstance(v, str):
            safe_kwargs[k] = _redact(v)
        else:
            safe_kwargs[k] = v
    return args, safe_kwargs


def _truncated_result(result) -> str:
    try:
        if len(result) > 8192:
            return str(result[:8192])
    except TypeError:
        pass
    return str(result)


def log_call(func: Callable) -> Callable:
    """Logging decorator that works for both sync and async functions."""
    if asyncio.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            safe_args, safe_kwargs = _redact_args(args, kwargs)
            logger.info(f"function name:{func.__name__}, function arguments: {safe_args}, function keyword arguments: {safe_kwargs}")
            start_time = time.perf_counter()
            result = await func(*args, **kwargs)
            elapsed = time.perf_counter() - start_time
            logger.info(
                f"function name:{func.__name__}, return value: {_truncated_result(result)}, duration: {elapsed:.4f}s")
            return result
        return async_wrapper
    else:
        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            safe_args, safe_kwargs = _redact_args(args, kwargs)
            logger.info(f"function name:{func.__name__}, function arguments: {safe_args}, function keyword arguments: {safe_kwargs}")
            start_time = time.perf_counter()
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start_time
            logger.info(
                f"function name:{func.__name__}, return value: {_truncated_result(result)}, duration: {elapsed:.4f}s")
            return result
        return sync_wrapper


@log_call
def find_newest_file_in_downloads() -> Path:
    downloads_dir = Path(settings.download_dir)
    try:
        files = [f for f in downloads_dir.iterdir() if f.is_file()]

        newest_file = max(files, key=os.path.getmtime)
        last_modified = datetime.fromtimestamp(
            os.path.getmtime(newest_file))
        if datetime.now() - last_modified > timedelta(minutes=settings.selenium_download_timeout_minutes):
            raise FileNotFoundError()
        last_modified = last_modified.strftime('%Y-%m-%d %H:%M:%S')

        logger.info({"file name": newest_file.name, "time": last_modified})
        return newest_file.absolute()
    except Exception as e:
        raise FileNotFoundError("Error locating the file downloaded with Selenium") from e
