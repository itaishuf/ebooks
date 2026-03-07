from __future__ import annotations

import os
import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from abuse_protection import (
    RateLimitPolicy,
    SlidingWindowRateLimiter,
    cleanup_download_artifacts,
    cleanup_expired_jobs,
    extract_client_ip,
    sanitize_for_log,
)


def test_sanitize_for_log_redacts_emails_urls_and_secrets():
    message = (
        "reader@example.com requested https://goodreads.example/book "
        'with Authorization: Bearer eyJabc.def.ghi and password="super-secret"'
    )

    sanitized = sanitize_for_log(message)

    assert "reader@example.com" not in sanitized
    assert "https://goodreads.example/book" not in sanitized
    assert "super-secret" not in sanitized
    assert "eyJabc.def.ghi" not in sanitized
    assert "[redacted-email]" in sanitized
    assert "[redacted-url]" in sanitized
    assert "[redacted-secret]" in sanitized


def test_cleanup_expired_jobs_removes_only_old_terminal_jobs():
    jobs = {
        "old-done": {"status": "done", "finished_at_epoch": 10.0},
        "old-error": {"status": "error", "finished_at_epoch": 20.0},
        "fresh-done": {"status": "done", "finished_at_epoch": 95.0},
        "active": {"status": "downloading", "finished_at_epoch": None},
    }

    removed = cleanup_expired_jobs(jobs, ttl_seconds=30, now=100.0)

    assert removed == ["old-done", "old-error"]
    assert set(jobs) == {"fresh-done", "active"}


def test_cleanup_download_artifacts_removes_old_files_and_directories(tmp_path):
    recent_file = tmp_path / "recent.epub"
    recent_file.write_bytes(b"recent")
    stale_file = tmp_path / "stale.epub"
    stale_file.write_bytes(b"stale")
    stale_dir = tmp_path / "selenium-old"
    stale_dir.mkdir()
    (stale_dir / "artifact.part").write_bytes(b"partial")

    now = time.time()
    old_timestamp = now - 3600
    recent_timestamp = now - 30
    recent_file.touch()
    stale_file.touch()
    for path in (stale_file, stale_dir, stale_dir / "artifact.part"):
        if path.is_file():
            path.touch()

    os.utime(recent_file, (recent_timestamp, recent_timestamp))
    os.utime(stale_file, (old_timestamp, old_timestamp))
    os.utime(stale_dir / "artifact.part", (old_timestamp, old_timestamp))
    os.utime(stale_dir, (old_timestamp, old_timestamp))

    removed = cleanup_download_artifacts(str(tmp_path), ttl_seconds=300, now=now)

    assert str(stale_file) in removed
    assert str(stale_dir) in removed
    assert recent_file.exists()
    assert not stale_file.exists()
    assert not stale_dir.exists()


def test_extract_client_ip_prefers_forwarded_header_from_trusted_proxy():
    app = FastAPI()

    @app.get("/ip")
    async def ip(request: Request):
        return {"client_ip": extract_client_ip(request, trusted_proxy_ips=["testclient"])}

    client = TestClient(app)

    response = client.get("/ip", headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})

    assert response.status_code == 200
    assert response.json() == {"client_ip": "203.0.113.9"}


def test_sliding_window_rate_limiter_returns_retry_after():
    limiter = SlidingWindowRateLimiter()
    policy = RateLimitPolicy(name="search:ip", limit=1, window_seconds=60)

    first = limiter.check(policy, "ip:testclient")
    second = limiter.check(policy, "ip:testclient")

    assert first.allowed is True
    assert second.allowed is False
    assert second.retry_after_seconds > 0
