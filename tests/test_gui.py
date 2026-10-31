import os
import unittest


@unittest.skipUnless(
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"),
    "no graphical display",
)
class TestGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk

        Gtk.init([])

    def test_window_builds(self):
        from rufus_linux.app import MainWindow

        window = MainWindow()
        try:
            self.assertEqual(window.get_title(), "RufusLinux")
            self.assertIsNotNone(window.start_button)
            self.assertFalse(window.start_button.get_sensitive())
        finally:
            window.destroy()

    def test_fake_usb_enables_start_and_requires_confirmation(self):
        from unittest.mock import patch

        from rufus_linux import devices, images
        from rufus_linux.app import ConfirmDialog, MainWindow

        fake = devices.Device(
            name="sdb", path="/dev/sdb", size=16 * 1024**3,
            model="Test USB Stick", vendor="", tran="usb",
            removable=True, read_only=False,
        )
        with patch.object(devices, "list_devices", return_value=[fake]):
            window = MainWindow()
            try:
                # This also regresses a GTK4-only call in refresh_devices().
                self.assertEqual(window.device_combo.get_active_id(), "/dev/sdb")

                window.image_path = "fake-win.iso"
                window.image_info = images.ImageInfo(
                    path="fake-win.iso", size=8 * 1024**3, label="WIN",
                    kind="windows", efi_boot=True,
                    install_image="sources/install.wim",
                    install_size=5 * 1024**3, entries={}, listing_ok=True,
                )
                window.mode_combo.set_active_id("extract")
                window.fs_combo.set_active_id("NTFS")
                window.target_combo.set_active_id("both")
                window._update_state()

                errors, _warnings = window._validation()
                self.assertEqual(errors, [])
                self.assertTrue(window.start_button.get_sensitive())

                dialog = ConfirmDialog(window, [("Device", "/dev/sdb")], [], "/dev/sdb")
                try:
                    self.assertFalse(dialog.write_button.get_sensitive())
                    dialog.confirm.set_active(True)
                    self.assertTrue(dialog.write_button.get_sensitive())
                finally:
                    dialog.destroy()
            finally:
                window.destroy()

    def test_image_analysis_populates_form(self):
        iso = "/home/bruno/Descargas/Windows11_Client_x64_es-es_26300_9457.iso"
        if not os.path.exists(iso):
            self.skipTest("Windows ISO not present")
        from gi.repository import GLib

        from rufus_linux.app import MainWindow

        window = MainWindow()
        try:
            window._start_analysis(iso)
            loop = GLib.MainLoop()
            GLib.timeout_add(5000, loop.quit)
            loop.run()
            self.assertIsNotNone(window.image_info)
            self.assertEqual(window.image_info.kind, "windows")
            self.assertEqual(window.fs_combo.get_active_id(), "NTFS")
            self.assertEqual(window.label_entry.get_text(), "WIN11")
        finally:
            window.destroy()


if __name__ == "__main__":
    unittest.main()
