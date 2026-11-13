"""Domain errors and their mapping from database SQLSTATE codes.

The database is the authority for rental rules; PL/pgSQL functions raise
errors with custom SQLSTATEs (see db/init/03_functions.sql) which are turned
into typed exceptions here and into RFC 7807 problem responses in app.main.
"""

from __future__ import annotations

from typing import ClassVar

from sqlalchemy.exc import DBAPIError


class DomainError(Exception):
    code: ClassVar[str] = "DOMAIN_ERROR"
    http_status: ClassVar[int] = 400
    title: ClassVar[str] = "Request cannot be processed"

    def __init__(self, detail: str | None = None) -> None:
        super().__init__(detail or self.title)
        self.detail = detail


class NotFound(DomainError):
    code = "NOT_FOUND"
    http_status = 404
    title = "Resource not found"


class InvalidInput(DomainError):
    code = "INVALID_INPUT"
    http_status = 422
    title = "Invalid input"


class RentalLimitReached(DomainError):
    code = "RENTAL_LIMIT"
    http_status = 409
    title = "Rental limit reached"


class NoCopyAvailable(DomainError):
    code = "NO_COPY_AVAILABLE"
    http_status = 409
    title = "No free copy of this title is available"


class AlreadyRented(DomainError):
    code = "ALREADY_RENTED"
    http_status = 409
    title = "You already have this title rented"


class InvalidTransition(DomainError):
    code = "INVALID_TRANSITION"
    http_status = 409
    title = "Operation not allowed in the rental's current state"


class RentalFailed(DomainError):
    """The rent saga could not grant access on the node and was compensated."""

    code = "RENTAL_FAILED"
    http_status = 502
    title = "The node could not grant access; the rental was cancelled"


class SyncInProgress(DomainError):
    code = "SYNC_IN_PROGRESS"
    http_status = 409
    title = "A catalog sync is already running"


class Unauthorized(DomainError):
    code = "UNAUTHORIZED"
    http_status = 401
    title = "Authentication required"


class Forbidden(DomainError):
    code = "FORBIDDEN"
    http_status = 403
    title = "Not allowed"


class UsernameTaken(DomainError):
    code = "USERNAME_TAKEN"
    http_status = 409
    title = "Username is already taken"


SQLSTATE_TO_ERROR: dict[str, type[DomainError]] = {
    "JR001": RentalLimitReached,
    "JR002": NoCopyAvailable,
    "JR003": AlreadyRented,
    "JR005": InvalidTransition,
    "JR404": NotFound,
    "22023": InvalidInput,
}


def translate_db_error(exc: DBAPIError) -> DomainError | None:
    """Map a database error raised by our PL/pgSQL rules to a domain error."""
    orig = exc.orig
    sqlstate = getattr(orig, "sqlstate", None)
    error_cls = SQLSTATE_TO_ERROR.get(sqlstate) if sqlstate else None
    if error_cls is None:
        return None
    diag = getattr(orig, "diag", None)
    detail = getattr(diag, "message_detail", None) if diag is not None else None
    return error_cls(detail)
