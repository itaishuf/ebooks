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
            return "<html>" + ("ok " * 2000) + "</html>"  # > min useful size

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


class _FakeResponseFactory:
    """Builds fake aiohttp responses carrying arbitrary HTML."""

    def __init__(self, html_by_mirror):
        self.html_by_mirror = html_by_mirror
        self.calls = []
        self.post_calls = []

    def response(self, mirror):
        outer = self

        class _Resp:
            status = 200
            request_info = None
            history = ()

            async def text(self):
                return outer.html_by_mirror.get(mirror, "<html></html>")

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        return _Resp()

    def session(self):
        outer = self

        class _Sess:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def get(self, url, **kwargs):
                outer.calls.append(url)
                if url not in outer.html_by_mirror:
                    raise TimeoutError("boom")
                return outer.response(url)

            def post(self, url, json=None, **kwargs):
                outer.post_calls.append((url, json))
                long_html = "<html>" + ("trawl-results-html " * 400) + "</html>"

                class _PostResp:
                    status = 200

                    async def json(self, content_type=None):
                        return {"html": long_html, "mirror": "https://via.trawl"}

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, *exc):
                        return False

                return _PostResp()

        return _Sess


CHALLENGE_HTML = (
    '<html><head><title>Checking your browser before accessing</title></head>'
    '<body>ddos-guard js-challenge protection</body></html>'
)


def test_challenge_page_counts_as_failure_not_success(monkeypatch):
    factory = _FakeResponseFactory({
        "https://annas-archive.gs/search?q=x": CHALLENGE_HTML,
    })
    monkeypatch.setattr(mirror_selector.settings, "annas_archive_mirrors", [
        "https://annas-archive.gs",
    ])
    mirror_selector.reset_mirror_state_for_tests()
    monkeypatch.setattr(mirror_selector.aiohttp, "ClientSession", factory.session())

    import asyncio
    asyncio.run(mirror_selector.fetch_aa_html("/search?q=x"))

    state = mirror_selector._mirror_state["https://annas-archive.gs"]
    assert state["failures"] == 1, "challenge page must demote the mirror"
    assert state["successes"] == 0, "challenge page must never count as success"


# The actual shell annas-archive.gs served on 2026-08-26: HTTP 200, ~1159
# bytes, "Antibot solution" click-redirect to an adware domain.
ADWARE_SHELL_HTML = (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
    "<title>Loading...</title></head><body>"
    '<div class="b">Click for continue....</div>'
    '<div class="s">Antibot solution</div>'
    "<script>var d=[\"aHR0cHM6\",\"Ly9idWxz\",\"aXMubmV0\"];"
    "setTimeout(function(){location.href=atob(d.join(''))},1500);</script>"
    "</body></html>"
)


def test_adware_shell_mirror_counts_as_failure(monkeypatch):
    factory = _FakeResponseFactory({
        "https://annas-archive.gs/search?q=x": ADWARE_SHELL_HTML,
    })
    monkeypatch.setattr(mirror_selector.settings, "annas_archive_mirrors", [
        "https://annas-archive.gs",
    ])
    mirror_selector.reset_mirror_state_for_tests()
    monkeypatch.setattr(mirror_selector.aiohttp, "ClientSession", factory.session())

    import asyncio
    asyncio.run(mirror_selector.fetch_aa_html("/search?q=x"))

    state = mirror_selector._mirror_state["https://annas-archive.gs"]
    assert state["failures"] == 1 and state["successes"] == 0


def test_tiny_page_fails_even_without_known_markers(monkeypatch):
    tiny = "<html><body>weird new protection x</body></html>"  # < 5000 bytes
    factory = _FakeResponseFactory({
        "https://m1.test/search?q=x": tiny,
    })
    monkeypatch.setattr(mirror_selector.aiohttp, "ClientSession", factory.session())

    import asyncio
    html = asyncio.run(mirror_selector.fetch_aa_html("/search?q=x"))
    assert len(factory.calls) >= 1
    state = mirror_selector._mirror_state["https://m1.test"]
    assert state["successes"] == 0
    del html


def test_all_plain_mirrors_challenged_falls_back_to_trawl_search(monkeypatch):
    factory = _FakeResponseFactory({
        "https://m1.test/search?q=tolkien": CHALLENGE_HTML,
    })
    monkeypatch.setattr(mirror_selector.aiohttp, "ClientSession", factory.session())

    import asyncio
    html = asyncio.run(mirror_selector.fetch_aa_html("/search?q=tolkien"))
    assert "trawl-results-html" in html, "must fall back to trawl /aa/search"
    assert len(factory.post_calls) == 1
    post_url, post_json = factory.post_calls[0]
    assert post_url.endswith("/aa/search")
    assert post_json == {"query": "tolkien"}


def test_non_search_path_never_hits_trawl(monkeypatch):
    factory = _FakeResponseFactory({})  # everything fails
    monkeypatch.setattr(mirror_selector.aiohttp, "ClientSession", factory.session())

    import asyncio
    with pytest.raises(mirror_selector.AnnasArchiveUnreachableError):
        asyncio.run(mirror_selector.fetch_aa_html("/md5/deadbeef"))
    assert factory.post_calls == [], "md5 pages must not go through trawl search"


def test_trawl_breaker_open_blocks_fallback(monkeypatch):
    factory = _FakeResponseFactory({})
    monkeypatch.setattr(mirror_selector.aiohttp, "ClientSession", factory.session())
    trawl_breaker.record_trawl_failure("timeout")
    trawl_breaker.record_trawl_failure("timeout")  # opens the breaker

    import asyncio
    with pytest.raises(mirror_selector.AnnasArchiveUnreachableError):
        asyncio.run(mirror_selector.fetch_aa_html("/search?q=x"))
    assert factory.post_calls == []


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
