"""Operator CLI commands, including initial SUPER_ADMIN creation.

The first administrator is created here rather than seeded into the database,
so the system ships with no default credentials at all (spec section 5).
"""

from __future__ import annotations

import getpass
import os
import sys

import click
from flask import Flask
from flask.cli import with_appcontext

from .models import User, db
from .models.audit import BOOTSTRAP_SUPER_ADMIN, CHANGE_PASSWORD
from .models.user import ROLE_SUPER_ADMIN, STATUS_ACTIVE
from .security.passwords import PasswordPolicyError, hash_password
from .security.rbac import count_active_super_admins
from .security.tokens import invalidate_all_sessions
from .services import audit


def _read_new_password(password_stdin: bool) -> str:
    """Read a password from stdin or an interactive prompt.

    Neither path puts it in argv, so it stays out of shell history and the
    process table.
    """
    if password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
        if not password:
            raise click.ClickException("No password received on stdin.")
        return password
    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Confirm password: "):
        raise click.ClickException("Passwords do not match.")
    return password


@click.command("bootstrap-super-admin")
@click.option("--username", required=True, help="Username for the initial SUPER_ADMIN.")
@click.option(
    "--password-stdin",
    is_flag=True,
    help="Read the password from stdin instead of prompting (for unattended installs).",
)
@with_appcontext
def bootstrap_super_admin(username: str, password_stdin: bool) -> None:
    """Create the first SUPER_ADMIN during installation.

    Guarded twice: EEM_BOOTSTRAP_SECRET must be present in the environment
    (an explicit install window the operator opens and then closes), and no
    active SUPER_ADMIN may already exist.

    The password is read from a prompt, or from stdin with --password-stdin.
    Neither path puts it in argv, so it stays out of shell history and the
    process table.
    """
    if not os.environ.get("EEM_BOOTSTRAP_SECRET", "").strip():
        raise click.ClickException(
            "EEM_BOOTSTRAP_SECRET is not set. Set it to open the installation "
            "window, run this command, then remove it from the environment."
        )

    if count_active_super_admins() > 0:
        raise click.ClickException(
            "A SUPER_ADMIN already exists. Use the API to manage administrators. "
            "If you have lost access, restore from backup rather than adding a "
            "second bootstrap path."
        )

    username = username.strip().lower()
    existing = db.session.query(User).filter(User.username == username).one_or_none()
    if existing is not None:
        raise click.ClickException(f"Username {username!r} is already taken.")

    password = _read_new_password(password_stdin)

    try:
        password_hash = hash_password(password)
    except PasswordPolicyError as exc:
        raise click.ClickException(str(exc)) from exc

    user = User(
        username=username,
        password_hash=password_hash,
        role=ROLE_SUPER_ADMIN,
        status=STATUS_ACTIVE,
    )
    db.session.add(user)
    db.session.flush()

    audit.record(
        BOOTSTRAP_SUPER_ADMIN,
        actor=user,
        target_type="user",
        target_id=user.id,
        metadata={"username": username, "via": "cli"},
    )
    db.session.commit()

    click.echo(f"Created SUPER_ADMIN {username!r} (id {user.id}).")
    click.secho(
        "Now remove EEM_BOOTSTRAP_SECRET from the environment.", fg="yellow", err=True
    )


@click.command("reset-password")
@click.option("--username", required=True, help="Account whose password to reset.")
@click.option(
    "--password-stdin",
    is_flag=True,
    help="Read the new password from stdin instead of prompting.",
)
@with_appcontext
def reset_password(username: str, password_stdin: bool) -> None:
    """Reset any account's password from the server itself.

    This is the recovery path for the one case the console cannot handle: the
    single SUPER_ADMIN has lost their password. Only that account may set its
    own password through the API, and no second SUPER_ADMIN can exist to reset
    it -- so recovery must come from someone with access to the server.

    Guarded like bootstrap-super-admin: EEM_BOOTSTRAP_SECRET must be present,
    an explicit window the operator opens and then closes. That is the same
    trust level, and no weaker: anyone who can set environment variables on the
    server and run flask commands already owns the deployment.

    Every session on the account is revoked, and the reset is written to the
    audit log as a CLI action.
    """
    if not os.environ.get("EEM_BOOTSTRAP_SECRET", "").strip():
        raise click.ClickException(
            "EEM_BOOTSTRAP_SECRET is not set. Set it to open the recovery "
            "window, run this command, then remove it from the environment."
        )

    username = username.strip().lower()
    user = (
        db.session.query(User)
        .filter(User.username == username, User.deleted_at.is_(None))
        .one_or_none()
    )
    if user is None:
        raise click.ClickException(f"No active account named {username!r}.")

    password = _read_new_password(password_stdin)
    try:
        user.password_hash = hash_password(password)
    except PasswordPolicyError as exc:
        raise click.ClickException(str(exc)) from exc

    revoked = invalidate_all_sessions(user, "password_reset_cli")
    audit.record(
        CHANGE_PASSWORD,
        actor_type="SYSTEM",
        actor_username="cli",
        target_type="user",
        target_id=user.id,
        metadata={"username": username, "via": "cli", "sessionsRevoked": revoked},
    )
    db.session.commit()

    click.echo(f"Password reset for {username!r} ({user.role}). {revoked} session(s) revoked.")
    click.secho(
        "Now remove EEM_BOOTSTRAP_SECRET from the environment.", fg="yellow", err=True
    )


@click.command("init-db")
@with_appcontext
def init_db() -> None:
    """Create tables directly. For development; use migrations in production."""
    db.create_all()
    click.echo("Database tables created.")


@click.command("sweep-recordings")
@with_appcontext
def sweep_recordings() -> None:
    """Delete recording segments past their retention (for cron)."""
    from flask import current_app

    from .services.retention import sweep_expired, sweep_expired_screenshots

    app = current_app._get_current_object()
    removed = sweep_expired(app)
    shots = sweep_expired_screenshots(app)
    click.echo(f"Removed {removed} expired recording segment(s) and {shots} screenshot(s).")


@click.command("check-config")
@with_appcontext
def check_config() -> None:
    """Report configuration that is unsafe for a production deployment."""
    from flask import current_app

    problems: list[str] = []
    warnings: list[str] = []

    if current_app.config.get("DEBUG"):
        problems.append("DEBUG is enabled; the debugger allows remote code execution.")
    if os.environ.get("EEM_BOOTSTRAP_SECRET", "").strip():
        problems.append(
            "EEM_BOOTSTRAP_SECRET is still set. Remove it once bootstrap is done."
        )
    supers = count_active_super_admins()
    if supers == 0:
        warnings.append("No active SUPER_ADMIN exists. Run 'flask bootstrap-super-admin'.")
    elif supers > 1:
        # The API can no longer mint a second one, so more than one means rows
        # predating that rule. They keep working; this just surfaces them.
        warnings.append(
            f"{supers} active SUPER_ADMINs exist, but the system is designed for "
            "exactly one (the account created at install). Demote the extras."
        )
    if current_app.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite"):
        warnings.append("Using SQLite. Move to PostgreSQL before production.")
    if not current_app.config.get("TRUST_PROXY_HEADERS"):
        warnings.append(
            "TRUST_PROXY_HEADERS is off. Behind a reverse proxy, audit source IPs "
            "will show the proxy address."
        )

    # Perpetual tokens are supported on purpose, but they are the one credential
    # in the system with no automatic end, so a deploy check should name them.
    from .models import EnrollmentToken

    perpetual = (
        db.session.query(EnrollmentToken)
        .filter(EnrollmentToken.expires_at.is_(None), EnrollmentToken.revoked_at.is_(None))
        .all()
    )
    for token in perpetual:
        warnings.append(
            f"Enrollment token {token.label!r} ({token.id[:8]}) never expires "
            f"and is used {token.use_count} time(s). Revoke it if the installer "
            f"it belongs to is no longer in circulation."
        )

    if not current_app.config.get("SIGNING_ENABLED"):
        warnings.append(
            "Code signing is not configured (EEM_SIGNING_PFX). Generated MSIs "
            "will be unsigned and may be blocked by SmartScreen / AppLocker. "
            "See agent/signing/README.md."
        )

    if not current_app.config.get("RECORDING_ENABLED"):
        import os as _os

        if not current_app.config.get("RECORDING_KEY_PASSPHRASE"):
            warnings.append(
                "Screen recording is off: no encryption key (EEM_RECORDING_KEY "
                "unset and EEM_RECORDING_AUTO_KEY disabled), so segments would be "
                "unencrypted. Recording will not run."
            )
        elif not _os.path.isfile(current_app.config["FFMPEG_PATH"]):
            warnings.append(
                f"Screen recording is off: FFmpeg not found at "
                f"{current_app.config['FFMPEG_PATH']}. On Linux: apt install ffmpeg."
            )

    # An auto-generated key lives next to the data; fine for internal use, but
    # name it so it gets backed up and, ideally, moved to a secret manager.
    if current_app.config.get("RECORDING_KEY_SOURCE") == "file":
        warnings.append(
            "Screen-data key was auto-generated at instance/recording.key. Back "
            "it up (losing it makes existing recordings/screenshots undecryptable) "
            "and protect it; for production prefer EEM_RECORDING_KEY from a secret "
            "manager."
        )

    for item in problems:
        click.secho(f"PROBLEM  {item}", fg="red")
    for item in warnings:
        click.secho(f"WARNING  {item}", fg="yellow")
    if not problems and not warnings:
        click.secho("Configuration looks reasonable.", fg="green")

    sys.exit(1 if problems else 0)


def register_commands(app: Flask) -> None:
    app.cli.add_command(bootstrap_super_admin)
    app.cli.add_command(reset_password)
    app.cli.add_command(init_db)
    app.cli.add_command(check_config)
    app.cli.add_command(sweep_recordings)
