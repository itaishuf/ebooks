import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, EmailStr, HttpUrl

from bitwarden import fetch_secrets
from config import settings
from download_flow import ebook_download
from exceptions import BitwardenError, BookNotFoundError, DownloadError, EmailDeliveryError, InvalidURLError

log_path = Path(settings.log_path)
log_path.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(log_path),
    level=logging.DEBUG,
    format='%(asctime)s, [%(filename)s:%(lineno)s - %(funcName)s()], %(levelname)s, "%(message)s"',
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    try:
        fetch_secrets(settings)
    except BitwardenError:
        logger.critical("Failed to fetch secrets from Bitwarden — refusing to start", exc_info=True)
        raise
    yield


app = FastAPI(lifespan=lifespan)


class DownloadRequest(BaseModel):
    goodreads_url: HttpUrl
    kindle_mail: EmailStr


def verify_api_key(api_key: str = Query(alias="key", default="")):
    if settings.api_key and api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")


@app.get('/')
async def handler(
    request: DownloadRequest = Depends(),
    _: None = Depends(verify_api_key),
):
    try:
        await ebook_download(str(request.goodreads_url), request.kindle_mail)
        return "success, check your inbox for confirmation"
    except (InvalidURLError, BookNotFoundError) as e:
        logger.warning(e)
        raise HTTPException(status_code=404, detail=str(e))
    except EmailDeliveryError as e:
        logger.error(e)
        raise HTTPException(status_code=502, detail=str(e))
    except DownloadError as e:
        logger.error(e)
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        logger.error(e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Unexpected error processing request",
        )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
