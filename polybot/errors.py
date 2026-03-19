"""Custom exceptions for Polymarket CLOB API interactions."""


class ClobApiError(Exception):
    """Raised when a CLOB API call fails (timeout, 429, 5xx, network error).

    Attributes:
        status_code: HTTP status code if available, else None.
        retry_after: Seconds to wait before retrying (from 429 Retry-After header), else None.
        cancel_only: True if the exchange is in cancel-only mode (503).
    """

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        retry_after: float | None = None,
        cancel_only: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.cancel_only = cancel_only
