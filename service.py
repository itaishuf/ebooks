import asyncio
import contextvars
import logging
import os
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import UUID, uuid4

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, HttpUrl, field_validator

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

log_path = Path(settings.log_path)
log_path.parent.mkdir(parents=True, exist_ok=True)


class _JobIdFilter(logging.Filter):
    def filter(self, record):
        record.job_id = current_job_id.get("-")
        return True


_log_format = '%(asctime)s, [%(filename)s:%(lineno)s - %(funcName)s()], %(levelname)s, job=%(job_id)s, "%(message)s"'
_handler = RotatingFileHandler(str(log_path), maxBytes=5 * 1024 * 1024, backupCount=3)
_handler.setFormatter(logging.Formatter(_log_format))
_handler.addFilter(_JobIdFilter())

logging.basicConfig(level=logging.DEBUG, handlers=[_handler])
logger = logging.getLogger(__name__)

for _noisy in ("selenium", "urllib3", "aiohttp", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

jobs: dict[str, dict] = {}
_start_time: float = 0.0


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _start_time
    try:
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
    if not settings.api_key:
        logger.warning("API_KEY is not set — all endpoints are unauthenticated")
    await bootstrap_annas_archive_url()
    _start_time = time.monotonic()
    yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' https:; "
        "connect-src 'self'; "
        "font-src 'self'"
    )
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


def verify_api_key(api_key: str = Query(alias="key", default="")):
    if settings.api_key and api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


class Md5DownloadRequest(BaseModel):
    md5: str = Field(pattern=r'^[0-9a-fA-F]{32}$')
    ext: str = Field(default="epub", pattern=r'^(epub|pdf|mobi|azw3)$')
    kindle_mail: EmailStr


def _make_job() -> str:
    job_id = str(uuid4())
    jobs[job_id] = {
        "status": "queued",
        "error": None,
        "fallback": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return job_id


def _job_error_update(error: Exception) -> dict:
    update = {"status": "error", "error": str(error), "fallback": None}
    if isinstance(error, ManualDownloadRequiredError):
        update["fallback"] = {
            "url": error.fallback_url,
            "message": error.fallback_message,
        }
    return update


async def _run_job(job_id: str, coro) -> None:
    current_job_id.set(job_id)
    try:
        await coro
    except (InvalidURLError, BookNotFoundError) as e:
        logger.warning(e)
        jobs[job_id].update(_job_error_update(e))
    except (EmailDeliveryError, DownloadError) as e:
        logger.error(e)
        jobs[job_id].update(_job_error_update(e))
    except aiohttp.ClientError as e:
        logger.error(f"Network error: {e}", exc_info=True)
        jobs[job_id].update(
            status="error",
            error=f"Failed to connect to external service: {e}",
            fallback=None,
        )
    except Exception as e:
        logger.error(e, exc_info=True)
        jobs[job_id].update(status="error", error="Unexpected error processing request", fallback=None)


@app.get('/')
async def index():
    return FileResponse(
        static_dir / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get('/health')
async def health():
    download_dir = Path(settings.download_dir)
    dir_ok = download_dir.is_dir() and os.access(download_dir, os.W_OK)
    try:
        disk = shutil.disk_usage(download_dir)
        disk_free_mb = round(disk.free / (1024 * 1024))
    except OSError:
        disk_free_mb = None

    in_flight = sum(1 for j in jobs.values() if j["status"] not in ("done", "error"))
    uptime_s = round(time.monotonic() - _start_time) if _start_time else 0

    return {
        "status": "ok",
        "uptime_seconds": uptime_s,
        "jobs_in_flight": in_flight,
        "jobs_total": len(jobs),
        "download_dir_writable": dir_ok,
        "disk_free_mb": disk_free_mb,
    }


@app.get('/search')
async def search(
    q: str = Query(min_length=1, max_length=200),
    _: None = Depends(verify_api_key),
):
    try:
        results = await search_books(q.strip())
    except Exception as e:
        logger.error(f"Search failed for query '{q}': {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Search failed: {e}") from e
    return {"results": results}


@app.get('/download')
async def download_from_goodreads(
    request: DownloadRequest = Depends(),
    _: None = Depends(verify_api_key),
):
    job_id = _make_job()

    def on_status(s):
        jobs[job_id]["status"] = s

    asyncio.create_task(
        _run_job(job_id, ebook_download(str(request.goodreads_url), request.kindle_mail, on_status=on_status))
    )
    return {"job_id": job_id}


@app.get('/download/md5')
async def download_from_md5(
    request: Md5DownloadRequest = Depends(),
    _: None = Depends(verify_api_key),
):
    job_id = _make_job()

    def on_status(s):
        jobs[job_id]["status"] = s

    asyncio.create_task(
        _run_job(job_id, ebook_download_by_md5(request.md5, request.ext, request.kindle_mail, on_status=on_status))
    )
    return {"job_id": job_id}


@app.get('/jobs/{job_id}')
async def get_job(job_id: UUID, _: None = Depends(verify_api_key)):
    job_id = str(job_id)
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
