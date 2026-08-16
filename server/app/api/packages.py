"""Installer package generation and download: /api/packages (spec section 18).

One request does the whole job an administrator asked for: mint an enrollment
token with the requested validity and usage limit, build an MSI around it, and
hand back a download link.

The built file contains a live enrollment token, so it is treated as a secret:
downloading requires permission, every download is audited, and stored files
are swept after a retention window.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request, send_file

from ..errors import AuthorizationError, ConflictError, NotFoundError, ValidationError
from ..models import EnrollmentToken, db, utcnow
from ..models.audit import CREATE_ENROLLMENT_TOKEN
from ..models.package import (
    STATUS_DELETED,
    STATUS_FAILED,
    STATUS_READY,
    InstallationPackage,
)
from ..request_context import require_current_user
from ..security.authn import require_permission
from ..security.passwords import generate_secret, sha256_hex
from ..security.rbac import PACKAGES_CREATE, PACKAGES_DOWNLOAD, PACKAGES_READ
from ..services import agent_build, audit, packaging
from .enrollment import MAX_USES, _read_validity
from .validation import get_int, get_str, json_body, paginated, pagination

bp = Blueprint("packages", __name__, url_prefix="/api/packages")

CREATE_PACKAGE = "CREATE_PACKAGE"
DOWNLOAD_PACKAGE = "DOWNLOAD_PACKAGE"
DELETE_PACKAGE = "DELETE_PACKAGE"
REBUILD_AGENT = "REBUILD_AGENT"


@bp.get("/toolchain")
@require_permission(PACKAGES_READ)
def toolchain():
    """Whether this server can build packages. Lets the console say why not."""
    return jsonify(packaging.toolchain_status())


# --- rebuilding the agent binary -------------------------------------------
#
# Generating a package only rewraps a pre-built EndpointAgent.exe, so changing
# agent source and forgetting to rebuild ships a stale agent with no symptom.
# These two routes let the console notice that and fix it, instead of requiring
# a PowerShell session on the server.
#
# SUPER_ADMIN only, and not reachable through the grantable "packages" feature:
# this replaces the executable that lands on every managed endpoint, which is a
# larger act than minting one package. Both starting a build and its outcome are
# audited.


def _agent_paths() -> tuple[Path, Path]:
    config = current_app.config
    return Path(config["AGENT_ROOT_PATH"]), Path(config["AGENT_BINARY_PATH"])


def _agent_build_payload() -> dict:
    agent_root, binary = _agent_paths()
    payload = agent_build.source_status(agent_root / "src", binary)
    payload["build"] = agent_build.builder.status()
    payload["dotnetAvailable"] = bool(
        agent_build.find_dotnet(current_app.config.get("DOTNET_COMMAND", ""))
    )
    payload["canRebuild"] = payload["sourceAvailable"] and payload["dotnetAvailable"]
    return payload


@bp.get("/agent-build")
@require_permission(PACKAGES_READ)
def agent_build_status():
    """Is the built agent stale, can this server rebuild it, and how did it go?"""
    return jsonify(_agent_build_payload())


@bp.post("/agent-build")
@require_permission(PACKAGES_CREATE)
def start_agent_build():
    actor = require_current_user()
    if not actor.is_super_admin:
        audit.record_denied(REBUILD_AGENT, reason="not super admin")
        db.session.commit()
        raise AuthorizationError("只有最高管理員可以重建 Agent 程式。")

    agent_root, _ = _agent_paths()
    if not (agent_root / "src").is_dir():
        raise ConflictError(
            f"這台伺服器沒有 Agent 原始碼（{agent_root / 'src'}），無法重建。"
            "請改在有原始碼的機器執行 agent\\build.ps1。"
        )

    dotnet = agent_build.find_dotnet(current_app.config.get("DOTNET_COMMAND", ""))
    if dotnet is None:
        raise ConflictError(
            "找不到 .NET SDK，無法重建 Agent。請執行 install.ps1 讓它安裝，"
            "或自行安裝後重啟伺服器。"
        )

    publish_dir = Path(current_app.config["AGENT_BINARY_PATH"]).parent
    if not agent_build.builder.start(
        dotnet=dotnet, agent_root=agent_root, publish_dir=publish_dir
    ):
        raise ConflictError("已經有一個重建在進行中。")

    audit.record(REBUILD_AGENT, metadata={"agentRoot": str(agent_root)})
    db.session.commit()
    return jsonify(_agent_build_payload()), 202


@bp.get("")
@require_permission(PACKAGES_READ)
def list_packages():
    offset, limit = pagination()
    query = db.session.query(InstallationPackage).filter(
        InstallationPackage.status != STATUS_DELETED
    )
    total = query.count()
    rows = (
        query.order_by(InstallationPackage.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return jsonify(paginated([row.to_dict() for row in rows], total, offset, limit))


@bp.post("")
@require_permission(PACKAGES_CREATE)
def create_package():
    """Mint a token, build the MSI, and return where to download it."""
    actor = require_current_user()
    body = json_body()

    label = get_str(body, "label", max_length=128)
    organization_id = get_str(body, "organizationId", required=False, max_length=64)
    server_url = _resolve_server_url(body)
    expires_at, validity = _read_validity(body)
    max_uses = get_int(body, "maxUses", default=1, minimum=0, maximum=MAX_USES)
    admin_password = get_str(
        body, "adminPassword", required=False, max_length=256, strip=False
    )

    status = packaging.toolchain_status()
    if not status["ready"]:
        raise ConflictError("此伺服器目前無法建置安裝包：" + "；".join(status["problems"]))

    password_hash = None
    if admin_password:
        minimum = current_app.config["AGENT_PASSWORD_MIN_LENGTH"]
        if len(admin_password) < minimum:
            raise ValidationError(f"管理密碼至少需要 {minimum} 個字元。")
        # Hashed here; the plaintext is never stored, logged or audited.
        password_hash = packaging.hash_admin_password(admin_password)

    plaintext_token = generate_secret(48)
    token = EnrollmentToken(
        token_hash=sha256_hex(plaintext_token),
        label=label,
        organization_id=organization_id,
        expires_at=expires_at,
        max_uses=max_uses,
        created_by=actor.id,
        notes=f"由安裝包產生器建立（{label}）",
    )
    db.session.add(token)
    db.session.flush()

    package = InstallationPackage(
        label=label,
        organization_id=organization_id,
        enrollment_token_id=token.id,
        created_by=actor.id,
        has_admin_password=1 if password_hash else 0,
        file_expires_at=utcnow()
        + timedelta(days=current_app.config["PACKAGE_RETENTION_DAYS"]),
    )
    db.session.add(package)
    db.session.flush()

    filename = packaging.safe_filename(label, package.id)
    output = packaging.package_path(filename)

    try:
        built = packaging.build_msi(
            package_id=package.id,
            label=label,
            server_url=server_url,
            enrollment_token=plaintext_token,
            organization_id=organization_id,
            admin_password_hash=password_hash,
            output_path=output,
        )
    except packaging.PackagingError as exc:
        package.status = STATUS_FAILED
        package.failure_reason = str(exc)[:255]
        # The token was never shipped anywhere, so retire it rather than leave
        # a usable credential behind for a package that does not exist.
        token.revoked_at = utcnow()
        token.revoked_reason = "package_build_failed"
        audit.record(
            CREATE_PACKAGE,
            target_type="package",
            target_id=package.id,
            result="FAILURE",
            metadata={"label": label, "reason": str(exc)[:200]},
        )
        db.session.commit()
        raise ConflictError(f"安裝包建置失敗：{exc}") from exc

    package.status = STATUS_READY
    package.filename = filename
    package.size_bytes = built["sizeBytes"]
    package.sha256 = built["sha256"]
    package.agent_version = built["agentVersion"]
    package.signed = 1 if built.get("signed") else 0

    audit.record(
        CREATE_ENROLLMENT_TOKEN,
        target_type="enrollment_token",
        target_id=token.id,
        metadata={
            "label": label,
            "via": "package_generator",
            "validity": validity,
            "neverExpires": token.never_expires,
            "maxUses": max_uses,
        },
    )
    audit.record(
        CREATE_PACKAGE,
        target_type="package",
        target_id=package.id,
        # Neither the token nor the password appears here.
        metadata={
            "label": label,
            "organizationId": organization_id,
            "serverUrl": server_url,
            "validity": validity,
            "maxUses": max_uses,
            "adminPasswordSet": bool(password_hash),
            "sha256": built["sha256"],
            "sizeBytes": built["sizeBytes"],
        },
    )
    db.session.commit()

    payload = package.to_dict()
    payload["validity"] = validity
    payload["downloadUrl"] = f"/api/packages/{package.id}/download"
    payload["serverUrl"] = server_url
    return jsonify(payload), 201


@bp.get("/<package_id>/download")
@require_permission(PACKAGES_DOWNLOAD)
def download_package(package_id: str):
    package = db.session.get(InstallationPackage, package_id)
    if package is None or package.status != STATUS_READY or not package.filename:
        raise NotFoundError("找不到可下載的安裝包。")

    path = packaging.package_path(package.filename)
    if not path.is_file():
        # Swept by retention, or removed from disk out of band.
        package.status = STATUS_DELETED
        db.session.commit()
        raise NotFoundError("安裝包檔案已不存在，請重新產生。")

    package.download_count += 1
    package.last_downloaded_at = utcnow()

    audit.record(
        DOWNLOAD_PACKAGE,
        target_type="package",
        target_id=package.id,
        metadata={"label": package.label, "downloadCount": package.download_count},
    )
    db.session.commit()

    return send_file(
        path,
        as_attachment=True,
        download_name=package.filename,
        mimetype="application/x-msi",
        max_age=0,
    )


@bp.delete("/<package_id>")
@require_permission(PACKAGES_CREATE)
def delete_package(package_id: str):
    """Remove the stored file and revoke the token it carries."""
    package = db.session.get(InstallationPackage, package_id)
    if package is None or package.status == STATUS_DELETED:
        raise NotFoundError("找不到安裝包。")

    if package.filename:
        path = packaging.package_path(package.filename)
        if path.is_file():
            path.unlink()

    revoked = False
    token = db.session.get(EnrollmentToken, package.enrollment_token_id)
    if token is not None and token.revoked_at is None:
        # Deleting the file does not recall copies already downloaded; revoking
        # the token is what actually stops them working.
        token.revoked_at = utcnow()
        token.revoked_reason = "package_deleted"
        revoked = True

    package.status = STATUS_DELETED

    audit.record(
        DELETE_PACKAGE,
        target_type="package",
        target_id=package.id,
        metadata={"label": package.label, "tokenRevoked": revoked},
    )
    db.session.commit()
    return jsonify({"status": "deleted", "id": package.id, "tokenRevoked": revoked})


def _resolve_server_url(body: dict) -> str:
    """The URL the agent will call home to.

    Defaults to the address this request arrived on, which is right for a
    single-server deployment and wrong behind a proxy -- so it can be
    overridden explicitly.
    """
    supplied = get_str(body, "serverUrl", required=False, max_length=255)
    url = supplied or request.url_root.rstrip("/")

    if not url.startswith(("https://", "http://")):
        raise ValidationError("伺服器網址必須以 https:// 或 http:// 開頭。")
    if url.startswith("http://") and not _is_loopback(url):
        # The device credential travels over this URL (section 24).
        raise ValidationError(
            "非本機的管理伺服器網址必須使用 https://，否則裝置憑證會以明文傳輸。"
        )
    return url


def _is_loopback(url: str) -> bool:
    host = url.split("://", 1)[1].split("/", 1)[0].split(":", 1)[0]
    return host in {"localhost", "127.0.0.1", "::1"}
