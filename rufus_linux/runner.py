"""Launch the privileged engine and stream its JSON progress to the GUI."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from gi.repository import GLib

from . import util
from .resources import resource_dir

ENGINE = Path(__file__).resolve().parent.parent / "engine_main.py"


def privilege_backend() -> str | None:
    if util.is_root():
        return "root"
    if shutil.which("pkexec"):
        return "pkexec"
    return None


class EngineRunner:
    """Runs engine_main.py under pkexec (or directly when already root)."""

    def __init__(self, job: dict, on_event, on_exit):
        self.job_data = dict(job)
        self.on_event = on_event
        self.on_exit = on_exit
        self.proc: subprocess.Popen | None = None
        self.cancelled = False
        self.saw_done = False
        self.done_ok = False
        self.done_message = ""
        self._job_path = ""
        self._cancel_path = ""
        self._stderr_tail = ""
        self._watch_ids: list[int] = []
        self._kill_timer = 0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        backend = privilege_backend()
        if backend is None:
            raise RuntimeError(
                "Neither root nor pkexec is available. Install polkit or run the app with sudo."
            )

        resources = str(resource_dir())
        # Create the cancel marker path securely, then remove it so its mere
        # existence means "cancel".  mktemp() would be guessable/racy.
        handle, cancel_path = tempfile.mkstemp(prefix="rufus-cancel-", suffix=".flag")
        os.close(handle)
        os.unlink(cancel_path)
        self._cancel_path = cancel_path
        job = dict(self.job_data)
        job["cancel_file"] = self._cancel_path
        job["uefi_ntfs"] = job.get("uefi_ntfs") or str(Path(resources) / "uefi-ntfs.img")

        handle, self._job_path = tempfile.mkstemp(prefix="rufus-job-", suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(job, out, indent=2)
        os.chmod(self._job_path, 0o600)

        cmd = [sys.executable, "-u", str(ENGINE), "--job", self._job_path]
        if backend == "pkexec":
            cmd = ["pkexec", *cmd]

        environment = dict(os.environ)
        environment.setdefault("PYTHONUNBUFFERED", "1")
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(ENGINE.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=environment,
            start_new_session=True,
        )
        self._watch(self.proc.stdout, self._on_stdout)
        self._watch(self.proc.stderr, self._on_stderr)
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.proc.pid, self._on_child)

    def _watch(self, stream, callback) -> None:
        if stream is None:
            return
        watch_id = GLib.io_add_watch(stream, GLib.IO_IN | GLib.IO_HUP, callback)
        self._watch_ids.append(watch_id)

    def is_running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def cancel(self) -> None:
        if not self.is_running():
            return
        self.cancelled = True
        try:
            Path(self._cancel_path).touch()
        except OSError:
            pass
        self.on_event({"type": "status", "text": "Cancelling - finishing the current step..."})
        self._kill_timer = GLib.timeout_add_seconds(8, self._force_kill)

    def _force_kill(self) -> bool:
        self._kill_timer = 0
        if not self.is_running():
            return False
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                self.proc.terminate()
            except ProcessLookupError:
                pass
        GLib.timeout_add_seconds(4, self._kill_now)
        return False

    def _kill_now(self) -> bool:
        if self.is_running():
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        return False

    # -- stream handling -------------------------------------------------
    def _on_stdout(self, stream, _condition) -> bool:
        line = stream.readline()
        if line == "":
            return False
        line = line.strip()
        if not line:
            return True
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = {"type": "log", "text": line}
        if payload.get("type") == "done":
            self.saw_done = True
            self.done_ok = bool(payload.get("ok"))
            self.done_message = str(payload.get("message") or "")
        self.on_event(payload)
        return True

    def _on_stderr(self, stream, _condition) -> bool:
        line = stream.readline()
        if line == "":
            return False
        text = line.rstrip()
        if text:
            self._stderr_tail = (self._stderr_tail + "\n" + text)[-4000:]
            self.on_event({"type": "log", "text": text, "level": "error"})
        return True

    def _on_child(self, pid, status) -> None:
        returncode = os.waitstatus_to_exitcode(status) if status >= 0 else status
        # Drain whatever is still buffered.
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is None:
                continue
            try:
                rest = stream.read()
            except (ValueError, OSError):
                rest = ""
            for line in (rest or "").splitlines():
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    payload = {"type": "log", "text": line}
                if payload.get("type") == "done":
                    self.saw_done = True
                    self.done_ok = bool(payload.get("ok"))
                    self.done_message = str(payload.get("message") or "")
                self.on_event(payload)

        for watch_id in self._watch_ids:
            try:
                GLib.source_remove(watch_id)
            except Exception:
                pass
        self._watch_ids.clear()

        message = ""
        ok = self.done_ok
        if self.cancelled:
            message = "Cancelled"
            ok = False
        elif self.saw_done:
            message = self.done_message
        else:
            if returncode in (126, 127):
                message = "Authorisation was cancelled or denied"
            elif returncode == 130:
                message = "Cancelled"
            elif returncode != 0:
                message = "The engine exited unexpectedly (status %s)" % returncode
                if self._stderr_tail:
                    message += ": " + util.stderr_tail(self._stderr_tail, 3)
            else:
                message = "The engine finished without reporting a result"

        if not self.saw_done:
            self.on_event({"type": "done", "ok": ok, "message": message})

        self._cleanup_files()
        self.on_exit(ok, message)

    def _cleanup_files(self) -> None:
        for path in (self._job_path, self._cancel_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
        self._job_path = ""
        self._cancel_path = ""
