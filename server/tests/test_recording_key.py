"""Zero-config screen-data key: auto-generated, stable, private; env still wins."""

from __future__ import annotations

import os
import stat

from app.config import Config, _load_or_create_recording_key


def test_key_is_created_stable_and_private(tmp_path):
    instance_dir = str(tmp_path / "inst")
    first = _load_or_create_recording_key(instance_dir)
    assert first and len(first) >= 40

    # A second call reuses the same key (yesterday's recordings still decrypt).
    assert _load_or_create_recording_key(instance_dir) == first

    key_file = tmp_path / "inst" / "recording.key"
    assert key_file.is_file()
    if os.name != "nt":  # POSIX perms only
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600


def test_env_key_wins_over_auto_file(monkeypatch):
    monkeypatch.setenv("EEM_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("EEM_RECORDING_KEY", "an-explicit-operator-key")
    monkeypatch.setenv("EEM_RECORDING_AUTO_KEY", "1")

    cfg = Config()
    assert cfg.RECORDING_KEY_PASSPHRASE == "an-explicit-operator-key"
    assert cfg.RECORDING_KEY_SOURCE == "env"


def test_auto_key_can_be_disabled(monkeypatch):
    monkeypatch.setenv("EEM_SECRET_KEY", "x" * 40)
    monkeypatch.delenv("EEM_RECORDING_KEY", raising=False)
    monkeypatch.setenv("EEM_RECORDING_AUTO_KEY", "0")

    cfg = Config()
    assert cfg.RECORDING_KEY_PASSPHRASE is None
    assert cfg.RECORDING_KEY_SOURCE is None
