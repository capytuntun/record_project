"""Where the server looks for FFmpeg on Windows when EEM_FFMPEG_PATH is unset.

install.ps1 provisions FFmpeg into <InstallDir>\\tools\\ffmpeg, which is outside
the server tree an upgrade mirrors over. These pin the lookup order so the
installer and the config keep agreeing on that location.
"""

from __future__ import annotations

import os

import pytest

from app.config import _default_ffmpeg

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows FFmpeg lookup")


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


def test_installer_location_wins(tmp_path):
    server = tmp_path / "server"
    installed = _touch(tmp_path / "tools" / "ffmpeg" / "ffmpeg.exe")
    _touch(server / "tools" / "ffmpeg" / "ffmpeg.exe")
    assert _default_ffmpeg(str(server)) == str(installed)


def test_hand_placed_server_copy_still_found(tmp_path):
    server = tmp_path / "server"
    legacy = _touch(server / "tools" / "ffmpeg" / "ffmpeg.exe")
    assert _default_ffmpeg(str(server)) == str(legacy)


def test_missing_points_at_installer_location(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.shutil.which", lambda name: None)
    server = tmp_path / "server"
    expected = tmp_path / "tools" / "ffmpeg" / "ffmpeg.exe"
    assert _default_ffmpeg(str(server)) == str(expected)
