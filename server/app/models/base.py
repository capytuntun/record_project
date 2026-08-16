"""Model mixins and time helpers shared across the schema."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from ..extensions import db


def utcnow() -> datetime:
    """Timezone-aware 'now'. Every timestamp in the system is stored in UTC."""
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """Re-attach UTC to a value read back from a driver that drops tzinfo.

    SQLite has no native timestamp type, so SQLAlchemy returns naive datetimes.
    Everything is written as UTC, so a naive value is by definition UTC.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: datetime | None) -> str | None:
    """Serialize a timestamp for API responses."""
    normalized = as_utc(value)
    return normalized.isoformat() if normalized else None


def days_until(target: datetime | None) -> int | None:
    """Whole days from now until ``target``, rounded up. None if no target.

    Rounded up rather than truncated: something expiring in 23 hours is "1 day"
    to a person, not "0 days". Truncating would under-report every remaining
    lifetime by up to a day, which matters when the number drives a warning.
    Already-past targets give 0.
    """
    normalized = as_utc(target)
    if normalized is None:
        return None
    seconds = (normalized - utcnow()).total_seconds()
    if seconds <= 0:
        return 0
    return math.ceil(seconds / 86400)


def _days_in_month(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


def add_period(start: datetime, *, years: int = 0, months: int = 0, days: int = 0) -> datetime:
    """Add a calendar period to ``start``.

    Months and years are calendar units, not fixed spans -- "1 month" from
    31 January is 28 February, not 2 or 3 March. A day-of-month that does not
    exist in the target month clamps to that month's last day.
    """
    if years < 0 or months < 0 or days < 0:
        raise ValueError("Period components must not be negative.")

    total_months = start.month - 1 + months + years * 12
    year = start.year + total_months // 12
    month = total_months % 12 + 1
    day = min(start.day, _days_in_month(year, month))

    return start.replace(year=year, month=month, day=day) + timedelta(days=days)


def describe_period(years: int, months: int, days: int) -> str:
    """Human-readable period, e.g. '1 年 6 個月'. Empty string for no period."""
    parts = []
    if years:
        parts.append(f"{years} 年")
    if months:
        parts.append(f"{months} 個月")
    if days:
        parts.append(f"{days} 天")
    return " ".join(parts)


class TimestampMixin:
    """created_at / updated_at on every table that matters (spec section 22)."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class SoftDeleteMixin:
    """Soft delete so audit history keeps referring to a real row (section 8)."""

    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


__all__ = [
    "db",
    "utcnow",
    "as_utc",
    "iso",
    "add_period",
    "describe_period",
    "days_until",
    "TimestampMixin",
    "SoftDeleteMixin",
]
