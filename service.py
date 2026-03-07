import asyncio
import contextvars
import logging
import time
import zoneinfo
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import UUID, uuid4

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, HttpUrl, field_validator

from abuse_protection import (
    RateLimitPolicy,
    SlidingWindowRateLimiter,
    cleanup_download_artifacts,
    cleanup_expired_jobs,
    enforce_job_admission,
    extract_client_ip,
    rate_limit_exceeded,
    reject_query_string_auth,
    sanitize_error_detail,
    sanitize_for_log,
)
from auth import AuthenticatedUser, get_current_user, validate_auth_settings
from bitwarden import fetch_secrets
from config import settings
from download_flow import ebook_download, ebook_download_by_md5, search_books
from exceptions import (
    BitwardenError,
    BookNotFoundError,
    DownloadError,
    EmailDeliveryError,
    InvalidURLError,
    ManualDownloadRequiredError,
)
from runtime_bootstrap import bootstrap_annas_archive_url

current_job_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_job_id", default="-")
current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_user_id", default="-")

log_path = Path(settings.log_path)
log_path.parent.mkdir(parents=True, exist_ok=True)

_TZ = zoneinfo.ZoneInfo("Asia/Jerusalem")


class _TZFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S") + f",{record.msecs:03.0f}"


class _RequestContextFilter(logging.Filter):
    def filter(self, record):
        record.msg = sanitize_for_log(record.getMessage())
        record.args = ()
        record.job_id = current_job_id.get("-")
        record.user_id = current_user_id.get("-")
        return True


_log_format = (
    '%(asctime)s, [%(filename)s:%(lineno)s - %(funcName)s()], %(levelname)s, '
    'job=%(job_id)s, user=%(user_id)s, "%(message)s"'
)
_handler = RotatingFileHandler(str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3)
_handler.setFormatter(_TZFormatter(_log_format))
_handler.addFilter(_RequestContextFilter())

logging.basicConfig(level=logging.DEBUG, handlers=[_handler])
logger = logging.getLogger(__name__)

for _noisy in ("selenium", "urllib3", "aiohttp", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

jobs: dict[str, dict] = {}
_start_time: float = 0.0
_last_cleanup_at: float = 0.0
_rate_limiter = SlidingWindowRateLimiter()
_download_semaphore: asyncio.Semaphore | None = None


def _content_security_policy() -> str:
    connect_sources = ["'self'"]
    if settings.supabase_url:
        connect_sources.append(settings.supabase_url.rstrip("/"))

    return (
        "default-src 'none'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "img-src 'self' https:; "
        f"connect-src {' '.join(connect_sources)}; "
        "font-src 'self' https://fonts.gstatic.com"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _download_semaphore, _start_time
    try:
        validate_auth_settings()
        fetch_secrets(settings)
    except BitwardenError as exc:
        raise SystemExit(
            f"\n  Bitwarden error: {exc}\n\n"
            "  Ebookarr could not fetch secrets from Bitwarden.\n"
            "  To fix this, either:\n"
            "    1. Verify the bootstrap Bitwarden credentials are set in the "
            "environment or minimal .env\n"
            "    2. Verify the configured Bitwarden item IDs still point to the "
            "correct vault items\n"
        ) from None
    except ValueError as exc:
        raise SystemExit(f"\n  Auth configuration error: {exc}\n") from None
    await bootstrap_annas_archive_url()
    _download_semaphore = asyncio.Semaphore(settings.max_concurrent_download_jobs)
    _start_time = time.monotonic()
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    try:
        reject_query_string_auth(request)
    except HTTPException as exc:
        response = JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = _content_security_policy()
        return response
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = _content_security_policy()
    return response


static_dir = Path(__file__).parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


class DownloadRequest(BaseModel):
    goodreads_url: HttpUrl
    kindle_mail: EmailStr

    @field_validator('goodreads_url')
    @classmethod
    def must_be_goodreads(cls, v):
        host = str(v.host or '')
        if host not in ('goodreads.com', 'www.goodreads.com'):
            raise ValueError('URL must be a goodreads.com link')
        return v


class Md5DownloadRequest(BaseModel):
    md5: str = Field(pattern=r'^[0-9a-fA-F]{32}$')
    ext: str = Field(default="epub", pattern=r'^(epub|pdf|mobi|azw3)$')
    kindle_mail: EmailStr


def require_authenticated_user(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    current_user_id.set(user.user_id)
    return user


def _make_job(owner: AuthenticatedUser | None = None, client_ip: str | None = None) -> str:
    now = datetime.now(timezone.utc)
    job_id = str(uuid4())
    jobs[job_id] = {
        "status": "queued",
        "error": None,
        "fallback": None,
        "created_at": now.isoformat(),
        "created_at_epoch": now.timestamp(),
        "finished_at_epoch": None,
        "owner_user_id": owner.user_id if owner else None,
        "owner_email": owner.email if owner else None,
        "client_ip": client_ip,
    }
    return job_id


def _job_error_update(error: Exception) -> dict:
    error_message = "Request failed"
    if isinstance(error, InvalidURLError):
        error_message = "Invalid Goodreads URL"
    elif isinstance(error, BookNotFoundError):
        error_message = "No matching book was found."
    elif isinstance(error, ManualDownloadRequiredError):
        error_message = "Automatic download failed after trying the available sources."
    elif isinstance(error, EmailDeliveryError):
        error_message = "The ebook was downloaded, but delivery to Kindle failed."
    elif isinstance(error, DownloadError):
        error_message = "The ebook download failed."

    update = {
        "status": "error",
        "error": sanitize_error_detail(error_message, "Request failed"),
        "fallback": None,
        "finished_at_epoch": time.time(),
    }
    if isinstance(error, ManualDownloadRequiredError):
        update["fallback"] = {
            "url": error.fallback_url,
            "message": error.fallback_message,
        }
    return update


def _download_semaphore_instance() -> asyncio.Semaphore:
    global _download_semaphore
    if _download_semaphore is None:
        _download_semaphore = asyncio.Semaphore(settings.max_concurrent_download_jobs)
    return _download_semaphore


def _public_job_payload(job: dict) -> dict:
    return {
        "status": job["status"],
        "error": job["error"],
        "fallback": job["fallback"],
        "created_at": job["created_at"],
    }


def _perform_maintenance(*, force: bool = False) -> None:
    global _last_cleanup_at
    now = time.time()
    if not force and now - _last_cleanup_at < settings.cleanup_interval_seconds:
        return

    removed_jobs = cleanup_expired_jobs(jobs, ttl_seconds=settings.job_ttl_seconds, now=now)
    removed_artifacts = cleanup_download_artifacts(
        settings.download_dir,
        ttl_seconds=settings.download_artifact_ttl_seconds,
        now=now,
    )
    if removed_jobs:
        logger.info(f"Removed {len(removed_jobs)} expired jobs from memory.")
    if removed_artifacts:
        logger.info(f"Removed {len(removed_artifacts)} expired download artifacts.")
    _last_cleanup_at = now


def _enforce_endpoint_rate_limits(
    *,
    endpoint_name: str,
    ip_policy: RateLimitPolicy,
    user_policy: RateLimitPolicy,
    client_ip: str,
    user: AuthenticatedUser,
) -> None:
    for policy, key in (
        (ip_policy, f"ip:{client_ip}"),
        (user_policy, f"user:{user.user_id}"),
    ):
        result = _rate_limiter.check(policy, key)
        if result.allowed:
            continue
        logger.warning(f"Rate limit hit for {endpoint_name} using {key}.")
        raise rate_limit_exceeded(result.retry_after_seconds)


async def _run_download_job(job_id: str, job_coro_factory) -> None:
    async with _download_semaphore_instance():
        await _run_job(job_id, job_coro_factory())


async def _run_job(job_id: str, coro) -> None:
    current_job_id.set(job_id)
    try:
        await coro
        jobs[job_id]["finished_at_epoch"] = time.time()
    except (InvalidURLError, BookNotFoundError) as e:
        logger.warning(f"Job failed with a request error: {sanitize_error_detail(e, 'Request failed')}")
        jobs[job_id].update(_job_error_update(e))
    except (EmailDeliveryError, DownloadError) as e:
        logger.error(f"Job failed while downloading or sending: {sanitize_error_detail(e, 'Download failed')}")
        jobs[job_id].update(_job_error_update(e))
    except aiohttp.ClientError as e:
        logger.error(f"Network error while processing job: {sanitize_error_detail(e, 'External service unavailable')}")
        jobs[job_id].update(
            status="error",
            error="Failed to connect to an external service.",
            fallback=None,
            finished_at_epoch=time.time(),
        )
    except Exception as e:
        logger.error(f"Unexpected job failure {e.__class__.__name__}: {sanitize_error_detail(e, 'Unexpected error')}")
        jobs[job_id].update(
            status="error",
            error="Unexpected error processing request.",
            fallback=None,
            finished_at_epoch=time.time(),
        )


@app.get('/')
async def index():
    return FileResponse(
        static_dir / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get('/health')
async def health():
    return {"status": "ok"}


@app.get('/auth/config')
async def auth_config():
    return {
        "supabase_url": settings.supabase_url,
        "supabase_publishable_key": settings.supabase_publishable_key,
    }


@app.get('/search')
async def search(
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _perform_maintenance()
    client_ip = extract_client_ip(request, settings.trusted_proxy_ips)
    _enforce_endpoint_rate_limits(
        endpoint_name="search",
        ip_policy=RateLimitPolicy(
            name="search:ip",
            limit=settings.search_rate_limit_per_ip,
            window_seconds=settings.search_rate_limit_window_seconds,
        ),
        user_policy=RateLimitPolicy(
            name="search:user",
            limit=settings.search_rate_limit_per_user,
            window_seconds=settings.search_rate_limit_window_seconds,
        ),
        client_ip=client_ip,
        user=user,
    )
    try:
        results = await search_books(q.strip())
    except Exception as e:
        logger.error(f"Search failed for query {sanitize_for_log(q)}: {sanitize_error_detail(e, 'Search failed')}")
        raise HTTPException(status_code=502, detail="Search is temporarily unavailable.") from e
    return {"results": results}


@app.post('/download')
async def download_from_goodreads(
    http_request: Request,
    payload: DownloadRequest,
    user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _perform_maintenance()
    client_ip = extract_client_ip(http_request, settings.trusted_proxy_ips)
    _enforce_endpoint_rate_limits(
        endpoint_name="download",
        ip_policy=RateLimitPolicy(
            name="download:ip",
            limit=settings.download_rate_limit_per_ip,
            window_seconds=settings.download_rate_limit_window_seconds,
        ),
        user_policy=RateLimitPolicy(
            name="download:user",
            limit=settings.download_rate_limit_per_user,
            window_seconds=settings.download_rate_limit_window_seconds,
        ),
        client_ip=client_ip,
        user=user,
    )
    enforce_job_admission(
        jobs,
        user_id=user.user_id,
        client_ip=client_ip,
        max_in_flight_jobs=settings.max_in_flight_jobs,
        max_queued_jobs=settings.max_queued_jobs,
        max_jobs_per_user=settings.max_jobs_per_user,
        max_jobs_per_ip=settings.max_jobs_per_ip,
        retry_after_seconds=settings.overload_retry_after_seconds,
    )
    job_id = _make_job(user, client_ip=client_ip)
    logger.info(f"Created Goodreads download job for {user.user_id}")

    def on_status(s):
        if job_id in jobs:
            jobs[job_id]["status"] = s

    asyncio.create_task(
        _run_download_job(
            job_id,
            lambda: ebook_download(str(payload.goodreads_url), payload.kindle_mail, on_status=on_status),
        )
    )
    return {"job_id": job_id}


@app.post('/download/md5')
async def download_from_md5(
    http_request: Request,
    payload: Md5DownloadRequest,
    user: AuthenticatedUser = Depends(require_authenticated_user),
):
    _perform_maintenance()
    client_ip = extract_client_ip(http_request, settings.trusted_proxy_ips)
    _enforce_endpoint_rate_limits(
        endpoint_name="download-md5",
        ip_policy=RateLimitPolicy(
            name="download-md5:ip",
            limit=settings.download_rate_limit_per_ip,
            window_seconds=settings.download_rate_limit_window_seconds,
        ),
        user_policy=RateLimitPolicy(
            name="download-md5:user",
            limit=settings.download_rate_limit_per_user,
            window_seconds=settings.download_rate_limit_window_seconds,
        ),
        client_ip=client_ip,
        user=user,
    )
    enforce_job_admission(
        jobs,
        user_id=user.user_id,
        client_ip=client_ip,
        max_in_flight_jobs=settings.max_in_flight_jobs,
        max_queued_jobs=settings.max_queued_jobs,
        max_jobs_per_user=settings.max_jobs_per_user,
        max_jobs_per_ip=settings.max_jobs_per_ip,
        retry_after_seconds=settings.overload_retry_after_seconds,
    )
    job_id = _make_job(user, client_ip=client_ip)
    logger.info(f"Created MD5 download job for {user.user_id}")

    def on_status(s):
        if job_id in jobs:
            jobs[job_id]["status"] = s

    asyncio.create_task(
        _run_download_job(
            job_id,
            lambda: ebook_download_by_md5(payload.md5, payload.ext, payload.kindle_mail, on_status=on_status),
        )
    )
    return {"job_id": job_id}


@app.get('/jobs/{job_id}')
async def get_job(job_id: UUID, request: Request, user: AuthenticatedUser = Depends(require_authenticated_user)):
    _perform_maintenance()
    client_ip = extract_client_ip(request, settings.trusted_proxy_ips)
    _enforce_endpoint_rate_limits(
        endpoint_name="jobs",
        ip_policy=RateLimitPolicy(
            name="jobs:ip",
            limit=settings.job_poll_rate_limit_per_ip,
            window_seconds=settings.job_poll_rate_limit_window_seconds,
        ),
        user_policy=RateLimitPolicy(
            name="jobs:user",
            limit=settings.job_poll_rate_limit_per_user,
            window_seconds=settings.job_poll_rate_limit_window_seconds,
        ),
        client_ip=client_ip,
        user=user,
    )
    job_id = str(job_id)
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    if jobs[job_id]["owner_user_id"] != user.user_id:
        logger.warning(f"Rejected access to foreign job {job_id}")
        raise HTTPException(status_code=404, detail="Job not found")
    return _public_job_payload(jobs[job_id])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
