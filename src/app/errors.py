
"""Application-specific exception classes."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base application exception carrying API-safe details."""

    def __init__(self, message: str, error_code: str, status_code: int, details: dict[str, Any] | None = None) -> None:
        """Initialize the base application error."""
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        self.details = details or {}


class ValidationError(AppError):
    """Raised when incoming business input is invalid."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        """Initialize a validation error."""
        super().__init__(message=message, error_code="VALIDATION_ERROR", status_code=400, details=details)


class DataAccessError(AppError):
    """Raised when JSON or API-backed retrieval fails."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        """Initialize a data access error."""
        super().__init__(message=message, error_code="DATA_ACCESS_ERROR", status_code=500, details=details)


class UniqueIntegrationError(AppError):
    """Raised when the Unique AI integration cannot be used safely."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        """Initialize a Unique AI integration error."""
        super().__init__(message=message, error_code="UNIQUE_INTEGRATION_ERROR", status_code=500, details=details)


class RoutingError(AppError):
    """Raised when the orchestrator cannot determine an execution path."""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        """Initialize an orchestrator routing error."""
        super().__init__(message=message, error_code="ROUTING_ERROR", status_code=400, details=details)