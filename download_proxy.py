"""Minimal HTTP download proxy that runs on the internal Docker network.

The ebookarr container routes all traffic through Tailscale, which means the
AA download CDN (e.g. wbsg8v.xyz) is unreachable from it.  This sidecar sits
on the internal network (direct internet, no Tailscale) and uses curl_cffi
with Chrome TLS impersonation to fetch the file and stream it back.
"""
import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urljoin, urlsplit

from aiohttp import ClientSession, ClientTimeout, web
from curl_cffi.requests import AsyncSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_MAX_REDIRECTS = 5


async def _is_safe_download_url(url: str) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    try:
        addresses = await asyncio.get_running_loop().getaddrinfo(
            parsed.hostname,
            parsed.port or 443,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(address[4][0]).is_global for address in addresses)


async def _fetch_safe_download(session: AsyncSession, url: str, headers: dict[str, str], cookies: dict[str, str]):
    current_url = url
    for redirect_count in range(_MAX_REDIRECTS + 1):
        if not await _is_safe_download_url(current_url):
            logger.warning(f"Proxy decision destination=rejected redirect_count={redirect_count}")
            raise ValueError("download URL is not a permitted public HTTPS destination")
        response = await session.get(
            current_url,
            headers=headers,
            cookies=cookies,
            allow_redirects=False,
            timeout=300,
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            logger.info(f"Proxy decision chrome_tls=response status={response.status_code} redirects={redirect_count}")
            return response
        location = response.headers.get("Location")
        if not location:
            logger.warning("Proxy decision redirect=rejected reason=missing_location")
            raise ValueError("download redirect is missing a location")
        logger.info(f"Proxy decision redirect=follow count={redirect_count + 1}/{_MAX_REDIRECTS}")
        current_url = urljoin(current_url, location)
    logger.warning("Proxy decision redirect=rejected reason=limit_exceeded")
    raise ValueError("download exceeded the redirect limit")


async def _fetch_safe_standard_download(
    session: ClientSession, url: str, headers: dict[str, str], cookies: dict[str, str]
):
    current_url = url
    for redirect_count in range(_MAX_REDIRECTS + 1):
        if not await _is_safe_download_url(current_url):
            logger.warning(f"Proxy decision standard_https=rejected reason=unsafe_destination redirects={redirect_count}")
            raise ValueError("download URL is not a permitted public HTTPS destination")
        response = await session.get(
            current_url,
            headers=headers,
            cookies=cookies,
            allow_redirects=False,
            timeout=ClientTimeout(total=300),
        )
        if response.status not in {301, 302, 303, 307, 308}:
            logger.info(f"Proxy decision standard_https=response status={response.status} redirects={redirect_count}")
            return response
        location = response.headers.get("Location")
        response.release()
        if not location:
            logger.warning("Proxy decision standard_https=rejected reason=missing_location")
            raise ValueError("download redirect is missing a location")
        current_url = urljoin(current_url, location)
    logger.warning("Proxy decision standard_https=rejected reason=redirect_limit")
    raise ValueError("download exceeded the redirect limit")


async def handle_download(request: web.Request) -> web.Response:
    url = request.rel_url.query.get("url", "")
    if not url:
        logger.warning("Proxy decision request=rejected reason=missing_url")
        return web.Response(status=400, text="url query parameter required")

    referer = request.rel_url.query.get("referer", "https://annas-archive.gl/")
    cookie_str = request.headers.get("X-Cookies", "")
    user_agent = request.headers.get("User-Agent", _CHROME_UA)

    cookies: dict[str, str] = {}
    for part in cookie_str.split("; "):
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()

    logger.info("Proxy decision request=accepted")
    try:
        headers = {"User-Agent": user_agent, "Referer": referer}
        try:
            async with AsyncSession(impersonate="chrome124") as session:
                resp = await _fetch_safe_download(session, url, headers, cookies)
        except Exception:
            logger.warning("Proxy decision chrome_tls=failed next=standard_https")
            async with ClientSession(headers=headers, cookies=cookies) as session:
                response = await _fetch_safe_standard_download(session, url, headers, cookies)
                content_type = response.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
                return web.Response(body=await response.read(), status=response.status, content_type=content_type)
        logger.info(f"Proxy decision chrome_tls=success status={resp.status_code}")
        content_type = resp.headers.get("Content-Type", "application/octet-stream").split(";", 1)[0]
        return web.Response(body=resp.content, status=resp.status_code, content_type=content_type)
    except Exception:
        logger.error("Proxy decision terminal=transport_failure")
        return web.Response(status=502, text="Download failed")


app = web.Application()
app.router.add_get("/download", handle_download)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8192, access_log=None)
