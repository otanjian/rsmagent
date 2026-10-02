"""Shared refusal shape for the desktop integrations package.

Handlers translate :class:`DesktopAccessError` into the contract error
envelope; services raise it and never invent an HTTP status of their own.
"""

from __future__ import annotations


class DesktopAccessError(RuntimeError):
    """A refused step, carrying the HTTP shape the handler should report."""

    def __init__(self, message: str, code: str = "invalid_request",
                 status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
