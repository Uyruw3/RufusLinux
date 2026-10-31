#!/usr/bin/env bash
# Install RufusLinux for the current user (default) or system-wide.
#
# Runtime dependencies are installed with the system package manager.  Root
# privileges are used only for the package installation (sudo/pkexec); the
# application itself is copied into $PREFIX.
#
# With --brew the application is installed through Homebrew instead, while
# the root-only disk tools still come from the system package manager.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

INSTALL_DEPS=1
DEPS_ONLY=0
DRY_RUN=0
DEPS_FAILED=0
USE_BREW=0
GUI_PYTHON="python3"
BREW=""
BREW_FORMULA=""

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --brew        install the application with Homebrew (application formula)
  --no-deps     do not install system packages (application only)
  --deps-only   install the dependencies, then exit
  --dry-run     print the commands without running them
  -h, --help    show this help

With --brew, Homebrew provides GTK3, PyGObject and Python, while the
root-capable disk tools (parted, mkfs, GRUB, pkexec, 7z, wimlib) are still
installed with the system package manager because pkexec uses a secure PATH.
--deps-only --brew installs the formula dependencies and those system tools.

Environment:
  PREFIX=/path        install prefix (default: ~/.local, or /usr/local as root;
                      ignored with --brew)
  SKIP_APT_UPDATE=1   skip "apt-get update" and use the cached package lists
EOF
}

for arg in "$@"; do
    case "$arg" in
        --brew)      USE_BREW=1 ;;
        --no-deps)   INSTALL_DEPS=0 ;;
        --deps-only) DEPS_ONLY=1 ;;
        --dry-run)   DRY_RUN=1 ;;
        -h|--help)   usage; exit 0 ;;
        *)
            echo "error: unknown option: $arg" >&2
            echo "Run ./install.sh --help for usage." >&2
            exit 2
            ;;
    esac
done

# --deps-only always installs dependencies, even if --no-deps was also given.
if [ "$DEPS_ONLY" -eq 1 ]; then
    INSTALL_DEPS=1
fi

if [ "$(id -u)" -eq 0 ] && [ -z "${PREFIX:-}" ]; then
    PREFIX=/usr/local
else
    PREFIX="${PREFIX:-$HOME/.local}"
fi

DEST="$PREFIX/share/rufus-linux"
BIN="$PREFIX/bin/rufus-linux"
ICON="$PREFIX/share/icons/hicolor/scalable/apps/rufus-linux.svg"
DESKTOP="$PREFIX/share/applications/rufus-linux.desktop"

# ---------------------------------------------------------------------------
# Dependency installation
# ---------------------------------------------------------------------------

have() {
    command -v "$1" >/dev/null 2>&1 \
        || [ -x "/usr/sbin/$1" ] \
        || [ -x "/sbin/$1" ] \
        || [ -x "/usr/local/sbin/$1" ]
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    elif command -v pkexec >/dev/null 2>&1; then
        pkexec "$@"
    else
        echo "error: administrator rights are required to install packages." >&2
        echo "       Re-run as root, install sudo/pkexec, or use --no-deps." >&2
        return 1
    fi
}

run_cmd() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '+'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

package_family() {
    if have apt-get; then
        echo apt
    elif have dnf; then
        echo dnf
    elif have pacman; then
        echo pacman
    elif have zypper; then
        echo zypper
    elif have apk; then
        echo apk
    else
        echo unknown
    fi
}

apt_7z_package() {
    # p7zip-full was replaced by 7zip on newer Debian/Ubuntu releases.
    if apt-cache show p7zip-full >/dev/null 2>&1; then
        echo p7zip-full
    elif apt-cache show 7zip >/dev/null 2>&1; then
        echo 7zip
    else
        echo p7zip-full
    fi
}

debian_grub_efi() {
    case "$(dpkg --print-architecture 2>/dev/null || true)" in
        arm64) echo grub-efi-arm64-bin ;;
        armhf) echo grub-efi-arm-bin ;;
        i386)  echo grub-efi-ia32-bin ;;
        *)     echo grub-efi-amd64-bin ;;
    esac
}

fedora_grub_efi() {
    case "$(uname -m 2>/dev/null || true)" in
        aarch64) echo grub2-efi-aa64-modules ;;
        *)       echo grub2-efi-x64-modules ;;
    esac
}

# Packages needed by the application itself (GUI, image handling, writing).
core_packages() {
    case "$1" in
        apt)
            echo "python3 python3-gi gir1.2-gtk-3.0 policykit-1 pkexec \
                  $(apt_7z_package) parted dosfstools ntfs-3g exfatprogs wimtools"
            ;;
        dnf)
            echo "python3 python3-gobject gtk3 polkit p7zip p7zip-plugins \
                  parted dosfstools ntfs-3g exfatprogs wimlib-utils"
            ;;
        pacman)
            echo "python python-gobject gtk3 polkit p7zip parted dosfstools \
                  ntfs-3g exfatprogs wimlib"
            ;;
        zypper)
            echo "python3 python3-gobject gtk3 polkit p7zip parted dosfstools \
                  ntfs-3g exfatprogs wimlib"
            ;;
        apk)
            echo "python3 py3-gobject3 gtk+3.0 polkit p7zip parted dosfstools \
                  ntfs-3g exfatprogs wimlib"
            ;;
    esac
}

# Packages needed for the extract-mode bootloaders (BIOS/UEFI GRUB).
boot_packages() {
    case "$1" in
        apt)    echo "grub-pc-bin $(debian_grub_efi)" ;;
        dnf)    echo "grub2-tools grub2-pc-modules $(fedora_grub_efi)" ;;
        pacman) echo "grub" ;;
        zypper) echo "grub2 grub2-x86_64-efi" ;;
        apk)    echo "grub" ;;
    esac
}

# System tools needed even when the GUI itself comes from Homebrew.
brew_system_packages() {
    case "$1" in
        apt)
            echo "policykit-1 pkexec $(apt_7z_package) parted dosfstools \
                  ntfs-3g exfatprogs wimtools"
            ;;
        dnf)
            echo "polkit p7zip p7zip-plugins parted dosfstools ntfs-3g \
                  exfatprogs wimlib-utils"
            ;;
        pacman)
            echo "polkit p7zip parted dosfstools ntfs-3g exfatprogs wimlib"
            ;;
        zypper)
            echo "polkit p7zip parted dosfstools ntfs-3g exfatprogs wimlib"
            ;;
        apk)
            echo "polkit p7zip parted dosfstools ntfs-3g exfatprogs wimlib"
            ;;
    esac
}

install_group() {
    local family="$1"
    shift
    if [ "$#" -eq 0 ]; then
        return 0
    fi

    case "$family" in
        apt)
            run_cmd as_root env DEBIAN_FRONTEND=noninteractive \
                apt-get install -y --no-install-recommends "$@"
            ;;
        dnf)    run_cmd as_root dnf install -y "$@" ;;
        pacman) run_cmd as_root pacman -S --needed --noconfirm "$@" ;;
        zypper) run_cmd as_root zypper --non-interactive install --no-recommends "$@" ;;
        apk)    run_cmd as_root apk add --no-cache "$@" ;;
    esac
}

# Tools that the privileged writer needs from the system.  They must not come
# from a user-writable Homebrew prefix because pkexec resets PATH.
missing_write_tools() {
    local missing=0

    for cmd in parted partprobe mkfs.vfat mkfs.ntfs mkfs.exfat \
               7z wimlib-imagex grub-install pkexec; do
        if ! have "$cmd"; then
            echo "  missing: $cmd"
            missing=1
        fi
    done

    # grub-install ships in grub-common, but the BIOS/UEFI modules live in
    # separate packages on some distributions.
    if have grub-install; then
        case "$(uname -m 2>/dev/null || true)" in
            x86_64)
                for dir in i386-pc x86_64-efi; do
                    if [ ! -d "/usr/lib/grub/$dir" ]; then
                        echo "  missing: GRUB modules ($dir)"
                        missing=1
                    fi
                done
                ;;
            i?86)
                for dir in i386-pc i386-efi; do
                    if [ ! -d "/usr/lib/grub/$dir" ]; then
                        echo "  missing: GRUB modules ($dir)"
                        missing=1
                    fi
                done
                ;;
            aarch64)
                if [ ! -d /usr/lib/grub/arm64-efi ]; then
                    echo "  missing: GRUB modules (arm64-efi)"
                    missing=1
                fi
                ;;
        esac
    fi

    return "$missing"
}

missing_gui_tools() {
    if ! [ -x "$GUI_PYTHON" ] && ! have "$GUI_PYTHON"; then
        echo "  missing: $GUI_PYTHON"
        return 1
    fi

    if ! "$GUI_PYTHON" -c 'import gi; gi.require_version("Gtk", "3.0"); from gi.repository import Gtk' >/dev/null 2>&1; then
        echo "  missing: PyGObject/GTK3 (python3-gi + gir1.2-gtk-3.0)"
        return 1
    fi

    return 0
}

missing_tools() {
    local missing=0

    missing_write_tools || missing=1
    missing_gui_tools || missing=1

    return "$missing"
}

# ---------------------------------------------------------------------------
# Homebrew installation
# ---------------------------------------------------------------------------

find_brew() {
    if command -v brew >/dev/null 2>&1; then
        command -v brew
        return 0
    fi

    local candidate
    for candidate in /home/linuxbrew/.linuxbrew/bin/brew \
                     /opt/homebrew/bin/brew \
                     /usr/local/bin/brew; do
        if [ -x "$candidate" ]; then
            echo "$candidate"
            return 0
        fi
    done

    return 1
}

project_version() {
    local version
    version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' \
        "$SRC/rufus_linux/version.py" | head -n 1)"
    echo "${version:-1.0.0}"
}

prepare_brew_formula() {
    local repo tapdir version tarball sha stage

    repo="$("$BREW" --repository)"
    tapdir="$repo/Library/Taps/rufuslinux/homebrew-rufus-linux"
    version="$(project_version)"
    tarball="$tapdir/rufus-linux-$version.tar.gz"
    BREW_FORMULA="$tapdir/Formula/rufus-linux.rb"

    echo
    echo "Homebrew tap: rufuslinux/rufus-linux"
    echo "  formula: $BREW_FORMULA"
    echo "  source:  $tarball"

    if [ "$DRY_RUN" -eq 1 ]; then
        return 0
    fi

    mkdir -p "$tapdir/Formula"
    rm -f "$tapdir"/rufus-linux-*.tar.gz
    stage="$(mktemp -d)"
    mkdir -p "$stage/rufus-linux-$version"
    cp -a "$SRC/rufus-linux" "$SRC/engine_main.py" "$SRC/rufus_linux" \
          "$SRC/resources" "$SRC/README.md" "$SRC/LICENSE" "$SRC/NOTICE" \
          "$stage/rufus-linux-$version/"
    find "$stage" -type d -name __pycache__ -prune -exec rm -rf {} +
    tar -C "$stage" -czf "$tarball" "rufus-linux-$version"
    rm -rf "$stage"

    sha="$(sha256sum "$tarball" | awk '{print $1}')"
    sed -e "s|@URL@|file://$tarball|g" \
        -e "s|@SHA256@|$sha|g" \
        -e "s|@VERSION@|$version|g" \
        "$SRC/Formula/rufus-linux.rb" > "$BREW_FORMULA"

    # Homebrew 7 refuses to load formulae from third-party taps unless the tap
    # is trusted.  Older releases do not have the trust command.
    if "$BREW" trust --help >/dev/null 2>&1; then
        if ! run_cmd "$BREW" trust rufuslinux/rufus-linux; then
            echo "error: could not trust the local rufuslinux/rufus-linux tap." >&2
            return 1
        fi
    fi
}

install_brew_system_deps() {
    local family core boot
    family="$(package_family)"

    if missing_write_tools; then
        echo "All system disk tools are already installed."
        return 0
    fi

    if [ "$family" = unknown ]; then
        echo "error: unsupported distribution: no apt, dnf, pacman, zypper or apk found." >&2
        echo "Install these system tools manually: pkexec, 7z, parted," >&2
        echo "mkfs.vfat/mkfs.ntfs/mkfs.exfat, wimlib-imagex and grub-install." >&2
        return 1
    fi

    echo
    echo "Installing system disk tools with $family..."

    # Refresh package lists first: apt-cache may not know yet whether the 7z
    # package is called p7zip-full or 7zip.
    if [ "$family" = apt ] && [ "${SKIP_APT_UPDATE:-0}" != "1" ]; then
        if ! run_cmd as_root env DEBIAN_FRONTEND=noninteractive apt-get update; then
            echo "warning: apt-get update failed; trying the cached package lists." >&2
        fi
    fi

    core="$(brew_system_packages "$family")"
    boot="$(boot_packages "$family")"

    # shellcheck disable=SC2086
    if ! install_group "$family" $core; then
        echo "error: failed to install the system disk tools." >&2
        DEPS_FAILED=1
        return 1
    fi

    # shellcheck disable=SC2086
    if [ -n "$boot" ] && ! install_group "$family" $boot; then
        echo "warning: failed to install the GRUB packages; extract mode may not" >&2
        echo "         be able to install a BIOS/UEFI bootloader." >&2
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        echo
        echo "Dry run: no packages were installed."
        return 0
    fi

    echo
    echo "Remaining missing system tools (if any):"
    if missing_write_tools; then
        echo "  none."
    else
        DEPS_FAILED=1
        echo "Some system tools are still missing; see README.md." >&2
        return 1
    fi
}

install_brew() {
    local bin

    BREW="$(find_brew || true)"
    if [ -z "$BREW" ]; then
        echo "error: Homebrew is not installed or 'brew' is not in PATH." >&2
        echo "Install it from https://brew.sh, or run ./install.sh without --brew." >&2
        return 1
    fi

    echo "Using Homebrew: $BREW"
    prepare_brew_formula || return 1

    if [ "$INSTALL_DEPS" -eq 1 ]; then
        if [ "$DEPS_ONLY" -eq 1 ]; then
            echo
            echo "Installing the Homebrew formula dependencies..."
            run_cmd "$BREW" install --only-dependencies --formula "$BREW_FORMULA" || {
                echo "error: failed to install the Homebrew dependencies." >&2
                return 1
            }
        fi

        if ! install_brew_system_deps; then
            if [ "$DEPS_ONLY" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; then
                return 1
            fi
            echo
            echo "Continuing with the Homebrew installation; install the missing" >&2
            echo "system tools later or re-run ./install.sh --brew." >&2
        fi
    fi

    if [ "$DEPS_ONLY" -eq 1 ]; then
        return "$DEPS_FAILED"
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        echo
        run_cmd "$BREW" install --formula "$BREW_FORMULA"
        echo
        echo "Dry run: no Homebrew packages were installed."
        return "$DEPS_FAILED"
    fi

    echo
    echo "Installing RufusLinux with Homebrew..."
    if "$BREW" list --formula rufus-linux >/dev/null 2>&1; then
        run_cmd "$BREW" reinstall --formula "$BREW_FORMULA"
    else
        run_cmd "$BREW" install --formula "$BREW_FORMULA"
    fi

    bin="$("$BREW" --prefix)/bin/rufus-linux"
    if [ ! -x "$bin" ]; then
        echo "error: Homebrew did not create $bin." >&2
        return 1
    fi

    echo
    echo "Installed: $bin"
    if ! "$bin" --version; then
        echo "error: the installed launcher could not be run." >&2
        return 1
    fi

    if missing_write_tools; then
        echo
        echo "All system disk tools are present."
    else
        echo
        echo "Some system disk tools are missing; run ./install.sh --brew again" >&2
        echo "or install them manually (see README.md)." >&2
        return 1
    fi
}

install_dependencies() {
    local family core boot
    family="$(package_family)"

    if missing_tools; then
        echo "All runtime dependencies are already installed."
        return 0
    fi

    if [ "$family" = unknown ]; then
        echo "error: unsupported distribution: no apt, dnf, pacman, zypper or apk found." >&2
        echo "Install these tools manually: python3-gi, GTK3, pkexec, 7z, parted," >&2
        echo "mkfs.vfat/mkfs.ntfs/mkfs.exfat, wimlib-imagex and grub-install." >&2
        return 1
    fi

    echo
    echo "Installing dependencies with $family..."

    # Refresh package lists first: on a fresh install apt-cache may not know
    # yet whether a package is named p7zip-full or 7zip.
    if [ "$family" = apt ] && [ "${SKIP_APT_UPDATE:-0}" != "1" ]; then
        if ! run_cmd as_root env DEBIAN_FRONTEND=noninteractive apt-get update; then
            echo "warning: apt-get update failed; trying the cached package lists." >&2
        fi
    fi

    core="$(core_packages "$family")"
    boot="$(boot_packages "$family")"

    # Core packages first: if the bootloader packages fail (for example on an
    # architecture without a matching GRUB build), the app still works.
    # shellcheck disable=SC2086
    if ! install_group "$family" $core; then
        echo "error: failed to install the core dependencies." >&2
        DEPS_FAILED=1
        return 1
    fi

    # shellcheck disable=SC2086
    if [ -n "$boot" ] && ! install_group "$family" $boot; then
        echo "warning: failed to install the GRUB packages; extract mode may not" >&2
        echo "         be able to install a BIOS/UEFI bootloader." >&2
    fi

    if [ "$DRY_RUN" -eq 1 ]; then
        echo
        echo "Dry run: no packages were installed."
        return 0
    fi

    echo
    echo "Remaining missing tools (if any):"
    if missing_tools; then
        echo "  none."
    else
        DEPS_FAILED=1
        echo "Some tools are still missing; see README.md for package names." >&2
        return 1
    fi
}

if [ "$USE_BREW" -eq 1 ]; then
    if ! install_brew; then
        exit 1
    fi
    exit "$DEPS_FAILED"
fi

if [ "$INSTALL_DEPS" -eq 1 ]; then
    if ! install_dependencies; then
        if [ "$DEPS_ONLY" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; then
            exit 1
        fi
        echo
        echo "Continuing with the application installation; install the missing" >&2
        echo "packages later or re-run ./install.sh." >&2
    fi
fi

if [ "$DEPS_ONLY" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; then
    exit "$DEPS_FAILED"
fi

# ---------------------------------------------------------------------------
# Application installation
# ---------------------------------------------------------------------------

echo "Installing RufusLinux to $DEST"

mkdir -p "$DEST" "$PREFIX/bin" \
    "$PREFIX/share/applications" \
    "$PREFIX/share/icons/hicolor/scalable/apps"

cp -a "$SRC/rufus-linux" "$SRC/engine_main.py" "$SRC/rufus_linux" \
      "$SRC/resources" "$SRC/README.md" "$SRC/LICENSE" "$SRC/NOTICE" "$DEST/"
find "$DEST" -type d -name __pycache__ -prune -exec rm -rf {} +

chmod +x "$DEST/rufus-linux" "$DEST/engine_main.py"
ln -sfn "$DEST/rufus-linux" "$BIN"

install -m 0644 "$SRC/resources/rufus-linux.svg" "$ICON"
sed "s|@EXEC@|$DEST/rufus-linux|g; s|@ICON@|$ICON|g" \
    "$SRC/rufus-linux.desktop.in" > "$DESKTOP"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$PREFIX/share/applications" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -f -t "$PREFIX/share/icons/hicolor" >/dev/null 2>&1 || true
fi

echo
echo "Installed: $BIN"
echo "Desktop entry: $DESKTOP"

if ! printf '%s' ":$PATH:" | grep -q ":$PREFIX/bin:"; then
    echo
    echo "Note: $PREFIX/bin is not in your PATH. Add it or run $BIN directly."
fi

echo
echo "Checking runtime tools:"
if missing_tools; then
    echo "  all present."
else
    echo "  Install the missing packages above, or re-run ./install.sh."
    exit 1
fi
