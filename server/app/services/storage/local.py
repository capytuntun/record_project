"""Local-disk storage backend (the default; current behaviour)."""

from __future__ import annotations

import os

from .base import PROBE_NAME, StorageBackend, StorageError


class LocalBackend(StorageBackend):
    type = "LOCAL"
    is_local = True

    def __init__(self, recording_dir: str, screenshot_dir: str) -> None:
        self._roots = {"recordings": recording_dir, "screenshots": screenshot_dir}

    def _abs(self, kind: str, filename: str) -> str:
        root = self._roots[kind]
        return os.path.join(root, filename.replace("/", os.sep))

    def put(self, kind: str, filename: str, data: bytes) -> int:
        path = self._abs(kind, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
        return len(data)

    def get(self, kind: str, filename: str) -> bytes:
        path = self._abs(kind, filename)
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except FileNotFoundError as exc:
            raise StorageError("檔案不存在。") from exc

    def remove(self, kind: str, filename: str) -> None:
        path = self._abs(kind, filename)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            raise StorageError("無法刪除本機檔案。") from exc

    def test(self) -> tuple[bool, str]:
        try:
            self.put("recordings", PROBE_NAME, b"ok")
            data = self.get("recordings", PROBE_NAME)
            self.remove("recordings", PROBE_NAME)
        except Exception as exc:  # noqa: BLE001
            return False, f"本機儲存無法寫入：{exc}"
        return (data == b"ok"), "本機儲存正常。"
