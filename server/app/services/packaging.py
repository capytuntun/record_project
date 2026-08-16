"""Builds a configured MSI by invoking WiX (spec section 18).

The agent binary is published once, ahead of time. Each request rebuilds only
the MSI wrapper around it, with that package's server URL, enrollment token and
administrator password hash baked in -- a few seconds, rather than the minutes
a full agent build would take.

Nothing here ever writes the plaintext enrollment token or password to a log.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

from flask import current_app

logger = logging.getLogger(__name__)

# Must match EndpointAgent's AdminPassword.Verify.
PBKDF2_ITERATIONS = 210_000
PBKDF2_SALT_BYTES = 16
PBKDF2_HASH_BYTES = 32

BUILD_TIMEOUT_SECONDS = 180


class PackagingError(RuntimeError):
    """The MSI could not be built. The message is safe to show an admin."""


def hash_admin_password(password: str) -> str:
    """Produce the PBKDF2 string the agent verifies against.

    Format: pbkdf2-sha256$iterations$base64(salt)$base64(hash)
    """
    salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS, PBKDF2_HASH_BYTES
    )
    return (
        f"pbkdf2-sha256${PBKDF2_ITERATIONS}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    )


def toolchain_status() -> dict:
    """Report whether this server can build packages, and why not if it cannot.

    Called by the API so the console can explain a missing toolchain instead of
    failing at build time with a stack trace.
    """
    config = current_app.config
    agent_binary = Path(config["AGENT_BINARY_PATH"])
    wix_source = Path(config["WIX_SOURCE_PATH"])
    ca_dll = Path(config["AGENT_CA_DLL_PATH"])
    wix_command = shutil.which(config["WIX_COMMAND"])

    # WiX only supports Windows -- v5 says so itself ("The WiX Toolset only
    # supports Windows ... All behavior after this point is undefined") and a
    # build of this .wxs on Linux fails on WIX0389 path validation. So on a
    # Linux server, say that instead of suggesting a `dotnet tool install` that
    # cannot lead anywhere.
    if os.name != "nt":
        return {
            "ready": False,
            "problems": [
                "此伺服器是 Linux，無法建置 MSI：WiX 只支援 Windows。請改在一台 "
                "Windows 上執行 agent/packaging/New-AgentMsi.ps1 產生安裝包"
                "（先在主控台建立註冊憑證），詳見 docs/deployment.md §8.1。"
            ],
            "agentBinary": str(agent_binary),
            "agentBinaryExists": agent_binary.is_file(),
            "wixCommand": wix_command,
            "certificateEmbedded": False,
            "signingEnabled": bool(config.get("SIGNING_ENABLED")),
        }

    problems = []
    if not agent_binary.is_file():
        problems.append(f"找不到 Agent 執行檔：{agent_binary}")
    if not ca_dll.is_file():
        problems.append(
            f"找不到解除安裝自訂動作 DLL：{ca_dll}。請先建置 EndpointAgent.CustomActions。"
        )
    if not wix_source.is_file():
        problems.append(f"找不到 WiX 封裝定義：{wix_source}")
    if wix_command is None:
        problems.append(
            f"找不到 WiX 工具（{config['WIX_COMMAND']}）。"
            "請執行：dotnet tool install --global wix --version 5.*"
        )

    # Whether generated packages will carry this server's certificate. Without
    # it, every endpoint has to be told to trust the server some other way
    # before the agent can connect at all -- so the console says which it is.
    cert_path = (config.get("PACKAGE_CA_CERT_PATH") or "").strip()

    return {
        "ready": not problems,
        "problems": problems,
        "agentBinary": str(agent_binary),
        "agentBinaryExists": agent_binary.is_file(),
        "wixCommand": wix_command,
        "certificateEmbedded": bool(cert_path) and Path(cert_path).is_file(),
        # Whether generated MSIs will be signed. Unsigned is allowed but worth
        # surfacing so the console can warn.
        "signingEnabled": bool(config.get("SIGNING_ENABLED")),
    }


_PEM_CERT = re.compile(
    rb"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", re.DOTALL
)


def _stage_server_certificate(work: Path) -> tuple[Path, str] | None:
    """Stage the server's TLS certificate for embedding, as DER + SHA-1 thumbprint.

    An endpoint must trust this server before the agent's very first HTTPS call,
    and the default deployment uses a self-signed certificate. Without a GPO to
    push it, the one thing that reaches every endpoint is the installer itself,
    so the certificate rides along inside the MSI (section 24).

    Only the **leaf** is embedded, never an issuing CA. That leaf carries
    BasicConstraints CA:FALSE, so adding it to the endpoint's trusted roots
    authorises exactly one certificate -- this server's own -- and cannot be
    used to vouch for any other name. Installing a real CA would be a far larger
    grant, and is deliberately not what this does.

    Returns None when no certificate is configured or it cannot be read, in
    which case the package is built without one.
    """
    configured = (current_app.config.get("PACKAGE_CA_CERT_PATH") or "").strip()
    if not configured:
        return None

    source = Path(configured)
    if not source.is_file():
        logger.warning("package certificate not found at %s; building without it", source)
        return None

    raw = source.read_bytes()
    if b"-----BEGIN CERTIFICATE-----" in raw:
        # A PEM may hold a chain; the first block is the leaf.
        match = _PEM_CERT.search(raw)
        if match is None:
            logger.warning("no certificate block in %s; building without it", source)
            return None
        try:
            der = base64.b64decode(match.group(1), validate=False)
        except (ValueError, TypeError):
            logger.warning("certificate in %s is not valid base64; building without it", source)
            return None
    else:
        der = raw

    if not der:
        logger.warning("certificate in %s is empty; building without it", source)
        return None

    staged = work / "server-ca.cer"
    staged.write_bytes(der)
    # certutil identifies a certificate for deletion by its SHA-1 thumbprint,
    # so the uninstall side needs it baked in at build time.
    return staged, hashlib.sha1(der, usedforsecurity=False).hexdigest()


_SAFE_LABEL = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(label: str, package_id: str) -> str:
    """A download name derived from the label, with anything risky stripped.

    The label is administrator-supplied and ends up in a Content-Disposition
    header and on a filesystem, so it is reduced to a conservative character
    set rather than trusted.
    """
    cleaned = _SAFE_LABEL.sub("-", label).strip("-.")[:48] or "endpoint-agent"
    return f"{cleaned}-{package_id[:8]}.msi"


def build_msi(
    *,
    package_id: str,
    label: str,
    server_url: str,
    enrollment_token: str,
    organization_id: str | None,
    admin_password_hash: str | None,
    output_path: Path,
) -> dict:
    """Build the MSI and return {'sizeBytes', 'sha256', 'agentVersion'}.

    Raises PackagingError with an administrator-readable message on failure.
    """
    status = toolchain_status()
    if not status["ready"]:
        raise PackagingError("；".join(status["problems"]))

    config = current_app.config
    agent_binary = Path(config["AGENT_BINARY_PATH"]).resolve()
    wix_source = Path(config["WIX_SOURCE_PATH"]).resolve()
    ca_dll = Path(config["AGENT_CA_DLL_PATH"]).resolve()
    agent_version = config["AGENT_VERSION"]

    # The generated config is the agent's starting state. The token is here
    # because the agent needs it once, on first start; the agent clears it from
    # its own copy as soon as enrollment succeeds.
    agent_config = {
        "serverUrl": server_url,
        "enrollmentToken": enrollment_token,
        "organizationId": organization_id,
        "adminPasswordHash": admin_password_hash,
        "logLevel": "Information",
        "heartbeatIntervalSeconds": config["HEARTBEAT_INTERVAL_SECONDS"],
    }
    agent_config = {k: v for k, v in agent_config.items() if v is not None}

    with tempfile.TemporaryDirectory(prefix="eem-pkg-") as workdir:
        work = Path(workdir)
        config_file = work / "agent.config.json"
        config_file.write_text(json.dumps(agent_config, indent=2), encoding="utf-8")

        staged_msi = work / "package.msi"

        # Optional: the server's certificate, so the endpoint trusts it without
        # a separate GPO push. Absent -> the .wxs omits the whole component.
        certificate = _stage_server_certificate(work)
        cert_args: list[str] = []
        if certificate is not None:
            cert_path, cert_thumbprint = certificate
            cert_args = [
                "-d", f"ServerCaCert={cert_path}",
                "-d", f"ServerCaThumbprint={cert_thumbprint}",
            ]
            logger.info(
                "package %s embeds server certificate %s", package_id, cert_thumbprint[:16]
            )

        command = [
            config["WIX_COMMAND"], "build", str(wix_source),
            # 64-bit package: installs to Program Files (not x86) and writes
            # native-hive registry (the tray Run key lands in the real Run key,
            # not the WOW6432Node redirect). The agent binary is win-x64.
            "-arch", "x64",
            "-ext", "WixToolset.Util.wixext",
            "-ext", "WixToolset.Firewall.wixext",
            "-d", f"AgentBinary={agent_binary}",
            "-d", f"CustomActionDll={ca_dll}",
            "-d", f"ConfigFile={config_file}",
            "-d", f"ServerUrl={server_url}",
            "-d", f"OrgId={organization_id or ''}",
            "-d", f"PackageLabel={label}",
            "-d", f"AgentVersion={agent_version}",
            *cert_args,
            "-o", str(staged_msi),
        ]

        logger.info("building package %s (label=%r)", package_id, label)
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                # Explicit UTF-8 rather than text=True: text=True decodes with
                # the system locale, and on a Traditional Chinese Windows
                # (cp950) a byte WiX emits can kill the reader thread with
                # UnicodeDecodeError -- losing the build output on exactly the
                # failures whose message we are about to report.
                encoding="utf-8",
                errors="replace",
                timeout=BUILD_TIMEOUT_SECONDS,
                cwd=work,
                # No shell: the label and URL reach WiX as argv entries, so
                # nothing in them can be interpreted as shell syntax.
                shell=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise PackagingError("建置逾時，請稍後再試。") from exc
        except FileNotFoundError as exc:
            raise PackagingError(f"找不到 WiX 工具：{config['WIX_COMMAND']}") from exc

        if result.returncode != 0:
            # WiX errors can name server paths, so they go to the log and only
            # a summary reaches the client (section 26).
            logger.error(
                "wix build failed for package %s rc=%s\n%s\n%s",
                package_id, result.returncode, result.stdout[-4000:], result.stderr[-4000:],
            )
            first_error = next(
                (line for line in (result.stdout or "").splitlines() if "error" in line.lower()),
                "",
            )
            detail = first_error.split(":", 1)[-1].strip() if first_error else ""
            raise PackagingError(f"MSI 建置失敗。{detail}"[:240])

        if not staged_msi.is_file():
            raise PackagingError("MSI 建置未產生檔案。")

        # Sign in place, before hashing, so the recorded hash is of the file the
        # client actually receives. Unsigned when no certificate is configured.
        signed = _sign_if_configured(staged_msi, package_id)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged_msi), str(output_path))

    digest = hashlib.sha256()
    with output_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)

    size = output_path.stat().st_size
    logger.info("package %s built: %s bytes (signed=%s)", package_id, size, signed)

    return {
        "sizeBytes": size,
        "sha256": digest.hexdigest(),
        "agentVersion": agent_version,
        "signed": signed,
    }


def _sign_if_configured(msi_path: Path, package_id: str) -> bool:
    """Authenticode-sign the MSI when a certificate is configured.

    Returns True if signed, False if signing is not configured. Raises
    PackagingError if signing IS configured but fails -- a package that was
    meant to be signed must not go out unsigned silently.
    """
    config = current_app.config
    if not config.get("SIGNING_ENABLED"):
        return False

    pfx = Path(config["SIGNING_PFX_PATH"])
    if not pfx.is_file():
        raise PackagingError(f"設定的簽章憑證不存在：{pfx}")

    command = [
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
        "-File", config["SIGNING_SCRIPT"],
        "-File", str(msi_path),
        "-CertPath", str(pfx),
    ]
    if config.get("SIGNING_TIMESTAMP_URL"):
        command += ["-TimestampUrl", config["SIGNING_TIMESTAMP_URL"]]

    # The password crosses to PowerShell through the environment, not argv, so
    # it never appears in the process list.
    env = dict(os.environ)
    env["EEM_SIGNING_PASSWORD"] = config["SIGNING_PASSWORD"]

    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=BUILD_TIMEOUT_SECONDS, env=env, shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PackagingError("簽章逾時。") from exc

    if result.returncode != 0:
        logger.error("signing failed for %s\n%s\n%s",
                     package_id, result.stdout[-2000:], result.stderr[-2000:])
        raise PackagingError("MSI 簽章失敗，請檢查伺服器記錄。")

    logger.info("package %s signed", package_id)
    return True


def package_path(filename: str) -> Path:
    """Resolve a stored package file, refusing anything outside the store."""
    root = Path(current_app.config["PACKAGE_OUTPUT_DIR"]).resolve()
    candidate = (root / os.path.basename(filename)).resolve()
    # basename() already strips traversal; this rejects symlink escapes too.
    if not candidate.is_relative_to(root):
        raise PackagingError("套件路徑無效。")
    return candidate
