"""Export recording segments as a ZIP of plain MP4 files (spec section 23,
"Data Export").

Segments live on disk (or a NAS) only as AES-GCM ciphertext, so the only way to
hand a recording to someone outside the console is to decrypt it here, on the
server that holds the key. This module turns a list of segments into a ZIP
stream: one ``.mp4`` per segment plus a ``manifest.json`` describing what was
exported (time spans in UTC, the SHA-256 recorded at capture time, and any
segment whose file could not be produced).

The ZIP is generated lazily so a large export never has to sit in memory:
each segment is loaded, decrypted, written into the archive and yielded before
the next one is touched. Python's ``zipfile`` supports non-seekable output by
emitting data descriptors, which every mainstream unarchiver understands.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterator

from .recording_crypto import decrypt_bytes
from .storage import StorageError, load_file

logger = logging.getLogger("eem.recording")

# One day of 5-minute segments is 288. The cap keeps a single request from
# spanning weeks of footage; anyone needing more can export in batches, and
# every batch is its own audit entry.
MAX_SEGMENTS_PER_EXPORT = 300

_UNSAFE_NAME = re.compile(r"[^0-9A-Za-z一-鿿㐀-䶿._-]+")


@dataclass(frozen=True)
class ExportItem:
    """What the streamer needs to know about one segment, captured before
    streaming starts so the generator never touches the ORM."""

    segment_id: str
    filename: str
    storage_backend: str | None
    started_at: datetime
    ended_at: datetime | None
    sha256: str | None
    size_bytes: int | None


def safe_name(value: str | None, fallback: str) -> str:
    """Reduce a device name to something safe inside a filename."""
    cleaned = _UNSAFE_NAME.sub("_", (value or "").strip()).strip("._-")
    return cleaned[:60] or fallback


def _local(dt: datetime, tz: timezone) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def entry_name(device: str, item: ExportItem, tz: timezone) -> str:
    """``<device>_<YYYYMMDD>_<HHMMSS>-<HHMMSS>.mp4`` in the viewer's local time.

    Local time is what the person opening the file will compare against; the
    manifest keeps the unambiguous UTC values alongside.
    """
    start = _local(item.started_at, tz)
    end = _local(item.ended_at or item.started_at, tz)
    return (
        f"{device}_{start.strftime('%Y%m%d')}_{start.strftime('%H%M%S')}"
        f"-{end.strftime('%H%M%S')}.mp4"
    )


def archive_name(device: str, items: list[ExportItem], tz: timezone) -> str:
    first = _local(min(i.started_at for i in items), tz).strftime("%Y%m%d")
    last = _local(max(i.ended_at or i.started_at for i in items), tz).strftime("%Y%m%d")
    span = first if first == last else f"{first}-{last}"
    return f"recording_{device}_{span}.zip"


def tz_from_offset(minutes: int | None) -> timezone:
    """Fixed-offset zone from the browser's ``-getTimezoneOffset()``.

    A fixed offset (not an IANA zone) is enough for naming files, needs no
    tz database on the server, and cannot be wrong for the moment the export
    was requested. Out-of-range values fall back to UTC rather than erroring.
    """
    if minutes is None or not -14 * 60 <= minutes <= 14 * 60:
        return timezone.utc
    return timezone(timedelta(minutes=minutes))


class _Sink:
    """Write-only buffer that ``zipfile`` can target without seeking."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []

    def write(self, data) -> int:  # noqa: ANN001 - file protocol
        self._chunks.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def stream_export(app, *, key: bytes, device: str, items: list[ExportItem],
                  tz: timezone, exported_by: str, exported_at: datetime,
                  endpoint_id: str, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
    """Yield a ZIP archive containing each segment as plain MP4.

    A segment whose ciphertext is gone (retention swept it, NAS unreachable) or
    that fails to decrypt is skipped and listed under ``missing`` in the
    manifest instead of aborting the whole download -- by the time the first
    byte is out there is no way to send an error status any more.
    """
    sink = _Sink()
    manifest: dict = {
        "exportedAt": exported_at.astimezone(timezone.utc).isoformat(),
        "exportedBy": exported_by,
        "endpointId": endpoint_id,
        "deviceName": device,
        "timezoneOffsetMinutes": int(tz.utcoffset(None).total_seconds() // 60),
        "segments": [],
        "missing": [],
    }
    used_names: set[str] = set()

    with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for item in items:
            name = entry_name(device, item, tz)
            if name in used_names:
                name = name[:-4] + f"_{item.segment_id[:8]}.mp4"
            used_names.add(name)

            reason = None
            try:
                ciphertext = load_file(app, "recordings", item.filename, item.storage_backend)
            except StorageError:
                reason = "file_missing"
            except Exception:  # noqa: BLE001 - a NAS hiccup must not kill the export
                logger.exception("export: cannot read segment %s", item.segment_id)
                reason = "read_failed"

            plaintext = None
            if reason is None:
                try:
                    plaintext = decrypt_bytes(key, ciphertext)
                except Exception:  # noqa: BLE001 - never leak crypto detail
                    reason = "decrypt_failed"

            if plaintext is None:
                manifest["missing"].append({
                    "segmentId": item.segment_id,
                    "startedAt": _iso(item.started_at),
                    "endedAt": _iso(item.ended_at),
                    "reason": reason,
                })
                continue

            digest = hashlib.sha256(plaintext).hexdigest()
            info = zipfile.ZipInfo(name, date_time=_local(item.started_at, tz).timetuple()[:6])
            info.compress_type = zipfile.ZIP_STORED
            # Known up front, so zipfile can pick plain vs ZIP64 headers
            # correctly even though the output stream is not seekable.
            info.file_size = len(plaintext)
            # Write in chunks so a multi-megabyte segment streams out as it is
            # copied rather than after.
            with zf.open(info, "w") as entry:
                for offset in range(0, len(plaintext), chunk_size):
                    entry.write(plaintext[offset:offset + chunk_size])
                    yield sink.drain()
            manifest["segments"].append({
                "file": name,
                "segmentId": item.segment_id,
                "startedAt": _iso(item.started_at),
                "endedAt": _iso(item.ended_at),
                "sizeBytes": len(plaintext),
                "sha256": digest,
                # The hash recorded when the segment was captured. A mismatch
                # would mean the file changed between capture and export.
                "sha256AtCapture": item.sha256,
                "integrity": (
                    "ok" if not item.sha256 or item.sha256 == digest else "mismatch"
                ),
            })
            yield sink.drain()

        zf.writestr(
            "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    yield sink.drain()


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()
