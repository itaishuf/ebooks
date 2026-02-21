from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from exceptions import BookNotFoundError, InvalidURLError


@pytest.mark.asyncio
async def test_get_isbn_valid_page():
    from download_flow import get_isbn

    fake_html = '<html><body>isbn: 9781234567890</body></html>'
    mock_response = AsyncMock()
    mock_response.text = AsyncMock(return_value=fake_html)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch('download_flow.aiohttp.ClientSession', return_value=mock_session):
        result = await get_isbn('https://www.goodreads.com/book/show/12345')

    assert result == '9781234567890'


@pytest.mark.asyncio
async def test_get_isbn_no_isbn_raises():
    from download_flow import get_isbn

    fake_html = '<html><body>No book info here</body></html>'
    mock_response = AsyncMock()
    mock_response.text = AsyncMock(return_value=fake_html)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch('download_flow.aiohttp.ClientSession', return_value=mock_session):
        with pytest.raises(BookNotFoundError):
            await get_isbn('https://www.goodreads.com/book/show/99999')


@pytest.mark.asyncio
async def test_get_book_md5_parses_hashes():
    from download_flow import get_book_md5

    fake_html = '''<html><body>
        <a href="/md5/abc123def456">Book 1</a>
        <a href="/md5/789abc012def">Book 2</a>
    </body></html>'''
    mock_response = AsyncMock()
    mock_response.text = AsyncMock(return_value=fake_html)
    mock_response.__aenter__ = AsyncMock(return_value=mock_response)
    mock_response.__aexit__ = AsyncMock(return_value=False)

    mock_session = AsyncMock()
    mock_session.get = MagicMock(return_value=mock_response)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch('download_flow.aiohttp.ClientSession', return_value=mock_session):
        result = await get_book_md5('9781234567890')

    assert result == ['abc123def456', '789abc012def']
