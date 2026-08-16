"""One-off migration: make enrollment_tokens.expires_at nullable.

SQLite cannot drop a NOT NULL constraint in place, so the table is rebuilt.
Run once against an existing development database:

    .venv/Scripts/python.exe migrate_token_expiry.py

Production should use a proper Alembic revision instead; this exists so the
local SQLite database survives the schema change without losing the bootstrap
administrator or the audit trail.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).parent / "instance" / "eem.db"

NEW_TABLE = """
CREATE TABLE enrollment_tokens_new (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    token_hash VARCHAR(64) NOT NULL UNIQUE,
    label VARCHAR(128) NOT NULL,
    organization_id VARCHAR(64),
    policy_id VARCHAR(36),
    expires_at DATETIME,                       -- was NOT NULL; NULL = never expires
    max_uses INTEGER NOT NULL DEFAULT 1,       -- 0 = unlimited
    use_count INTEGER NOT NULL DEFAULT 0,
    revoked_at DATETIME,
    revoked_reason VARCHAR(64),
    created_by VARCHAR(36) NOT NULL REFERENCES users (id),
    notes TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""


def main() -> int:
    if not DB.exists():
        print(f"No database at {DB}. Nothing to migrate; run 'flask init-db' instead.")
        return 0

    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    cols = {r["name"]: r for r in conn.execute("PRAGMA table_info(enrollment_tokens)")}
    if not cols:
        print("enrollment_tokens does not exist; run 'flask init-db'.")
        return 0
    if cols["expires_at"]["notnull"] == 0:
        print("Already migrated: expires_at is nullable.")
        return 0

    backup = DB.with_suffix(".db.bak")
    shutil.copy2(DB, backup)
    print(f"Backed up to {backup}")

    rows = conn.execute("SELECT COUNT(*) AS n FROM enrollment_tokens").fetchone()["n"]
    print(f"Rebuilding enrollment_tokens ({rows} existing rows will be preserved)")

    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("BEGIN")
        conn.execute(NEW_TABLE)
        conn.execute(
            """
            INSERT INTO enrollment_tokens_new
                (id, token_hash, label, organization_id, policy_id, expires_at,
                 max_uses, use_count, revoked_at, revoked_reason, created_by,
                 notes, created_at, updated_at)
            SELECT id, token_hash, label, organization_id, policy_id, expires_at,
                   max_uses, use_count, revoked_at, revoked_reason, created_by,
                   notes, created_at, updated_at
            FROM enrollment_tokens
            """
        )
        conn.execute("DROP TABLE enrollment_tokens")
        conn.execute("ALTER TABLE enrollment_tokens_new RENAME TO enrollment_tokens")
        conn.execute(
            "CREATE INDEX ix_enrollment_tokens_organization_id "
            "ON enrollment_tokens (organization_id)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX ix_enrollment_tokens_token_hash "
            "ON enrollment_tokens (token_hash)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        print("Migration failed; database is unchanged.", file=sys.stderr)
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()

    print("Done. expires_at is now nullable (NULL = never expires).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
