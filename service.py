import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiohttp
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, HttpUrl, field_validator

from config import settings
from download_flow import ebook_download, ebook_download_by_md5, search_books
from exceptions import BitwardenError, BookNotFoundError, DownloadError, EmailDeliveryError, InvalidURLError

log_path = Path(settings.log_path)
log_path.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(log_path),
    level=logging.DEBUG,
    format='%(asctime)s, [%(filename)s:%(lineno)s - %(funcName)s()], %(levelname)s, "%(message)s"',
)
logger = logging.getLogger(__name__)

jobs: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    try:
        # fetch_secrets(settings)
        # the bw server is down so for develpoment purposes only i will hardcode the gmail api key
        pass
    except BitwardenError:
        logger.critical("Failed to fetch secrets from Bitwarden — refusing to start", exc_info=True)
        raise
    yield


app = FastAPI(lifespan=lifespan)

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
    md5: str = Field(min_length=1, max_length=64)
    ext: str = Field(default="epub", pattern=r'^(epub|pdf|mobi|azw3)$')
    kindle_mail: EmailStr


def _make_job() -> str:
    job_id = str(uuid4())
    jobs[job_id] = {
        "status": "queued",
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return job_id


async def _run_job(job_id: str, coro) -> None:
    try:
        await coro
    except (InvalidURLError, BookNotFoundError) as e:
        logger.warning(e)
        jobs[job_id].update(status="error", error=str(e))
    except (EmailDeliveryError, DownloadError) as e:
        logger.error(e)
        jobs[job_id].update(status="error", error=str(e))
    except aiohttp.ClientError as e:
        logger.error(f"Network error: {e}", exc_info=True)
        jobs[job_id].update(status="error", error=f"Failed to connect to external service: {e}")
    except Exception as e:
        logger.error(e, exc_info=True)
        jobs[job_id].update(status="error", error="Unexpected error processing request")


@app.get('/')
async def index():
    return FileResponse(static_dir / "index.html")


@app.get('/health')
async def health():
    return {"status": "ok"}


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
async def get_job(job_id: str, _: None = Depends(verify_api_key)):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
