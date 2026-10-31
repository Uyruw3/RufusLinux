"""Block-device discovery and safety checks.

The device list is intentionally conservative: by default only removable
media and USB-attached disks are shown.  Internal disks can be revealed with
"Show all drives", but the disk hosting the running system is always treated
as protected and the engine refuses to write to it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import util

#: Mount points that mean "this disk contains the running system or user data".
PROTECTED_MOUNTS = ("/", "/boot", "/boot/efi", "/usr", "/var", "/home")

#: Kernel block devices that are not real target candidates.
IGNORED_PREFIXES = (
    "loop",
    "ram",
    "zram",
    "sr",
    "fd",
    "md",
    "dm-",
    "nbd",
    "rbd",
    "drbd",
)


@dataclass
class Partition:
    name: str
    path: str
    size: int
    fstype: str
    label: str
    mountpoints: list[str] = field(default_factory=list)

    @property
    def mounted(self) -> bool:
        return bool(self.mountpoints)


@dataclass
class Device:
    name: str
    path: str
    size: int
    model: str
    vendor: str
    tran: str
    removable: bool
    read_only: bool
    partitions: list[Partition] = field(default_factory=list)
    is_system: bool = False
    protected_mounts: list[str] = field(default_factory=list)

    @property
    def is_usb(self) -> bool:
        return self.tran.lower() == "usb" or self.removable

    @property
    def mountpoints(self) -> list[str]:
        out: list[str] = []
        for part in self.partitions:
            out.extend(part.mountpoints)
        return out

    def description(self) -> str:
        bits = [self.path]
        if self.model:
            bits.append(self.model)
        if self.vendor and self.vendor not in (self.model or ""):
            bits.append(self.vendor)
        bits.append(util.human_size(self.size))
        if self.read_only:
            bits.append("read-only")
        if self.is_system:
            bits.append("SYSTEM DISK")
        return " - ".join(b for b in bits if b)

    def details(self) -> str:
        lines = [self.description()]
        if self.partitions:
            for part in self.partitions:
                text = "  %s  %s  %s" % (
                    part.path,
                    util.human_size(part.size),
                    part.fstype or "unknown",
                )
                if part.label:
                    text += "  [%s]" % part.label
                if part.mountpoints:
                    text += "  mounted at " + ", ".join(part.mountpoints)
                lines.append(text)
        else:
            lines.append("  (no partitions)")
        return "\n".join(lines)


def _sysfs_flag(name: str, flag: str) -> bool:
    try:
        return Path("/sys/block", name, flag).read_text().strip() == "1"
    except OSError:
        return False


def _list_mountpoints(node: dict, fallback: bool) -> list[str]:
    if fallback:
        value = node.get("mountpoint")
        return [value] if value else []
    value = node.get("mountpoints") or []
    return [m for m in value if m]


def _walk(node: dict, fallback: bool) -> tuple[list[Partition], list[str]]:
    parts: list[Partition] = []
    mounts: list[str] = []
    for child in node.get("children") or []:
        ctype = child.get("type")
        cmounts = _list_mountpoints(child, fallback)
        mounts.extend(cmounts)
        if ctype == "part":
            parts.append(
                Partition(
                    name=child.get("name") or "",
                    path=child.get("path") or ("/dev/" + (child.get("name") or "")),
                    size=int(child.get("size") or 0),
                    fstype=child.get("fstype") or "",
                    label=child.get("label") or "",
                    mountpoints=cmounts,
                )
            )
        # LVM / dm / md children can hide the real partitions; recurse anyway.
        sub_parts, sub_mounts = _walk(child, fallback)
        parts.extend(p for p in sub_parts if p.path not in {x.path for x in parts})
        mounts.extend(m for m in sub_mounts if m not in mounts)
    return parts, mounts


def _root_disk_name() -> str:
    source = util.findmnt_source("/")
    if not source:
        return ""
    proc = util.run(["lsblk", "-no", "PKNAME", source])
    if proc.returncode != 0:
        return ""
    names = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    return names[-1] if names else ""


def list_devices(show_all: bool = False) -> list[Device]:
    """Enumerate disks (removable/USB only unless ``show_all``)."""
    proc = util.run(
        [
            "lsblk",
            "-J",
            "-b",
            "-o",
            "NAME,PATH,TYPE,SIZE,MODEL,VENDOR,TRAN,RM,RO,MOUNTPOINTS,FSTYPE,LABEL",
        ]
    )
    fallback = proc.returncode != 0
    if fallback:
        proc = util.run(
            [
                "lsblk",
                "-J",
                "-b",
                "-o",
                "NAME,PATH,TYPE,SIZE,MODEL,VENDOR,TRAN,RM,RO,MOUNTPOINT,FSTYPE,LABEL",
            ]
        )
    if proc.returncode != 0:
        raise RuntimeError("lsblk failed: %s" % (proc.stderr or "").strip())

    try:
        nodes = json.loads(proc.stdout).get("blockdevices") or []
    except json.JSONDecodeError as exc:
        raise RuntimeError("unable to parse lsblk output: %s" % exc)

    root_disk = _root_disk_name()
    devices: list[Device] = []

    for node in nodes:
        name = node.get("name") or ""
        if not name or node.get("type") != "disk":
            continue
        if name.startswith(IGNORED_PREFIXES):
            continue
        if int(node.get("size") or 0) <= 0:
            continue

        removable = bool(node.get("rm")) or _sysfs_flag(name, "removable")
        tran = (node.get("tran") or "").strip()
        if tran.lower() == "usb":
            removable = True
        if not show_all and not removable:
            continue

        partitions, mounts = _walk(node, fallback)
        protected = [m for m in mounts if m in PROTECTED_MOUNTS]
        if not protected:
            protected = [m for m in mounts if m.startswith("/boot")]
        is_system = bool(protected) or name == root_disk

        devices.append(
            Device(
                name=name,
                path=node.get("path") or ("/dev/" + name),
                size=int(node.get("size") or 0),
                model=(node.get("model") or "").strip(),
                vendor=(node.get("vendor") or "").strip(),
                tran=tran,
                removable=removable,
                read_only=bool(node.get("ro")),
                partitions=partitions,
                is_system=is_system,
                protected_mounts=protected,
            )
        )

    devices.sort(key=lambda d: (not d.removable, d.is_system, d.path))
    return devices


def find_device(path: str, show_all: bool = True) -> Device | None:
    wanted = os.path.realpath(path)
    for device in list_devices(show_all=show_all):
        if os.path.realpath(device.path) == wanted:
            return device
    return None


def is_protected(path: str) -> tuple[bool, str]:
    """Return (protected, reason) for a would-be target device."""
    device = find_device(path, show_all=True)
    if device is None:
        return True, "not a recognised block device"
    if device.is_system:
        mounts = ", ".join(device.protected_mounts) or "the running system"
        return True, "device contains %s" % mounts
    if device.read_only:
        return True, "device is read-only"
    return False, ""


def unmount_device(device_path: str, *, lazy: bool = False) -> None:
    """Unmount every mounted partition belonging to ``device_path``."""
    device = find_device(device_path, show_all=True)
    if device is None:
        return
    for part in sorted(device.partitions, key=lambda p: p.path, reverse=True):
        targets = util.mountpoints_for(part.path)
        for target in targets:
            args = ["umount"]
            if lazy:
                args.append("-l")
            args.append(target)
            proc = util.run(args)
            if proc.returncode != 0:
                raise util.CommandError(args, proc.returncode, proc.stdout, proc.stderr)


def swap_partitions(device_path: str) -> list[str]:
    proc = util.run(["swapon", "--show=NAME", "--noheadings"])
    if proc.returncode != 0:
        return []
    active = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    device = find_device(device_path, show_all=True)
    if device is None:
        return []
    return [part.path for part in device.partitions if part.path in active]


def wait_for_partition(device: str, number: int, timeout: float = 8.0) -> str:
    node = util.partition_node(device, number)
    if util.wait_for(lambda: os.path.exists(node), timeout=timeout):
        return node
    # Some kernels need a nudge.
    util.run(["partprobe", device])
    util.run(["blockdev", "--rereadpt", device])
    if util.wait_for(lambda: os.path.exists(node), timeout=timeout):
        return node
    raise RuntimeError("partition %s did not appear" % node)
