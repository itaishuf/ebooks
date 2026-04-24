import logging
import re
from pathlib import Path
from urllib.parse import urlencode, urlparse

import aiohttp
from bs4 import BeautifulSoup

from config import settings
from exceptions import DownloadError
from utils import log_call

logger = logging.getLogger(__name__)

# AA's countdown timer is ~60 s; 70 gives a buffer for slow page renders.
AA_COUNTDOWN_WAIT_S = 70
# Total FlareSolverr budget: DDoS-Guard JS challenge (~10 s) + countdown wait + network.
FLARESOLVERR_TIMEOUT_MS = 120_000

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
}


def _extract_filename(content_disposition: str, url: str, md5: str) -> str:
    """Derive a filename from Content-Disposition, the URL, or the MD5 hash."""
    if "filename=" in content_disposition:
        part = content_disposition.split("filename=")[-1].strip().strip('"').strip("'")
        if part:
            return part
    url_path = url.split("?")[0].rstrip("/").split("/")[-1]
    return url_path if "." in url_path else f"{md5}.epub"


async def _fetch_md5_page(md5: str) -> str:
    """Fetch the AA MD5 detail page HTML (single shared fetch for all callers)."""
    md5_url = f"{settings.annas_archive_url}/md5/{md5}"
    logger.info(f"Fetching AA MD5 page for md5={md5}")
    async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
        async with session.get(md5_url, allow_redirects=True) as resp:
            return await resp.text()


async def _try_internet_archive(md5: str, html: str) -> Path | None:
    """Try downloading the book directly from Internet Archive.

    Parses the pre-fetched AA MD5 page HTML for an archive.org/details link,
    then downloads from archive.org/download/{item}/{item}.{ext}.
    Returns the saved Path on success, None if no IA source is found.
    Raises DownloadError only if an IA source is found but every download attempt fails.
    """
    soup = BeautifulSoup(html, "html.parser")
    ia_link = soup.find("a", href=re.compile(r"https://archive\.org/details/([^/?#]+)"))
    if not ia_link:
        logger.info(f"No Internet Archive source on AA MD5 page for md5={md5}")
        return None

    item_id = re.search(r"https://archive\.org/details/([^/?#]+)", ia_link["href"]).group(1)
    logger.info(f"Found Internet Archive item for md5={md5}: {item_id}")

    output_dir = Path(settings.download_dir) / f"aa-{md5[:8]}"
    output_dir.mkdir(parents=True, exist_ok=True)

    for ext in ("epub", "pdf"):
        ia_url = f"https://archive.org/download/{item_id}/{item_id}.{ext}"
        logger.info(f"Trying IA download: {ia_url}")
        try:
            async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
                async with session.get(
                    ia_url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=300)
                ) as resp:
                    if resp.status == 200:
                        file_path = output_dir / f"{item_id}.{ext}"
                        file_path.write_bytes(await resp.read())
                        size_kb = round(file_path.stat().st_size / 1000, 1)
                        logger.info(f"IA download complete: {file_path.name} ({size_kb} KB)")
                        return file_path
                    logger.info(f"IA returned HTTP {resp.status} for {ia_url}")
        except Exception as e:
            logger.warning(f"IA download failed for {ia_url}: {e}")

    raise DownloadError(f"Internet Archive item {item_id} found but all format attempts failed")


def _get_slow_download_url(md5: str, html: str) -> str:
    """Extract the slow_download URL from pre-fetched AA MD5 page HTML."""
    soup = BeautifulSoup(html, "html.parser")
    link = soup.find("a", href=re.compile(r"/slow_download/"))
    if not link:
        anchors = [a.get("href", "") for a in soup.find_all("a", href=True)]
        logger.warning(f"No slow_download link on AA MD5 page for {md5}. Anchors: {anchors[:20]}")
        raise DownloadError(f"No slow_download link found on AA page for md5={md5}")

    href = link["href"]
    url = href if href.startswith("http") else f"{settings.annas_archive_url}{href}"
    logger.info(f"Found AA slow_download URL for md5={md5}: {url}")
    return url


async def _solve_and_get_download_link(md5: str, slow_url: str) -> tuple[dict, str, str]:
    """Use FlareSolverr to bypass the DDoS-Guard JS challenge on the AA slow-download
    page and extract the actual download URL from the rendered HTML.

    Returns (all_cookies, user_agent, absolute_download_url).
    """
    logger.info(f"Sending AA slow-download URL to FlareSolverr: {slow_url}")

    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{settings.flaresolverr_url}/v1",
            json={
                "cmd": "request.get",
                "url": slow_url,
                "maxTimeout": FLARESOLVERR_TIMEOUT_MS,
                "waitInSeconds": AA_COUNTDOWN_WAIT_S,
            },
        )
        data = await resp.json()

    status = data.get("status")
    if status != "ok":
        raise DownloadError(
            f"FlareSolverr returned status={status!r} for md5={md5}: {data.get('message', '')}"
        )

    solution = data["solution"]
    all_cookies = {c["name"]: c["value"] for c in solution.get("cookies", [])}
    user_agent = solution["userAgent"]

    html = solution["response"]
    soup = BeautifulSoup(html, "html.parser")

    anchors = [(a.get("id", ""), a.get("class", ""), a.get("href", "")) for a in soup.find_all("a")]
    logger.info(f"FlareSolverr rendered anchors for md5={md5}: {anchors}")

    btn = soup.find(id="download-button")
    if not btn or not btn.get("href"):
        # Fall back to any anchor whose href looks like a direct download path.
        btn = next(
            (
                a
                for a in soup.find_all("a", href=True)
                if "/dl/" in a["href"] or a["href"].endswith((".epub", ".pdf", ".mobi", ".azw3"))
            ),
            None,
        )

    if not btn or not btn.get("href"):
        raise DownloadError(
            f"No download link found in FlareSolverr-rendered page for md5={md5}. "
            f"Anchors found: {anchors[:10]}. "
            "The AA countdown timer may not have elapsed within the wait window. "
            f"Consider increasing AA_COUNTDOWN_WAIT_S (currently {AA_COUNTDOWN_WAIT_S}s)."
        )

    href = btn["href"]
    download_url = href if href.startswith("http") else f"{settings.annas_archive_url}{href}"
    logger.info(f"Extracted AA download URL for md5={md5}: {download_url}")
    # Return all cookies — DDoS-Guard uses __ddg* names, not cf_clearance.
    logger.info(f"FlareSolverr cookies for md5={md5}: {list(all_cookies.keys())}")
    return all_cookies, user_agent, download_url


@log_call
async def download_book_from_annas_archive(md5: str) -> Path:
    """Download an ebook from Anna's Archive.

    Strategy:
    1. Fetch the AA MD5 page once to extract both the IA link (if any) and the
       slow_download URL.
    2. Try Internet Archive directly (fast, no bot protection) if an IA source is
       linked from the page.
    3. Fall back to the FlareSolverr slow-download path + download-proxy sidecar
       for books not on IA.
    """
    html = await _fetch_md5_page(md5)

    ia_path = await _try_internet_archive(md5, html)
    if ia_path:
        return ia_path

    logger.info(f"No IA source for md5={md5}, falling back to AA slow download via FlareSolverr")
    slow_url = _get_slow_download_url(md5, html)
    all_cookies, user_agent, download_url = await _solve_and_get_download_link(md5, slow_url)

    output_dir = Path(settings.download_dir) / f"aa-{md5[:8]}"
    output_dir.mkdir(parents=True, exist_ok=True)

    _parsed = urlparse(download_url)
    logger.info(f"Requesting AA file via proxy: host={_parsed.netloc} path={_parsed.path[:60]}")

    cookie_str = "; ".join(f"{k}={v}" for k, v in all_cookies.items())
    proxy_url = (
        f"{settings.download_proxy_url}/download?"
        + urlencode({"url": download_url, "referer": f"{settings.annas_archive_url}/"})
    )

    async with aiohttp.ClientSession() as session:
        async with session.get(
            proxy_url,
            headers={"User-Agent": user_agent, "X-Cookies": cookie_str},
            timeout=aiohttp.ClientTimeout(total=360),
        ) as resp:
            logger.info(f"Proxy response: HTTP {resp.status} for host={_parsed.netloc}")
            if resp.status != 200:
                body = await resp.text()
                raise DownloadError(
                    f"Download proxy returned HTTP {resp.status} for host={_parsed.netloc}: {body[:200]}"
                )
            content_disp = resp.headers.get("Content-Disposition", "")
            filename = _extract_filename(content_disp, download_url, md5)
            file_path = output_dir / filename
            file_path.write_bytes(await resp.read())

    size_kb = round(file_path.stat().st_size / 1000, 1)
    logger.info(f"AA download complete: {file_path.name} ({size_kb} KB)")
    return file_path
