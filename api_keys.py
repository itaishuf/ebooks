from __future__ import annotations

import hashlib
import json
import logging
import secrets
import threading
from dataclasses import dataclass, field
from pathlib import Path

from auth import AuthenticatedUser

logger = logging.getLogger(__name__)

_RAW_KEY_PREFIX = "ebk_"
_RAW_KEY_LENGTH = 32


@dataclass
class ApiKeyRecord:
    key_id: str
    key_hash: str
    label: str
    allowed_emails: list[str]
    daily_quota: int
    enabled: bool


class ApiKeyStore:
    def __init__(self, file_path: str) -> None:
        self._path = Path(file_path)
        self._lock = threading.Lock()
        self._keys: dict[str, ApiKeyRecord] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            logger.warning(f"API key store file not found: {self._path}")
            return
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(f"Failed to load API key store: {exc}")
            return
        for key_id, entry in data.get("keys", {}).items():
            self._keys[key_id] = ApiKeyRecord(
                key_id=key_id,
                key_hash=entry["key_hash"],
                label=entry.get("label", ""),
                allowed_emails=entry.get("allowed_emails", []),
                daily_quota=entry.get("daily_quota", 15),
                enabled=entry.get("enabled", True),
            )
        logger.info(f"Loaded {len(self._keys)} API keys from {self._path}")

    def _save(self) -> None:
        data: dict[str, dict] = {"keys": {}}
        for key_id, record in self._keys.items():
            data["keys"][key_id] = {
                "label": record.label,
                "key_hash": record.key_hash,
                "allowed_emails": record.allowed_emails,
                "daily_quota": record.daily_quota,
                "enabled": record.enabled,
            }
        self._path.write_text(json.dumps(data, indent=2) + "\n")

    @staticmethod
    def _hash_key(raw_key: str) -> str:
        return hashlib.sha256(raw_key.encode()).hexdigest()

    def lookup(self, raw_key: str) -> ApiKeyRecord | None:
        key_hash = self._hash_key(raw_key)
        with self._lock:
            for record in self._keys.values():
                if record.enabled and secrets.compare_digest(record.key_hash, key_hash):
                    return record
        return None

    def is_email_allowed(self, record: ApiKeyRecord, kindle_email: str) -> bool:
        if not record.allowed_emails:
            return True
        return kindle_email in record.allowed_emails

    @staticmethod
    def to_user(record: ApiKeyRecord) -> AuthenticatedUser:
        return AuthenticatedUser(
            user_id=f"api:{record.key_id}",
            email=record.label,
            email_verified=True,
        )

    def list_keys(self) -> list[ApiKeyRecord]:
        with self._lock:
            return list(self._keys.values())

    def get_key(self, key_id: str) -> ApiKeyRecord | None:
        with self._lock:
            return self._keys.get(key_id)

    def add_key(
        self,
        label: str,
        *,
        allowed_emails: list[str] | None = None,
        daily_quota: int = 15,
    ) -> tuple[str, ApiKeyRecord]:
        raw_key = _RAW_KEY_PREFIX + secrets.token_hex(_RAW_KEY_LENGTH // 2)
        key_hash = self._hash_key(raw_key)
        key_id = secrets.token_hex(8)
        record = ApiKeyRecord(
            key_id=key_id,
            key_hash=key_hash,
            label=label,
            allowed_emails=allowed_emails or [],
            daily_quota=daily_quota,
            enabled=True,
        )
        with self._lock:
            self._keys[key_id] = record
            self._save()
        logger.info(f"Added API key {key_id} for label={label}")
        return raw_key, record

    def revoke_key(self, key_id: str) -> bool:
        with self._lock:
            if key_id not in self._keys:
                return False
            del self._keys[key_id]
            self._save()
        logger.info(f"Revoked API key {key_id}")
        return True

    def update_quota(self, key_id: str, daily_quota: int) -> bool:
        with self._lock:
            record = self._keys.get(key_id)
            if record is None:
                return False
            record.daily_quota = daily_quota
            self._save()
        logger.info(f"Updated daily quota for key {key_id} to {daily_quota}")
        return True


_store: ApiKeyStore | None = None


def get_api_key_store() -> ApiKeyStore | None:
    return _store


def load_api_key_store(file_path: str) -> ApiKeyStore | None:
    global _store
    if not file_path:
        _store = None
        return None
    try:
        _store = ApiKeyStore(file_path)
        return _store
    except Exception as exc:
        logger.error(f"Failed to initialize API key store: {exc}")
        _store = None
        return None
