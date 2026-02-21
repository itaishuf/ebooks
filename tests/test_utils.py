import os
import tempfile
import time
from pathlib import Path

import pytest


def test_find_newest_file_in_downloads(monkeypatch):
    from config import settings

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(settings, 'download_dir', tmpdir)

        file_path = Path(tmpdir) / 'testbook.epub'
        file_path.write_bytes(b'fake epub content')

        from utils import find_newest_file_in_downloads
        result = find_newest_file_in_downloads()
        assert result.name == 'testbook.epub'


def test_find_newest_file_empty_dir_raises(monkeypatch):
    from config import settings

    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(settings, 'download_dir', tmpdir)

        from utils import find_newest_file_in_downloads
        with pytest.raises(FileNotFoundError):
            find_newest_file_in_downloads()


def test_log_call_sync():
    from utils import log_call

    @log_call
    def add(a: int, b: int) -> int:
        return a + b

    assert add(2, 3) == 5


@pytest.mark.asyncio
async def test_log_call_async():
    from utils import log_call

    @log_call
    async def add_async(a: int, b: int) -> int:
        return a + b

    assert await add_async(2, 3) == 5


def test_redact_args():
    from utils import _redact_args

    args = ('positional',)
    kwargs = {'email': 'user@example.com', 'name': 'John'}
    _, safe = _redact_args(args, kwargs)
    assert safe['email'] == 'use***'
    assert safe['name'] == 'John'
