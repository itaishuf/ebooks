import logging
from dataclasses import dataclass

import aiohttp

from config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnnasArchiveBootstrapResult:
    selected_url: str
    healthy_url: str | None

    @property
    def used_fallback(self) -> bool:
        return self.healthy_url is None


async def _find_healthy_annas_archive_mirror() -> str | None:
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        for mirror in settings.annas_archive_mirrors:
            try:
                async with session.get(f"{mirror}/search?q=the") as response:
                    page = await response.text()
            except (aiohttp.ClientError, TimeoutError):
                continue
            if response.status == 200 and "for sale" not in page and "domain may" not in page:
                return mirror
    return None


async def bootstrap_annas_archive_url() -> AnnasArchiveBootstrapResult:
    mirror = await _find_healthy_annas_archive_mirror()
    if mirror:
        settings.annas_archive_url = mirror
        logger.info(f"Anna's Archive mirror selected: {mirror}")
        return AnnasArchiveBootstrapResult(selected_url=mirror, healthy_url=mirror)

    fallback_url = "https://annas-archive.gl"
    settings.annas_archive_url = fallback_url
    logger.warning(f"No Anna's Archive mirror responded; using {fallback_url} (search via FlareSolverr)")
    return AnnasArchiveBootstrapResult(selected_url=fallback_url, healthy_url=None)
