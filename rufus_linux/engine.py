"""Privileged write engine.

This module is launched through ``pkexec`` (or directly by root) and performs
all destructive work.  It never depends on the GUI, validates every argument,
and always unmounts/flushes in a ``finally`` block.

It emits one JSON object per line on stdout so the GUI can follow progress:

    {"type": "status",   "text": "..."}
    {"type": "log",      "text": "..."}
    {"type": "progress", "value": 42.0, "text": "..."}
    {"type": "done",     "ok": true, "message": "..."}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rufus_linux import devices, images, util
from rufus_linux.resources import resource_path

FAT32_MAX_FILE = 4 * 1024**3 - 1
SWM_PART_SIZE_MB = 3800

MODES = ("auto", "dd", "extract")
SCHEMES = ("mbr", "gpt")
TARGETS = ("both", "uefi", "bios")
FILESYSTEMS = ("FAT32", "NTFS", "exFAT")


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
class Reporter:
    def __init__(self, human: bool = False):
        self.human = human
        self._last_progress = -100.0
        self._last_emit = 0.0

    def _emit(self, payload: dict) -> None:
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    def status(self, text: str) -> None:
        if self.human:
            print(":: %s" % text, flush=True)
        else:
            self._emit({"type": "status", "text": text})

    def log(self, text: str, level: str = "info") -> None:
        if self.human:
            print("   %s" % text, flush=True)
        else:
            self._emit({"type": "log", "text": text, "level": level})

    def progress(self, value: float, text: str = "") -> None:
        value = max(0.0, min(100.0, float(value)))
        now = time.monotonic()
        if not self.human and value < 100:
            if value - self._last_progress < 0.35 and now - self._last_emit < 0.08:
                return
        self._last_progress = value
        self._last_emit = now
        if self.human:
            bar = "#" * int(value / 4)
            print("\r[%-25s] %5.1f%% %s" % (bar[:25], value, text[:50]), end="", flush=True)
        else:
            self._emit({"type": "progress", "value": round(value, 2), "text": text})

    def meta(self, **fields) -> None:
        if not self.human:
            self._emit({"type": "meta", **fields})

    def done(self, ok: bool, message: str = "", **extra) -> None:
        if self.human:
            print()
            print(("Done: " if ok else "Failed: ") + message, flush=True)
        else:
            self._emit({"type": "done", "ok": ok, "message": message, **extra})


class Cancelled(Exception):
    pass


class CancelToken:
    def __init__(self, cancel_file: str = ""):
        self.path = Path(cancel_file) if cancel_file else None
        self.event = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(sig, self._handler)
            except (ValueError, OSError):
                pass

    def _handler(self, *_args) -> None:
        self.event.set()

    def is_set(self) -> bool:
        if self.path is not None and self.path.exists():
            self.event.set()
        return self.event.is_set()

    def check(self) -> None:
        if self.is_set():
            raise Cancelled()


# --------------------------------------------------------------------------
# Job
# --------------------------------------------------------------------------
@dataclass
class Job:
    image: str
    device: str
    mode: str = "auto"
    scheme: str = "mbr"
    target: str = "both"
    filesystem: str = "FAT32"
    label: str = "USB"
    verify: bool = False
    split_wim: bool = True
    confirm: str = ""
    cancel_file: str = ""
    dry_run: bool = False
    debug: bool = False
    uefi_ntfs: str = ""


def load_job(path: str) -> Job:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("job file must contain a JSON object")

    image = str(data.get("image") or "").strip()
    device = str(data.get("device") or "").strip()
    if not image or not device:
        raise ValueError("job is missing 'image' or 'device'")

    fs_lookup = {name.lower(): name for name in FILESYSTEMS}
    filesystem = fs_lookup.get(str(data.get("filesystem") or "FAT32").lower())
    if filesystem is None:
        raise ValueError("invalid filesystem: %s" % data.get("filesystem"))

    job = Job(
        image=image,
        device=device,
        mode=str(data.get("mode") or "auto").lower(),
        scheme=str(data.get("scheme") or "mbr").lower(),
        target=str(data.get("target") or "both").lower(),
        filesystem=filesystem,
        label=str(data.get("label") or "USB"),
        verify=bool(data.get("verify")),
        split_wim=bool(data.get("split_wim", True)),
        confirm=str(data.get("confirm") or ""),
        cancel_file=str(data.get("cancel_file") or ""),
        dry_run=bool(data.get("dry_run")),
        debug=bool(data.get("debug")),
        uefi_ntfs=str(data.get("uefi_ntfs") or ""),
    )
    if job.mode not in MODES:
        raise ValueError("invalid mode: %s" % job.mode)
    if job.scheme not in SCHEMES:
        raise ValueError("invalid partition scheme: %s" % job.scheme)
    if job.target not in TARGETS:
        raise ValueError("invalid target system: %s" % job.target)
    if job.filesystem not in FILESYSTEMS:
        raise ValueError("invalid filesystem: %s" % job.filesystem)
    job.label = util.sanitize_label(job.label, job.filesystem)
    if not job.label:
        job.label = "USB"
    return job


# --------------------------------------------------------------------------
# Command helpers
# --------------------------------------------------------------------------
def run_step(rep: Reporter, cancel: CancelToken, cmd, *, check: bool = True, timeout=None):
    cancel.check()
    rep.log("$ " + util.log_command(cmd))
    proc = util.run(cmd, timeout=timeout)
    if proc.stdout and proc.stdout.strip():
        for line in proc.stdout.strip().splitlines():
            rep.log("  " + line)
    if proc.returncode != 0:
        for line in (proc.stderr or "").strip().splitlines():
            rep.log("  ! " + line)
        if check:
            raise util.CommandError(cmd, proc.returncode, proc.stdout, proc.stderr)
        rep.log("Command failed but continuing: %s" % util.stderr_tail(proc.stderr), "warning")
    return proc


def resolve_case(root: str, relative: str) -> str:
    """Find ``relative`` under ``root`` ignoring case (FAT is case-insensitive)."""
    current = root
    for part in [p for p in relative.split("/") if p]:
        try:
            names = os.listdir(current)
        except OSError:
            return ""
        match = None
        for name in names:
            if name.lower() == part.lower():
                match = name
                break
        if match is None:
            return ""
        current = os.path.join(current, match)
    return current if os.path.exists(current) else ""


def iter_files(root: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                size = os.path.getsize(full)
            except OSError:
                size = 0
            yield full, rel, size


def tree_size(root: str) -> int:
    return sum(size for _full, _rel, size in iter_files(root))


def safe_component(name: str, filesystem: str) -> str:
    if filesystem.upper() == "NTFS":
        bad = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    else:
        bad = util.INVALID_FAT_CHARS
    cleaned = bad.sub("_", name)
    if filesystem.upper() != "NTFS":
        cleaned = cleaned.rstrip(" .")
    return cleaned or "_"


def safe_relpath(rel: str, filesystem: str) -> str:
    return "/".join(safe_component(part, filesystem) for part in rel.split("/"))


def unmount_all(paths) -> None:
    for mountpoint in reversed(list(paths)):
        proc = util.run(["umount", mountpoint])
        if proc.returncode != 0:
            util.run(["umount", "-l", mountpoint])


def _source_looks_complete(root: str, info: images.ImageInfo | None) -> bool:
    """Sanity-check a mounted source so we never copy a truncated ISO 9660 stub.

    Microsoft's media puts the real file tree in UDF and ships a minimal
    ISO 9660 tree containing only README.TXT.  A plain ``mount`` can happily
    mount that stub, so after mounting we confirm a few known files exist.
    """
    try:
        if not os.listdir(root):
            return False
    except OSError:
        return False

    if info is None:
        return True

    if info.install_image and not resolve_case(root, info.install_image):
        return False

    if info.kind == "linux":
        if not any(resolve_case(root, candidate) for candidate in images.LINUX_KERNELS):
            return False

    if not info.entries:
        return True

    files = [entry for entry in info.entries.values() if not entry.is_dir and entry.path]
    files.sort(key=lambda entry: entry.size, reverse=True)
    for entry in files[:3]:
        if not resolve_case(root, entry.path):
            return False
    return True


def mount_source(
    image: str,
    tempdir: str,
    rep: Reporter,
    cancel: CancelToken,
    info: images.ImageInfo | None = None,
) -> tuple[str, str]:
    """Mount (or unpack) the source image read-only. Returns (path, mountpoint)."""
    if os.path.isdir(image):
        return image, ""
    mountpoint = os.path.join(tempdir, "source")
    os.makedirs(mountpoint, exist_ok=True)

    # UDF first: Microsoft ISOs store the real tree in UDF, and the kernel's
    # ISO 9660 driver would otherwise mount a stub with just README.TXT.
    attempts = []
    if util.is_block_device(image):
        attempts.append(["mount", "-t", "udf", "-o", "ro", image, mountpoint])
        attempts.append(["mount", "-o", "ro", image, mountpoint])
        attempts.append(["mount", "-t", "iso9660", "-o", "ro", image, mountpoint])
    else:
        attempts.append(["mount", "-t", "udf", "-o", "loop,ro", image, mountpoint])
        attempts.append(["mount", "-o", "loop,ro", image, mountpoint])
        attempts.append(["mount", "-t", "iso9660", "-o", "loop,ro", image, mountpoint])

    for cmd in attempts:
        cancel.check()
        proc = util.run(cmd)
        if proc.returncode != 0:
            rep.log("Mount attempt failed: %s" % util.stderr_tail(proc.stderr, 2), "warning")
            continue
        if _source_looks_complete(mountpoint, info):
            return mountpoint, mountpoint
        rep.log(
            "Mounted with %s but the expected files are missing; retrying with another filesystem"
            % cmd[1],
            "warning",
        )
        util.run(["umount", mountpoint])
        util.run(["umount", "-l", mountpoint])

    # Last resort: extract with 7z.  It understands UDF and needs disk space.
    if not util.which("7z"):
        raise RuntimeError("unable to mount the image properly and 7z is not installed")
    extract_dir = os.path.join(tempdir, "extracted")
    if info is not None and info.size:
        free = util.free_space(tempdir)
        if free < info.size + 64 * 1024**2:
            raise RuntimeError(
                "the image cannot be mounted readably and there is not enough space in "
                "%s to unpack it (%s needed, %s free); free some space or set TMPDIR"
                % (os.path.dirname(tempdir) or "/tmp", util.human_size(info.size), util.human_size(free))
            )
    os.makedirs(extract_dir, exist_ok=True)
    rep.status("Image could not be mounted readably; extracting it with 7z (slower)")
    run_step(rep, cancel, ["7z", "x", "-y", "-o" + extract_dir, image])
    if not _source_looks_complete(extract_dir, info):
        raise RuntimeError(
            "the image was extracted but the expected files are still missing; "
            "it may be corrupt or not an installable image"
        )
    return extract_dir, ""


def mount_target(partition: str, filesystem: str, tempdir: str, rep: Reporter, cancel: CancelToken) -> str:
    mountpoint = os.path.join(tempdir, "target")
    os.makedirs(mountpoint, exist_ok=True)
    candidates = [["mount", partition, mountpoint]]
    if filesystem.upper() == "NTFS":
        candidates.append(["mount", "-t", "ntfs3", partition, mountpoint])
        candidates.append(["mount", "-t", "ntfs-3g", partition, mountpoint])
    elif filesystem.upper() == "FAT32":
        candidates.append(["mount", "-t", "vfat", partition, mountpoint])
    elif filesystem.upper() == "EXFAT":
        candidates.append(["mount", "-t", "exfat", partition, mountpoint])
    for cmd in candidates:
        cancel.check()
        proc = util.run(cmd)
        if proc.returncode == 0:
            return mountpoint
        rep.log("Mount attempt failed: %s" % util.stderr_tail(proc.stderr, 2), "warning")
    raise RuntimeError("unable to mount the freshly formatted partition")


# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------
def preflight(job: Job, info: images.ImageInfo, mode: str, rep: Reporter, cancel: CancelToken) -> devices.Device:
    cancel.check()
    if not job.dry_run and not util.is_root():
        raise RuntimeError("the write engine must run as root")

    if not os.path.isfile(job.image) and not util.is_block_device(job.image):
        raise RuntimeError("image does not exist: %s" % job.image)
    if not util.is_block_device(job.device):
        raise RuntimeError("%s is not a block device" % job.device)

    device = devices.find_device(job.device, show_all=True)
    if device is None:
        raise RuntimeError("%s is not a known disk" % job.device)

    protected, reason = devices.is_protected(job.device)
    if protected:
        raise RuntimeError("refusing to write to %s: %s" % (job.device, reason))
    if device.read_only:
        raise RuntimeError("%s is read-only" % job.device)
    if job.confirm != job.device and not job.dry_run:
        raise RuntimeError("safety confirmation does not match the target device")

    # Work with the canonical node name from here on so partition nodes are
    # correct even when the user passed a /dev/disk/by-id/... symlink.
    job.device = device.path

    if mode == "dd" and os.path.isfile(job.image):
        if info.size > device.size:
            raise RuntimeError(
                "image (%s) is larger than the target device (%s)"
                % (util.human_size(info.size), util.human_size(device.size))
            )

    holders_path = Path("/sys/block", device.name, "holders")
    if holders_path.is_dir():
        holders = [h for h in os.listdir(holders_path) if h]
        if holders:
            raise RuntimeError(
                "%s is in use by %s (device-mapper/LVM); detach it first"
                % (job.device, ", ".join(holders))
            )

    if job.dry_run:
        rep.log("Dry run: skipping unmount, wipe and write operations")
        return device

    # Unmount everything and disable swap on the target.
    for swap in devices.swap_partitions(job.device):
        rep.log("Disabling swap on %s" % swap)
        run_step(rep, cancel, ["swapoff", swap], check=False)
    try:
        devices.unmount_device(job.device)
    except util.CommandError as exc:
        raise RuntimeError("target is busy: %s" % exc)

    for part in device.partitions:
        if util.mountpoints_for(part.path):
            raise RuntimeError("still mounted: %s" % part.path)

    return device


# --------------------------------------------------------------------------
# DD mode
# --------------------------------------------------------------------------
def write_dd(job: Job, info: images.ImageInfo, rep: Reporter, cancel: CancelToken) -> None:
    size = info.size
    rep.meta(mode="dd", bytes=size)
    rep.status("Writing image in DD mode - every sector is copied verbatim")

    run_step(rep, cancel, ["wipefs", "--all", job.device], check=False)
    fd = os.open(job.device, os.O_WRONLY)
    done = 0
    try:
        with open(job.image, "rb") as source:
            while True:
                cancel.check()
                chunk = source.read(util.CHUNK_SIZE)
                if not chunk:
                    break
                util.write_all(fd, chunk)
                done += len(chunk)
                rep.progress(5 + 85 * done / max(size, 1), "Writing %s" % util.human_size(done))
        os.fsync(fd)
    finally:
        os.close(fd)
    util.run(["blockdev", "--flushbufs", job.device])
    rep.log("Wrote %s" % util.human_size(done))

    if job.verify:
        rep.status("Verifying written data (SHA-256)")
        a = util.checksum_file(
            job.image,
            "sha256",
            progress=lambda f: rep.progress(90 + 4 * f, "Checksumming image"),
            cancel=cancel,
        )
        b = util.checksum_file(
            job.device,
            "sha256",
            limit=size,
            progress=lambda f: rep.progress(94 + 5 * f, "Reading back device"),
            cancel=cancel,
        )
        if a != b:
            raise RuntimeError("verification failed: image and device checksums differ")
        rep.log("Checksums match (%s)" % a)


# --------------------------------------------------------------------------
# Extract mode
# --------------------------------------------------------------------------
def create_partitions(
    job: Job,
    info: images.ImageInfo,
    rep: Reporter,
    cancel: CancelToken,
    *,
    windows: bool,
) -> tuple[str, str | None, int]:
    """Create the table/partitions, format them, return (main_part, support_part, main_index)."""
    device = job.device
    scheme = job.scheme
    filesystem = job.filesystem
    target = job.target

    rep.status("Wiping old partition table and filesystem signatures")
    run_step(rep, cancel, ["wipefs", "--all", device], check=False)
    if scheme == "gpt" and util.which("sgdisk"):
        run_step(rep, cancel, ["sgdisk", "--zap-all", device], check=False)

    rep.status("Creating a %s partition table" % scheme.upper())
    run_step(rep, cancel, ["parted", "--script", device, "mklabel", "msdos" if scheme == "mbr" else "gpt"])

    main_index = 1
    if scheme == "gpt" and target in ("bios", "both"):
        # GRUB's i386-pc core needs a dedicated BIOS boot partition on GPT.
        rep.status("Creating a BIOS boot partition for legacy GRUB")
        run_step(
            rep,
            cancel,
            ["parted", "--script", device, "mkpart", "primary", "1MiB", "4MiB"],
        )
        run_step(rep, cancel, ["parted", "--script", device, "set", "1", "bios_grub", "on"])
        main_index = 2

    end = "100%"
    needs_uefi_ntfs = windows and filesystem.upper() == "NTFS" and target in ("uefi", "both")
    if needs_uefi_ntfs:
        # Reserve 4 MiB at the end for the UEFI:NTFS boot partition.  The
        # image itself is 1 MiB; the margin keeps us away from the backup GPT
        # and from parted's inclusive end-sector rounding.  -8193s (rather
        # than -8192s) leaves a one-sector gap so the partitions never overlap.
        end = "-8193s"

    fs_type = {"FAT32": "fat32", "NTFS": "ntfs", "EXFAT": None}[filesystem.upper()]
    cmd = ["parted", "--align", "none", "--script", device, "mkpart", "primary"]
    if fs_type:
        cmd.append(fs_type)
    if end.startswith("-"):
        # A leading dash would otherwise be parsed as an option.
        cmd.append("--")
    cmd.extend(["4MiB", end])
    rep.status("Creating the %s data partition" % filesystem)
    run_step(rep, cancel, cmd)

    run_step(rep, cancel, ["partprobe", device], check=False)
    run_step(rep, cancel, ["blockdev", "--rereadpt", device], check=False)

    main_part = devices.wait_for_partition(device, main_index)
    rep.log("Data partition: %s" % main_part)

    rep.status("Formatting %s as %s" % (main_part, filesystem))
    label = job.label
    if filesystem.upper() == "FAT32":
        run_step(rep, cancel, ["mkfs.vfat", "-F", "32", "-n", label, main_part])
    elif filesystem.upper() == "NTFS":
        run_step(rep, cancel, ["mkfs.ntfs", "--quick", "--force", "-L", label, main_part])
    elif filesystem.upper() == "EXFAT":
        proc = run_step(rep, cancel, ["mkfs.exfat", "-L", label, main_part], check=False)
        if proc.returncode != 0:
            run_step(rep, cancel, ["mkfs.exfat", "-n", label, main_part])

    # Partition type / boot flags.
    if scheme == "mbr":
        run_step(rep, cancel, ["parted", "--script", device, "set", str(main_index), "boot", "on"], check=False)
        if filesystem.upper() == "EXFAT":
            run_step(rep, cancel, ["sfdisk", "--part-type", device, str(main_index), "7"], check=False)
    else:
        if filesystem.upper() == "EXFAT" and util.which("sgdisk"):
            run_step(rep, cancel, ["sgdisk", "-t", "%d:0700" % main_index, device], check=False)
        if filesystem.upper() == "FAT32" and target in ("uefi", "both"):
            # Mark the FAT32 partition as an EFI System Partition so strict
            # firmwares find it.  exFAT is not an ESP filesystem, but the
            # type flag still helps firmwares that can read exFAT themselves.
            run_step(rep, cancel, ["parted", "--script", device, "set", str(main_index), "esp", "on"], check=False)
        elif filesystem.upper() == "EXFAT" and target in ("uefi", "both"):
            run_step(rep, cancel, ["parted", "--script", device, "set", str(main_index), "esp", "on"], check=False)

    support_part = None
    if needs_uefi_ntfs:
        # GPT stores a backup header in the last 33 sectors, so the partition
        # cannot end at -1s there (parted rejects it as "not usable").
        support_end = "-34s" if scheme == "gpt" else "-1s"
        rep.status("Creating the UEFI:NTFS boot partition (4 MiB)")
        run_step(
            rep,
            cancel,
            [
                "parted", "--align", "none", "--script", device,
                "mkpart", "primary", "fat16", "--", "-8192s", support_end,
            ],
        )
        support_index = main_index + 1
        support_part = devices.wait_for_partition(device, support_index)
        if scheme == "gpt":
            run_step(rep, cancel, ["parted", "--script", device, "set", str(support_index), "esp", "on"], check=False)
        else:
            run_step(rep, cancel, ["sfdisk", "--part-type", device, str(support_index), "ef"], check=False)
        install_uefi_ntfs(job, support_part, rep, cancel)

    run_step(rep, cancel, ["blockdev", "--flushbufs", device], check=False)
    return main_part, support_part, main_index


def install_uefi_ntfs(job: Job, partition: str, rep: Reporter, cancel: CancelToken) -> None:
    base = job.uefi_ntfs or resource_path("uefi-ntfs.img")
    image = base
    if not os.path.isfile(image):
        rep.log(
            "UEFI:NTFS bootloader image is missing (%s); NTFS media may not boot on "
            "UEFI firmware. Reinstall RufusLinux or use FAT32 instead." % image,
            "warning",
        )
        return
    size = os.path.getsize(image)
    part_size = 0
    if os.path.exists(partition):
        # os.path.getsize() is always 0 for a block device; ask the kernel.
        proc = util.run(["blockdev", "--getsize64", partition])
        if proc.returncode == 0:
            try:
                part_size = int(proc.stdout.strip())
            except ValueError:
                part_size = 0
    if part_size and size > part_size:
        rep.log(
            "UEFI:NTFS image (%s) does not fit in its partition (%s); skipped"
            % (util.human_size(size), util.human_size(part_size)),
            "warning",
        )
        return
    cancel.check()
    rep.log("Writing UEFI:NTFS bootloader to %s" % partition)
    proc = util.run(["dd", "if=" + image, "of=" + partition, "bs=1M", "conv=fsync"], timeout=120)
    if proc.returncode != 0:
        rep.log("Unable to write UEFI:NTFS: %s" % util.stderr_tail(proc.stderr), "warning")


def check_fat32_limits(source: str, info: images.ImageInfo, job: Job, rep: Reporter) -> set[str]:
    """Return relative paths that must not be copied as single files."""
    if job.filesystem.upper() != "FAT32":
        return set()

    skipped: set[str] = set()
    huge = [(rel, size) for _full, rel, size in iter_files(source) if size > FAT32_MAX_FILE]
    for rel, size in huge:
        low = rel.lower()
        if low.endswith("sources/install.wim") or low == "sources/install.wim":
            if not job.split_wim:
                raise RuntimeError(
                    "%s is %s, larger than FAT32's 4 GB file limit. "
                    "Enable WIM splitting or choose NTFS." % (rel, util.human_size(size))
                )
            if not util.which("wimlib-imagex"):
                raise RuntimeError(
                    "%s is %s and must be split, but wimlib-imagex is missing. "
                    "Install it with: sudo apt install wimtools" % (rel, util.human_size(size))
                )
            skipped.add(rel)
            rep.log("%s will be split into install.swm parts on the target" % rel)
        elif low.endswith("sources/install.esd"):
            raise RuntimeError(
                "%s is %s. An ESD cannot be split for FAT32; choose NTFS."
                % (rel, util.human_size(size))
            )
        else:
            hint = (
                "Use DD mode instead, or NTFS with BIOS-only boot."
                if info.kind == "linux"
                else "Choose NTFS or exFAT."
            )
            raise RuntimeError(
                "%s is %s, larger than FAT32's 4 GB file limit. %s"
                % (rel, util.human_size(size), hint)
            )
    return skipped


def copy_tree(
    source: str,
    target: str,
    filesystem: str,
    rep: Reporter,
    cancel: CancelToken,
    *,
    skip: set[str] | None = None,
) -> int:
    skip = {s.lower() for s in (skip or set())}
    total = 0
    for _full, rel, size in iter_files(source):
        if rel.lower() not in skip:
            total += size

    copied = 0
    renamed = 0
    for dirpath, dirnames, filenames in os.walk(source):
        cancel.check()
        rel_dir = os.path.relpath(dirpath, source)
        if rel_dir == ".":
            rel_dir = ""
        target_dir = os.path.join(target, safe_relpath(rel_dir, filesystem)) if rel_dir else target
        os.makedirs(target_dir, exist_ok=True)
        for name in filenames:
            cancel.check()
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, source).replace(os.sep, "/")
            if rel.lower() in skip:
                continue
            safe_name = safe_component(name, filesystem)
            if safe_name != name:
                renamed += 1
            dest = os.path.join(target_dir, safe_name)
            if os.path.islink(full):
                link = os.readlink(full)
                try:
                    os.symlink(link, dest)
                except OSError:
                    shutil.copyfile(full, dest)
                continue
            try:
                source_size = os.path.getsize(full)
            except OSError:
                source_size = 0
            with open(full, "rb") as src, open(dest, "wb") as dst:
                while True:
                    cancel.check()
                    chunk = src.read(util.CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
                    copied += len(chunk)
                    rep.progress(22 + 63 * copied / max(total, 1), util.human_size(copied))
            try:
                os.chmod(dest, stat.S_IMODE(os.stat(full).st_mode))
            except OSError:
                pass

    if renamed:
        rep.log("%d file name(s) contained characters not allowed on %s and were renamed" % (renamed, filesystem), "warning")
    return copied


def split_wim(source: str, target: str, rep: Reporter, cancel: CancelToken) -> None:
    wim = resolve_case(source, "sources/install.wim")
    if not wim:
        return
    out_dir = os.path.join(target, "sources")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "install.swm")
    rep.status("Splitting install.wim into FAT32-sized parts")
    run_step(rep, cancel, ["wimlib-imagex", "split", wim, out, str(SWM_PART_SIZE_MB)], timeout=3600)
    parts = sorted(Path(out_dir).glob("install*.swm"))
    rep.log("Created %s (%s total)" % (", ".join(p.name for p in parts), util.human_size(sum(p.stat().st_size for p in parts))))
    if not parts:
        raise RuntimeError("WIM splitting produced no output")


def install_windows_boot(
    source: str,
    target: str,
    job: Job,
    rep: Reporter,
    cancel: CancelToken,
    *,
    support_part: str | None,
) -> None:
    # UEFI boot files should already be on the media.  Windows 7 media needs help.
    if job.target in ("uefi", "both"):
        efi_boot = resolve_case(target, "EFI/BOOT/BOOTX64.EFI") or resolve_case(target, "EFI/BOOT/BOOTIA32.EFI")
        if not efi_boot:
            rep.log("No EFI/BOOT/BOOTX64.EFI found on the media; trying to recover one", "warning")
            bootmgr_efi = resolve_case(source, "bootmgr.efi")
            destination = os.path.join(target, "EFI", "BOOT")
            os.makedirs(destination, exist_ok=True)
            if bootmgr_efi:
                shutil.copy2(bootmgr_efi, os.path.join(destination, "BOOTX64.EFI"))
                rep.log("Copied bootmgr.efi to EFI/BOOT/BOOTX64.EFI")
            else:
                wim = resolve_case(target, "sources/install.wim")
                if wim and util.which("7z"):
                    proc = subprocess.run(
                        ["7z", "e", "-so", wim, "Windows/Boot/EFI/bootmgfw.efi"],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL,
                    )
                    if proc.returncode == 0 and proc.stdout:
                        with open(os.path.join(destination, "BOOTX64.EFI"), "wb") as out:
                            out.write(proc.stdout)
                        rep.log("Extracted bootmgfw.efi from install.wim for UEFI boot")
                if not os.path.exists(os.path.join(destination, "BOOTX64.EFI")):
                    rep.log("UEFI boot files are missing; the drive may not boot on UEFI systems", "warning")
        if job.filesystem.upper() == "NTFS" and support_part:
            rep.log("UEFI boot will use the UEFI:NTFS partition %s" % support_part)

    if job.target not in ("bios", "both"):
        return

    if not util.which("grub-install"):
        if job.target == "bios":
            raise RuntimeError("grub-install is required for legacy BIOS boot (install grub-pc)")
        rep.log("grub-install is missing; legacy BIOS boot was skipped", "warning")
        return

    rep.status("Installing the legacy BIOS bootloader (GRUB)")
    boot_dir = os.path.join(target, "boot")
    os.makedirs(boot_dir, exist_ok=True)
    run_step(
        rep,
        cancel,
        ["grub-install", "--target=i386-pc", "--boot-directory=" + boot_dir, "--recheck", "--force", job.device],
        check=False,
    )
    grub_cfg = os.path.join(boot_dir, "grub", "grub.cfg")
    os.makedirs(os.path.dirname(grub_cfg), exist_ok=True)
    with open(grub_cfg, "w", encoding="utf-8") as handle:
        handle.write(
            "set timeout=0\n"
            "set timeout_style=hidden\n"
            "\n"
            "menuentry \"Windows installer\" {\n"
            "    insmod part_msdos\n"
            "    insmod part_gpt\n"
            "    insmod ntfs\n"
            "    insmod fat\n"
            "    insmod ntldr\n"
            "    search --no-floppy --file --set=root /bootmgr\n"
            "    ntldr /bootmgr\n"
            "    boot\n"
            "}\n"
        )
    rep.log("Wrote %s" % grub_cfg)


def write_linux_grub_config(target: str, job: Job, rep: Reporter) -> str:
    label = job.label
    entries: list[str] = []

    def add(title, kernel, initrd, args):
        initrd_line = "    initrd %s\n" % initrd if initrd else ""
        entries.append(
            "if [ -f %s ]; then\n"
            "menuentry \"%s\" {\n"
            "    search --no-floppy --file --set=root %s\n"
            "    linux %s %s\n"
            "%s"
            "}\n"
            "fi\n" % (kernel, title, kernel, kernel, args, initrd_line)
        )

    if resolve_case(target, "casper/vmlinuz"):
        add("Ubuntu / Debian live", "/casper/vmlinuz", "/casper/initrd", "boot=casper quiet splash ---")
    if resolve_case(target, "casper/vmlinuz.efi"):
        add("Ubuntu / Debian live (EFI)", "/casper/vmlinuz.efi", "/casper/initrd", "boot=casper quiet splash ---")
    if resolve_case(target, "live/vmlinuz"):
        add("Debian live", "/live/vmlinuz", "/live/initrd.img", "boot=live components quiet splash")
    if resolve_case(target, "live/vmlinuz.efi"):
        add("Debian live (EFI)", "/live/vmlinuz.efi", "/live/initrd.img", "boot=live components quiet splash")
    if resolve_case(target, "isolinux/vmlinuz"):
        add("Linux installer", "/isolinux/vmlinuz", "/isolinux/initrd.img", "quiet")
    if resolve_case(target, "images/pxeboot/vmlinuz"):
        add("Fedora / RHEL live", "/images/pxeboot/vmlinuz", "/images/pxeboot/initrd.img",
            "root=live:LABEL=%s rd.live.image quiet" % label)
    if resolve_case(target, "arch/boot/x86_64/vmlinuz-linux"):
        initrd = "/arch/boot/intel-ucode.img /arch/boot/x86_64/initramfs-linux.img"
        add("Arch Linux", "/arch/boot/x86_64/vmlinuz-linux", initrd,
            "archisobasedir=arch archisolabel=%s" % label)
    if resolve_case(target, "boot/vmlinuz"):
        initrd = "/boot/initrd.img" if resolve_case(target, "boot/initrd.img") else ""
        add("Installed Linux", "/boot/vmlinuz", initrd, "ro quiet")

    if not entries:
        entries.append(
            "menuentry \"Chainload first partition\" {\n"
            "    insmod chain\n"
            "    insmod part_msdos\n"
            "    insmod part_gpt\n"
            "    set root=(hd0,1)\n"
            "    chainloader +1\n"
            "}\n"
        )
        rep.log("No known Linux kernel layout found; wrote a chainload entry instead", "warning")

    cfg = (
        "set timeout=10\n"
        "set default=0\n"
        "insmod part_msdos\n"
        "insmod part_gpt\n"
        "insmod fat\n"
        "insmod ntfs\n"
        "insmod search_fs_file\n"
        "insmod chain\n"
        "\n" + "\n".join(entries)
    )
    grub_dir = os.path.join(target, "boot", "grub")
    os.makedirs(grub_dir, exist_ok=True)
    path = os.path.join(grub_dir, "grub.cfg")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(cfg)
    rep.log("Wrote %s" % path)
    return path


def install_linux_boot(source: str, target: str, job: Job, rep: Reporter, cancel: CancelToken) -> None:
    efi_present = bool(
        resolve_case(target, "EFI/BOOT/BOOTX64.EFI") or resolve_case(target, "EFI/BOOT/BOOTIA32.EFI")
    )

    if job.target in ("uefi", "both") and not efi_present:
        if job.filesystem.upper() == "FAT32" and util.which("grub-install"):
            rep.status("Installing a UEFI fallback bootloader (GRUB)")
            run_step(
                rep,
                cancel,
                [
                    "grub-install",
                    "--target=x86_64-efi",
                    "--efi-directory=" + target,
                    "--boot-directory=" + os.path.join(target, "boot"),
                    "--removable",
                    "--no-nvram",
                    "--recheck",
                ],
                check=False,
            )
        else:
            rep.log(
                "This image has no EFI/BOOT/BOOTX64.EFI and no UEFI bootloader could be "
                "installed. Use FAT32 or DD mode for UEFI boot.",
                "warning",
            )

    if job.target in ("bios", "both"):
        if util.which("grub-install"):
            rep.status("Installing the legacy BIOS bootloader (GRUB)")
            run_step(
                rep,
                cancel,
                [
                    "grub-install",
                    "--target=i386-pc",
                    "--boot-directory=" + os.path.join(target, "boot"),
                    "--recheck",
                    "--force",
                    job.device,
                ],
                check=False,
            )
        else:
            rep.log("grub-install is missing; legacy BIOS boot was skipped", "warning")

    write_linux_grub_config(target, job, rep)


def verify_extract(source: str, target: str, skip: set[str], filesystem: str, rep: Reporter, cancel: CancelToken) -> None:
    rep.status("Verifying copied files")
    expected: dict[str, int] = {}
    for _full, rel, size in iter_files(source):
        if rel.lower() in {s.lower() for s in skip}:
            continue
        expected[safe_relpath(rel, filesystem).lower()] = size

    actual: dict[str, int] = {}
    for _full, rel, size in iter_files(target):
        actual[rel.lower()] = size

    missing = [rel for rel in expected if rel not in actual]
    wrong = [rel for rel, size in expected.items() if rel in actual and actual[rel] != size]
    if missing or wrong:
        detail = []
        if missing:
            detail.append("%d missing (e.g. %s)" % (len(missing), missing[0]))
        if wrong:
            detail.append("%d size mismatches (e.g. %s)" % (len(wrong), wrong[0]))
        raise RuntimeError("verification failed: " + "; ".join(detail))
    rep.log("Verified %d files (%s)" % (len(expected), util.human_size(sum(expected.values()))))


def extract_mode(job: Job, info: images.ImageInfo, rep: Reporter, cancel: CancelToken) -> None:
    windows = info.kind == "windows"
    target_tmp = tempfile.mkdtemp(prefix="rufus-linux.")
    source_mount = ""
    target_mount = ""
    cancel_token = cancel

    try:
        main_part, support_part, _main_index = create_partitions(
            job, info, rep, cancel_token, windows=windows
        )

        rep.status("Mounting source image")
        source, source_mount = mount_source(job.image, target_tmp, rep, cancel_token, info)

        rep.status("Mounting the new filesystem")
        target_mount = mount_target(main_part, job.filesystem, target_tmp, rep, cancel_token)

        skip = check_fat32_limits(source, info, job, rep)
        needed = tree_size(source)
        free = util.free_space(target_mount)
        rep.log("Source payload: %s; free space on target: %s" % (util.human_size(needed), util.human_size(free)))
        if needed > free:
            raise RuntimeError(
                "not enough space on the target: need %s, have %s"
                % (util.human_size(needed), util.human_size(free))
            )
        if free < needed * 1.05:
            rep.log(
                "The target is almost full; filesystem overhead can still make the copy fail",
                "warning",
            )

        rep.status("Copying files - this is the slow part")
        copy_tree(source, target_mount, job.filesystem, rep, cancel_token, skip=skip)

        if windows and skip:
            split_wim(source, target_mount, rep, cancel_token)

        rep.status("Installing the bootloader")
        if windows:
            install_windows_boot(source, target_mount, job, rep, cancel_token, support_part=support_part)
        else:
            install_linux_boot(source, target_mount, job, rep, cancel_token)

        rep.status("Flushing data to the device")
        util.run(["sync"])
        run_step(rep, cancel_token, ["blockdev", "--flushbufs", job.device], check=False)

        if job.verify:
            if windows and skip:
                rep.log("Split WIM verification checks file presence only")
            verify_extract(source, target_mount, skip, job.filesystem, rep, cancel_token)
    finally:
        rep.log("Cleaning up")
        util.run(["sync"])
        for mountpoint in (target_mount, source_mount):
            if mountpoint:
                proc = util.run(["umount", mountpoint])
                if proc.returncode != 0:
                    util.run(["umount", "-l", mountpoint])
                    rep.log("Lazy-unmounted %s (was busy)" % mountpoint, "warning")
        util.run(["blockdev", "--flushbufs", job.device], check=False)
        shutil.rmtree(target_tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# Main entry
# --------------------------------------------------------------------------
def run_job(job: Job, rep: Reporter) -> int:
    cancel = CancelToken(job.cancel_file)
    info = images.analyze(job.image)
    mode = info.recommended_mode if job.mode == "auto" else job.mode

    rep.meta(
        mode=mode,
        filesystem=job.filesystem,
        scheme=job.scheme,
        target=job.target,
        kind=info.kind,
        image_size=info.size,
        label=job.label,
    )
    rep.log("Image: %s" % job.image)
    rep.log("Image type: %s (%s)" % (info.kind, info.summary()))
    rep.log("Target: %s (%s)" % (job.device, _device_size_text(job.device)))
    rep.log("Mode: %s, filesystem: %s, table: %s, boot: %s"
            % (mode, job.filesystem, job.scheme.upper(), job.target))

    if mode == "extract" and not info.extract_supported:
        raise RuntimeError(
            "this image has no readable filesystem, so extract mode is not possible; "
            "use DD mode instead"
        )
    unsupported = images.unsupported_image_reason(job.image)
    if unsupported:
        raise RuntimeError(unsupported)
    if mode == "extract" and info.kind == "linux" and job.target in ("uefi", "both") \
            and job.filesystem.upper() != "FAT32":
        raise RuntimeError("Linux UEFI boot needs a FAT32 target; choose FAT32 or BIOS only")
    if mode == "extract" and job.filesystem.upper() == "EXFAT":
        if info.kind == "linux":
            raise RuntimeError("Linux extract mode requires FAT32; exFAT is not supported")
        if job.target != "uefi":
            raise RuntimeError(
                "exFAT Windows media can only boot on UEFI; choose UEFI only, "
                "or use FAT32/NTFS for BIOS support"
            )
    if mode == "extract" and info.kind == "windows" and job.filesystem.upper() == "FAT32" \
            and info.install_image.lower().endswith(".wim") \
            and info.install_size > FAT32_MAX_FILE:
        if not job.split_wim:
            raise RuntimeError(
                "sources/install.wim is larger than 4 GB; enable WIM splitting or choose NTFS"
            )
        if not util.which("wimlib-imagex"):
            raise RuntimeError(
                "sources/install.wim is larger than 4 GB and wimlib-imagex is missing; "
                "install wimtools or choose NTFS"
            )
    if mode == "dd" and info.kind == "windows" and not info.hybrid:
        rep.log(
            "This Windows image is not a hybrid ISO; DD mode will copy it but it "
            "will almost certainly not boot. Extract mode is recommended.",
            "warning",
        )

    device = preflight(job, info, mode, rep, cancel)

    if job.dry_run:
        rep.status("Dry run finished - no data was written")
        rep.log("Would write %s to %s (%s)" % (job.image, job.device, util.human_size(device.size)))
        rep.done(True, "Dry run complete; the device was not modified")
        return 0

    rep.status("Starting write to %s" % job.device)
    if mode == "dd":
        write_dd(job, info, rep, cancel)
    else:
        extract_mode(job, info, rep, cancel)

    rep.progress(100, "Finished")
    rep.done(True, "The target device should now be bootable")
    return 0


def _device_size_text(path: str) -> str:
    device = devices.find_device(path, show_all=True)
    return util.human_size(device.size) if device else "unknown size"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RufusLinux privileged write engine")
    parser.add_argument("--job", required=True, help="path to the JSON job file")
    parser.add_argument("--human", action="store_true", help="human-readable log instead of JSON")
    parser.add_argument("--version", action="version", version="RufusLinux engine")
    args = parser.parse_args(argv)

    rep = Reporter(human=args.human)
    try:
        job = load_job(args.job)
    except Exception as exc:
        rep.done(False, "invalid job: %s" % exc)
        return 2

    try:
        return run_job(job, rep)
    except Cancelled:
        rep.log("Cancelled by the user", "warning")
        rep.done(False, "Cancelled")
        return 130
    except util.CommandError as exc:
        rep.log(str(exc), "error")
        rep.done(False, str(exc))
        return 1
    except Exception as exc:
        rep.log("Error: %s" % exc, "error")
        if job.debug:
            for line in traceback.format_exc().splitlines():
                rep.log(line, "error")
        rep.done(False, str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
