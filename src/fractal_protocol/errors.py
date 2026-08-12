from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """An expected protocol or domain failure that can be returned to a client."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


def require(condition: bool, code: str, message: str, **details: Any) -> None:
    if not condition:
        raise DomainError(code, message, details=details)
