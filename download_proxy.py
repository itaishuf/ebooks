"""Minimal HTTP download proxy that runs on the internal Docker network.

The ebookarr container routes all traffic through Tailscale, which means the
AA download CDN (e.g. wbsg8v.xyz) is unreachable from it.  This sidecar sits
on the internal network (direct internet, no Tailscale) and uses curl_cffi
with Chrome TLS impersonation to fetch the file and stream it back.
"""
import logging

from aiohttp import web
from curl_cffi.requests import AsyncSession

from abuse_protection import sanitize_error_detail, sanitize_for_log

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def handle_download(request: web.Request) -> web.Response:
    url = request.rel_url.query.get("url", "")
    if not url:
        return web.Response(status=400, text="url query parameter required")

    referer = request.rel_url.query.get("referer", "https://annas-archive.gl/")
    cookie_str = request.headers.get("X-Cookies", "")
    user_agent = request.headers.get("User-Agent", _CHROME_UA)

    cookies: dict[str, str] = {}
    for part in cookie_str.split("; "):
        if "=" in part:
            k, _, v = part.partition("=")
            cookies[k.strip()] = v.strip()

    safe_url = sanitize_for_log(url[:80])
    logger.info(f"Proxying download: {safe_url} cookies={list(cookies.keys())}")
    try:
        async with AsyncSession(impersonate="chrome124") as session:
            resp = await session.get(
                url,
                headers={"User-Agent": user_agent, "Referer": referer},
                cookies=cookies,
                allow_redirects=True,
                timeout=300,
            )
        logger.info(f"Proxy got HTTP {resp.status_code} for {safe_url}")
        content_type = resp.headers.get("Content-Type", "application/octet-stream")
        return web.Response(body=resp.content, status=resp.status_code, content_type=content_type)
    except Exception as e:
        logger.error(f"Proxy error for {safe_url}: {sanitize_for_log(e)}")
        return web.Response(status=502, text=sanitize_error_detail(e, "Download failed"))


app = web.Application()
app.router.add_get("/download", handle_download)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8192)
