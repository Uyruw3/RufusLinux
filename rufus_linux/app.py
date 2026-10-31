"""GTK3 user interface for RufusLinux."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

from . import devices, images, util  # noqa: E402
from .resources import resource_path  # noqa: E402
from .runner import EngineRunner, privilege_backend  # noqa: E402
from .version import APP_NAME, __version__  # noqa: E402

MODE_LABELS = (("auto", "Automatic"), ("dd", "DD image mode"), ("extract", "Extract files"))
SCHEME_LABELS = (("mbr", "MBR / DOS"), ("gpt", "GPT"))
TARGET_LABELS = (("both", "BIOS or UEFI"), ("uefi", "UEFI only"), ("bios", "BIOS only"))
FS_LABELS = (("FAT32", "FAT32"), ("NTFS", "NTFS"), ("exFAT", "exFAT"))


def _esc(text: str) -> str:
    return GLib.markup_escape_text(text or "")


class MainWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=APP_NAME)
        self.set_default_size(850, 780)
        self.set_size_request(760, 640)
        self.set_position(Gtk.WindowPosition.CENTER)
        icon = resource_path("rufus-linux.svg")
        if os.path.exists(icon):
            try:
                self.set_icon_from_file(icon)
            except GLib.Error:
                pass

        self.device_map: dict[str, devices.Device] = {}
        self.image_path = ""
        self.image_info: images.ImageInfo | None = None
        self.runner: EngineRunner | None = None
        self.busy = False
        self._analysis_seq = 0
        self._device_signature = None
        self._last_exit: tuple[bool, str] | None = None
        self._building = True
        self._destroyed = False
        self._poll_id = 0
        self._quit_after_cancel = False

        self._load_css()
        self._build_ui()
        self._building = False
        self.connect("destroy", self._on_destroy)
        self.refresh_devices()
        self._update_hint()
        self._poll_id = GLib.timeout_add_seconds(3, self._poll_devices)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    def _load_css(self) -> None:
        path = resource_path("style.css")
        if not os.path.exists(path):
            return
        provider = Gtk.CssProvider()
        try:
            provider.load_from_path(path)
        except GLib.Error:
            return
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _build_ui(self) -> None:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(outer)

        outer.pack_start(self._build_hero(), False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        outer.pack_start(scrolled, True, True, 0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(14)
        content.set_margin_bottom(10)
        content.set_margin_start(16)
        content.set_margin_end(16)
        scrolled.add(content)

        content.pack_start(self._build_device_section(), False, False, 0)
        content.pack_start(self._build_boot_section(), False, False, 0)
        content.pack_start(self._build_options_section(), False, False, 0)
        content.pack_start(self._build_progress_section(), True, True, 0)

        footer = self._build_footer()
        outer.pack_start(footer, False, False, 0)

        self.connect("delete-event", self._on_delete)

    def _build_hero(self) -> Gtk.Widget:
        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        hero.get_style_context().add_class("hero")
        hero.set_margin_start(18)
        hero.set_margin_end(18)
        hero.set_margin_top(12)
        hero.set_margin_bottom(12)

        icon_path = resource_path("rufus-linux.svg")
        if os.path.exists(icon_path):
            try:
                image = Gtk.Image.new_from_file(icon_path)
                image.set_pixel_size(52)
                hero.pack_start(image, False, False, 0)
            except GLib.Error:
                pass

        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        title = Gtk.Label()
        title.set_markup("<span size='xx-large' weight='bold'>RufusLinux</span>")
        title.get_style_context().add_class("hero-title")
        title.set_xalign(0)
        subtitle = Gtk.Label()
        subtitle.set_markup(
            "<span size='medium'>Create bootable USB drives — version %s</span>" % _esc(__version__)
        )
        subtitle.get_style_context().add_class("hero-subtitle")
        subtitle.set_xalign(0)
        text.pack_start(title, False, False, 0)
        text.pack_start(subtitle, False, False, 0)
        hero.pack_start(text, True, True, 0)

        backend = privilege_backend()
        self.privilege_label = Gtk.Label()
        self.privilege_label.set_xalign(1)
        self.privilege_label.get_style_context().add_class("hero-badge")
        if backend == "root":
            self.privilege_label.set_text("running as root")
        elif backend == "pkexec":
            self.privilege_label.set_text("admin rights via pkexec")
        else:
            self.privilege_label.set_markup("<b>pkexec missing</b>")
        hero.pack_end(self.privilege_label, False, False, 0)

        about_button = Gtk.Button(label="About")
        about_button.set_valign(Gtk.Align.CENTER)
        about_button.connect("clicked", self._on_about)
        hero.pack_end(about_button, False, False, 0)
        return hero

    def _on_about(self, *_args) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name(APP_NAME)
        dialog.set_version(__version__)
        dialog.set_comments(
            "A Rufus-style bootable USB writer for Linux.\n\n"
            "DD mode copies any hybrid image verbatim. Extract mode rebuilds the "
            "partition table/filesystem and installs GRUB or the UEFI:NTFS "
            "bootloader for Windows media."
        )
        dialog.set_license_type(Gtk.License.GPL_3_0)
        dialog.set_website("https://github.com/pbatard/rufus")
        dialog.set_website_label("Inspired by Rufus")
        dialog.set_authors(["RufusLinux contributors"])
        dialog.set_credits(
            "Boot logic adapted from WoeUSB / WoeUSB-ng (GPL-3.0).\n"
            "UEFI:NTFS bootloader by Pete Batard / the Rufus project (GPL-3.0).\n"
            "Built with GTK+ 3 and PyGObject."
        )
        icon = resource_path("rufus-linux.svg")
        if os.path.exists(icon):
            try:
                dialog.set_logo(GdkPixbuf.Pixbuf.new_from_file_at_size(icon, 96, 96))
            except Exception:
                pass
        dialog.run()
        dialog.destroy()

    def _section(self, title: str) -> tuple[Gtk.Frame, Gtk.Grid]:
        frame = Gtk.Frame()
        frame.get_style_context().add_class("section")
        label = Gtk.Label()
        label.set_markup("<b>%s</b>" % _esc(title))
        label.set_margin_start(4)
        label.set_margin_end(4)
        frame.set_label_widget(label)
        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(8)
        grid.set_margin_top(10)
        grid.set_margin_bottom(12)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        frame.add(grid)
        return frame, grid

    @staticmethod
    def _label(text: str, *, dim: bool = False) -> Gtk.Label:
        label = Gtk.Label(label=text)
        label.set_xalign(0)
        if dim:
            label.get_style_context().add_class("dim")
        return label

    def _build_device_section(self) -> Gtk.Widget:
        frame, grid = self._section("Device")

        grid.attach(self._label("Target device"), 0, 0, 1, 1)
        self.device_combo = Gtk.ComboBoxText()
        self.device_combo.set_hexpand(True)
        self.device_combo.connect("changed", self._on_device_changed)
        grid.attach(self.device_combo, 1, 0, 1, 1)

        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        self.refresh_button.set_tooltip_text("Rescan connected devices")
        self.refresh_button.connect("clicked", lambda *_: self.refresh_devices())
        grid.attach(self.refresh_button, 2, 0, 1, 1)

        self.show_all_check = Gtk.CheckButton(label="Show all drives")
        self.show_all_check.set_tooltip_text(
            "Also list internal disks. RufusLinux always refuses to write to the disk "
            "that contains the running system."
        )
        self.show_all_check.connect("toggled", self._on_show_all)
        grid.attach(self.show_all_check, 1, 1, 2, 1)

        self.device_info = self._label("", dim=True)
        self.device_info.set_line_wrap(True)
        self.device_info.set_selectable(True)
        grid.attach(self.device_info, 0, 2, 3, 1)
        return frame

    def _build_boot_section(self) -> Gtk.Widget:
        frame, grid = self._section("Boot selection")

        grid.attach(self._label("Image"), 0, 0, 1, 1)
        self.image_entry = Gtk.Entry()
        self.image_entry.set_hexpand(True)
        self.image_entry.set_placeholder_text("Select an ISO or disk image…")
        self.image_entry.set_editable(False)
        grid.attach(self.image_entry, 1, 0, 1, 1)

        select_button = Gtk.Button(label="SELECT")
        select_button.connect("clicked", self._on_select_image)
        grid.attach(select_button, 2, 0, 1, 1)

        self.checksum_button = Gtk.Button(label="Checksum")
        self.checksum_button.set_tooltip_text("Compute the SHA-256 checksum of the selected image")
        self.checksum_button.connect("clicked", self._on_checksum)
        grid.attach(self.checksum_button, 3, 0, 1, 1)

        self.image_info_label = self._label("No image selected.", dim=True)
        self.image_info_label.set_line_wrap(True)
        self.image_info_label.set_selectable(True)
        grid.attach(self.image_info_label, 0, 1, 4, 1)
        return frame

    def _build_options_section(self) -> Gtk.Widget:
        frame, grid = self._section("Format options")

        self.mode_combo = Gtk.ComboBoxText()
        self.scheme_combo = Gtk.ComboBoxText()
        self.target_combo = Gtk.ComboBoxText()
        self.fs_combo = Gtk.ComboBoxText()
        for widget, choices in (
            (self.mode_combo, MODE_LABELS),
            (self.scheme_combo, SCHEME_LABELS),
            (self.target_combo, TARGET_LABELS),
            (self.fs_combo, FS_LABELS),
        ):
            for key, text in choices:
                widget.append(key, text)
            widget.set_hexpand(True)
            widget.connect("changed", self._on_option_changed)

        self.mode_combo.set_active_id("auto")
        self.scheme_combo.set_active_id("mbr")
        self.target_combo.set_active_id("both")
        self.fs_combo.set_active_id("FAT32")

        grid.attach(self._label("Write mode"), 0, 0, 1, 1)
        grid.attach(self.mode_combo, 1, 0, 1, 1)
        grid.attach(self._label("Partition scheme"), 2, 0, 1, 1)
        grid.attach(self.scheme_combo, 3, 0, 1, 1)

        grid.attach(self._label("Target system"), 0, 1, 1, 1)
        grid.attach(self.target_combo, 1, 1, 1, 1)
        grid.attach(self._label("File system"), 2, 1, 1, 1)
        grid.attach(self.fs_combo, 3, 1, 1, 1)

        grid.attach(self._label("Volume label"), 0, 2, 1, 1)
        self.label_entry = Gtk.Entry()
        self.label_entry.set_text("USB")
        self.label_entry.set_max_length(32)
        self.label_entry.connect("changed", self._on_option_changed)
        grid.attach(self.label_entry, 1, 2, 1, 1)

        self.split_check = Gtk.CheckButton(label="Split install.wim over 4 GB")
        self.split_check.set_active(True)
        self.split_check.set_tooltip_text(
            "Required for FAT32 when sources/install.wim is larger than 4 GB. "
            "Uses wimlib-imagex (package: wimtools)."
        )
        self.split_check.connect("toggled", self._on_option_changed)
        grid.attach(self.split_check, 3, 2, 1, 1)

        self.verify_check = Gtk.CheckButton(label="Verify written data (slower)")
        self.verify_check.set_active(False)
        self.verify_check.connect("toggled", self._on_option_changed)
        grid.attach(self.verify_check, 2, 3, 2, 1)

        self.quick_check = Gtk.CheckButton(label="Quick format")
        self.quick_check.set_active(True)
        self.quick_check.set_sensitive(False)
        self.quick_check.set_tooltip_text("RufusLinux always performs a quick format")
        grid.attach(self.quick_check, 0, 3, 2, 1)

        self.hint_label = Gtk.Label()
        self.hint_label.set_xalign(0)
        self.hint_label.set_line_wrap(True)
        self.hint_label.set_margin_top(2)
        grid.attach(self.hint_label, 0, 4, 4, 1)
        return frame

    def _build_progress_section(self) -> Gtk.Widget:
        frame, grid = self._section("Status")

        self.status_label = self._label("Ready.")
        self.status_label.set_line_wrap(True)
        self.status_label.set_selectable(True)
        grid.attach(self.status_label, 0, 0, 1, 1)

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(False)
        self.progress.set_hexpand(True)
        grid.attach(self.progress, 0, 1, 1, 1)

        expander = Gtk.Expander(label="Show log")
        grid.attach(expander, 0, 2, 1, 1)

        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        log_box.set_margin_top(6)
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(160)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_buffer = self.log_view.get_buffer()
        scroll.add(self.log_view)
        log_box.pack_start(scroll, True, True, 0)

        save_button = Gtk.Button(label="Save log…")
        save_button.set_halign(Gtk.Align.END)
        save_button.connect("clicked", self._on_save_log)
        log_box.pack_start(save_button, False, False, 0)
        expander.add(log_box)
        return frame

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        footer.get_style_context().add_class("footer")
        footer.set_margin_start(16)
        footer.set_margin_end(16)
        footer.set_margin_top(8)
        footer.set_margin_bottom(10)

        self.footer_status = self._label("Ready", dim=True)
        footer.pack_start(self.footer_status, True, True, 0)

        self.start_button = Gtk.Button(label="START")
        self.start_button.get_style_context().add_class("start-button")
        self.start_button.set_size_request(150, 42)
        self.start_button.connect("clicked", self._on_start)
        footer.pack_end(self.start_button, False, False, 0)

        close_button = Gtk.Button(label="CLOSE")
        close_button.set_size_request(100, 42)
        close_button.connect("clicked", lambda *_: self._on_delete(None, None))
        footer.pack_end(close_button, False, False, 0)
        return footer

    # ------------------------------------------------------------------
    # Device handling
    # ------------------------------------------------------------------
    def _signature(self):
        try:
            listing = devices.list_devices(show_all=self.show_all_check.get_active())
        except Exception:
            return None
        self._all_devices = listing
        return tuple((d.path, d.size, tuple(sorted(d.mountpoints))) for d in listing)

    def refresh_devices(self) -> None:
        if self.busy or self._destroyed:
            return
        try:
            listing = devices.list_devices(show_all=self.show_all_check.get_active())
        except Exception as exc:
            self.device_info.set_text("Unable to list devices: %s" % exc)
            return

        current = self.device_combo.get_active_id()
        self.device_map = {d.path: d for d in listing}
        self.device_combo.remove_all()
        for device in listing:
            text = device.description()
            self.device_combo.append(device.path, text)
        if current in self.device_map:
            self.device_combo.set_active_id(current)
        elif listing:
            self.device_combo.set_active(0)
        else:
            self.device_combo.set_active(-1)
        self._device_signature = self._signature()
        self._on_device_changed()

    def _selected_device(self) -> devices.Device | None:
        active = self.device_combo.get_active_id()
        return self.device_map.get(active) if active else None

    def _on_device_changed(self, *_args) -> None:
        device = self._selected_device()
        if device is None:
            self.device_info.set_markup("<i>No removable device found. Plug in a USB drive and refresh.</i>")
        else:
            text = _esc(device.details())
            if device.is_system:
                text += "\n<span foreground='#c01c28'><b>This disk contains the running system and cannot be used.</b></span>"
            elif device.mountpoints:
                text += "\n<span foreground='#b87500'>Mounted partitions will be unmounted before writing.</span>"
            self.device_info.set_markup(text)
        self._update_state()

    def _on_show_all(self, *_args) -> None:
        if self.show_all_check.get_active() and not self.busy:
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.WARNING,
                buttons=Gtk.ButtonsType.OK_CANCEL,
                text="Show internal disks?",
            )
            dialog.format_secondary_text(
                "Internal drives can be destroyed by this program. The disk holding "
                "your running system stays protected, but every other disk is fair game."
            )
            response = dialog.run()
            dialog.destroy()
            if response != Gtk.ResponseType.OK:
                self.show_all_check.set_active(False)
                return
        self.refresh_devices()

    def _on_destroy(self, *_args) -> None:
        self._destroyed = True
        if self._poll_id:
            try:
                GLib.source_remove(self._poll_id)
            except Exception:
                pass
            self._poll_id = 0

    def _poll_devices(self) -> bool:
        if self._destroyed:
            return False
        if self.busy:
            return True
        signature = self._signature()
        if signature is not None and signature != self._device_signature:
            self.refresh_devices()
        return True

    # ------------------------------------------------------------------
    # Image handling
    # ------------------------------------------------------------------
    def _on_select_image(self, *_args) -> None:
        if self.busy:
            return
        dialog = Gtk.FileChooserDialog(
            title="Select a bootable image",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OPEN,
            Gtk.ResponseType.OK,
        )
        filter_images = Gtk.FileFilter()
        filter_images.set_name("Disk images (*.iso, *.img, *.vhd)")
        for pattern in ("*.iso", "*.ISO", "*.img", "*.IMG", "*.vhd", "*.VHD"):
            filter_images.add_pattern(pattern)
        dialog.add_filter(filter_images)
        all_files = Gtk.FileFilter()
        all_files.set_name("All files")
        all_files.add_pattern("*")
        dialog.add_filter(all_files)

        start_dir = os.path.dirname(self.image_path) if self.image_path else ""
        if not start_dir:
            for candidate in ("~/Descargas", "~/Downloads", "~"):
                expanded = os.path.expanduser(candidate)
                if os.path.isdir(expanded):
                    start_dir = expanded
                    break
        if start_dir:
            dialog.set_current_folder(start_dir)

        response = dialog.run()
        path = dialog.get_filename() if response == Gtk.ResponseType.OK else ""
        dialog.destroy()
        if path:
            self._start_analysis(path)

    def _start_analysis(self, path: str) -> None:
        reason = images.unsupported_image_reason(path)
        if reason:
            self._error("Unsupported image", reason)
            return

        self.image_path = path
        self.image_info = None
        self.image_entry.set_text(path)
        self.image_info_label.set_markup("<i>Analysing image…</i>")
        self._analysis_seq += 1
        seq = self._analysis_seq
        self._update_state()

        def worker():
            try:
                info = images.analyze(path)
                error = ""
            except Exception as exc:
                info = None
                error = str(exc)
            GLib.idle_add(self._analysis_done, seq, path, info, error)

        threading.Thread(target=worker, daemon=True).start()

    def _analysis_done(self, seq: int, path: str, info, error: str) -> bool:
        if self._destroyed or seq != self._analysis_seq or path != self.image_path:
            return False
        if info is None:
            self.image_info_label.set_markup(
                "<span foreground='#c01c28'>Unable to analyse image: %s</span>" % _esc(error)
            )
            self._update_state()
            return False

        self.image_info = info
        text = _esc(info.summary())
        text += "\n<span size='small' foreground='#666666'>%s — %s</span>" % (
            _esc(util.human_size(info.size)),
            _esc(os.path.basename(path)),
        )
        if info.label:
            text += "\n<span size='small' foreground='#666666'>ISO label: %s</span>" % _esc(info.label)
        self.image_info_label.set_markup(text)
        self._apply_image_defaults(info)
        self._update_state()
        return False

    def _apply_image_defaults(self, info: images.ImageInfo) -> None:
        self.mode_combo.set_active_id("auto")
        self.scheme_combo.set_active_id("mbr")
        self.target_combo.set_active_id("both")

        filesystem = info.recommended_filesystem
        self.fs_combo.set_active_id(filesystem if filesystem in ("FAT32", "NTFS", "exFAT") else "FAT32")

        if info.kind == "windows":
            lower = os.path.basename(info.path).lower()
            if "win11" in lower or "windows11" in lower:
                label = "WIN11"
            elif "win10" in lower or "windows10" in lower:
                label = "WIN10"
            else:
                label = "WINDOWS"
        else:
            label = util.default_label(info.kind, info.distro)
        self.label_entry.set_text(label)

        if info.kind == "windows" and info.install_size > 4 * 1024**3:
            self.split_check.set_active(True)

    # ------------------------------------------------------------------
    # State / validation
    # ------------------------------------------------------------------
    def _resolved_mode(self) -> str:
        chosen = self.mode_combo.get_active_id() or "auto"
        if chosen != "auto":
            return chosen
        if self.image_info is not None:
            return self.image_info.recommended_mode
        return "extract"

    def _selected_fs(self) -> str:
        return self.fs_combo.get_active_id() or "FAT32"

    def _selected_target(self) -> str:
        return self.target_combo.get_active_id() or "both"

    def _context_warnings(self, info: images.ImageInfo) -> list[str]:
        mode = self._resolved_mode()
        target = self._selected_target()
        filesystem = self._selected_fs()
        out: list[str] = []
        for warning in info.warnings():
            if mode == "dd" and "DD mode is recommended" in warning:
                continue
            if target == "bios" and "UEFI boot may need" in warning:
                continue
            if filesystem.upper() != "FAT32" and "FAT32 needs it split" in warning:
                continue
            out.append(warning)
        if mode == "dd" and not info.hybrid:
            out.append("DD writing a non-hybrid image usually produces an unbootable drive.")
        if mode == "extract" and info.kind == "linux":
            out.append(
                "Linux extract mode is experimental; DD mode preserves distro boot "
                "features (persistence, Secure Boot shims, etc.)."
            )
        if mode == "extract" and info.kind == "windows" and target in ("uefi", "both") \
                and filesystem.upper() == "NTFS":
            out.append(
                "NTFS is not readable by most UEFI firmware. RufusLinux adds the "
                "UEFI:NTFS boot partition, but FAT32 is more compatible."
            )
        if self.scheme_combo.get_active_id() == "gpt" and target in ("bios", "both") and mode == "extract":
            out.append("GPT + BIOS mode adds a small BIOS boot partition for GRUB.")
        if mode == "extract" and filesystem.upper() == "EXFAT":
            if info.kind == "linux":
                out.append("Linux extract mode requires FAT32; exFAT is not supported.")
            elif target != "uefi":
                out.append(
                    "exFAT Windows media can only boot on UEFI; choose UEFI only or "
                    "use FAT32/NTFS for BIOS support."
                )
            else:
                out.append(
                    "Booting exFAT media on UEFI depends on firmware exFAT support. "
                    "FAT32 or NTFS is safer."
                )
        # Deduplicate while preserving order.
        seen = set()
        return [w for w in out if not (w in seen or seen.add(w))]

    def _validation(self) -> tuple[list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        device = self._selected_device()
        info = self.image_info
        mode = self._resolved_mode()
        filesystem = self._selected_fs()
        target = self._selected_target()

        if device is None:
            errors.append("Select a target device.")
        else:
            if device.is_system:
                errors.append("The selected disk contains the running system and cannot be used.")
            if device.read_only:
                errors.append("The selected device is read-only.")

        if not self.image_path:
            errors.append("Select an ISO or disk image.")
        elif info is None:
            errors.append("The image is still being analysed.")
        else:
            warnings.extend(self._context_warnings(info))
            if mode == "extract" and not info.extract_supported:
                errors.append(
                    "This image cannot be extracted (no readable filesystem). Use DD mode."
                )
            if mode == "dd" and device is not None and info.size > device.size:
                errors.append(
                    "The image (%s) does not fit on the device (%s)."
                    % (util.human_size(info.size), util.human_size(device.size))
                )
            if mode == "extract" and info.kind == "windows" and filesystem.upper() == "FAT32" \
                    and info.install_size > 4 * 1024**3:
                if info.install_image.lower().endswith(".esd"):
                    errors.append(
                        "install.esd is larger than 4 GB and cannot be split; choose NTFS."
                    )
                elif not self.split_check.get_active():
                    errors.append(
                        "install.wim is larger than 4 GB. Enable WIM splitting or choose NTFS."
                    )
                elif not util.which("wimlib-imagex"):
                    errors.append(
                        "WIM splitting needs wimlib-imagex. Install it with: sudo apt install wimtools"
                    )
            if mode == "extract" and filesystem.upper() == "FAT32" and info.entries:
                known = ("sources/install.wim", "sources/install.esd")
                huge = [
                    entry for entry in info.entries.values()
                    if not entry.is_dir
                    and entry.size > 4 * 1024**3
                    and not entry.path.lower().endswith(known)
                ]
                if huge:
                    biggest = max(huge, key=lambda entry: entry.size)
                    hint = (
                        "Use DD mode, or NTFS with BIOS-only boot."
                        if info.kind == "linux"
                        else "Choose NTFS or exFAT."
                    )
                    errors.append(
                        "%s is %s and does not fit on FAT32. %s"
                        % (biggest.path, util.human_size(biggest.size), hint)
                    )
            if mode == "extract" and info.kind == "linux" and target in ("uefi", "both") \
                    and filesystem.upper() != "FAT32":
                errors.append("Linux UEFI boot needs FAT32; choose FAT32 or BIOS only.")
            if mode == "extract" and filesystem.upper() == "EXFAT":
                if info.kind == "linux":
                    errors.append("Linux extract mode requires FAT32; exFAT is not supported.")
                elif target != "uefi":
                    errors.append(
                        "exFAT Windows media can only boot on UEFI; choose UEFI only, "
                        "or use FAT32/NTFS for BIOS support."
                    )
            if not (self.label_entry.get_text() or "").strip():
                warnings.append("The volume label is empty; 'USB' will be used.")

        return errors, warnings

    def _update_state(self) -> None:
        mode = self._resolved_mode()
        extracting = mode == "extract"
        info = self.image_info

        for widget in (self.mode_combo, self.scheme_combo, self.target_combo, self.fs_combo, self.label_entry):
            widget.set_sensitive(not self.busy and bool(self.image_path))

        if not extracting:
            for widget in (self.scheme_combo, self.target_combo, self.fs_combo, self.label_entry):
                widget.set_sensitive(False)

        self.split_check.set_sensitive(
            not self.busy
            and extracting
            and info is not None
            and info.kind == "windows"
            and self._selected_fs().upper() == "FAT32"
            and self._selected_target() != "bios"
        )
        self.verify_check.set_sensitive(not self.busy)
        self.checksum_button.set_sensitive(not self.busy and bool(self.image_path))
        self.refresh_button.set_sensitive(not self.busy)
        self.device_combo.set_sensitive(not self.busy)
        self.show_all_check.set_sensitive(not self.busy)

        if self.busy:
            self.start_button.set_label("CANCEL")
            self.start_button.get_style_context().remove_class("start-button")
            self.start_button.get_style_context().add_class("destructive-action")
            self.start_button.set_sensitive(True)
            self.footer_status.set_text("Working…")
            return

        self.start_button.set_label("START")
        self.start_button.get_style_context().remove_class("destructive-action")
        self.start_button.get_style_context().add_class("start-button")
        self.footer_status.set_text("Ready")
        self._update_hint()

    def _update_hint(self) -> None:
        errors, warnings = self._validation()
        if self.busy:
            return
        if errors:
            text = "<span foreground='#c01c28'><b>⚠ %s</b></span>" % _esc(errors[0])
            if len(errors) > 1:
                text += "<span foreground='#c01c28'> (+%d more)</span>" % (len(errors) - 1)
            self.hint_label.set_markup(text)
            self.hint_label.set_tooltip_text("\n".join(errors))
        elif warnings:
            text = "<span foreground='#b87500'><b>⚠ %s</b></span>" % _esc(warnings[0])
            if len(warnings) > 1:
                text += "<span foreground='#b87500'> (+%d more)</span>" % (len(warnings) - 1)
            self.hint_label.set_markup(text)
            self.hint_label.set_tooltip_text("\n".join(warnings))
        else:
            mode = self._resolved_mode().upper()
            self.hint_label.set_markup(
                "<span foreground='#2a7d2e'><b>Ready.</b></span> "
                "<span foreground='#666666'>%s mode will be used.</span>" % _esc(mode)
            )
            self.hint_label.set_tooltip_text("")
        self.start_button.set_sensitive(not errors)

    def _on_option_changed(self, *_args) -> None:
        if self._building or self.busy:
            return
        self._update_state()

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------
    def _on_start(self, *_args) -> None:
        if self.busy:
            if self.runner is not None and self.runner.is_running():
                self._ask_cancel()
            return

        errors, warnings = self._validation()
        if errors:
            self._error("Cannot start", "\n".join(errors))
            return
        device = self._selected_device()
        info = self.image_info
        if device is None or info is None:
            return

        mode = self._resolved_mode()
        filesystem = self._selected_fs()
        summary = [
            ("Device", "%s  (%s, %s)" % (device.path, device.model or "unknown model", util.human_size(device.size))),
            ("Image", os.path.basename(self.image_path)),
            ("Image type", info.summary()),
            ("Write mode", "DD image" if mode == "dd" else "Extract files"),
        ]
        if mode == "extract":
            summary.extend(
                [
                    ("Partition scheme", "MBR / DOS" if self.scheme_combo.get_active_id() == "mbr" else "GPT"),
                    ("Target system", dict(TARGET_LABELS).get(self._selected_target(), "")),
                    ("File system", filesystem),
                    ("Volume label", util.sanitize_label(self.label_entry.get_text() or "USB", filesystem)),
                ]
            )

        dialog = ConfirmDialog(self, summary, warnings, device.path)
        response = dialog.run()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return

        job = {
            "image": self.image_path,
            "device": device.path,
            "mode": mode,
            "scheme": self.scheme_combo.get_active_id() or "mbr",
            "target": self._selected_target(),
            "filesystem": filesystem,
            "label": util.sanitize_label(self.label_entry.get_text() or "USB", filesystem),
            "verify": self.verify_check.get_active(),
            "split_wim": self.split_check.get_active(),
            "confirm": device.path,
        }

        self._prepare_run(mode, device)
        self.runner = EngineRunner(job, self._on_engine_event, self._on_engine_exit)
        try:
            self.runner.start()
        except Exception as exc:
            self.runner = None
            self.busy = False
            self._update_state()
            self._error("Unable to start", str(exc))

    def _prepare_run(self, mode: str, device: devices.Device) -> None:
        self.busy = True
        self._last_exit = None
        self.progress.set_fraction(0.0)
        self.status_label.set_text("Preparing…")
        self._append_log("")
        self._append_log("==== %s ====" % time.strftime("%Y-%m-%d %H:%M:%S"))
        self._append_log("Target: %s (%s)" % (device.path, util.human_size(device.size)))
        self._append_log("Image:  %s" % self.image_path)
        self._append_log("Mode:   %s" % mode)
        self._update_state()

    def _on_engine_event(self, payload: dict) -> None:
        if self._destroyed:
            return
        kind = payload.get("type")
        if kind == "log":
            self._append_log(payload.get("text", ""))
        elif kind == "status":
            text = payload.get("text", "")
            self.status_label.set_text(text)
            self._append_log("• " + text)
        elif kind == "progress":
            value = float(payload.get("value") or 0.0)
            self.progress.set_fraction(max(0.0, min(1.0, value / 100.0)))
            text = payload.get("text") or ""
            if text:
                self.status_label.set_text("%s — %.0f%%" % (text, value))
        elif kind == "meta":
            mode = payload.get("mode")
            filesystem = payload.get("filesystem")
            if mode:
                self._append_log("Engine plan: mode=%s filesystem=%s scheme=%s target=%s"
                                 % (mode, filesystem, payload.get("scheme"), payload.get("target")))
        elif kind == "done":
            self.progress.set_fraction(1.0 if payload.get("ok") else self.progress.get_fraction())
            if payload.get("message"):
                self.status_label.set_text(payload["message"])

    def _on_engine_exit(self, ok: bool, message: str) -> None:
        if self._destroyed:
            return
        self.busy = False
        if self._quit_after_cancel:
            self._quit_after_cancel = False
            self.runner = None
            Gtk.main_quit()
            return
        self._last_exit = (ok, message)
        self.runner = None
        self._update_state()
        self.footer_status.set_text("Finished" if ok else "Failed")
        if ok:
            self.progress.set_fraction(1.0)
            self.status_label.set_text("Done. The target device should now be bootable.")
            self._append_log("Done. The target device should now be bootable.")
        else:
            self.status_label.set_text(message or "The operation failed.")
            self._append_log("Failed: %s" % (message or "unknown error"))
        self.refresh_devices()
        if ok:
            self._info("Finished", "The target device should now be bootable.\nYou can remove it.")
        else:
            self._error("Operation failed", message or "The operation failed.")

    def _ask_cancel(self) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Cancel the operation?",
        )
        dialog.format_secondary_text(
            "The device may be left half-written and unusable until you write it again."
        )
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.YES and self.runner is not None:
            self.runner.cancel()
            self.status_label.set_text("Cancelling…")

    def _on_delete(self, *_args) -> bool:
        if self.busy and self.runner is not None and self.runner.is_running():
            dialog = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.QUESTION,
                buttons=Gtk.ButtonsType.YES_NO,
                text="An operation is still running. Cancel it and quit?",
            )
            if dialog.run() != Gtk.ResponseType.YES:
                dialog.destroy()
                return True
            dialog.destroy()
            self._quit_after_cancel = True
            self.runner.cancel()
            return True
        Gtk.main_quit()
        return False

    # ------------------------------------------------------------------
    # Checksum / log / dialogs
    # ------------------------------------------------------------------
    def _on_checksum(self, *_args) -> None:
        if not self.image_path:
            return
        dialog = ChecksumDialog(self, self.image_path)
        dialog.run()
        dialog.destroy()

    def _on_save_log(self, *_args) -> None:
        dialog = Gtk.FileChooserDialog(
            title="Save log",
            transient_for=self,
            action=Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL,
            Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE,
            Gtk.ResponseType.OK,
        )
        dialog.set_do_overwrite_confirmation(True)
        dialog.set_current_name("rufuslinux.log")
        if dialog.run() == Gtk.ResponseType.OK:
            path = dialog.get_filename()
            start, end = self.log_buffer.get_bounds()
            text = self.log_buffer.get_text(start, end, True)
            try:
                Path(path).write_text(text, encoding="utf-8")
            except OSError as exc:
                self._error("Unable to save log", str(exc))
        dialog.destroy()

    def _append_log(self, text: str) -> None:
        if self.log_buffer.get_line_count() > 5000:
            start = self.log_buffer.get_start_iter()
            cut = self.log_buffer.get_iter_at_line(1000)
            self.log_buffer.delete(start, cut)
        end = self.log_buffer.get_end_iter()
        self.log_buffer.insert(end, (text or "") + "\n")
        mark = self.log_buffer.create_mark(None, self.log_buffer.get_end_iter(), False)
        self.log_view.scroll_mark_onscreen(mark)

    def _info(self, title: str, text: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dialog.format_secondary_text(text)
        dialog.run()
        dialog.destroy()

    def _error(self, title: str, text: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        dialog.format_secondary_text(text)
        dialog.run()
        dialog.destroy()


class ConfirmDialog(Gtk.Dialog):
    def __init__(self, parent, summary: list[tuple[str, str]], warnings: list[str], device: str):
        super().__init__(title="Confirm write", transient_for=parent, modal=True)
        self.set_default_size(620, -1)
        self.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, "WRITE", Gtk.ResponseType.OK)
        self.set_default_response(Gtk.ResponseType.CANCEL)

        box = self.get_content_area()
        box.set_spacing(10)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.set_margin_top(14)
        box.set_margin_bottom(10)

        warning = Gtk.Label()
        warning.set_markup(
            "<span size='large' weight='bold' foreground='#c01c28'>"
            "All data on the selected device will be destroyed</span>"
        )
        warning.set_xalign(0)
        box.pack_start(warning, False, False, 0)

        grid = Gtk.Grid()
        grid.set_column_spacing(14)
        grid.set_row_spacing(6)
        for row, (key, value) in enumerate(summary):
            label = Gtk.Label()
            label.set_markup("<b>%s</b>" % _esc(key))
            label.set_xalign(1)
            grid.attach(label, 0, row, 1, 1)
            value_label = Gtk.Label(label=value)
            value_label.set_xalign(0)
            value_label.set_line_wrap(True)
            value_label.set_selectable(True)
            grid.attach(value_label, 1, row, 1, 1)
        box.pack_start(grid, False, False, 0)

        if warnings:
            expander = Gtk.Expander(label="Warnings (%d)" % len(warnings))
            inner = Gtk.Label()
            inner.set_markup("\n".join("• " + _esc(w) for w in warnings))
            inner.set_xalign(0)
            inner.set_line_wrap(True)
            inner.set_margin_start(12)
            inner.set_margin_top(4)
            expander.add(inner)
            expander.set_expanded(True)
            box.pack_start(expander, False, False, 0)

        self.confirm = Gtk.CheckButton(
            label="I understand that everything on %s will be erased." % device
        )
        self.confirm.connect("toggled", self._on_toggle)
        box.pack_start(self.confirm, False, False, 0)

        self.write_button = self.get_widget_for_response(Gtk.ResponseType.OK)
        if self.write_button is not None:
            self.write_button.set_sensitive(False)
            self.write_button.get_style_context().add_class("destructive-action")

        self.show_all()

    def _on_toggle(self, checkbox) -> None:
        if self.write_button is not None:
            self.write_button.set_sensitive(checkbox.get_active())


class ChecksumDialog(Gtk.Dialog):
    def __init__(self, parent, path: str):
        super().__init__(title="Checksum", transient_for=parent, modal=True)
        self.path = path
        self.cancelled = threading.Event()
        self.finished = False
        self.alive = True
        self.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        self.set_default_size(660, -1)

        box = self.get_content_area()
        box.set_spacing(10)
        box.set_margin_start(18)
        box.set_margin_end(18)
        box.set_margin_top(14)
        box.set_margin_bottom(10)

        self.title_label = Gtk.Label()
        self.title_label.set_markup(
            "<b>SHA-256</b> of %s (%s)"
            % (_esc(os.path.basename(path)), _esc(util.human_size(os.path.getsize(path))))
        )
        self.title_label.set_xalign(0)
        self.title_label.set_line_wrap(True)
        box.pack_start(self.title_label, False, False, 0)

        self.spinner = Gtk.Spinner()
        self.spinner.set_halign(Gtk.Align.START)
        box.pack_start(self.spinner, False, False, 0)

        self.result = Gtk.Label(label="Computing…")
        self.result.set_xalign(0)
        self.result.set_selectable(True)
        self.result.set_line_wrap(True)
        self.result.set_monospace(True)
        box.pack_start(self.result, False, False, 0)

        self.progress = Gtk.ProgressBar()
        box.pack_start(self.progress, False, False, 0)
        self.show_all()
        self.spinner.start()

        self.connect("response", self._on_response)
        self.connect("delete-event", self._on_delete)
        self.connect("destroy", self._on_destroy)
        threading.Thread(target=self._work, daemon=True).start()

    def _work(self) -> None:
        try:
            digest = util.checksum_file(
                self.path,
                "sha256",
                progress=lambda fraction: GLib.idle_add(self._set_progress, fraction),
                cancel=self.cancelled,
            )
            GLib.idle_add(self._done, digest, None)
        except util.Cancelled:
            GLib.idle_add(self._done, None, "Cancelled")
        except Exception as exc:
            GLib.idle_add(self._done, None, str(exc))

    def _set_progress(self, fraction: float) -> bool:
        if self.alive and not self.finished:
            self.progress.set_fraction(max(0.0, min(1.0, fraction)))
        return False

    def _done(self, digest: str | None, error: str | None) -> bool:
        if not self.alive:
            return False
        self.finished = True
        self.spinner.stop()
        self.spinner.hide()
        if digest:
            self.result.set_text(digest)
            self.progress.set_fraction(1.0)
            self.set_response_sensitive(Gtk.ResponseType.CLOSE, True)
        else:
            self.result.set_text(error or "Failed")
            self.progress.set_fraction(0.0)
        return False

    def _on_response(self, _dialog, response) -> None:
        if response == Gtk.ResponseType.CLOSE and not self.finished:
            self.cancelled.set()
            self.result.set_text("Cancelling…")
            return
        self.destroy()

    def _on_delete(self, *_args) -> bool:
        if not self.finished:
            self.cancelled.set()
            return True
        return False

    def _on_destroy(self, *_args) -> None:
        self.alive = False
        self.cancelled.set()


def main(argv: list[str] | None = None) -> int:
    Gtk.init(argv)
    window = MainWindow()
    window.connect("destroy", lambda *_: Gtk.main_quit())
    window.show_all()
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
