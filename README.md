# RufusLinux

A Rufus-style bootable USB writer for Linux, built with GTK+ 3 / PyGObject.

RufusLinux gives you the familiar Rufus workflow — pick a device, pick an
image, choose the partition scheme/filesystem, press **START** — while using
the battle-tested Linux toolchain (`parted`, `mkfs.*`, `GRUB`, `wimlib`,
`dd`) underneath. The Windows-media logic is adapted from
[WoeUSB/WoeUSB-ng](https://github.com/WoeUSB/WoeUSB-ng).

![mode](https://img.shields.io/badge/license-GPL--3.0-blue)

## Features

- **Device detection** — lists removable/USB disks by default; “Show all
  drives” reveals internal disks, but the disk containing the running system
  is always protected and cannot be written.
- **Two write modes**
  - **DD image mode** — copies an ISO/IMG byte-for-byte. Best for hybrid Linux
    images because it preserves the image’s own boot setup, persistence and
    Secure Boot shims.
  - **Extract mode** — rebuilds the partition table and filesystem, extracts
    the image, then installs a bootloader. Needed for Windows ISOs and useful
    for non-hybrid Linux images.
- **Windows media**
  - FAT32, NTFS and exFAT targets. exFAT is UEFI-only and depends on the
    firmware being able to read exFAT; FAT32/NTFS are more compatible.
  - Automatic `install.wim` splitting (`install.swm`) when the WIM is larger
    than FAT32’s 4 GB file limit (requires `wimtools`).
  - **UEFI:NTFS** support partition for booting NTFS media on UEFI systems
    (bundled `uefi-ntfs.img` from the Rufus project).
  - Legacy BIOS boot through GRUB (`ntldr /bootmgr`), including the GPT
    BIOS-boot partition.
- **Linux media** — DD mode, plus an experimental FAT32 extract mode that
  installs GRUB for BIOS and UEFI and writes entries for
  Ubuntu/Debian/Fedora/Arch layouts.
- **Verification** — optional SHA-256 read-back verification for DD mode and
  file-size verification for extract mode.
- **Image inspector** — detects Windows vs. Linux media, hybrid vs.
  non-hybrid, EFI boot files and `install.wim` size; computes SHA-256 on
  demand.
- **Safety rails** — refuses system disks, read-only devices, device-mapper
  holders and busy mounts, requires an explicit confirmation, and
  unmounts/flushes in a `finally` block even when cancelled.
- **CLI** for headless use and dry runs.

## Requirements

Tested on Linux Mint 22 / Ubuntu 24.04 with Python 3.12 and GTK 3.

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 policykit-1 pkexec \
    p7zip-full parted dosfstools ntfs-3g exfatprogs \
    wimtools grub-pc-bin grub-efi-amd64-bin
```

`./install.sh` installs these automatically for your distribution; the
command above is for manual installs. With `./install.sh --brew`, Homebrew
provides Python, GTK+ 3, PyGObject, `7z` and `wimlib`, while the system
package manager still installs the root-only disk tools.

| Tool | Used for |
| --- | --- |
| `python3-gi`, `gir1.2-gtk-3.0` | GUI |
| `pkexec` (policykit-1) | privilege escalation |
| `parted`, `partprobe` | partitioning |
| `mkfs.vfat`, `mkfs.ntfs`, `mkfs.exfat` | formatting |
| `7z` (p7zip-full or 7zip) | image inspection / UDF fallback |
| `wimlib-imagex` (wimtools) | splitting large `install.wim` |
| `grub-install` | BIOS/UEFI fallback bootloaders |
| `isoinfo`, `xorriso` | optional ISO tooling |

`wimtools` and `grub-pc-bin`/`grub-efi-amd64-bin` are only needed for the
features that use them; the app tells you what is missing.

## Run from the checkout

```bash
./rufus-linux              # GUI
./rufus-linux --list       # list candidate devices
./rufus-linux --check image.iso
```

No installation is required. The GUI asks for administrator authentication
through `pkexec` only at the moment you press START.

## Install

```bash
./install.sh                       # per-user: ~/.local
sudo PREFIX=/usr/local ./install.sh   # system-wide
```

The installer copies the app to `$PREFIX/share/rufus-linux`, creates a
`rufus-linux` launcher in `$PREFIX/bin`, installs the icon and desktop entry,
and installs the runtime dependencies with `apt`, `dnf`, `pacman`, `zypper`
or `apk`. Installing packages needs administrator rights; the script uses
`sudo` (or `pkexec`) and asks for your password only for that step. The
application itself stays in `$PREFIX`, so a per-user install still works.

Useful options:

```bash
./install.sh --deps-only   # install packages, do not install the app
./install.sh --no-deps     # install the app only
./install.sh --dry-run     # show the package commands without running them
SKIP_APT_UPDATE=1 ./install.sh   # reuse the existing apt package lists
```

If a package cannot be installed, the app is still installed and the script
lists what is missing.

### Install with Homebrew

If Homebrew is already installed, the application can be installed as a
Homebrew formula instead:

```bash
./install.sh --brew              # formula + system disk tools
./install.sh --brew --deps-only  # dependencies only
./install.sh --brew --dry-run    # show what would be installed
```

The installer creates a local tap at `rufuslinux/rufus-linux`, builds a
source tarball, and installs the formula from it. Homebrew provides Python,
GTK+ 3, PyGObject, `7z` and `wimlib`; the privileged writer still uses the
system tools (`parted`, `mkfs.*`, `GRUB`, `pkexec`) because `pkexec` runs
with a secure `PATH` that excludes user-writable Homebrew prefixes. The
script installs those system tools with the distribution package manager.

The checked-in `Formula/rufus-linux.rb` is a template that `--brew` fills in
with the local tarball URL and SHA-256; install through `./install.sh --brew`
rather than calling `brew install` on the template directly. To remove a
Homebrew installation:

```bash
brew uninstall rufus-linux
brew untap rufuslinux/rufus-linux
brew untrust --tap rufuslinux/rufus-linux   # Homebrew 7+
```

With `--brew`, `--no-deps` skips the system disk tools (Homebrew still
installs the formula's own dependencies).

## CLI usage

```bash
# Inspect an image
./rufus-linux --check ~/Descargas/Win11.iso

# Dry run: validate everything without touching the device
./rufus-linux --image ~/Descargas/Win11.iso --device /dev/sdb --dry-run

# Write a Windows ISO in extract mode (auto mode picks extract)
sudo ./rufus-linux --image ~/Descargas/Win11.iso --device /dev/sdb --yes

# Write a hybrid Linux ISO in DD mode
sudo ./rufus-linux --image ubuntu.iso --device /dev/sdb --mode dd --yes

# Extract mode with explicit options
sudo ./rufus-linux --image win.iso --device /dev/sdb \
    --mode extract --scheme gpt --target uefi --fs FAT32 --label WIN11 --yes
```

Without `--yes` the CLI asks you to type `ERASE`. Without root it re-executes
the engine through `pkexec`.

## How it works

**DD mode** opens the block device and streams the image in 4 MiB chunks,
then optionally re-reads the device and compares SHA-256 checksums.

**Extract mode** follows this sequence:

1. Unmount all partitions of the target and disable swap on it.
2. `wipefs` the device and create an MBR or GPT table. GPT + BIOS adds a
   1 MiB BIOS-boot partition; NTFS + UEFI reserves 4 MiB at the end for the
   UEFI:NTFS partition (GPT ends it before the backup header).
3. Format the data partition (FAT32/NTFS/exFAT).
4. Mount the image read-only (or unpack it with 7z if mounting is not
   possible) and mount the new filesystem.
5. Check free space and FAT32’s 4 GB file limit.
6. Copy every file, reporting progress, sanitizing names that FAT cannot
   store.
7. Windows: split `install.wim` into `install.swm` parts if needed, create
   the UEFI:NTFS partition, install GRUB for legacy BIOS.
   Linux: install GRUB for BIOS and/or UEFI and write a kernel/initrd
   `grub.cfg`.
8. `sync`, flush, unmount, and optionally verify.

The privileged engine (`engine_main.py`) is a separate process. It emits
one JSON object per line; the GUI renders progress and the log from that
stream. The engine re-validates every field of its job file and refuses to
write to the system disk even if the GUI is bypassed.

## Safety notes

- Writing a USB drive **erases it completely**. Double-check the device path.
- The app never writes to the disk holding `/`, `/boot`, `/boot/efi`,
  `/usr`, `/var` or `/home`.
- Unmount the target’s partitions before writing if they are busy; the app
  tries automatically and aborts if it cannot.
- Do not remove the drive while writing.
- Windows 11’s TPM/Secure Boot/RAM/account bypasses are **not** implemented;
  use Rufus or an `autounattend.xml` if you need them.
- Linux extract mode is experimental. For normal Linux installation media,
  DD mode is the correct choice.

## Limitations

- No persistence-partition creation, Windows To Go, bad-block scan, or
  compressed-image (`.gz`/`.xz`/`.zst`) writing yet.
- Linux extract mode cannot support every distro’s custom initrd arguments.
- UEFI boot from NTFS relies on the bundled UEFI:NTFS bootloader; FAT32 is
  more compatible. exFAT boot depends on firmware support and is limited to
  UEFI.
- The source ISO is mounted with UDF first and checked for the expected
  files, because Microsoft ISOs contain only a stub ISO 9660 tree. If no
  mount exposes the real contents, the image is unpacked with 7z.
- The project has been validated with image/device detection, dry runs and
  GUI construction on Linux Mint 22; the destructive path should be tested
  on a spare USB stick before relying on it.

## Credits

- [Rufus](https://github.com/pbatard/rufus) by Pete Batard — the inspiration
  and the `uefi-ntfs.img` bootloader (GPL-3.0).
- [WoeUSB-ng](https://github.com/WoeUSB/WoeUSB-ng) — Windows USB creation
  sequence, UEFI:NTFS handling and GRUB chainloading approach (GPL-3.0).
- GTK+ 3 and PyGObject.

## License

GNU General Public License v3.0 or later. See `LICENSE`.
