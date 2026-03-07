from __future__ import annotations

import math
import shutil
import time
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from fastapi import HTTPException, Request, status

EMAIL_REPLACEMENT = "[redacted-email]"
URL_REPLACEMENT = "[redacted-url]"
SECRET_REPLACEMENT = "[redacted-secret]"
ALLOWED_AUTH_QUERY_PARAMS = {"access_token", "token", "apikey", "api_key", "key", "auth", "authorization"}
TRUSTED_PROXY_LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
TERMINAL_JOB_STATUSES = {"done", "error"}
IN_FLIGHT_JOB_STATUSES = {"queued", "fetching_isbn", "searching", "downloading", "sending"}


@dataclass(frozen=True)
class RateLimitPolicy:
    name: str
    limit: int
    window_seconds: int


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after_seconds: int


def sanitize_for_log(value: object) -> str:
    text = str(value)
    text = _replace_emails(text)
    text = _replace_urls(text)
    text = _replace_bearer_tokens(text)
    text = _replace_labeled_secrets(text)
    return text


def sanitize_error_detail(value: object, fallback_message: str) -> str:
    sanitized = sanitize_for_log(value).strip()
    if not sanitized:
        return fallback_message
    return sanitized


def reject_query_string_auth(request: Request) -> None:
    provided = ALLOWED_AUTH_QUERY_PARAMS.intersection({key.lower() for key in request.query_params.keys()})
    if not provided:
        return
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="Authentication query parameters are not supported; use the Authorization header.",
    )


def extract_client_ip(request: Request, trusted_proxy_ips: Iterable[str]) -> str:
    client_host = request.client.host if request.client else "unknown"
    trusted_sources = {host.strip() for host in trusted_proxy_ips if host.strip()}
    if client_host in TRUSTED_PROXY_LOCAL_HOSTS:
        trusted_sources.add(client_host)
    if client_host in trusted_sources:
        forwarded_for = request.headers.get("x-forwarded-for", "")
        if forwarded_for:
            first_ip = forwarded_for.split(",")[0].strip()
            if first_ip:
                return first_ip
        real_ip = request.headers.get("x-real-ip", "").strip()
        if real_ip:
            return real_ip
    return client_host


def rate_limit_exceeded(retry_after_seconds: int, detail: str = "Too many requests") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=detail,
        headers={"Retry-After": str(retry_after_seconds)},
    )


def enforce_job_admission(
    jobs: Mapping[str, dict],
    *,
    user_id: str,
    client_ip: str,
    max_in_flight_jobs: int,
    max_queued_jobs: int,
    max_jobs_per_user: int,
    max_jobs_per_ip: int,
    retry_after_seconds: int,
) -> None:
    in_flight_jobs = [job for job in jobs.values() if job.get("status") in IN_FLIGHT_JOB_STATUSES]
    queued_jobs = [job for job in in_flight_jobs if job.get("status") == "queued"]
    user_jobs = [job for job in in_flight_jobs if job.get("owner_user_id") == user_id]
    ip_jobs = [job for job in in_flight_jobs if job.get("client_ip") == client_ip]

    if len(in_flight_jobs) >= max_in_flight_jobs:
        raise rate_limit_exceeded(retry_after_seconds, "The download queue is full. Please try again shortly.")
    if len(queued_jobs) >= max_queued_jobs:
        raise rate_limit_exceeded(retry_after_seconds, "Too many download jobs are already queued.")
    if len(user_jobs) >= max_jobs_per_user:
        raise rate_limit_exceeded(retry_after_seconds, "You already have too many active download jobs.")
    if len(ip_jobs) >= max_jobs_per_ip:
        raise rate_limit_exceeded(retry_after_seconds, "This IP address already has too many active download jobs.")


def cleanup_expired_jobs(jobs: dict[str, dict], *, ttl_seconds: int, now: float | None = None) -> list[str]:
    if ttl_seconds <= 0:
        return []

    current_time = now if now is not None else time.time()
    removed_job_ids: list[str] = []
    for job_id, job in list(jobs.items()):
        if job.get("status") not in TERMINAL_JOB_STATUSES:
            continue
        finished_at = job.get("finished_at_epoch")
        if finished_at is None:
            continue
        if current_time - float(finished_at) < ttl_seconds:
            continue
        jobs.pop(job_id, None)
        removed_job_ids.append(job_id)
    return removed_job_ids


def cleanup_download_artifacts(download_dir: str, *, ttl_seconds: int, now: float | None = None) -> list[str]:
    if ttl_seconds <= 0:
        return []

    base_dir = Path(download_dir)
    if not base_dir.exists():
        return []

    current_time = now if now is not None else time.time()
    removed_paths: list[str] = []
    for path in base_dir.iterdir():
        try:
            age_seconds = current_time - path.stat().st_mtime
        except FileNotFoundError:
            continue
        if age_seconds < ttl_seconds:
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
        removed_paths.append(str(path))
    return removed_paths


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._events: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, policy: RateLimitPolicy, key: str) -> RateLimitResult:
        if policy.limit <= 0 or policy.window_seconds <= 0:
            return RateLimitResult(allowed=True, retry_after_seconds=0)

        now = time.monotonic()
        window_start = now - policy.window_seconds
        bucket_key = (policy.name, key)
        with self._lock:
            bucket = self._events[bucket_key]
            while bucket and bucket[0] <= window_start:
                bucket.popleft()

            if len(bucket) >= policy.limit:
                retry_after_seconds = max(1, math.ceil(bucket[0] + policy.window_seconds - now))
                return RateLimitResult(allowed=False, retry_after_seconds=retry_after_seconds)

            bucket.append(now)
            return RateLimitResult(allowed=True, retry_after_seconds=0)


def _replace_emails(text: str) -> str:
    import re

    return re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", EMAIL_REPLACEMENT, text, flags=re.IGNORECASE)


def _replace_urls(text: str) -> str:
    import re

    return re.sub(r"https?://[^\s\"'>]+", URL_REPLACEMENT, text, flags=re.IGNORECASE)


def _replace_bearer_tokens(text: str) -> str:
    import re

    text = re.sub(r"Bearer\s+[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+\.[A-Za-z0-9\-_=]+", f"Bearer {SECRET_REPLACEMENT}", text)
    return re.sub(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b", SECRET_REPLACEMENT, text)


def _replace_labeled_secrets(text: str) -> str:
    import re

    return re.sub(
        r"(?i)\b(password|secret|token|api[_-]?key|key)\b([\"'\s:=]+)([^\s,;]+)",
        lambda match: f"{match.group(1)}{match.group(2)}{SECRET_REPLACEMENT}",
        text,
    )
