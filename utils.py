import functools
import inspect
import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)

_REDACTED_KEYWORDS = {"password", "key", "secret", "token", "mail", "email"}


def _redact(value: str) -> str:
    return f"{value[:3]}***" if len(value) > 3 else "***"


def _redact_bound_args(func: Callable, args: tuple, kwargs: dict) -> dict[str, object]:
    """Redact all arguments (positional and keyword) whose parameter names contain sensitive words."""
    try:
        bound = inspect.signature(func).bind(*args, **kwargs)
        bound.apply_defaults()
    except (TypeError, ValueError):
        return {"args": args, "kwargs": kwargs}
    safe: dict[str, object] = {}
    for name, value in bound.arguments.items():
        if any(word in name.lower() for word in _REDACTED_KEYWORDS) and isinstance(value, str):
            safe[name] = _redact(value)
        else:
            safe[name] = value
    return safe


def _truncated_result(result) -> str:
    try:
        if len(result) > 8192:
            return str(result[:8192])
    except TypeError:
        pass
    return str(result)


def log_call(func: Callable) -> Callable:
    """Logging decorator that works for both sync and async functions."""
    if inspect.iscoroutinefunction(func):
        @functools.wraps(func)
        async def async_wrapper(*args, **kwargs):
            safe = _redact_bound_args(func, args, kwargs)
            logger.info(f"function name:{func.__name__}, arguments: {safe}")
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
            safe = _redact_bound_args(func, args, kwargs)
            logger.info(f"function name:{func.__name__}, arguments: {safe}")
            start_time = time.perf_counter()
            result = func(*args, **kwargs)
            elapsed = time.perf_counter() - start_time
            logger.info(
                f"function name:{func.__name__}, return value: {_truncated_result(result)}, duration: {elapsed:.4f}s")
            return result
        return sync_wrapper
