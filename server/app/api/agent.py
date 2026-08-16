"""Endpoint-agent facing API: /api/agent (spec sections 9, 10, 11, 21).

Two routes, deliberately: enroll once with an installer token, then heartbeat
with the per-device credential that enrollment issued. Nothing here accepts an
endpoint id from the caller -- identity comes from the presented credential, so
one agent cannot report as another.
"""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, current_app, jsonify
from sqlalchemy import or_, update

from ..errors import AuthenticationError, ValidationError
from ..extensions import limiter
from ..models import Endpoint, EndpointCredential, EnrollmentToken, db, iso, utcnow
from ..models.inventory import EndpointInventory
from ..models.audit import (
    ENDPOINT_ENROLLED,
    RESULT_DENIED,
    RESULT_FAILURE,
    UNINSTALL_ATTEMPT,
)
from ..request_context import client_ip, current_credential, current_endpoint
from ..security.authn import require_agent_auth
from ..security.passwords import generate_secret, sha256_hex
from ..services import audit
from .validation import get_str, json_body

bp = Blueprint("agent", __name__, url_prefix="/api/agent")

# Device credentials outlive an installer token but still rotate.
CREDENTIAL_TTL_DAYS = 365

# Start warning the endpoint this far ahead. The warning has to reach the agent
# while its credential still authenticates -- after expiry there is no
# authenticated channel left to warn over.
CREDENTIAL_WARN_WITHIN_DAYS = 30

# Shown on the endpoint so enterprise IT sees why an install failed.
ENROLL_FAILURE_MESSAGES = {
    "expired": "此安裝包的註冊憑證已過期，請向 IT 索取新的安裝包。",
    "revoked": "此安裝包的註冊憑證已被撤銷，請向 IT 索取新的安裝包。",
    "exhausted": "此註冊憑證的可用次數已用完，請向 IT 索取新的安裝包。",
}


@bp.post("/enroll")
@limiter.limit("20 per hour")
def enroll():
    """Exchange a valid enrollment token for a unique endpoint id and credential.

    Failure is deliberately vague to the caller: an installer running on an
    untrusted machine should not learn whether a token exists, is expired, or
    is used up. The specific reason goes to the audit log.
    """
    body = json_body()
    presented = get_str(body, "enrollmentToken", min_length=10, max_length=512, strip=False)

    token = (
        db.session.query(EnrollmentToken)
        .filter(EnrollmentToken.token_hash == sha256_hex(presented or ""))
        .one_or_none()
    )
    if token is None:
        # An unrecognised token stays vague: the caller does not hold a token we
        # know about, so there is nothing to confirm to them.
        _record_enroll_failure("unknown_token")
        raise AuthenticationError("註冊失敗，請確認安裝包是否正確。")

    reason = token.unusable_reason()
    if reason is not None:
        _record_enroll_failure(reason, token_id=token.id)
        # The caller already holds this token, so naming the problem reveals
        # nothing new -- and it lets the agent show enterprise IT something
        # actionable instead of a dead end.
        raise AuthenticationError(ENROLL_FAILURE_MESSAGES[reason], details={"reason": reason})

    # Claim one use atomically so two installers racing on a single-use token
    # cannot both succeed. max_uses 0 means unlimited.
    claimed = db.session.execute(
        update(EnrollmentToken)
        .where(
            EnrollmentToken.id == token.id,
            EnrollmentToken.revoked_at.is_(None),
            or_(EnrollmentToken.max_uses == 0,
                EnrollmentToken.use_count < EnrollmentToken.max_uses),
        )
        .values(use_count=EnrollmentToken.use_count + 1)
    )
    if claimed.rowcount != 1:
        db.session.rollback()
        _record_enroll_failure("usage_limit_race", token_id=token.id)
        raise AuthenticationError(ENROLL_FAILURE_MESSAGES["exhausted"],
                                  details={"reason": "exhausted"})

    endpoint = Endpoint(
        organization_id=token.organization_id,
        device_name=get_str(body, "deviceName", required=False, max_length=128),
        os_name=get_str(body, "os", required=False, max_length=128),
        os_version=get_str(body, "osVersion", required=False, max_length=64),
        agent_version=get_str(body, "agentVersion", required=False, max_length=32),
        local_user=get_str(body, "localUser", required=False, max_length=128),
        last_ip=client_ip(),
        enrolled_at=utcnow(),
        last_seen_at=utcnow(),
        enrolled_via_token_id=token.id,
    )
    db.session.add(endpoint)
    db.session.flush()

    secret, credential = _issue_credential(endpoint)

    audit.record(
        ENDPOINT_ENROLLED,
        actor=None,
        actor_type="AGENT",
        actor_username=None,
        target_type="endpoint",
        target_id=endpoint.id,
        metadata={
            "enrollmentTokenId": token.id,
            "deviceName": endpoint.device_name,
            "os": endpoint.os_name,
            "agentVersion": endpoint.agent_version,
        },
    )
    db.session.commit()

    return (
        jsonify(
            {
                "endpointId": endpoint.id,
                "organizationId": endpoint.organization_id,
                "policyId": token.policy_id,
                # Returned once. The agent stores it in machine-scoped
                # protected storage; the server keeps only its hash.
                "deviceCredential": secret,
                "credentialExpiresAt": iso(credential.expires_at),
                "heartbeatIntervalSeconds": current_app.config["HEARTBEAT_INTERVAL_SECONDS"],
            }
        ),
        201,
    )


def _clamp_int(value, lo: int = 0, hi: int = 1_000_000_000) -> int | None:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return max(lo, min(hi, n))


def _store_inventory(endpoint_id: str, inv: dict) -> None:
    """Upsert an endpoint's asset inventory from a heartbeat payload.

    Every field is sanitised here rather than trusted: the agent is
    authenticated, but a compromised or buggy one must not be able to write
    unbounded data. The software list is capped and each entry truncated.
    """
    import json

    def _str(v, n):
        return v[:n] if isinstance(v, str) and v.strip() else None

    software_in = inv.get("software")
    software: list[dict] = []
    if isinstance(software_in, list):
        for item in software_in[:2000]:
            if not isinstance(item, dict):
                continue
            name = _str(item.get("name"), 200)
            if not name:
                continue   # an entry with no display name is not a real program
            software.append({
                "name": name,
                "version": _str(item.get("version"), 64),
                "publisher": _str(item.get("publisher"), 200),
            })

    row = db.session.get(EndpointInventory, endpoint_id)
    if row is None:
        row = EndpointInventory(endpoint_id=endpoint_id)
        db.session.add(row)

    row.collected_at = utcnow()
    row.os_build = _str(inv.get("osBuild"), 64)
    row.cpu = _str(inv.get("cpu"), 128)
    row.cpu_cores = _clamp_int(inv.get("cpuCores"), 0, 4096)
    row.memory_total_mb = _clamp_int(inv.get("memoryTotalMb"))
    row.memory_free_mb = _clamp_int(inv.get("memoryFreeMb"))
    row.disk_total_gb = _clamp_int(inv.get("diskTotalGb"))
    row.disk_free_gb = _clamp_int(inv.get("diskFreeGb"))
    row.disk_free_percent = _clamp_int(inv.get("diskFreePercent"), 0, 100)
    row.uptime_seconds = _clamp_int(inv.get("uptimeSeconds"))
    row.software_count = _clamp_int(inv.get("softwareCount")) or len(software)
    row.data_json = json.dumps({"software": software}, ensure_ascii=False)


def _evaluate_endpoint_alerts(endpoint, inventory_present: bool, credential) -> None:
    """Raise/resolve the alerts a heartbeat can decide: low disk (from the just-
    stored inventory) and an expiring device credential. Deduplicated by a stable
    key so a standing condition stays a single open alert."""
    from ..models.alert import (
        SEV_CRITICAL,
        SEV_WARNING,
        TYPE_CREDENTIAL_EXPIRING,
        TYPE_LOW_DISK,
    )
    from ..services import alerting

    name = endpoint.device_name or endpoint.id

    if inventory_present:
        row = db.session.get(EndpointInventory, endpoint.id)
        pct = row.disk_free_percent if row is not None else None
        key = f"lowdisk:{endpoint.id}"
        if isinstance(pct, int):
            if pct < current_app.config["ALERT_LOW_DISK_PERCENT"]:
                alerting.raise_alert(
                    type=TYPE_LOW_DISK, severity=SEV_WARNING,
                    title=f"磁碟空間不足：{name}",
                    message=f"系統磁碟可用空間僅剩 {pct}%。",
                    endpoint_id=endpoint.id, dedup_key=key,
                )
            else:
                alerting.resolve(key)

    if credential is not None:
        key = f"credexp:{endpoint.id}"
        days = credential.days_remaining()
        if days is not None and days <= CREDENTIAL_WARN_WITHIN_DAYS:
            alerting.raise_alert(
                type=TYPE_CREDENTIAL_EXPIRING,
                severity=SEV_CRITICAL if days <= 3 else SEV_WARNING,
                title=f"裝置憑證即將到期：{name}",
                message=f"此端點的裝置憑證約 {days} 天後到期，屆時將無法回報。",
                endpoint_id=endpoint.id, dedup_key=key,
            )
        else:
            alerting.resolve(key)


@bp.post("/heartbeat")
@require_agent_auth
def heartbeat():
    """Report liveness, refresh inventory, and carry back any local warnings.

    Accepts only device inventory. Screen frames and network activity are not
    part of this MVP and will arrive on their own policy-gated routes.
    """
    endpoint = current_endpoint()
    credential = current_credential()
    assert endpoint is not None  # require_agent_auth guarantees this

    body = json_body()
    endpoint.last_seen_at = utcnow()
    endpoint.last_ip = client_ip()

    for field, attr, max_length in (
        ("deviceName", "device_name", 128),
        ("os", "os_name", 128),
        ("osVersion", "os_version", 64),
        ("agentVersion", "agent_version", 32),
        ("localUser", "local_user", 128),
    ):
        value = get_str(body, field, required=False, max_length=max_length)
        if value is not None:
            setattr(endpoint, attr, value)

    # Optional: extended asset inventory. Present only on the periodic heartbeat
    # that carries it (every few hours), not every minute, so this is skipped on
    # most heartbeats.
    inventory = body.get("inventory")
    if isinstance(inventory, dict):
        _store_inventory(endpoint.id, inventory)

    # Turn per-endpoint signals into alerts (deduplicated, so a standing
    # condition is one alert, not one per heartbeat; cleared when it recovers).
    _evaluate_endpoint_alerts(endpoint, isinstance(inventory, dict), credential)

    # Routine heartbeats are not audited: at one per minute per endpoint they
    # would bury the entries that matter. last_seen_at is the record here.
    db.session.commit()

    # The agent displays these locally to whoever is using the machine, and to
    # enterprise IT. Warnings are advisory -- nothing here blocks the endpoint.
    warnings = []
    if credential is not None:
        expiry = credential.expiry_warning(CREDENTIAL_WARN_WITHIN_DAYS)
        if expiry is not None:
            warnings.append(expiry)

    return jsonify(
        {
            "status": "ok",
            "endpointId": endpoint.id,
            "serverTime": iso(utcnow()),
            "heartbeatIntervalSeconds": current_app.config["HEARTBEAT_INTERVAL_SECONDS"],
            "credentialExpiresAt": iso(credential.expires_at) if credential else None,
            "credentialDaysRemaining": credential.days_remaining() if credential else None,
            "warnings": warnings,
        }
    )


@bp.post("/credential/rotate")
@require_agent_auth
def rotate_credential():
    """Issue a fresh device credential and revoke the presented one (section 11)."""
    endpoint = current_endpoint()
    assert endpoint is not None

    now = utcnow()
    for credential in endpoint.credentials:
        if credential.revoked_at is None:
            credential.revoked_at = now
            credential.revoked_reason = "rotated"

    secret, credential = _issue_credential(endpoint)
    db.session.commit()

    return jsonify(
        {
            "deviceCredential": secret,
            "credentialExpiresAt": iso(credential.expires_at),
        }
    )


# UNPROTECTED = the package carried no uninstall password, so removal proceeds
# without a gate; the agent's custom action reports it at uninstall time so an
# alert still fires (there is no refused attempt to log otherwise).
UNINSTALL_OUTCOMES = ("WRONG_PASSWORD", "CANCELLED", "NO_PASSWORD", "UNPROTECTED")

# There is no attempt limit on the endpoint -- someone can keep guessing, and
# every guess is meant to be reported. So one report can carry a lot, and this
# caps how many become individual audit rows. Anything beyond it is summarised
# into a single row rather than dropped: an administrator must never be told
# "20 attempts" when there were 500.
MAX_UNINSTALL_ATTEMPTS_PER_REPORT = 20


@bp.post("/uninstall-attempt")
@require_agent_auth
def uninstall_attempt():
    """Report that someone at this endpoint failed to remove the agent.

    The MSI's uninstall guard refuses without the administrator password and
    leaves a note behind; the agent forwards it here on its next cycle. It is
    reported by the *agent* rather than by the installer because identity comes
    from the device credential -- the installer holds none, and should not.

    Written straight to the audit log as DENIED, which is where an administrator
    already looks for refused operations (section 17). Nothing is enforced here:
    the removal was already refused on the endpoint.
    """
    endpoint = current_endpoint()
    assert endpoint is not None  # require_agent_auth guarantees this

    body = json_body()
    attempts = body.get("attempts")
    if not isinstance(attempts, list):
        raise ValidationError("attempts 必須是陣列。")

    usable = [item for item in attempts if isinstance(item, dict)]
    detailed, overflow = (
        usable[:MAX_UNINSTALL_ATTEMPTS_PER_REPORT],
        usable[MAX_UNINSTALL_ATTEMPTS_PER_REPORT:],
    )

    def _record(outcome: str, **extra) -> None:
        audit.record(
            UNINSTALL_ATTEMPT,
            actor=None,
            actor_type="AGENT",
            target_type="endpoint",
            target_id=endpoint.id,
            result=RESULT_DENIED,
            metadata={"deviceName": endpoint.device_name, "outcome": outcome, **extra},
        )

    for item in detailed:
        outcome = get_str(item, "outcome", required=False, max_length=32) or "WRONG_PASSWORD"
        if outcome not in UNINSTALL_OUTCOMES:
            outcome = "WRONG_PASSWORD"
        _record(
            outcome,
            # Who was signed in at the endpoint, as the installer saw it.
            localUser=get_str(item, "localUser", required=False, max_length=128),
            attemptedAt=get_str(item, "at", required=False, max_length=32),
        )

    if overflow:
        # Summarised rather than silently dropped, so the count an administrator
        # sees is the real one.
        _record(
            "WRONG_PASSWORD",
            localUser=get_str(overflow[0], "localUser", required=False, max_length=128),
            attemptedAt=get_str(overflow[-1], "at", required=False, max_length=32),
            suppressed=len(overflow),
            note="同批次還有更多次嘗試，未逐筆記錄。",
        )

    # A tamper attempt is worth pushing, not just logging: raise a critical alert
    # so it can page a channel. Deduplicated per endpoint until acknowledged.
    if detailed or overflow:
        from ..models.alert import SEV_CRITICAL, TYPE_UNINSTALL_ATTEMPT
        from ..services import alerting

        alerting.raise_alert(
            type=TYPE_UNINSTALL_ATTEMPT, severity=SEV_CRITICAL,
            title=f"偵測到移除 Agent 的嘗試：{endpoint.device_name or endpoint.id}",
            message="有人在此端點嘗試移除管理 Agent，但未通過 IT 密碼。",
            endpoint_id=endpoint.id, dedup_key=f"tamper:{endpoint.id}",
        )

    db.session.commit()
    return jsonify(
        {"status": "recorded", "count": len(detailed), "suppressed": len(overflow)}
    )


def _issue_credential(endpoint: Endpoint) -> tuple[str, EndpointCredential]:
    secret = generate_secret(48)
    credential = EndpointCredential(
        endpoint_id=endpoint.id,
        secret_hash=sha256_hex(secret),
        expires_at=utcnow() + timedelta(days=CREDENTIAL_TTL_DAYS),
    )
    db.session.add(credential)
    db.session.flush()
    return secret, credential


def _record_enroll_failure(reason: str, token_id: str | None = None) -> None:
    audit.record(
        ENDPOINT_ENROLLED,
        actor=None,
        actor_type="AGENT",
        target_type="enrollment_token",
        target_id=token_id,
        result=RESULT_FAILURE,
        metadata={"reason": reason},
    )
    db.session.commit()
