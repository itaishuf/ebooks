import asyncio
import shutil

import pytest

from bitwarden import fetch_secrets
from config import settings
from exceptions import BitwardenError
from runtime_bootstrap import bootstrap_annas_archive_url


def _fail_e2e_prerequisites(messages: list[str]) -> None:
    formatted = "\n".join(f"- {message}" for message in messages)
    pytest.fail(f"E2E gate prerequisites not satisfied:\n{formatted}", pytrace=False)


@pytest.fixture(scope="session")
def e2e_runtime_bootstrap():
    missing = []
    if not settings.test_goodreads_url:
        missing.append("`test_goodreads_url` is not configured")
    if missing:
        _fail_e2e_prerequisites(missing)

    bootstrap = asyncio.run(bootstrap_annas_archive_url())
    if bootstrap.used_fallback:
        _fail_e2e_prerequisites(
            [
                "No Anna's Archive mirror is reachable, so direct search tests would run "
                f"against the fallback URL {bootstrap.selected_url} instead of a healthy mirror"
            ]
        )
    return bootstrap


@pytest.fixture(autouse=True)
def _bootstrap_marked_e2e_tests(request):
    if request.node.get_closest_marker("e2e") is not None:
        request.getfixturevalue("e2e_runtime_bootstrap")


@pytest.fixture
def delivery_prerequisites(e2e_runtime_bootstrap):
    missing = []
    if not settings.gmail_password:
        try:
            fetch_secrets(settings)
        except BitwardenError as exc:
            missing.append(f"could not load `gmail_password` from Bitwarden: {exc}")
    if not settings.gmail_password:
        missing.append("`gmail_password` is not configured")
    if not settings.test_kindle_email:
        missing.append("`test_kindle_email` is not configured")
    if shutil.which("firefox") is None:
        missing.append("Firefox is not installed or not available on PATH")
    if missing:
        _fail_e2e_prerequisites(missing)
    return e2e_runtime_bootstrap
