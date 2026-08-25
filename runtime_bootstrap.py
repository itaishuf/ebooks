import logging
from dataclasses import dataclass

from config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnnasArchiveBootstrapResult:
    selected_url: str
    healthy_url: str | None

    @property
    def used_fallback(self) -> bool:
        return self.healthy_url is None


async def bootstrap_annas_archive_url() -> AnnasArchiveBootstrapResult:
    """Seed mirror selection state.

    Since mirror_selector took over, no single URL is pinned at startup;
    per-request selection handles health. This keeps a settings URL populated
    for legacy checks and logs what the selector will try first.
    """
    from mirror_selector import current_annas_archive_url

    selected = current_annas_archive_url()
    settings.annas_archive_url = selected
    logger.info(f"Anna's Archive initial mirror: {selected} (per-request selection active)")
    return AnnasArchiveBootstrapResult(selected_url=selected, healthy_url=selected)
