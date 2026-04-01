from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.testclient import TestClient

import service
from abuse_protection import SlidingWindowRateLimiter
from auth import AuthenticatedUser

APP_BASE_URL = "https://testserver"


@pytest.fixture
def client(monkeypatch):
    service.jobs.clear()
    service.app.dependency_overrides.clear()
    service._rate_limiter = SlidingWindowRateLimiter()
    service._download_semaphore = None
    service._last_cleanup_at = 0.0

    async def fake_bootstrap():
        return None

    monkeypatch.setattr(service, "validate_auth_settings", lambda: None)
    monkeypatch.setattr(service, "fetch_secrets", lambda _settings: None)
    monkeypatch.setattr(service, "bootstrap_annas_archive_url", fake_bootstrap)
    monkeypatch.setattr(service.settings, "google_client_id", "google-client-id")
    monkeypatch.setattr(service.settings, "google_client_secret", "google-client-secret")
    monkeypatch.setattr(service.settings, "session_secret", "test-session-secret")
    monkeypatch.setattr(service.settings, "app_base_url", APP_BASE_URL)
    monkeypatch.setattr(service.settings, "session_https_only", False)
    monkeypatch.setattr(service.settings, "require_verified_email", True)

    with TestClient(service.app, base_url=APP_BASE_URL) as test_client:
        yield test_client

    service.jobs.clear()
    service.app.dependency_overrides.clear()


def _google_claims(
    *,
    user_id: str = "user-1",
    email: str = "reader@example.com",
    email_verified: bool = True,
    name: str = "Reader Example",
):
    return {
        "sub": user_id,
        "email": email,
        "email_verified": email_verified,
        "name": name,
    }


def _same_origin_headers() -> dict[str, str]:
    return {"Origin": APP_BASE_URL}


class FakeGoogleClient:
    def __init__(self, claims: dict | None = None):
        self.claims = claims or _google_claims()
        self.last_redirect_uri: str | None = None
        self.last_prompt: str | None = None

    async def authorize_redirect(self, request, redirect_uri: str, prompt: str | None = None):
        self.last_redirect_uri = redirect_uri
        self.last_prompt = prompt
        return RedirectResponse("https://accounts.google.com/o/oauth2/auth", status_code=302)

    async def authorize_access_token(self, request):
        return {"userinfo": self.claims}

    async def parse_id_token(self, request, token):
        return self.claims


def _install_google_client(monkeypatch, *, claims: dict | None = None) -> FakeGoogleClient:
    fake_client = FakeGoogleClient(claims=claims)
    monkeypatch.setattr(service, "_build_google_oauth_client", lambda: fake_client)
    return fake_client


def _sign_in_browser(client: TestClient, monkeypatch, **claim_overrides) -> None:
    _install_google_client(monkeypatch, claims=_google_claims(**claim_overrides))
    response = client.get("/auth/google/callback", follow_redirects=False)
    assert response.status_code == 302


def _make_auth_request(session: dict | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/auth/google/callback",
            "raw_path": b"/auth/google/callback",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 443),
            "session": session or {},
        }
    )


def test_public_routes_remain_accessible(client):
    assert client.get("/").status_code == 200
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    auth_session = client.get("/auth/session")
    assert auth_session.status_code == 200
    assert auth_session.json() == {"authenticated": False, "user": None}


def test_google_login_redirects_using_app_callback_url(client, monkeypatch):
    fake_client = _install_google_client(monkeypatch)

    response = client.get("/auth/google/login", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "https://accounts.google.com/o/oauth2/auth"
    assert fake_client.last_redirect_uri == f"{APP_BASE_URL}/auth/google/callback"
    assert fake_client.last_prompt == "select_account"


def test_google_callback_stores_session_and_exposes_authenticated_user(client, monkeypatch):
    _sign_in_browser(client, monkeypatch, user_id="google-sub-123", email="reader@gmail.com")

    auth_session = client.get("/auth/session")

    assert auth_session.status_code == 200
    assert auth_session.json() == {
        "authenticated": True,
        "user": {
            "id": "google-sub-123",
            "email": "reader@gmail.com",
            "email_verified": True,
            "name": "Reader Example",
        },
    }


@pytest.mark.asyncio
async def test_google_callback_logs_authenticated_email_once(monkeypatch):
    logged_messages = []
    _install_google_client(monkeypatch, claims=_google_claims(user_id="google-sub-123", email="reader@gmail.com"))

    def fake_info(message, *args, **kwargs):
        logged_messages.append((message, kwargs))

    monkeypatch.setattr(service.logger, "info", fake_info)

    response = await service.auth_google_callback(_make_auth_request())

    assert response.status_code == 302
    assert (
        "Authenticated Google user email reader@gmail.com",
        {"extra": {"allow_email_log": True}},
    ) in logged_messages


def test_google_callback_rejects_unverified_email(client, monkeypatch):
    _sign_in_browser(client, monkeypatch, email_verified=False)

    auth_session = client.get("/auth/session")

    assert auth_session.status_code == 200
    assert auth_session.json() == {"authenticated": False, "user": None}


def test_logout_clears_session(client, monkeypatch):
    _sign_in_browser(client, monkeypatch)

    response = client.post("/auth/logout", headers=_same_origin_headers())

    assert response.status_code == 200
    assert response.json() == {"status": "signed_out"}
    assert client.get("/auth/session").json() == {"authenticated": False, "user": None}


def test_logout_rejects_cross_site_requests(client):
    response = client.post("/auth/logout", headers={"Origin": "https://evil.example"})

    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-site requests are not allowed."


@pytest.mark.parametrize(
    ("method", "url", "kwargs"),
    [
        ("get", "/search?q=dune", {}),
        (
            "post",
            "/download",
            {
                "json": {
                    "goodreads_url": "https://www.goodreads.com/book/show/4671",
                    "kindle_mail": "reader@example.com",
                }
            },
        ),
        (
            "post",
            "/download/md5",
            {
                "json": {
                    "md5": "0123456789abcdef0123456789abcdef",
                    "ext": "epub",
                    "kindle_mail": "reader@example.com",
                }
            },
        ),
        ("get", f"/jobs/{uuid4()}", {}),
    ],
)
def test_protected_routes_require_authentication(client, method, url, kwargs):
    response = getattr(client, method)(url, **kwargs)

    assert response.status_code == 401
    assert response.json()["detail"] == "Authentication required"


def test_search_uses_cookie_authenticated_session(client, monkeypatch):
    _sign_in_browser(client, monkeypatch)

    async def fake_search_books(query: str):
        assert query == "dune"
        return [{"title": "Dune"}]

    monkeypatch.setattr(service, "search_books", fake_search_books)

    response = client.get("/search?q=dune")

    assert response.status_code == 200
    assert response.json() == {"results": [{"title": "Dune"}]}


def test_search_rejects_query_string_auth(client):
    response = client.get("/search?q=dune&access_token=leaky-token")

    assert response.status_code == 400
    assert response.json()["detail"] == "Authentication query parameters are not supported."


def test_search_is_rate_limited_per_ip(client, monkeypatch):
    _sign_in_browser(client, monkeypatch)
    monkeypatch.setattr(service.settings, "search_rate_limit_per_ip", 1)
    monkeypatch.setattr(service.settings, "search_rate_limit_per_user", 10)

    async def fake_search_books(_query: str):
        return [{"title": "Dune"}]

    monkeypatch.setattr(service, "search_books", fake_search_books)

    first = client.get("/search?q=dune")
    second = client.get("/search?q=dune")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"]


def test_download_creates_user_owned_job(client, monkeypatch):
    _sign_in_browser(client, monkeypatch, user_id="user-1")

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(service.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(service, "ebook_download", lambda *_args, **_kwargs: None)

    response = client.post(
        "/download",
        json={
            "goodreads_url": "https://www.goodreads.com/book/show/4671",
            "kindle_mail": "reader@example.com",
        },
        headers=_same_origin_headers(),
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert service.jobs[job_id]["owner_user_id"] == "user-1"
    assert service.jobs[job_id]["owner_email"] == "reader@example.com"
    assert service.jobs[job_id]["client_ip"] == "testclient"


def test_download_md5_uses_post_body(client, monkeypatch):
    _sign_in_browser(client, monkeypatch, user_id="user-1")

    def fake_create_task(coro):
        coro.close()
        return None

    monkeypatch.setattr(service.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(service, "ebook_download_by_md5", lambda *_args, **_kwargs: None)

    response = client.post(
        "/download/md5",
        json={
            "md5": "0123456789abcdef0123456789abcdef",
            "ext": "epub",
            "kindle_mail": "reader@example.com",
        },
        headers=_same_origin_headers(),
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert service.jobs[job_id]["owner_user_id"] == "user-1"


def test_download_rejects_cross_origin_when_authenticated(client, monkeypatch):
    _sign_in_browser(client, monkeypatch)

    response = client.post(
        "/download",
        json={
            "goodreads_url": "https://www.goodreads.com/book/show/4671",
            "kindle_mail": "reader@example.com",
        },
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Cross-site requests are not allowed."


def test_download_rejects_when_user_has_too_many_active_jobs(client, monkeypatch):
    _sign_in_browser(client, monkeypatch, user_id="user-1")
    monkeypatch.setattr(service.settings, "max_jobs_per_user", 1)
    monkeypatch.setattr(service.settings, "max_jobs_per_ip", 5)
    monkeypatch.setattr(service.settings, "max_in_flight_jobs", 10)
    monkeypatch.setattr(service.settings, "max_queued_jobs", 10)

    service._make_job(
        AuthenticatedUser(user_id="user-1", email="reader@example.com", email_verified=True),
        client_ip="testclient",
    )

    response = client.post(
        "/download",
        json={
            "goodreads_url": "https://www.goodreads.com/book/show/4671",
            "kindle_mail": "reader@example.com",
        },
        headers=_same_origin_headers(),
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(service.settings.overload_retry_after_seconds)


def test_job_lookup_is_limited_to_owner(client, monkeypatch):
    service.jobs.clear()
    _sign_in_browser(client, monkeypatch, user_id="user-1")
    job_id = service._make_job(AuthenticatedUser(user_id="user-1", email="reader@example.com", email_verified=True))

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert "owner_user_id" not in response.json()
    assert "owner_email" not in response.json()


def test_job_lookup_hides_foreign_jobs(client, monkeypatch):
    service.jobs.clear()
    _sign_in_browser(client, monkeypatch, user_id="user-2", email="other@example.com")
    job_id = service._make_job(AuthenticatedUser(user_id="user-1", email="reader@example.com", email_verified=True))

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


def test_job_polling_is_rate_limited(client, monkeypatch):
    _sign_in_browser(client, monkeypatch, user_id="user-1")
    monkeypatch.setattr(service.settings, "job_poll_rate_limit_per_ip", 1)
    monkeypatch.setattr(service.settings, "job_poll_rate_limit_per_user", 10)
    job_id = service._make_job(AuthenticatedUser(user_id="user-1", email="reader@example.com", email_verified=True))

    first = client.get(f"/jobs/{job_id}")
    second = client.get(f"/jobs/{job_id}")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"]


def test_download_job_completes_and_is_pollable(client, monkeypatch):
    """Full job lifecycle via HTTP: POST creates a queued job, background task runs to
    completion, GET returns the done status through the public API surface.

    This is the only test that exercises the on_status callback → job dict → public
    payload projection chain end-to-end through the HTTP endpoints.
    """
    _sign_in_browser(client, monkeypatch, user_id="user-1")

    captured_tasks = []

    def capture_task(coro):
        captured_tasks.append(coro)
        return None

    monkeypatch.setattr(service.asyncio, "create_task", capture_task)

    async def instant_download(url, kindle_mail, on_status=None):
        if on_status:
            on_status("done")

    monkeypatch.setattr(service, "ebook_download", instant_download)

    post = client.post(
        "/download",
        json={"goodreads_url": "https://www.goodreads.com/book/show/4671", "kindle_mail": "reader@example.com"},
        headers=_same_origin_headers(),
    )
    assert post.status_code == 200
    job_id = post.json()["job_id"]
    assert service.jobs[job_id]["status"] == "queued"

    assert len(captured_tasks) == 1
    asyncio.run(captured_tasks[0])

    poll = client.get(f"/jobs/{job_id}")
    assert poll.status_code == 200
    data = poll.json()
    assert data["status"] == "done"
    assert data["error"] is None
    assert data["fallback"] is None
