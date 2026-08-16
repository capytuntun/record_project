"""Input validation helpers (spec section 4.4).

Views read every field through these so unexpected types, oversized strings and
malformed bodies are rejected before touching the database.
"""

from __future__ import annotations

from datetime import datetime, timezone

from flask import request

from ..errors import ValidationError

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


def json_body() -> dict:
    if not request.is_json:
        raise ValidationError("Request body must be application/json.")
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ValidationError("Request body must be a JSON object.")
    return body


def get_str(
    body: dict,
    field: str,
    *,
    required: bool = True,
    min_length: int = 1,
    max_length: int = 255,
    default: str | None = None,
    choices: tuple[str, ...] | None = None,
    strip: bool = True,
) -> str | None:
    if field not in body or body[field] is None:
        if required:
            raise ValidationError(f"'{field}' is required.")
        return default

    value = body[field]
    if not isinstance(value, str):
        raise ValidationError(f"'{field}' must be a string.")
    if strip:
        value = value.strip()

    if not value and not required:
        return default
    if len(value) < min_length:
        raise ValidationError(f"'{field}' must be at least {min_length} characters.")
    if len(value) > max_length:
        raise ValidationError(f"'{field}' must be at most {max_length} characters.")
    if choices is not None and value not in choices:
        raise ValidationError(f"'{field}' must be one of: {', '.join(choices)}.")
    return value


def get_int(
    body: dict,
    field: str,
    *,
    required: bool = False,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int | None:
    if field not in body or body[field] is None:
        if required:
            raise ValidationError(f"'{field}' is required.")
        return default

    value = body[field]
    # bool is a subclass of int; reject it so True does not become 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"'{field}' must be an integer.")
    if minimum is not None and value < minimum:
        raise ValidationError(f"'{field}' must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise ValidationError(f"'{field}' must be at most {maximum}.")
    return value


def get_username(body: dict, field: str = "username") -> str:
    """Usernames are restricted so they cannot be confused with ids or emails."""
    value = get_str(body, field, min_length=3, max_length=64)
    assert value is not None  # required=True guarantees this
    allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
    lowered = value.lower()
    if set(lowered) - allowed:
        raise ValidationError(
            "'username' may contain only letters, digits, dot, underscore and hyphen."
        )
    return lowered


def pagination() -> tuple[int, int]:
    """Parse ?page and ?pageSize into (offset, limit)."""
    try:
        page = int(request.args.get("page", 1))
        page_size = int(request.args.get("pageSize", DEFAULT_PAGE_SIZE))
    except ValueError as exc:
        raise ValidationError("'page' and 'pageSize' must be integers.") from exc

    if page < 1:
        raise ValidationError("'page' must be 1 or greater.")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValidationError(f"'pageSize' must be between 1 and {MAX_PAGE_SIZE}.")
    return (page - 1) * page_size, page_size


def query_datetime(param: str) -> datetime | None:
    """Parse an ISO-8601 query parameter into an aware UTC datetime."""
    raw = request.args.get(param, "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"'{param}' must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def paginated(items: list, total: int, offset: int, limit: int) -> dict:
    return {
        "items": items,
        "page": offset // limit + 1,
        "pageSize": limit,
        "total": total,
        "hasMore": offset + len(items) < total,
    }
