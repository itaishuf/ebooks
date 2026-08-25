"""Unit tests for the AA mirror selector and trawl circuit breaker.

These run offline: no network, no Selenium. They cover the state machines
that keep ebookarr serving when mirrors or trawl degrade.
"""

import json

import pytest

import mirror_selector
import trawl_breaker


# -- mirror selector ---------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_selector(monkeypatch, tmp_path):
    monkeypatch.setattr(mirror_selector, "_MIRROR_STATE_FILE", tmp_path / "mirror_state.json")
    monkeypatch.setattr(mirror_selector.settings, "annas_archive_mirrors", [
        "https://m1.test", "https://m2.test", "https://m3.test",
    ])
    mirror_selector.reset_mirror_state_for_tests()
    yield
    mirror_selector.reset_mirror_state_for_tests()


def test_best_mirror_is_first_when_no_history():
    assert mirror_selector.current_annas_archive_url() == "https://m1.test"


def test_failures_demote_mirror():
    mirror_selector.record_mirror_failure("https://m1.test")
    assert mirror_selector.current_annas_archive_url() != "https://m1.test"


def test_success_restores_priority():
    mirror_selector.record_mirror_failure("https://m1.test")
    mirror_selector.record_mirror_success("https://m1.test")
    assert mirror_selector.current_annas_archive_url() == "https://m1.test"


def test_cooldown_expires(monkeypatch):
    mirror_selector.record_mirror_failure("https://m1.test")
    mirror_selector.record_mirror_failure("https://m1.test")  # opens cooldown
    assert mirror_selector.current_annas_archive_url() != "https://m1.test"
    # Total-outage fallback: with all others also cooled down, list still served
    for url in ("https://m2.test", "https://m3.test"):
        mirror_selector.record_mirror_failure(url)
        mirror_selector.record_mirror_failure(url)
    assert mirror_selector.current_annas_archive_url() in mirror_selector.settings.annas_archive_mirrors


def test_state_persists_and_reload_skips_unknown_urls(tmp_path, monkeypatch):
    mirror_selector.record_mirror_failure("https://m1.test")
    raw = json.loads((tmp_path / "mirror_state.json").read_text())
    assert "https://m1.test" in raw
    # Unknown URLs in a corrupt file are dropped on load
    (tmp_path / "mirror_state.json").write_text(json.dumps({
        "https://evil.test": {"successes": 0, "failures": 99, "cooldown_until": 9e12},
    }))
    globals_reset = mirror_selector.reset_mirror_state_for_tests()
    mirror_selector._state_loaded = False
    mirror_selector._load_state()
    assert "https://evil.test" not in mirror_selector._mirror_state


def test_fetch_aa_html_falls_over_on_failure(monkeypatch):
    calls = []

    class _FakeResponse:
        status = 200
        request_info = None
        history = ()

        async def text(self):
            return "<html>ok</html>"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            calls.append(url)
            if url.startswith("https://m1.test"):
                raise TimeoutError("boom")
            return _FakeResponse()

    monkeypatch.setattr(mirror_selector.aiohttp, "ClientSession", _FakeSession)
    import asyncio
    html = asyncio.run(mirror_selector.fetch_aa_html("/md5/x"))
    assert any(u.startswith("https://m2.test") for u in calls), f"expected failover, calls={calls}"
    assert mirror_selector._mirror_state["https://m1.test"]["failures"] == 1


# -- trawl breaker -----------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_breaker():
    trawl_breaker.reset_for_tests()
    yield
    trawl_breaker.reset_for_tests()


def test_breaker_opens_after_two_consecutive_failures():
    trawl_breaker.record_trawl_failure("timeout")
    assert not trawl_breaker.is_trawl_down()
    trawl_breaker.record_trawl_failure("http_502")
    assert trawl_breaker.is_trawl_down()


def test_breaker_success_resets():
    trawl_breaker.record_trawl_failure("timeout")
    trawl_breaker.record_trawl_success()
    trawl_breaker.record_trawl_failure("timeout")
    assert not trawl_breaker.is_trawl_down(), "one failure after reset must not open"


def test_failures_while_open_do_not_extend():
    trawl_breaker.record_trawl_failure("timeout")
    trawl_breaker.record_trawl_failure("timeout")
    first_open_until = trawl_breaker._state["open_until"]
    trawl_breaker.record_trawl_failure("timeout")
    assert trawl_breaker._state["open_until"] == first_open_until
