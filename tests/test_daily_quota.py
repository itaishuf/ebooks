from __future__ import annotations

import time

from abuse_protection import DailyQuotaTracker


def test_allows_first_download() -> None:
    tracker = DailyQuotaTracker()
    allowed, remaining, retry_after = tracker.check("user1", limit=15)

    assert allowed is True
    assert remaining == 14
    assert retry_after == 0


def test_allows_up_to_limit() -> None:
    tracker = DailyQuotaTracker()
    for i in range(14):
        allowed, remaining, _ = tracker.check("user1", limit=15)
        assert allowed is True
        assert remaining == 14 - i

    allowed, remaining, retry_after = tracker.check("user1", limit=15)
    assert allowed is True
    assert remaining == 0


def test_rejects_after_limit() -> None:
    tracker = DailyQuotaTracker()
    for _ in range(15):
        tracker.check("user1", limit=15)

    allowed, remaining, retry_after = tracker.check("user1", limit=15)
    assert allowed is False
    assert remaining == 0
    assert retry_after > 0


def test_separate_users_independent() -> None:
    tracker = DailyQuotaTracker()
    for _ in range(15):
        tracker.check("user1", limit=15)

    allowed, remaining, _ = tracker.check("user2", limit=15)
    assert allowed is True
    assert remaining == 14


def test_unlimited_quota() -> None:
    tracker = DailyQuotaTracker()
    for _ in range(100):
        allowed, remaining, _ = tracker.check("user1", limit=0)
        assert allowed is True


def test_zero_limit_allows_all() -> None:
    tracker = DailyQuotaTracker()
    allowed, remaining, _ = tracker.check("user1", limit=0)
    assert allowed is True


def test_different_limits_per_user() -> None:
    tracker = DailyQuotaTracker()
    for _ in range(5):
        tracker.check("user1", limit=5)

    allowed, _, _ = tracker.check("user1", limit=5)
    assert allowed is False

    allowed, remaining, _ = tracker.check("user2", limit=10)
    assert allowed is True
    assert remaining == 9


def test_quota_resets_next_day(monkeypatch) -> None:
    tracker = DailyQuotaTracker()

    # Fill quota
    for _ in range(3):
        tracker.check("user1", limit=3)

    allowed, _, _ = tracker.check("user1", limit=3)
    assert allowed is False

    # Simulate next day by patching time.time
    original_time = time.time
    future_ts = original_time() + 86400  # 24 hours later
    monkeypatch.setattr(time, "time", lambda: future_ts)

    allowed, remaining, _ = tracker.check("user1", limit=3)
    assert allowed is True
    assert remaining == 2

    monkeypatch.setattr(time, "time", original_time)
