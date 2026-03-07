from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import service
from abuse_protection import SlidingWindowRateLimiter
from auth import AuthenticatedUser


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

    with TestClient(service.app) as test_client:
        yield test_client

    service.jobs.clear()
    service.app.dependency_overrides.clear()


def _override_user(user_id: str, email: str = "reader@example.com"):
    return lambda: AuthenticatedUser(user_id=user_id, email=email, email_verified=True)


def test_public_routes_remain_accessible(client):
    assert client.get("/").status_code == 200
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    assert client.get("/auth/config").status_code == 200


@pytest.mark.parametrize(
    ("method", "url", "kwargs"),
    [
        ("get", "/search?q=dune", {}),
        (
            "post",
            "/download",
            {"json": {"goodreads_url": "https://www.goodreads.com/book/show/4671", "kindle_mail": "reader@example.com"}},
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


def test_search_uses_authenticated_dependency(client, monkeypatch):
    service.app.dependency_overrides[service.get_current_user] = _override_user("user-1")

    async def fake_search_books(query: str):
        assert query == "dune"
        return [{"title": "Dune"}]

    monkeypatch.setattr(service, "search_books", fake_search_books)

    response = client.get("/search?q=dune")

    assert response.status_code == 200
    assert response.json() == {"results": [{"title": "Dune"}]}


def test_search_rejects_query_string_auth(client):
    service.app.dependency_overrides[service.get_current_user] = _override_user("user-1")

    response = client.get("/search?q=dune&access_token=leaky-token")

    assert response.status_code == 400
    assert response.json()["detail"] == "Authentication query parameters are not supported; use the Authorization header."


def test_search_is_rate_limited_per_ip(client, monkeypatch):
    service.app.dependency_overrides[service.get_current_user] = _override_user("user-1")
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
    service.app.dependency_overrides[service.get_current_user] = _override_user("user-1")

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
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert service.jobs[job_id]["owner_user_id"] == "user-1"
    assert service.jobs[job_id]["owner_email"] == "reader@example.com"
    assert service.jobs[job_id]["client_ip"] == "testclient"


def test_download_md5_uses_post_body(client, monkeypatch):
    service.app.dependency_overrides[service.get_current_user] = _override_user("user-1")

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
    )

    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert service.jobs[job_id]["owner_user_id"] == "user-1"


def test_download_rejects_when_user_has_too_many_active_jobs(client, monkeypatch):
    service.app.dependency_overrides[service.get_current_user] = _override_user("user-1")
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
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == str(service.settings.overload_retry_after_seconds)


def test_job_lookup_is_limited_to_owner(client):
    service.jobs.clear()
    service.app.dependency_overrides[service.get_current_user] = _override_user("user-1")
    job_id = service._make_job(AuthenticatedUser(user_id="user-1", email="reader@example.com", email_verified=True))

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert "owner_user_id" not in response.json()
    assert "owner_email" not in response.json()


def test_job_lookup_hides_foreign_jobs(client):
    service.jobs.clear()
    service.app.dependency_overrides[service.get_current_user] = _override_user("user-2")
    job_id = service._make_job(AuthenticatedUser(user_id="user-1", email="reader@example.com", email_verified=True))

    response = client.get(f"/jobs/{job_id}")

    assert response.status_code == 404
    assert response.json()["detail"] == "Job not found"


def test_job_polling_is_rate_limited(client, monkeypatch):
    service.app.dependency_overrides[service.get_current_user] = _override_user("user-1")
    monkeypatch.setattr(service.settings, "job_poll_rate_limit_per_ip", 1)
    monkeypatch.setattr(service.settings, "job_poll_rate_limit_per_user", 10)
    job_id = service._make_job(AuthenticatedUser(user_id="user-1", email="reader@example.com", email_verified=True))

    first = client.get(f"/jobs/{job_id}")
    second = client.get(f"/jobs/{job_id}")

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["Retry-After"]
