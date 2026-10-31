"""Command-line interface.

Run without arguments for the GUI, or use it head-less:

    rufus-linux --list
    rufus-linux --check image.iso
    sudo rufus-linux --image image.iso --device /dev/sdb --yes

The writer automatically re-executes itself through pkexec when it is not
started as root.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import devices, images, util
from .version import __version__

# Keep the engine path independent from the package to survive pkexec's
# environment sanitising.
ENGINE = Path(__file__).resolve().parent.parent / "engine_main.py"


def print_devices(show_all: bool) -> int:
    try:
        listing = devices.list_devices(show_all=show_all)
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    if not listing:
        print("No removable devices found. Try --show-all.")
        return 0
    for device in listing:
        tags = []
        if device.removable:
            tags.append("removable")
        if device.tran:
            tags.append(device.tran)
        if device.is_system:
            tags.append("SYSTEM DISK")
        if device.read_only:
            tags.append("read-only")
        print("%s  %s  %s%s" % (
            device.path,
            device.model or "unknown",
            util.human_size(device.size),
            ("  [" + ", ".join(tags) + "]") if tags else "",
        ))
        for part in device.partitions:
            line = "    %s  %s  %s" % (
                part.path,
                util.human_size(part.size),
                part.fstype or "unknown",
            )
            if part.label:
                line += "  [%s]" % part.label
            if part.mountpoints:
                line += "  mounted at " + ", ".join(part.mountpoints)
            print(line)
    return 0


def check_image(path: str) -> int:
    try:
        info = images.analyze(path)
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print("Image:        %s" % path)
    print("Size:         %s" % util.human_size(info.size))
    print("Label:        %s" % (info.label or "-"))
    print("Type:         %s" % info.kind)
    print("Hybrid:       %s" % ("yes" if info.hybrid else "no"))
    print("UEFI boot:    %s" % ("yes" if info.efi_boot else "no"))
    if info.install_image:
        print("Install file: %s (%s)" % (info.install_image, util.human_size(info.install_size)))
    if info.distro:
        print("Distro:       %s" % info.distro)
    print("Extract mode: %s" % ("supported" if info.extract_supported else "not possible"))
    print("Recommended:  %s / %s" % (info.recommended_mode, info.recommended_filesystem))
    warnings = info.warnings()
    if warnings:
        print("Warnings:")
        for warning in warnings:
            print("  - %s" % warning)
    return 0


def _build_job(args, info, filesystem: str, label: str) -> dict:
    return {
        "image": os.path.abspath(args.image),
        "device": args.device,
        "mode": args.mode,
        "scheme": args.scheme,
        "target": args.target,
        "filesystem": filesystem,
        "label": label,
        "verify": args.verify,
        "split_wim": args.split_wim,
        "confirm": args.device,
        "dry_run": args.dry_run,
        "debug": args.debug,
    }


def write_image(args) -> int:
    if not args.image or not args.device:
        print("error: --image and --device are both required", file=sys.stderr)
        return 2
    if not os.path.exists(args.image):
        print("error: image not found: %s" % args.image, file=sys.stderr)
        return 2

    try:
        info = images.analyze(args.image)
    except Exception as exc:
        print("error: unable to analyse image: %s" % exc, file=sys.stderr)
        return 1

    mode = info.recommended_mode if args.mode == "auto" else args.mode
    if mode == "extract" and not info.extract_supported:
        print("error: this image cannot be extracted; use --mode dd", file=sys.stderr)
        return 1

    filesystem = args.filesystem
    if filesystem == "auto":
        filesystem = info.recommended_filesystem
    label = util.sanitize_label(args.label or util.default_label(info.kind, info.distro), filesystem)

    protected, reason = devices.is_protected(args.device)
    if protected:
        print("error: refusing to write to %s: %s" % (args.device, reason), file=sys.stderr)
        return 1

    print("Image:       %s (%s)" % (args.image, util.human_size(info.size)))
    print("Device:      %s (%s)" % (args.device, _device_text(args.device)))
    print("Mode:        %s" % mode)
    if mode == "extract":
        print("Filesystem:  %s, table %s, boot %s, label %s"
              % (filesystem, args.scheme.upper(), args.target, label))
    for warning in info.warnings():
        print("Warning:     %s" % warning)

    if not args.dry_run and not args.yes:
        if not sys.stdin.isatty():
            print("error: refusing to erase without --yes", file=sys.stderr)
            return 2
        print("\nTHIS WILL ERASE EVERYTHING ON %s." % args.device)
        if input("Type ERASE to continue: ").strip() != "ERASE":
            print("Aborted.")
            return 1

    job = _build_job(args, info, filesystem, label)
    handle, job_path = tempfile.mkstemp(prefix="rufus-job-", suffix=".json")
    with os.fdopen(handle, "w", encoding="utf-8") as out:
        json.dump(job, out, indent=2)
    os.chmod(job_path, 0o600)

    try:
        cmd = [sys.executable, "-u", str(ENGINE), "--job", job_path, "--human"]
        if not args.dry_run and not util.is_root():
            if not shutil.which("pkexec"):
                print("error: run as root (sudo) or install pkexec", file=sys.stderr)
                return 2
            cmd = ["pkexec", *cmd]
        return subprocess.call(cmd)
    finally:
        try:
            os.unlink(job_path)
        except OSError:
            pass


def _device_text(path: str) -> str:
    device = devices.find_device(path, show_all=True)
    if device is None:
        return "unknown"
    return "%s, %s" % (device.model or "unknown model", util.human_size(device.size))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rufus-linux",
        description="Create bootable USB drives from ISO/IMG images (Rufus-style).",
    )
    parser.add_argument("--gui", action="store_true", help="start the graphical interface (default)")
    parser.add_argument("--list", action="store_true", help="list candidate devices and exit")
    parser.add_argument("--show-all", action="store_true", help="include internal disks when listing")
    parser.add_argument("--check", metavar="IMAGE", help="inspect an image and exit")
    parser.add_argument("--image", metavar="IMAGE", help="image to write")
    parser.add_argument("--device", metavar="DEVICE", help="target block device, e.g. /dev/sdb")
    parser.add_argument("--mode", choices=("auto", "dd", "extract"), default="auto",
                        help="write mode (default: auto)")
    parser.add_argument("--scheme", choices=("mbr", "gpt"), default="mbr",
                        help="partition table for extract mode (default: mbr)")
    parser.add_argument("--target", choices=("both", "uefi", "bios"), default="both",
                        help="boot firmware for extract mode (default: both)")
    parser.add_argument("--fs", "--filesystem", dest="filesystem",
                        choices=("auto", "FAT32", "NTFS", "exFAT"), default="auto",
                        help="filesystem for extract mode (default: auto)")
    parser.add_argument("--label", help="volume label for extract mode")
    parser.add_argument("--verify", action="store_true", help="verify the written data")
    parser.add_argument("--no-split-wim", dest="split_wim", action="store_false",
                        help="do not split install.wim for FAT32")
    parser.set_defaults(split_wim=True)
    parser.add_argument("--dry-run", action="store_true", help="validate and describe, write nothing")
    parser.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    parser.add_argument("--debug", action="store_true", help="show a traceback on failure")
    parser.add_argument("--version", action="version", version="RufusLinux %s" % __version__)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list:
        return print_devices(args.show_all)
    if args.check:
        return check_image(args.check)
    if args.image or args.device:
        return write_image(args)
    if args.gui or not argv:
        from .app import main as gui_main

        return gui_main()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
