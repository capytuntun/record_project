"""Database models. Importing this package registers every table with SQLAlchemy."""

from .alert import Alert, AlertChannel
from .audit import AuditLog
from .base import (
    SoftDeleteMixin,
    TimestampMixin,
    add_period,
    as_utc,
    days_until,
    db,
    describe_period,
    iso,
    utcnow,
)
from .endpoint import Endpoint, EndpointCredential, EnrollmentToken
from .inventory import EndpointInventory
from .package import InstallationPackage
from .recording import RecordingPolicy, RecordingSegment
from .screen import ScreenSession
from .screenshot import Screenshot
from .storage import StorageTarget
from .user import (
    AdminEndpointScope,
    AdminFeatureGrant,
    AdminGroupAssignment,
    EndpointGroup,
    EndpointGroupMember,
    RefreshToken,
    User,
)

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
    "User",
    "RefreshToken",
    "AdminEndpointScope",
    "AdminFeatureGrant",
    "AdminGroupAssignment",
    "EndpointGroup",
    "EndpointGroupMember",
    "Endpoint",
    "EndpointCredential",
    "EnrollmentToken",
    "EndpointInventory",
    "InstallationPackage",
    "ScreenSession",
    "Screenshot",
    "StorageTarget",
    "RecordingPolicy",
    "RecordingSegment",
    "AuditLog",
    "Alert",
    "AlertChannel",
]
