"""MSI signing after the move to the shared signer (app.services.signing).

The console's agent rebuild signs through the same helper; its tests live in
test_agent_build.py. These pin that MSI signing kept its behavior.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services import packaging, signing


class _Result:
    def __init__(self, returncode: int = 0):
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


@pytest.fixture
def configured(app, tmp_path):
    pfx = tmp_path / "signing.pfx"
    pfx.write_bytes(b"pfx")
    app.config.update(
        SIGNING_ENABLED=True,
        SIGNING_PFX_PATH=str(pfx),
        SIGNING_PASSWORD="msi-pw",
        SIGNING_SCRIPT=r"C:\agent\signing\Sign-File.ps1",
        SIGNING_TIMESTAMP_URL=None,
    )
    return pfx


def test_unconfigured_signing_leaves_the_msi_unsigned(app, monkeypatch, tmp_path):
    app.config["SIGNING_ENABLED"] = False
    monkeypatch.setattr(signing.subprocess, "run",
                        lambda *a, **k: pytest.fail("signer must not run"))
    assert packaging._sign_if_configured(tmp_path / "x.msi", "pkg") is False


def test_configured_signing_signs_the_msi_with_the_password_in_env(app, configured,
                                                                   monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(signing.subprocess, "run",
                        lambda command, **kw: calls.append((command, kw)) or _Result())
    msi = tmp_path / "x.msi"

    assert packaging._sign_if_configured(msi, "pkg") is True

    command, kwargs = calls[0]
    assert str(msi) in command
    assert command[command.index("-CertPath") + 1] == str(configured)
    assert "-TimestampUrl" not in command
    assert "msi-pw" not in command
    assert kwargs["env"]["EEM_SIGNING_PASSWORD"] == "msi-pw"


def test_a_failed_msi_signature_raises(app, configured, monkeypatch, tmp_path):
    monkeypatch.setattr(signing.subprocess, "run", lambda command, **kw: _Result(1))
    with pytest.raises(packaging.PackagingError, match="簽章失敗"):
        packaging._sign_if_configured(tmp_path / "x.msi", "pkg")


def test_a_missing_certificate_raises_before_signing(app, configured, monkeypatch, tmp_path):
    configured.unlink()
    monkeypatch.setattr(signing.subprocess, "run",
                        lambda *a, **k: pytest.fail("signer must not run"))
    with pytest.raises(packaging.PackagingError, match="憑證不存在"):
        packaging._sign_if_configured(Path(tmp_path / "x.msi"), "pkg")
