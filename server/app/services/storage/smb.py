"""SMB / CIFS storage backend (Windows shares, Synology/QNAP, etc.).

Uses ``smbprotocol``'s high-level ``smbclient`` API, which speaks SMB2/3 with
per-server authenticated sessions. A session is registered (cached by server)
before each operation; credentials are a username (optionally ``DOMAIN\\user``)
and password. SMB3 encrypts in transit where the server negotiates it.
"""

from __future__ import annotations

from .base import PROBE_NAME, StorageBackend, StorageError, posix_join


class SmbBackend(StorageBackend):
    type = "SMB"

    def __init__(self, server: str, port: int | None, share: str, username: str,
                 password: str, domain: str, base_path: str) -> None:
        self._server = server
        self._port = port or 445
        self._share = share.strip("/\\")
        self._password = password or ""
        self._base = (base_path or "").strip("/\\")
        self._user = f"{domain}\\{username}" if domain else username

    def _register(self) -> None:
        import smbclient
        try:
            smbclient.register_session(
                self._server, username=self._user, password=self._password,
                port=self._port,
            )
        except Exception as exc:  # noqa: BLE001 - many low-level error types
            raise StorageError(f"SMB 連線或登入失敗：{exc}") from exc

    def _unc(self, kind: str | None = None, filename: str | None = None) -> str:
        rel = posix_join(self._base, kind or "", filename or "").replace("/", "\\")
        base = f"\\\\{self._server}\\{self._share}"
        return f"{base}\\{rel}" if rel else base

    def put(self, kind: str, filename: str, data: bytes) -> int:
        import smbclient
        self._register()
        path = self._unc(kind, filename)
        parent = path.rsplit("\\", 1)[0]
        try:
            smbclient.makedirs(parent, exist_ok=True)
            with smbclient.open_file(path, mode="wb") as handle:
                handle.write(data)
            return len(data)
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"SMB 上傳失敗：{exc}") from exc

    def get(self, kind: str, filename: str) -> bytes:
        import smbclient
        self._register()
        try:
            with smbclient.open_file(self._unc(kind, filename), mode="rb") as handle:
                return handle.read()
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"SMB 下載失敗：{exc}") from exc

    def remove(self, kind: str, filename: str) -> None:
        import smbclient
        self._register()
        try:
            smbclient.remove(self._unc(kind, filename))
        except FileNotFoundError:
            pass  # already gone
        except Exception as exc:  # noqa: BLE001
            raise StorageError(f"SMB 刪除失敗：{exc}") from exc

    def test(self) -> tuple[bool, str]:
        try:
            self.put("recordings", PROBE_NAME, b"ok")
            data = self.get("recordings", PROBE_NAME)
            self.remove("recordings", PROBE_NAME)
        except StorageError as exc:
            return False, str(exc)
        if data != b"ok":
            return False, "SMB 寫入後讀回的內容不符。"
        return True, f"SMB 連線成功（\\\\{self._server}\\{self._share}）。"
