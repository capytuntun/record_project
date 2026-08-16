"""Rebuilding the agent binary from the console (spec sections 16, 18).

Generating a package only reassembles the MSI wrapper (~6s) around a
pre-built ``EndpointAgent.exe``. That is deliberate: the binary is byte-identical
across every package, so recompiling it per download would waste minutes for
nothing. The cost is that changing agent source and forgetting to rebuild ships
a stale agent, silently.

This module closes that gap without giving up the fast path: the console can see
that the source is newer than the binary and trigger one rebuild, in the
background, before generating packages again.

What this is NOT: a remote command execution facility (section 16 forbids that).
The command line is fixed in code, takes nothing from the request, and runs
against source already sitting on the server's own disk -- placed there by the
installer, in a directory only administrators can write. It is the same build
``agent/build.ps1`` runs, reachable by a different door.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# A self-contained single-file publish is slow. Generous, but bounded: a build
# wedged forever would leave the console permanently claiming "building".
BUILD_TIMEOUT_SECONDS = 900

STATUS_IDLE = "IDLE"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"

_SOURCE_SUFFIXES = (".cs", ".csproj")


def find_dotnet(configured: str = "") -> str | None:
    """Locate a dotnet SDK, preferring an explicitly configured path."""
    candidates = [configured] if configured else []
    found = shutil.which("dotnet")
    if found:
        candidates.append(found)
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    candidates.append(str(Path(program_files) / "dotnet" / "dotnet.exe"))

    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            result = subprocess.run(
                [candidate, "--list-sdks"],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        # dotnet.exe exists for runtime-only installs too, but `dotnet publish`
        # needs an SDK -- so ask, rather than trusting the executable's presence.
        if result.returncode == 0 and result.stdout.strip():
            return candidate
    return None


def newest_source_mtime(source_dir: Path) -> float | None:
    """Most recent mtime among agent sources, or None when there is no source."""
    if not source_dir.is_dir():
        return None
    newest: float | None = None
    for path in source_dir.rglob("*"):
        if path.suffix.lower() not in _SOURCE_SUFFIXES or not path.is_file():
            continue
        # Build outputs are not source; a previous build must not make the tree
        # look permanently newer than the binary it produced.
        if any(part in ("bin", "obj") for part in path.parts):
            continue
        stamp = path.stat().st_mtime
        if newest is None or stamp > newest:
            newest = stamp
    return newest


def _iso(stamp: float | None) -> str | None:
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


def source_status(source_dir: Path, binary: Path) -> dict:
    """Whether a rebuild is available, and whether one looks needed."""
    newest = newest_source_mtime(source_dir)
    built = binary.stat().st_mtime if binary.is_file() else None

    return {
        "sourceAvailable": newest is not None,
        "binaryExists": built is not None,
        "sourceModifiedAt": _iso(newest),
        "binaryBuiltAt": _iso(built),
        # True when the console should suggest rebuilding before generating.
        "stale": bool(newest is not None and (built is None or newest > built)),
    }


class AgentBuilder:
    """One rebuild at a time, tracked in memory.

    Process-global like the screen hub, which is sound for the same reason:
    wsgi.py runs a single process on purpose.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state: dict = {
            "status": STATUS_IDLE,
            "startedAt": None,
            "finishedAt": None,
            "message": None,
            "output": None,
        }

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def is_running(self) -> bool:
        with self._lock:
            return self._state["status"] == STATUS_RUNNING

    def reset(self) -> None:
        """Test hook: forget any previous run."""
        with self._lock:
            self._thread = None
            self._state = {
                "status": STATUS_IDLE,
                "startedAt": None,
                "finishedAt": None,
                "message": None,
                "output": None,
            }

    def start(self, *, dotnet: str, agent_root: Path, publish_dir: Path) -> bool:
        """Begin a rebuild. Returns False when one is already running."""
        with self._lock:
            if self._state["status"] == STATUS_RUNNING:
                return False
            self._state = {
                "status": STATUS_RUNNING,
                "startedAt": datetime.now(tz=timezone.utc).isoformat(),
                "finishedAt": None,
                "message": "建置中…",
                "output": None,
            }
            self._thread = threading.Thread(
                target=self._run,
                kwargs={"dotnet": dotnet, "agent_root": agent_root, "publish_dir": publish_dir},
                daemon=True,
                name="agent-build",
            )
            self._thread.start()
            return True

    def _finish(self, status: str, message: str, output: str | None) -> None:
        with self._lock:
            self._state["status"] = status
            self._state["message"] = message
            # Only the tail: a full build log is large and the useful part -- the
            # first error and the summary -- is at the end.
            self._state["output"] = (output or "")[-4000:] or None
            self._state["finishedAt"] = datetime.now(tz=timezone.utc).isoformat()

    def _run(self, *, dotnet: str, agent_root: Path, publish_dir: Path) -> None:
        project = agent_root / "src" / "EndpointAgent" / "EndpointAgent.csproj"
        ca_project = (
            agent_root / "src" / "EndpointAgent.CustomActions"
            / "EndpointAgent.CustomActions.csproj"
        )

        # Same two commands as agent/build.ps1, in the same order: the MSI needs
        # both the agent binary and the custom-action DLL.
        steps = [
            ("agent", [dotnet, "publish", str(project), "-c", "Release", "-o", str(publish_dir)]),
            ("custom action", [dotnet, "build", str(ca_project), "-c", "Release"]),
        ]

        collected: list[str] = []
        for name, command in steps:
            logger.info("agent rebuild: %s", name)
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    # NOT bare text=True: that decodes with the system locale
                    # (cp950 on a Traditional Chinese Windows), and dotnet emits
                    # bytes that codec cannot represent. The reader threads then
                    # die with UnicodeDecodeError and the output is lost --
                    # precisely when a build has failed and the output is the
                    # only thing worth having.
                    encoding="utf-8",
                    errors="replace",
                    timeout=BUILD_TIMEOUT_SECONDS,
                    cwd=str(agent_root),
                    shell=False,
                )
            except subprocess.TimeoutExpired:
                self._finish(STATUS_FAILED, f"建置逾時（{name}）。", "\n".join(collected))
                return
            except OSError as exc:
                self._finish(STATUS_FAILED, f"無法執行 dotnet：{exc}", "\n".join(collected))
                return

            collected.append(f"$ {name}\n{result.stdout}\n{result.stderr}")
            if result.returncode != 0:
                logger.error("agent rebuild failed at %s rc=%s", name, result.returncode)
                self._finish(
                    STATUS_FAILED,
                    f"建置失敗（{name}，代碼 {result.returncode}）。",
                    "\n".join(collected),
                )
                return

        binary = publish_dir / "EndpointAgent.exe"
        if not binary.is_file():
            self._finish(STATUS_FAILED, "建置結束但沒有產生 EndpointAgent.exe。",
                         "\n".join(collected))
            return

        size_mb = round(binary.stat().st_size / (1024 * 1024), 1)
        logger.info("agent rebuild succeeded: %s (%s MB)", binary, size_mb)
        self._finish(STATUS_SUCCEEDED, f"已重建 Agent（{size_mb} MB）。", "\n".join(collected))


builder = AgentBuilder()
