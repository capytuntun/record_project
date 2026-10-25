"""Authenticode signing through ``agent/signing/Sign-File.ps1``.

Shared by the MSI packager and the console's agent rebuild, so both sign with
the same certificate and script, and both follow the same rule: the PFX
password crosses to PowerShell through the environment, never argv, so it
never appears in the process list.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

SIGN_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class SigningSettings:
    pfx_path: Path
    # Kept out of repr so the settings can be logged or shown in a traceback
    # without leaking the password.
    password: str = field(repr=False)
    script: str
    timestamp_url: str | None = None

    @classmethod
    def from_config(cls, config: Mapping) -> SigningSettings | None:
        """The configured certificate, or None when signing is not set up."""
        if not config.get("SIGNING_ENABLED"):
            return None
        return cls(
            pfx_path=Path(config["SIGNING_PFX_PATH"]),
            password=config["SIGNING_PASSWORD"],
            script=config["SIGNING_SCRIPT"],
            timestamp_url=config.get("SIGNING_TIMESTAMP_URL") or None,
        )


def sign_file(
    path: Path, settings: SigningSettings, *, timeout: int = SIGN_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess:
    """Run the signer on ``path``. Raises subprocess.TimeoutExpired on timeout;
    the caller decides what a non-zero return code means for it."""
    command = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", settings.script,
        "-File", str(path),
        "-CertPath", str(settings.pfx_path),
    ]
    if settings.timestamp_url:
        command += ["-TimestampUrl", settings.timestamp_url]

    env = dict(os.environ)
    env["EEM_SIGNING_PASSWORD"] = settings.password

    return subprocess.run(
        command,
        capture_output=True,
        # Same reason as the agent build: the locale codec (cp950) cannot
        # decode everything PowerShell may print.
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=env,
        shell=False,
    )
