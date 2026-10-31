"""Bootable-image inspection.

We avoid mounting the image for inspection (that needs root) and instead read
the ISO primary volume descriptor plus a 7-Zip table of contents.  7-Zip
understands both ISO 9660 and the UDF filesystem used by Microsoft's media,
which plain ``isoinfo`` does not.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import util

#: Markers that identify a Linux live/install image.
LINUX_KERNELS = (
    "casper/vmlinuz",
    "casper/initrd",
    "casper/initrd.img",
    "live/vmlinuz",
    "live/vmlinuz.efi",
    "live/initrd.img",
    "isolinux/vmlinuz",
    "isolinux/initrd.img",
    "boot/vmlinuz",
    "boot/vmlinuz.efi",
    "boot/initrd.img",
    "arch/boot/x86_64/vmlinuz-linux",
    "arch/boot/intel-ucode.img",
    "images/pxeboot/vmlinuz",
    "images/pxeboot/initrd.img",
)


@dataclass
class Entry:
    path: str
    size: int
    is_dir: bool


@dataclass
class ImageInfo:
    path: str
    size: int
    label: str
    kind: str = "other"  # windows | linux | other
    hybrid: bool = False
    efi_boot: bool = False
    install_image: str = ""
    install_size: int = 0
    distro: str = ""
    entries: dict[str, Entry] = field(default_factory=dict)
    listing_ok: bool = False
    notes: list[str] = field(default_factory=list)

    # -- lookups ---------------------------------------------------------
    def get(self, path: str) -> Entry | None:
        return self.entries.get(path.strip("/").lower())

    def has(self, path: str) -> bool:
        return self.get(path) is not None

    def find_first(self, *paths: str) -> Entry | None:
        for path in paths:
            entry = self.get(path)
            if entry is not None and not entry.is_dir:
                return entry
        return None

    def size_of(self, path: str) -> int:
        entry = self.get(path)
        return entry.size if entry else 0

    # -- policy ----------------------------------------------------------
    @property
    def extract_supported(self) -> bool:
        return self.listing_ok and self.kind in ("windows", "linux")

    @property
    def recommended_mode(self) -> str:
        if self.kind == "windows":
            return "extract"
        if self.kind == "linux":
            return "dd" if self.hybrid else "extract"
        if self.hybrid:
            return "dd"
        return "extract" if self.extract_supported else "dd"

    @property
    def recommended_filesystem(self) -> str:
        if self.kind == "windows" and self.install_size > 4 * 1024**3:
            return "NTFS"
        return "FAT32"

    def summary(self) -> str:
        bits = []
        if self.kind == "windows":
            bits.append("Windows installation media")
        elif self.kind == "linux":
            bits.append((self.distro or "Linux") + " image")
        else:
            bits.append("Disk image")

        if self.hybrid:
            bits.append("hybrid (DD bootable)")
        else:
            bits.append("non-hybrid")
        if self.efi_boot:
            bits.append("UEFI boot files")
        if self.kind == "windows" and self.install_size:
            bits.append("install image " + util.human_size(self.install_size))
        if not self.listing_ok:
            bits.append("no filesystem listing")
        return " - ".join(bits)

    def warnings(self) -> list[str]:
        out = list(self.notes)
        if not self.hybrid and self.kind != "windows":
            out.append(
                "This image has no hybrid MBR. Writing it raw (DD) will most "
                "likely produce a drive that does not boot; use extract mode."
            )
        if self.kind == "windows" and not self.efi_boot:
            out.append(
                "No EFI/BOOT/BOOTX64.EFI was found. UEFI boot may need an "
                "extracted Windows 7-style bootloader."
            )
        if self.kind == "linux" and self.hybrid:
            out.append(
                "This is a hybrid Linux image: DD mode is recommended because "
                "it preserves the image's own boot setup and extra features."
            )
        return out


def _iso_label(path: str) -> str:
    """Read the ISO 9660 primary volume descriptor label (no root needed)."""
    try:
        with open(path, "rb") as handle:
            handle.seek(16 * 2048)
            pvd = handle.read(2048)
        if len(pvd) >= 72 and pvd[1:6] == b"CD001":
            raw = pvd[40:72].split(b"\x00", 1)[0]
            return raw.decode("ascii", "replace").strip()
    except OSError:
        pass
    return ""


def _is_hybrid(path: str) -> bool:
    """True when the first sector contains a real MBR partition entry."""
    try:
        with open(path, "rb") as handle:
            mbr = handle.read(512)
    except OSError:
        return False
    if len(mbr) < 512 or mbr[510:512] != b"\x55\xaa":
        return False
    for index in range(4):
        entry = mbr[446 + index * 16 : 446 + (index + 1) * 16]
        part_type = entry[4]
        lba = int.from_bytes(entry[8:12], "little")
        sectors = int.from_bytes(entry[12:16], "little")
        if part_type and lba and sectors:
            return True
    return False


def _parse_7z_listing(output: str) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    block: dict[str, str] = {}

    def flush() -> None:
        raw = (block.get("Path") or "").strip()
        if not raw:
            block.clear()
            return
        key = raw.strip("/").lower()
        entries[key] = Entry(
            path=raw.strip("/"),
            size=int(block.get("Size") or 0),
            is_dir=(block.get("Folder") or "").strip() == "+",
        )
        block.clear()

    for line in output.splitlines():
        if not line.strip():
            flush()
            continue
        if " = " in line:
            key, value = line.split(" = ", 1)
            block[key.strip()] = value
    flush()
    return entries


def _list_7z(path: str) -> dict[str, Entry]:
    proc = subprocess.run(
        ["7z", "l", "-ba", "-slt", path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=180,
    )
    if proc.returncode not in (0, 1):
        raise RuntimeError((proc.stderr or proc.stdout or "7z failed").strip())
    return _parse_7z_listing(proc.stdout)


def _read_diskdefines(path: str) -> str:
    """Extract a short distro description from a live ISO if it exists."""
    candidates = (".disk/info", ".disk/README.diskdefines", "readme.diskdefines")
    for candidate in candidates:
        proc = subprocess.run(
            ["7z", "e", "-so", path, candidate],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            text = proc.stdout.strip().splitlines()[0]
            return text[:80]
    return ""


def _guess_distro(path: str, info: ImageInfo) -> str:
    text = _read_diskdefines(path)
    if text:
        # \"Ubuntu 24.04.1 LTS \"...\"\" -> Ubuntu 24.04.1 LTS
        text = re.sub(r"[“”\"]", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            return text

    if info.has("arch/boot/x86_64/vmlinuz-linux") or info.has("arch/pkglist.x86_64"):
        return "Arch Linux"
    if info.has("images/pxeboot/vmlinuz") or (info.has("isolinux/vmlinuz") and info.has("fedora")):
        return "Fedora"
    if info.has("casper/vmlinuz") or info.has("casper/vmlinuz.efi"):
        return "Ubuntu / Debian live"
    if info.has("live/vmlinuz") or info.has("live/vmlinuz.efi"):
        return "Debian live"
    if info.has("isolinux/vmlinuz"):
        return "Linux"
    return "Linux"


def _image_size(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    proc = util.run(["blockdev", "--getsize64", path])
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            return int(proc.stdout.strip())
        except ValueError:
            pass
    return 0


def analyze(path: str | os.PathLike) -> ImageInfo:
    """Inspect an image without mounting or modifying it."""
    image = str(Path(path).expanduser())
    if not os.path.isfile(image) and not util.is_block_device(image):
        raise FileNotFoundError(image)

    info = ImageInfo(
        path=image,
        size=_image_size(image),
        label=_iso_label(image),
        hybrid=_is_hybrid(image),
    )

    try:
        info.entries = _list_7z(image)
        info.listing_ok = bool(info.entries)
    except Exception as exc:  # 7z missing, raw image, corrupt ISO...
        info.notes.append("Unable to list the image contents: %s" % exc)

    lower = info.entries

    windows_files = (
        "bootmgr",
        "sources/install.wim",
        "sources/install.esd",
        "sources/boot.wim",
    )
    if any(name in lower for name in windows_files):
        info.kind = "windows"
    elif any(name in lower for name in LINUX_KERNELS):
        info.kind = "linux"
    elif "efi/boot/bootx64.efi" in lower and info.hybrid:
        info.kind = "linux"
    else:
        info.kind = "other"

    for name in ("sources/install.wim", "sources/install.esd"):
        entry = lower.get(name)
        if entry is not None and not entry.is_dir:
            info.install_image = entry.path
            info.install_size = entry.size
            break

    info.efi_boot = any(
        key.endswith("efi/boot/bootx64.efi")
        or key.endswith("efi/boot/bootia32.efi")
        or key.endswith("efi/microsoft/boot/bootmgfw.efi")
        for key in lower
    )

    if info.kind == "linux":
        info.distro = _guess_distro(image, info)

    if info.kind == "windows" and info.install_size > 4 * 1024**3:
        info.notes.append(
            "The install image is larger than 4 GB. FAT32 needs it split into "
            "install.swm parts, or use NTFS."
        )

    return info


def unsupported_image_reason(path: str) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in (".gz", ".xz", ".bz2", ".zst", ".zip"):
        return (
            "Compressed images are not supported yet; decompress the file "
            "first (for example: unxz image.iso.xz)."
        )
    return ""
