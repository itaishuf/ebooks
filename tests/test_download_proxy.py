import logging
import sys
import types

import pytest
from aiohttp.test_utils import make_mocked_request

if "curl_cffi.requests" not in sys.modules:
    curl_cffi = types.ModuleType("curl_cffi")
    curl_requests = types.ModuleType("curl_cffi.requests")
    curl_requests.AsyncSession = object
    curl_cffi.requests = curl_requests
    sys.modules["curl_cffi"] = curl_cffi
    sys.modules["curl_cffi.requests"] = curl_requests

import download_proxy


@pytest.mark.asyncio
async def test_proxy_transport_failure_does_not_expose_signed_url(monkeypatch, caplog):
    signed_url = "https://download.example/file.epub?signature=secret-value"

    class FailingChromeSession:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            raise RuntimeError(signed_url)

        async def __aexit__(self, *_args):
            return False

    async def fail_standard(*_args, **_kwargs):
        raise RuntimeError(signed_url)

    monkeypatch.setattr(download_proxy, "AsyncSession", FailingChromeSession)
    monkeypatch.setattr(download_proxy, "_fetch_safe_standard_download", fail_standard)
    caplog.set_level(logging.INFO, logger="download_proxy")

    response = await download_proxy.handle_download(
        make_mocked_request("GET", f"/download?url={signed_url}")
    )

    assert response.status == 502
    assert response.text == "Download failed"
    assert signed_url not in caplog.text
    assert "chrome_tls=failed next=standard_https" in caplog.text
