"""Small helpers shared by the GUI, the CLI and the privileged engine."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

CHUNK_SIZE = 4 * 1024 * 1024

#: Filesystems understood by the extract mode.
FILESYSTEMS = ("FAT32", "NTFS", "exFAT")

#: Characters that FAT/exFAT cannot store in a file name.
INVALID_FAT_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class CommandError(RuntimeError):
    """A privileged helper command failed."""

    def __init__(self, cmd, returncode, stdout="", stderr=""):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout or ""
        self.stderr = stderr or ""
        detail = (self.stderr or self.stdout).strip().splitlines()
        detail = detail[-1] if detail else "unknown error"
        super().__init__("%s failed (exit %s): %s" % (" ".join(self.cmd), returncode, detail))


class Cancelled(Exception):
    """The user asked to stop the operation."""


def which(name: str) -> str | None:
    return shutil.which(name)


def human_size(num: float, suffix: str = "B") -> str:
    """Return a Rufus-like size string (1024 based, but GB/MB labels)."""
    num = float(num)
    for unit in ("", "K", "M", "G", "T", "P"):
        if abs(num) < 1024.0 or unit == "P":
            if unit == "":
                return "%d %s%s" % (num, unit, suffix)
            return "%.1f %s%s" % (num, unit, suffix)
        num /= 1024.0
    return "%.1f P%s" % (num, suffix)


def run(
    cmd,
    *,
    check: bool = False,
    capture: bool = True,
    text: bool = True,
    timeout: float | None = None,
    env: dict | None = None,
    stdin_data: str | None = None,
) -> subprocess.CompletedProcess:
    """Run a command, optionally raising a descriptive CommandError."""
    cmd = [str(c) for c in cmd]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        input=stdin_data,
        text=text,
        timeout=timeout,
        env=env,
    )
    if check and proc.returncode != 0:
        raise CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return proc


def run_checked(cmd, **kwargs) -> subprocess.CompletedProcess:
    return run(cmd, check=True, **kwargs)


def sanitize_label(label: str, filesystem: str = "FAT32") -> str:
    """Make a volume label valid for the selected filesystem."""
    label = (label or "").strip()
    label = INVALID_FAT_CHARS.sub("_", label)
    label = re.sub(r"\s+", " ", label).strip(" .")
    if filesystem.upper() == "NTFS":
        return label[:32]
    # FAT32 and exFAT both use an 11 character label.
    return label[:11].upper()


def default_label(kind: str, distro: str = "") -> str:
    if kind == "windows":
        return "WINDOWS"
    if distro:
        first = re.split(r"[^A-Za-z0-9]+", distro)[0]
        return (first or "LINUX")[:11].upper()
    return "LINUX"


def partition_node(device: str, number: int) -> str:
    """Return the kernel name of partition ``number`` on ``device``."""
    dev = str(device)
    if re.search(r"\d$", dev):
        return "%sp%d" % (dev, number)
    return "%s%d" % (dev, number)


def is_block_device(path: str) -> bool:
    try:
        return os.path.exists(path) and os.stat(path).st_mode & 0o170000 == 0o060000
    except OSError:
        return False


def read_sysfs(path: str, default: str = "") -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return default


def findmnt_source(path: str) -> str:
    proc = run(["findmnt", "-n", "-o", "SOURCE", "--target", path])
    if proc.returncode == 0:
        return proc.stdout.strip()
    return ""


def mountpoints_for(source: str) -> list[str]:
    proc = run(["findmnt", "-rn", "-o", "TARGET", "-S", source])
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def free_space(path: str) -> int:
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def checksum_file(
    path: str | os.PathLike,
    algorithm: str = "sha256",
    *,
    limit: int | None = None,
    progress=None,
    cancel=None,
) -> str:
    """Hash a file (or the first ``limit`` bytes of a block device)."""
    digest = hashlib.new(algorithm)
    total = limit if limit is not None else os.path.getsize(path)
    done = 0
    with open(path, "rb") as handle:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            if limit is not None and done + len(chunk) > limit:
                chunk = chunk[: limit - done]
            digest.update(chunk)
            done += len(chunk)
            if progress is not None and total:
                progress(done / total)
            if limit is not None and done >= limit:
                break
    return digest.hexdigest()


def write_all(fd: int, data: bytes) -> None:
    """os.write() may write less than requested; keep going until it is all out."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to device")
        view = view[written:]


def sync_path(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def wait_for(predicate, timeout: float = 5.0, interval: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def safe_decode(data) -> str:
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data or ""


def log_command(cmd) -> str:
    return " ".join(str(c) for c in cmd)


def stderr_tail(text: str, lines: int = 8) -> str:
    chunks = [line for line in (text or "").splitlines() if line.strip()]
    return "\n".join(chunks[-lines:])


def is_root() -> bool:
    return os.geteuid() == 0


def program_dir() -> Path:
    return Path(__file__).resolve().parent.parent
