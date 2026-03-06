class EbookError(Exception):
    """Base exception for all ebook downloader errors."""


class InvalidURLError(EbookError):
    """The provided URL is not a valid Goodreads URL."""


class BookNotFoundError(EbookError):
    """No book matching the given ISBN was found."""


class DownloadError(EbookError):
    """Failed to download the ebook file."""


class ManualDownloadRequiredError(DownloadError):
    """Automated download failed, but a manual LibGen fallback is available."""

    def __init__(self, message: str, fallback_url: str, fallback_message: str):
        super().__init__(message)
        self.fallback_url = fallback_url
        self.fallback_message = fallback_message


class EmailDeliveryError(EbookError):
    """Failed to send the ebook via email."""


class BitwardenError(EbookError):
    """Failed to interact with the Bitwarden CLI."""
