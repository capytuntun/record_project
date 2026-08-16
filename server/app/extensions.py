"""Shared extension instances.

Kept in their own module so models and blueprints can import ``db`` without
importing the application factory (which would create a circular import).
"""

from __future__ import annotations

from argon2 import PasswordHasher
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_migrate import Migrate
from flask_sock import Sock
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()
migrate = Migrate()

# WebSocket transport for live screen viewing (spec section 29). Each connection
# is handled in its own thread by the dev server; the hub that fans frames out
# guards its shared state with locks.
sock = Sock()

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri="memory://",
)

# Argon2id is the default variant for argon2-cffi's PasswordHasher.
# Parameters follow the OWASP baseline: 64 MiB memory, 3 iterations.
password_hasher = PasswordHasher(
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)
