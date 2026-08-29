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
    from mirror_selector import current_annas_archive_url, refresh_slum_mirrors

    # Warm the open-slum.org mirror pool before first selection. Identical to
    # the per-request refresh in fetch_aa_html, but guarantees sync callers
    # (Selenium search, slow-download URL construction) see openslum's live
    # AA domains instead of the hardcoded emergency fallback on first use.
    await refresh_slum_mirrors()

    selected = current_annas_archive_url()
    settings.annas_archive_url = selected
    logger.info(f"Anna's Archive initial mirror: {selected} (per-request selection active)")
    return AnnasArchiveBootstrapResult(selected_url=selected, healthy_url=selected)
