"""FTP / FTPS storage backend.

A fresh connection is opened per operation. Screen-data traffic is intermittent
(a segment every few minutes, a screenshot on demand), so a short-lived
connection is simpler and more robust than holding one open across the process's
lifetime. Use FTPS (``use_tls``) whenever the NAS supports it -- plain FTP sends
the password in the clear (section 24).
"""

from __future__ import annotations

import ftplib
import io

from .base import PROBE_NAME, StorageBackend, StorageError, posix_join


class FtpBackend(StorageBackend):
    type = "FTP"

    def __init__(self, host: str, port: int | None, username: str, password: str,
                 base_path: str, use_tls: bool = True) -> None:
        self._host = host
        self._port = port or 21
        self._username = username or "anonymous"
        self._password = password or ""
        self._base = (base_path or "").strip("/")
        self._use_tls = use_tls

    def _connect(self) -> ftplib.FTP:
        try:
            ftp = ftplib.FTP_TLS() if self._use_tls else ftplib.FTP()
            ftp.connect(self._host, self._port, timeout=20)
            ftp.login(self._username, self._password)
            if isinstance(ftp, ftplib.FTP_TLS):
                ftp.prot_p()  # encrypt the data channel, not just the control channel
            ftp.set_pasv(True)
            return ftp
        except ftplib.all_errors as exc:
            raise StorageError(f"FTP 連線或登入失敗：{exc}") from exc

    def _remote_path(self, kind: str, filename: str) -> str:
        return posix_join(self._base, kind, filename)

    @staticmethod
    def _ensure_dirs(ftp: ftplib.FTP, path: str) -> None:
        """Make each parent directory of ``path`` (best effort; ignore exists)."""
        parts = path.split("/")[:-1]
        cur = ""
        for part in parts:
            cur = f"{cur}/{part}" if cur else part
            try:
                ftp.mkd(cur)
            except ftplib.error_perm:
                pass  # already exists (or no permission to recreate) -- keep going

    def put(self, kind: str, filename: str, data: bytes) -> int:
        path = self._remote_path(kind, filename)
        ftp = self._connect()
        try:
            self._ensure_dirs(ftp, path)
            ftp.storbinary(f"STOR {path}", io.BytesIO(data))
            return len(data)
        except ftplib.all_errors as exc:
            raise StorageError(f"FTP 上傳失敗：{exc}") from exc
        finally:
            _quiet_quit(ftp)

    def get(self, kind: str, filename: str) -> bytes:
        path = self._remote_path(kind, filename)
        ftp = self._connect()
        buffer = io.BytesIO()
        try:
            ftp.retrbinary(f"RETR {path}", buffer.write)
            return buffer.getvalue()
        except ftplib.all_errors as exc:
            raise StorageError(f"FTP 下載失敗：{exc}") from exc
        finally:
            _quiet_quit(ftp)

    def remove(self, kind: str, filename: str) -> None:
        path = self._remote_path(kind, filename)
        ftp = self._connect()
        try:
            ftp.delete(path)
        except ftplib.error_perm:
            pass  # already gone
        except ftplib.all_errors as exc:
            raise StorageError(f"FTP 刪除失敗：{exc}") from exc
        finally:
            _quiet_quit(ftp)

    def test(self) -> tuple[bool, str]:
        try:
            self.put("recordings", PROBE_NAME, b"ok")
            data = self.get("recordings", PROBE_NAME)
            self.remove("recordings", PROBE_NAME)
        except StorageError as exc:
            return False, str(exc)
        if data != b"ok":
            return False, "FTP 寫入後讀回的內容不符。"
        return True, f"FTP 連線成功（{self._host}:{self._port}）。"


def _quiet_quit(ftp: ftplib.FTP) -> None:
    try:
        ftp.quit()
    except Exception:  # noqa: BLE001
        try:
            ftp.close()
        except Exception:  # noqa: BLE001
            pass
