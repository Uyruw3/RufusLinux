# Homebrew formula template for RufusLinux.
#
# This file is not meant to be installed directly: `./install.sh --brew`
# generates a source tarball, fills in the URL, checksum and version fields
# below, copies the result into a local tap, and installs it with Homebrew.
class RufusLinux < Formula
  desc "Rufus-style bootable USB writer with a GTK3 interface"
  homepage "https://github.com/rufus-linux/rufus-linux"
  url "@URL@"
  sha256 "@SHA256@"
  version "@VERSION@"
  license "GPL-3.0-or-later"

  depends_on :linux

  depends_on "gtk+3"
  depends_on "pygobject3"
  depends_on "python@3.15"
  depends_on "p7zip"
  depends_on "wimlib"

  def install
    libexec.install "rufus-linux", "engine_main.py", "rufus_linux",
                    "resources", "README.md", "LICENSE", "NOTICE"

    chmod 0755, libexec/"rufus-linux"
    chmod 0755, libexec/"engine_main.py"

    python = Formula["python@3.15"].opt_bin/"python3.15"
    pygobject = Formula["pygobject3"].opt_lib/"python3.15/site-packages"
    pycairo = Formula["py3cairo"].opt_lib/"python3.15/site-packages"

    # Homebrew installs its own Python and GTK stack, so the launcher must
    # point at those paths explicitly.  The root engine still uses the system
    # tools (parted, mkfs, grub, pkexec) through pkexec's secure PATH.
    (bin/"rufus-linux").write <<~EOS
      #!/bin/bash
      export PATH="#{HOMEBREW_PREFIX}/bin:#{HOMEBREW_PREFIX}/sbin${PATH:+:$PATH}"
      export PYTHONPATH="#{pygobject}:#{pycairo}${PYTHONPATH:+:$PYTHONPATH}"
      export GI_TYPELIB_PATH="#{HOMEBREW_PREFIX}/lib/girepository-1.0${GI_TYPELIB_PATH:+:$GI_TYPELIB_PATH}"
      export XDG_DATA_DIRS="#{HOMEBREW_PREFIX}/share:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
      export GSETTINGS_SCHEMA_DIR="#{HOMEBREW_PREFIX}/share/glib-2.0/schemas${GSETTINGS_SCHEMA_DIR:+:$GSETTINGS_SCHEMA_DIR}"
      export RUFUS_LINUX_RESOURCES="#{libexec}/resources"
      exec "#{python}" "#{libexec}/rufus-linux" "$@"
    EOS
    chmod 0755, bin/"rufus-linux"

    icon_dir = share/"icons/hicolor/scalable/apps"
    icon_dir.mkpath
    icon_dir.install libexec/"resources/rufus-linux.svg"

    desktop = share/"applications/rufus-linux.desktop"
    desktop.dirname.mkpath
    desktop.write <<~EOS
      [Desktop Entry]
      Type=Application
      Version=1.0
      Name=RufusLinux
      GenericName=Bootable USB Creator
      Comment=Create bootable USB drives from ISO images
      Exec=#{opt_bin}/rufus-linux
      Icon=#{opt_prefix}/share/icons/hicolor/scalable/apps/rufus-linux.svg
      Terminal=false
      Categories=System;Utility;
      Keywords=usb;boot;iso;rufus;windows;linux;image;
      StartupNotify=true
    EOS
  end

  def caveats
    <<~EOS
      RufusLinux writes disks through pkexec and needs root-capable system
      tools that Homebrew does not provide: parted, dosfstools, ntfs-3g,
      exfatprogs, 7z, wimlib-imagex, grub-install and pkexec/polkit.

      Install them with your distribution's package manager, or run
      `./install.sh --brew` from the RufusLinux source checkout.
    EOS
  end

  test do
    assert_match "RufusLinux", shell_output("#{bin}/rufus-linux --version")

    python = Formula["python@3.15"].opt_bin/"python3.15"
    ENV["PYTHONPATH"] = "#{Formula["pygobject3"].opt_lib}/python3.15/site-packages"
    ENV["GI_TYPELIB_PATH"] = "#{HOMEBREW_PREFIX}/lib/girepository-1.0"
    system python, "-c", <<~PYTHON
      import gi
      gi.require_version("Gtk", "3.0")
      from gi.repository import Gtk
      assert Gtk.get_major_version() == 3
    PYTHON
  end
end
