from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from api_keys import ApiKeyRecord, ApiKeyStore


@pytest.fixture
def keys_file(tmp_path: Path) -> Path:
    return tmp_path / "api_keys.json"


@pytest.fixture
def store(keys_file: Path) -> ApiKeyStore:
    return ApiKeyStore(str(keys_file))


def _write_keys(keys_file: Path, keys: dict) -> None:
    keys_file.write_text(json.dumps({"keys": keys}))


def test_add_and_lookup(store: ApiKeyStore) -> None:
    raw_key, record = store.add_key(label="Test User")

    assert record.label == "Test User"
    assert record.enabled is True
    assert record.daily_quota == 15
    assert record.allowed_emails == []

    found = store.lookup(raw_key)
    assert found is not None
    assert found.key_id == record.key_id
    assert found.label == "Test User"


def test_lookup_returns_none_for_bad_key(store: ApiKeyStore) -> None:
    store.add_key(label="Test")
    assert store.lookup("ebk_wrongkey") is None


def test_lookup_returns_none_for_disabled_key(keys_file: Path) -> None:
    import hashlib
    raw = "ebk_deadbeefdeadbeefdeadbeefdeadbeef"
    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    _write_keys(keys_file, {
        "k1": {
            "label": "Disabled",
            "key_hash": key_hash,
            "allowed_emails": [],
            "daily_quota": 15,
            "enabled": False,
        }
    })
    store = ApiKeyStore(str(keys_file))
    assert store.lookup(raw) is None


def test_email_allowlist(store: ApiKeyStore) -> None:
    _, record = store.add_key(label="Restricted", allowed_emails=["a@kindle.com", "b@kindle.com"])

    assert store.is_email_allowed(record, "a@kindle.com") is True
    assert store.is_email_allowed(record, "b@kindle.com") is True
    assert store.is_email_allowed(record, "c@kindle.com") is False


def test_email_allowlist_empty_allows_all(store: ApiKeyStore) -> None:
    _, record = store.add_key(label="Open", allowed_emails=[])
    assert store.is_email_allowed(record, "anyone@kindle.com") is True


def test_to_user(store: ApiKeyStore) -> None:
    _, record = store.add_key(label="Dad")
    user = store.to_user(record)

    assert user.user_id == f"api:{record.key_id}"
    assert user.email == "Dad"
    assert user.email_verified is True


def test_revoke_key(store: ApiKeyStore) -> None:
    raw, record = store.add_key(label="Keep")
    assert store.lookup(raw) is not None

    assert store.revoke_key(record.key_id) is True
    assert store.lookup(raw) is None


def test_revoke_nonexistent_key(store: ApiKeyStore) -> None:
    assert store.revoke_key("nonexistent") is False


def test_update_quota(store: ApiKeyStore) -> None:
    _, record = store.add_key(label="Quota Test")
    assert store.update_quota(record.key_id, 25) is True
    updated = store.get_key(record.key_id)
    assert updated is not None
    assert updated.daily_quota == 25


def test_update_quota_nonexistent(store: ApiKeyStore) -> None:
    assert store.update_quota("nonexistent", 10) is False


def test_list_keys(store: ApiKeyStore) -> None:
    store.add_key(label="One")
    store.add_key(label="Two")
    keys = store.list_keys()
    assert len(keys) == 2
    labels = {k.label for k in keys}
    assert labels == {"One", "Two"}


def test_persists_to_file(keys_file: Path) -> None:
    store1 = ApiKeyStore(str(keys_file))
    raw, record = store1.add_key(label="Persistent")

    store2 = ApiKeyStore(str(keys_file))
    found = store2.lookup(raw)
    assert found is not None
    assert found.label == "Persistent"


def test_thread_safety(keys_file: Path) -> None:
    store = ApiKeyStore(str(keys_file))
    errors: list[Exception] = []

    def add_keys(n: int) -> None:
        try:
            for i in range(n):
                store.add_key(label=f"Thread-{threading.current_thread().name}-{i}")
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=add_keys, args=(10,)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(store.list_keys()) == 50


def test_load_missing_file_warning(tmp_path: Path) -> None:
    store = ApiKeyStore(str(tmp_path / "nonexistent.json"))
    assert store.list_keys() == []


def test_load_corrupted_file_warning(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json {{{")
    store = ApiKeyStore(str(bad_file))
    assert store.list_keys() == []


def test_load_api_key_store_empty_path() -> None:
    import api_keys
    result = api_keys.load_api_key_store("")
    assert result is None
    assert api_keys.get_api_key_store() is None


def test_load_api_key_store_valid_path(tmp_path: Path) -> None:
    import api_keys
    keys_file = tmp_path / "keys.json"
    keys_file.write_text('{"keys": {}}')
    result = api_keys.load_api_key_store(str(keys_file))
    assert result is not None
    # Cleanup
    api_keys.load_api_key_store("")
