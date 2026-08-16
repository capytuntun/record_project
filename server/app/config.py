"""Application configuration.

Every secret is read from the environment. There are deliberately no default
values for secrets: a missing secret raises at startup rather than silently
falling back to a well-known value (CLAUDE.md sections 5 and 24).
"""

from __future__ import annotations

import os
import shutil


class ConfigError(RuntimeError):
    """Raised when the environment is missing or has invalid configuration."""


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and provide a value. "
            f"Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(64))\""
        )
    return value


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _load_or_create_recording_key(instance_dir: str) -> str:
    """Return a stable screen-data encryption key, creating one on first run.

    When ``EEM_RECORDING_KEY`` is not set, we still must not write screen data
    unencrypted (sections 14, 24). So a strong key is generated ONCE and kept in
    ``instance/recording.key`` (0600), then reused every boot -- recording works
    with zero configuration while staying encrypted and stable (yesterday's
    recordings still decrypt tomorrow). For stronger security, set the key from a
    secret manager via the environment instead; that always wins over the file.
    """
    key_path = os.path.join(instance_dir, "recording.key")
    for _ in range(2):
        try:
            with open(key_path, encoding="utf-8") as handle:
                value = handle.read().strip()
            if value:
                return value
        except FileNotFoundError:
            pass
        os.makedirs(instance_dir, exist_ok=True)
        import secrets

        candidate = secrets.token_urlsafe(48)
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue  # created concurrently -- loop and read it back
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(candidate + "\n")
        return candidate
    with open(key_path, encoding="utf-8") as handle:
        return handle.read().strip()


class Config:
    """Configuration assembled from the process environment."""

    def __init__(self) -> None:
        self.SECRET_KEY = _require("EEM_SECRET_KEY")
        if len(self.SECRET_KEY) < 32:
            raise ConfigError("EEM_SECRET_KEY must be at least 32 characters.")

        self.SQLALCHEMY_DATABASE_URI = os.environ.get(
            "EEM_DATABASE_URI", "sqlite:///eem.db"
        )
        self.SQLALCHEMY_TRACK_MODIFICATIONS = False

        # Access tokens are short-lived but the console refreshes them silently,
        # so 60 min keeps idle sessions alive without a visible re-login. The
        # refresh token is the real session length.
        self.ACCESS_TOKEN_TTL_MINUTES = _int("EEM_ACCESS_TOKEN_TTL_MINUTES", 60)
        self.REFRESH_TOKEN_TTL_DAYS = _int("EEM_REFRESH_TOKEN_TTL_DAYS", 30)

        self.HEARTBEAT_INTERVAL_SECONDS = _int("EEM_HEARTBEAT_INTERVAL_SECONDS", 60)
        self.OFFLINE_AFTER_SECONDS = _int("EEM_OFFLINE_AFTER_SECONDS", 180)

        # --- Alerts ----------------------------------------------------------
        # Offline alerts use a longer threshold than the ONLINE/OFFLINE badge: a
        # machine powered off for the evening should not page anyone. Low-disk
        # alerts fire from inventory when the system drive drops below this %.
        self.ALERT_OFFLINE_AFTER_SECONDS = _int("EEM_ALERT_OFFLINE_AFTER_SECONDS", 1800)
        self.ALERT_LOW_DISK_PERCENT = _int("EEM_ALERT_LOW_DISK_PERCENT", 10)

        # SMTP for e-mail channels. Absent host = e-mail channels are inert
        # (they log and skip); webhook channels need no server-side config.
        self.SMTP_HOST = os.environ.get("EEM_SMTP_HOST") or None
        self.SMTP_PORT = _int("EEM_SMTP_PORT", 587)
        self.SMTP_USER = os.environ.get("EEM_SMTP_USER") or None
        self.SMTP_PASSWORD = os.environ.get("EEM_SMTP_PASSWORD", "")
        self.SMTP_FROM = os.environ.get("EEM_SMTP_FROM") or None
        self.SMTP_USE_TLS = os.environ.get("EEM_SMTP_USE_TLS", "1") not in ("0", "false", "False")

        # Only honour X-Forwarded-For when explicitly told we are behind a
        # trusted proxy; otherwise the audit log's source IP is spoofable.
        self.TRUST_PROXY_HEADERS = _bool("EEM_TRUST_PROXY_HEADERS", False)

        self.LOG_LEVEL = os.environ.get("EEM_LOG_LEVEL", "INFO").upper()

        # --- Installer package generation (section 18) ---------------------
        # The agent binary is published once by agent/build.ps1; each package
        # only rebuilds the MSI wrapper around it.
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        default_agent = os.path.join(
            os.path.dirname(repo_root), "agent", "publish", "EndpointAgent.exe"
        )
        default_wxs = os.path.join(
            os.path.dirname(repo_root), "agent", "packaging", "EndpointAgent.wxs"
        )

        default_ca = os.path.join(
            os.path.dirname(repo_root), "agent", "src", "EndpointAgent.CustomActions",
            "bin", "Release", "net472", "EndpointAgent.CustomActions.CA.dll",
        )

        # Agent source, so the console can rebuild the binary itself instead of
        # requiring someone to run agent/build.ps1 and copy files across. Only
        # present when the deployment shipped the source (install.ps1 copies it).
        self.AGENT_ROOT_PATH = os.environ.get(
            "EEM_AGENT_ROOT", os.path.join(os.path.dirname(repo_root), "agent")
        )
        self.DOTNET_COMMAND = os.environ.get("EEM_DOTNET_COMMAND", "")

        self.AGENT_BINARY_PATH = os.environ.get("EEM_AGENT_BINARY", default_agent)
        self.WIX_SOURCE_PATH = os.environ.get("EEM_WIX_SOURCE", default_wxs)
        # Passed to WiX as an absolute path; the relative default in the .wxs
        # only resolves for manual builds run from the packaging directory.
        self.AGENT_CA_DLL_PATH = os.environ.get("EEM_AGENT_CA_DLL", default_ca)
        self.WIX_COMMAND = os.environ.get("EEM_WIX_COMMAND", "wix")
        self.AGENT_VERSION = os.environ.get("EEM_AGENT_VERSION", "0.1.0")

        # The server's own TLS certificate, embedded in generated packages so an
        # endpoint trusts this server without a GPO push (section 24). Defaults
        # to the certificate this process serves with; behind a reverse proxy
        # (nginx) EEM_TLS_CERT is unset, so point this at the proxy's cert.
        self.PACKAGE_CA_CERT_PATH = (
            os.environ.get("EEM_PACKAGE_CA_CERT")
            or os.environ.get("EEM_TLS_CERT", "")
        )

        self.PACKAGE_OUTPUT_DIR = os.environ.get(
            "EEM_PACKAGE_OUTPUT_DIR", os.path.join(repo_root, "instance", "packages")
        )
        # Built MSIs carry an enrollment token, so they are swept on a schedule
        # rather than kept forever (section 23).
        self.PACKAGE_RETENTION_DAYS = _int("EEM_PACKAGE_RETENTION_DAYS", 30)

        # Minimum for the agent's local administrator password.
        self.AGENT_PASSWORD_MIN_LENGTH = _int("EEM_AGENT_PASSWORD_MIN_LENGTH", 8)

        # --- Live screen viewing (sections 14, 29) -------------------------
        # A browser cannot set an Authorization header on a WebSocket, so a
        # viewer first fetches a short-lived single-use ticket over the normal
        # authenticated API, then opens the socket with it.
        self.SCREEN_TICKET_TTL_SECONDS = _int("EEM_SCREEN_TICKET_TTL_SECONDS", 30)

        # A viewing session with no active viewer socket is torn down after
        # this long, forcing the capture to stop server-side even if a client
        # vanished without closing cleanly.
        self.SCREEN_SESSION_IDLE_SECONDS = _int("EEM_SCREEN_SESSION_IDLE_SECONDS", 20)

        # Reject oversized frames rather than buffer them. A 4K JPEG at
        # reasonable quality is well under this.
        self.SCREEN_MAX_FRAME_BYTES = _int("EEM_SCREEN_MAX_FRAME_BYTES", 6_000_000)

        # Capture hints sent to the agent. Frames are transient; they are never
        # written to the database or disk (section 14).
        self.SCREEN_TARGET_FPS = _int("EEM_SCREEN_TARGET_FPS", 5)
        self.SCREEN_JPEG_QUALITY = _int("EEM_SCREEN_JPEG_QUALITY", 55)

        # Endpoint capture CPU controls (also honoured during recording, where
        # frames cannot simply be skipped -- see ScreenHub._tell_agent_start).
        #  * CPU budget: ceiling on capture CPU as a share of one core; the agent
        #    trades JPEG quality (down to the floor) to hold it. 0 disables it.
        #  * quality floor: how far quality may fall before the agent logs that
        #    the budget cannot be met (a very high-resolution screen under
        #    continuous motion -- the fix there is a lower recording fps).
        self.SCREEN_CPU_BUDGET_PERCENT = _int("EEM_SCREEN_CPU_BUDGET_PERCENT", 5)
        self.SCREEN_JPEG_QUALITY_FLOOR = _int("EEM_SCREEN_JPEG_QUALITY_FLOOR", 25)

        # Recording frame rate on a 4K-class screen (longest edge >= 3840 px).
        # Encoding an 8-megapixel frame five times a second cannot fit the CPU
        # budget at full quality; a lower rate keeps a 4K recording within budget
        # without the governor having to drop quality hard. Everything below 4K
        # records at the policy's fps (default 5). Both the recorder and the agent
        # are driven from this same choice so the recording timeline stays exact.
        self.SCREEN_RECORDING_FPS_4K = _int("EEM_SCREEN_RECORDING_FPS_4K", 3)

        # --- Screen recording (sections 14, 23, 24) ------------------------
        # Frames are encoded to H.264 by FFmpeg and written as AES-encrypted
        # segment files. Frames themselves NEVER touch the database; only
        # segment metadata is indexed.
        # FFmpeg location. On Windows we ship it under server/tools/ffmpeg; on
        # Linux/macOS we find the system install (apt install ffmpeg), so a
        # normal server needs no path configuration.
        if os.name == "nt":
            default_ffmpeg = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "..", "tools", "ffmpeg", "ffmpeg.exe"
            ))
        else:
            default_ffmpeg = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        self.FFMPEG_PATH = os.environ.get("EEM_FFMPEG_PATH", default_ffmpeg)
        self.RECORDING_DIR = os.environ.get(
            "EEM_RECORDING_DIR", os.path.join(repo_root, "instance", "recordings")
        )
        # Passphrase for encrypting screen data (recordings + screenshots) at
        # rest; a 32-byte AES key is derived from it. Order: an explicit env key
        # wins (best -- keep it in a secret manager); otherwise, unless disabled,
        # a strong key is auto-generated and persisted so recording works out of
        # the box while never writing anything unencrypted (sections 14, 24).
        env_key = os.environ.get("EEM_RECORDING_KEY", "").strip()
        if env_key:
            self.RECORDING_KEY_PASSPHRASE = env_key
            self.RECORDING_KEY_SOURCE = "env"
        elif _bool("EEM_RECORDING_AUTO_KEY", True):
            self.RECORDING_KEY_PASSPHRASE = _load_or_create_recording_key(
                os.path.join(repo_root, "instance")
            )
            self.RECORDING_KEY_SOURCE = "file"
        else:
            self.RECORDING_KEY_PASSPHRASE = None
            self.RECORDING_KEY_SOURCE = None
        self.RECORDING_SEGMENT_SECONDS = _int("EEM_RECORDING_SEGMENT_SECONDS", 300)
        self.RECORDING_FPS = _int("EEM_RECORDING_FPS", 5)
        self.RECORDING_DEFAULT_RETENTION_DAYS = _int("EEM_RECORDING_RETENTION_DAYS", 30)

        # --- Screenshots (still frames captured from the live viewer) -------
        # Encrypted at rest with the SAME screen-data key as recordings
        # (section 14: screenshots go to encrypted storage with retention +
        # auto-deletion). No FFmpeg needed -- a screenshot is a single JPEG.
        self.SCREENSHOT_DIR = os.environ.get(
            "EEM_SCREENSHOT_DIR", os.path.join(repo_root, "instance", "screenshots")
        )
        self.SCREENSHOT_RETENTION_DAYS = _int("EEM_SCREENSHOT_RETENTION_DAYS", 90)
        self.SCREENSHOT_MAX_BYTES = _int("EEM_SCREENSHOT_MAX_BYTES", 25 * 1024 * 1024)

        # --- Code signing (optional) ---------------------------------------
        # For an internal deployment, sign each generated MSI with a self-signed
        # or internal-CA certificate whose public half is trusted on managed
        # machines via GPO. If no PFX is configured, packages are built unsigned
        # (they still install, but trigger SmartScreen / AppLocker).
        #
        # The PFX password is a secret: it comes from the environment and is
        # passed to the signer through the environment, never on a command line.
        self.SIGNING_PFX_PATH = os.environ.get("EEM_SIGNING_PFX", "").strip() or None
        self.SIGNING_PASSWORD = os.environ.get("EEM_SIGNING_PASSWORD", "").strip() or None
        self.SIGNING_TIMESTAMP_URL = os.environ.get("EEM_SIGNING_TIMESTAMP_URL", "").strip() or None
        default_signer = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "..", "agent", "signing", "Sign-File.ps1",
        )
        self.SIGNING_SCRIPT = os.environ.get("EEM_SIGNING_SCRIPT", os.path.normpath(default_signer))

        # Present only during initial installation, then removed from the env.
        self.BOOTSTRAP_SECRET = os.environ.get("EEM_BOOTSTRAP_SECRET", "").strip() or None

    @property
    def RECORDING_ENABLED(self) -> bool:  # noqa: N802
        """Recording can run only with an encryption key and FFmpeg present."""
        return bool(self.RECORDING_KEY_PASSPHRASE) and os.path.isfile(self.FFMPEG_PATH)

    @property
    def SCREENSHOT_ENABLED(self) -> bool:  # noqa: N802
        """Screenshots can be saved once a screen-data encryption key is set
        (they reuse RECORDING_KEY_PASSPHRASE). Unlike recording, no FFmpeg."""
        return bool(self.RECORDING_KEY_PASSPHRASE)

    @property
    def SIGNING_ENABLED(self) -> bool:  # noqa: N802 (config constant style)
        return bool(self.SIGNING_PFX_PATH and self.SIGNING_PASSWORD)

    def as_dict(self) -> dict:
        return {
            key: getattr(self, key)
            for key in dir(self)
            if key.isupper() and not key.startswith("_")
        }


class TestConfig(Config):
    """In-memory configuration used by the test suite."""

    # Not a pytest test class, despite the name.
    __test__ = False

    def __init__(self) -> None:
        os.environ.setdefault("EEM_SECRET_KEY", "test-secret-key-not-for-production-use-only")
        # Tests never write a key file; those that need recording set the
        # passphrase on app.config directly.
        os.environ.setdefault("EEM_RECORDING_AUTO_KEY", "0")
        super().__init__()
        self.SQLALCHEMY_DATABASE_URI = "sqlite://"
        self.TESTING = True
        self.RATELIMIT_ENABLED = False
