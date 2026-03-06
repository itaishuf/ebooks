import logging
from dataclasses import dataclass

from config import settings
from download_with_libgen import gather_page_status

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnnasArchiveBootstrapResult:
    selected_url: str
    healthy_url: str | None

    @property
    def used_fallback(self) -> bool:
        return self.healthy_url is None


async def bootstrap_annas_archive_url() -> AnnasArchiveBootstrapResult:
    status = await gather_page_status(settings.annas_archive_mirrors)
    mirror = next((url for url in status if url), None)
    if mirror:
        settings.annas_archive_url = mirror
        logger.info(f"Anna's Archive mirror selected: {mirror}")
        return AnnasArchiveBootstrapResult(selected_url=mirror, healthy_url=mirror)

    fallback_url = settings.annas_archive_mirrors[0]
    settings.annas_archive_url = fallback_url
    logger.warning(f"No Anna's Archive mirror responded; falling back to {fallback_url}")
    return AnnasArchiveBootstrapResult(selected_url=fallback_url, healthy_url=None)
