"""Server-side screen recorder: JPEG frames -> H.264 -> encrypted segments.

One :class:`Recorder` per endpoint being recorded. It runs an FFmpeg process
that segments the H.264 stream on disk; a watcher thread encrypts each completed
segment, indexes it in ``recording_segments``, and deletes the plaintext. Frame
content never touches the database (section 14).

:class:`RecorderManager` is the process-wide registry the hub feeds frames into.

Scope note: like the screen hub, this lives in one process. Multiple workers
would each record independently; a production multi-worker deployment needs a
single recording worker or external coordination. Documented, not hidden.
"""

from __future__ import annotations

import hashlib
import logging
import os
import queue
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from .recording_crypto import derive_key, encrypt_bytes
from .storage import store_file

logger = logging.getLogger("eem.recording")

_SENTINEL = object()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Recorder:
    def __init__(self, app, *, endpoint_id: str, policy_id: str | None,
                 mode: str, fps: int, retention_days: int,
                 storage_target_id: str | None = None) -> None:
        self._app = app
        self.endpoint_id = endpoint_id
        self.policy_id = policy_id
        self.mode = mode
        self.fps = max(1, min(fps, 15))
        self.retention_days = retention_days
        self.storage_target_id = storage_target_id

        config = app.config
        self._ffmpeg = config["FFMPEG_PATH"]
        self._key = derive_key(config["RECORDING_KEY_PASSPHRASE"])
        self._segment_seconds = config["RECORDING_SEGMENT_SECONDS"]
        self._storage_root = config["RECORDING_DIR"]

        self._queue: queue.Queue = queue.Queue(maxsize=300)
        self._proc: subprocess.Popen | None = None
        self._work_dir: str | None = None
        self._list_path: str | None = None
        self._threads: list[threading.Thread] = []
        self._stopping = threading.Event()
        self._segment_start = _utcnow()

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        # A per-run working directory holds the plaintext segments briefly,
        # before they are encrypted into the endpoint's storage folder.
        stamp = _utcnow().strftime("%Y%m%d-%H%M%S")
        self._work_dir = os.path.join(self._storage_root, "_work", f"{self.endpoint_id}-{stamp}")
        os.makedirs(self._work_dir, exist_ok=True)
        self._list_path = os.path.join(self._work_dir, "segments.txt")
        open(self._list_path, "w").close()

        # Differential = periodic keyframes + deltas (small). Full = all-intra:
        # every frame a keyframe, so any moment seeks exactly, at a size cost.
        gop = "1" if self.mode == "FULL" else str(self.fps * 10)

        command = [
            self._ffmpeg, "-y",
            "-f", "image2pipe", "-framerate", str(self.fps), "-i", "-",
            "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-g", gop,
            "-f", "segment",
            "-segment_time", str(self._segment_seconds),
            "-reset_timestamps", "1",
            "-segment_format", "mp4",
            "-segment_list", self._list_path,
            "-segment_list_type", "flat",
            os.path.join(self._work_dir, "seg_%05d.mp4"),
        ]
        self._proc = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._segment_start = _utcnow()

        self._spawn(self._writer_loop, "rec-writer")
        self._spawn(self._watcher_loop, "rec-watcher")
        logger.info("recording started endpoint=%s mode=%s fps=%s",
                    self.endpoint_id, self.mode, self.fps)

    def feed(self, jpeg: bytes) -> None:
        if self._stopping.is_set():
            return
        try:
            self._queue.put_nowait(jpeg)
        except queue.Full:
            # Drop the frame rather than block the hub's fan-out thread. A
            # dropped frame is a momentary quality loss, not a failure.
            pass

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        self._stopping.set()
        self._queue.put(_SENTINEL)
        for thread in self._threads:
            thread.join(timeout=15)
        logger.info("recording stopped endpoint=%s", self.endpoint_id)

    # --- internals --------------------------------------------------------

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _writer_loop(self) -> None:
        """Pump queued frames into FFmpeg's stdin, then close it cleanly."""
        assert self._proc and self._proc.stdin
        try:
            while True:
                item = self._queue.get()
                if item is _SENTINEL:
                    break
                try:
                    self._proc.stdin.write(item)
                except (BrokenPipeError, OSError):
                    break
        finally:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
            # Give FFmpeg a moment to flush and finalise the last segment.
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    def _watcher_loop(self) -> None:
        """Watch the segment list; encrypt + index each completed segment."""
        seen: set[str] = set()
        while True:
            done = self._stopping.is_set() and (self._proc is None or self._proc.poll() is not None)
            for name in self._read_new_segments(seen):
                self._process_segment(name)
            if done:
                # Final drain after FFmpeg exits.
                for name in self._read_new_segments(seen):
                    self._process_segment(name)
                break
            time.sleep(1.0)

    def _read_new_segments(self, seen: set[str]) -> list[str]:
        out: list[str] = []
        try:
            with open(self._list_path, "r") as handle:
                for line in handle:
                    name = line.strip()
                    if name and name not in seen:
                        seen.add(name)
                        out.append(name)
        except OSError:
            pass
        return out

    def _process_segment(self, name: str) -> None:
        """Encrypt one plaintext segment, index it, delete the plaintext."""
        assert self._work_dir
        src = os.path.join(self._work_dir, os.path.basename(name))
        # A name appearing in the list means FFmpeg closed it, but guard anyway.
        if not os.path.isfile(src):
            return

        ended = _utcnow()
        started = self._segment_start
        self._segment_start = ended

        import uuid

        segment_id = str(uuid.uuid4())
        rel_dir = os.path.join(self.endpoint_id, started.strftime("%Y%m%d"))
        rel_file = os.path.join(rel_dir, f"{segment_id}.mp4.enc").replace("\\", "/")

        try:
            plaintext = open(src, "rb").read()
            sha = hashlib.sha256(plaintext).hexdigest()
            frame_count = _count_frames(plaintext)
            # Encrypt here, then publish the ciphertext to the active storage
            # target (local disk, or a NAS over FTP/SMB). The target only ever
            # sees ciphertext. A remote outage falls back to local storage.
            enc = encrypt_bytes(self._key, plaintext)
            location, size = store_file(self._app, "recordings", rel_file, enc,
                                        target_id=self.storage_target_id)
        except Exception:
            logger.exception("failed to encrypt/store segment %s", src)
            return
        finally:
            try:
                os.remove(src)
            except OSError:
                pass

        self._index_segment(segment_id, started, ended, rel_file, size, sha,
                            frame_count, location)

    def _index_segment(self, segment_id, started, ended, rel_file, size, sha,
                       frame_count, location):
        from ..models import RecordingSegment, db

        with self._app.app_context():
            try:
                segment = RecordingSegment(
                    id=segment_id,
                    endpoint_id=self.endpoint_id,
                    policy_id=self.policy_id,
                    started_at=started,
                    ended_at=ended,
                    mode=self.mode,
                    filename=rel_file,
                    size_bytes=size,
                    sha256=sha,
                    frame_count=frame_count,
                    expires_at=ended + timedelta(days=self.retention_days),
                    storage_backend=location,
                )
                db.session.add(segment)
                db.session.commit()
                logger.info("segment indexed endpoint=%s %s bytes", self.endpoint_id, size)
            except Exception:
                db.session.rollback()
                logger.exception("failed to index segment")
            finally:
                db.session.remove()


def _count_frames(mp4_bytes: bytes) -> int:
    # Cheap heuristic: the exact frame count is not needed for playback, and a
    # full demux would be wasteful. Return 0; the segment's time span is what
    # playback uses.
    return 0


class RecorderManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._recorders: dict[str, Recorder] = {}

    def start(self, app, *, endpoint_id: str, policy_id: str | None,
              mode: str, fps: int, retention_days: int,
              storage_target_id: str | None = None) -> bool:
        with self._lock:
            if endpoint_id in self._recorders:
                return False
            recorder = Recorder(app, endpoint_id=endpoint_id, policy_id=policy_id,
                                mode=mode, fps=fps, retention_days=retention_days,
                                storage_target_id=storage_target_id)
            self._recorders[endpoint_id] = recorder
        try:
            recorder.start()
            return True
        except Exception:
            logger.exception("failed to start recorder for %s", endpoint_id)
            with self._lock:
                self._recorders.pop(endpoint_id, None)
            return False

    def stop(self, endpoint_id: str) -> None:
        with self._lock:
            recorder = self._recorders.pop(endpoint_id, None)
        if recorder is not None:
            recorder.stop()

    def feed(self, endpoint_id: str, jpeg: bytes) -> None:
        with self._lock:
            recorder = self._recorders.get(endpoint_id)
        if recorder is not None:
            recorder.feed(jpeg)

    def is_recording(self, endpoint_id: str) -> bool:
        with self._lock:
            return endpoint_id in self._recorders

    def active_endpoint_ids(self) -> set[str]:
        with self._lock:
            return set(self._recorders)

    def stop_all(self) -> None:
        for endpoint_id in list(self.active_endpoint_ids()):
            self.stop(endpoint_id)


manager = RecorderManager()
