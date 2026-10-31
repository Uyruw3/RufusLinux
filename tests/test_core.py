import json
import os
import tempfile
import unittest

from rufus_linux import devices, images, util
from rufus_linux import engine

WINDOWS_ISO = "/home/bruno/Descargas/Windows11_Client_x64_es-es_26300_9457.iso"


class TestUtil(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(util.human_size(0), "0 B")
        self.assertEqual(util.human_size(1024), "1.0 KB")
        self.assertEqual(util.human_size(1024**3), "1.0 GB")

    def test_sanitize_label(self):
        self.assertEqual(util.sanitize_label("my usb", "FAT32"), "MY USB")
        self.assertEqual(util.sanitize_label('a/b:c*?', "FAT32"), "A_B_C__")
        self.assertEqual(util.sanitize_label("x" * 40, "NTFS"), "x" * 32)
        self.assertEqual(util.sanitize_label("x" * 40, "FAT32"), "X" * 11)

    def test_default_label(self):
        self.assertEqual(util.default_label("windows"), "WINDOWS")
        self.assertEqual(util.default_label("linux", "Ubuntu 24.04 LTS"), "UBUNTU")

    def test_partition_node(self):
        self.assertEqual(util.partition_node("/dev/sda", 1), "/dev/sda1")
        self.assertEqual(util.partition_node("/dev/nvme0n1", 2), "/dev/nvme0n1p2")
        self.assertEqual(util.partition_node("/dev/mmcblk0", 1), "/dev/mmcblk0p1")

    def test_iso_label_and_hybrid_synthetic(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            path = handle.name
            data = bytearray(17 * 2048)
            data[16 * 2048 + 1 : 16 * 2048 + 6] = b"CD001"
            data[16 * 2048 + 40 : 16 * 2048 + 40 + 11] = b"TEST_LABEL "
            data[510:512] = b"\x55\xaa"
            entry = bytearray(16)
            entry[0] = 0x80
            entry[4] = 0x83
            entry[8:12] = (2048).to_bytes(4, "little")
            entry[12:16] = (100).to_bytes(4, "little")
            data[446:462] = entry
            handle.write(data)
        try:
            self.assertEqual(images._iso_label(path), "TEST_LABEL")
            self.assertTrue(images._is_hybrid(path))
        finally:
            os.unlink(path)


class TestDevices(unittest.TestCase):
    def test_list_devices_finds_system_disk(self):
        listing = devices.list_devices(show_all=True)
        self.assertTrue(listing, "expected at least one disk")
        system = [d for d in listing if d.is_system]
        if not system:
            self.skipTest("no disk with a protected mount was found")
        self.assertTrue(system[0].protected_mounts)
        protected, reason = devices.is_protected(system[0].path)
        self.assertTrue(protected)
        self.assertIn("contains", reason)

    def test_removable_filter(self):
        for device in devices.list_devices(show_all=False):
            self.assertTrue(device.removable or device.tran.lower() == "usb")


class TestImages(unittest.TestCase):
    def test_plain_file_is_not_extractable(self):
        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(b"not an image")
            path = handle.name
        try:
            info = images.analyze(path)
            self.assertFalse(info.hybrid)
            self.assertFalse(info.extract_supported)
            self.assertEqual(info.recommended_mode, "dd")
        finally:
            os.unlink(path)

    @unittest.skipUnless(os.path.exists(WINDOWS_ISO), "Windows 11 ISO not present")
    def test_windows_iso(self):
        info = images.analyze(WINDOWS_ISO)
        self.assertEqual(info.kind, "windows")
        self.assertFalse(info.hybrid)
        self.assertTrue(info.efi_boot)
        self.assertGreater(info.install_size, 4 * 1024**3)
        self.assertEqual(info.recommended_mode, "extract")
        self.assertEqual(info.recommended_filesystem, "NTFS")
        self.assertTrue(info.extract_supported)

    def test_compressed_image_rejected(self):
        self.assertIn("decompress", images.unsupported_image_reason("x.iso.xz"))
        self.assertEqual(images.unsupported_image_reason("x.iso"), "")


class TestEngineHelpers(unittest.TestCase):
    def _job_file(self, data):
        handle, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(handle, "w") as out:
            json.dump(data, out)
        return path

    def test_load_job_validation(self):
        path = self._job_file({"image": "a.iso", "device": "/dev/sdz"})
        try:
            job = engine.load_job(path)
            self.assertEqual(job.mode, "auto")
            self.assertEqual(job.filesystem, "FAT32")
        finally:
            os.unlink(path)

        path = self._job_file({"image": "a.iso", "device": "/dev/sdz", "mode": "bogus"})
        try:
            with self.assertRaises(ValueError):
                engine.load_job(path)
        finally:
            os.unlink(path)

        path = self._job_file({"image": "a.iso"})
        try:
            with self.assertRaises(ValueError):
                engine.load_job(path)
        finally:
            os.unlink(path)

    def test_safe_paths(self):
        self.assertEqual(engine.safe_component('a:b?c', "FAT32"), "a_b_c")
        self.assertEqual(engine.safe_component("trailing. ", "FAT32"), "trailing")
        self.assertEqual(engine.safe_relpath("EFI/Boot/bootx64.efi", "FAT32"),
                         "EFI/Boot/bootx64.efi")
        self.assertEqual(engine.FAT32_MAX_FILE, 4 * 1024**3 - 1)

    def test_copy_tree_and_verify(self):
        import tempfile

        with tempfile.TemporaryDirectory() as source, tempfile.TemporaryDirectory() as target:
            os.makedirs(os.path.join(source, "EFI", "BOOT"))
            with open(os.path.join(source, "EFI", "BOOT", "bootx64.efi"), "wb") as handle:
                handle.write(b"efi" * 1000)
            with open(os.path.join(source, "readme.txt"), "w") as handle:
                handle.write("hello")
            rep = engine.Reporter(human=True)
            cancel = engine.CancelToken()
            copied = engine.copy_tree(source, target, "FAT32", rep, cancel)
            self.assertEqual(copied, 3005)
            engine.verify_extract(source, target, set(), "FAT32", rep, cancel)
            with open(os.path.join(target, "readme.txt"), encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "hello")

    def test_linux_grub_config(self):
        import tempfile

        with tempfile.TemporaryDirectory() as target:
            os.makedirs(os.path.join(target, "casper"))
            for name in ("vmlinuz", "initrd"):
                open(os.path.join(target, "casper", name), "wb").close()
            job = engine.Job(image="x", device="/dev/fake", label="UBUNTU", filesystem="FAT32")
            path = engine.write_linux_grub_config(target, job, engine.Reporter(human=True))
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("boot=casper", text)
            self.assertIn("/casper/vmlinuz", text)

    def test_fat32_limit_needs_split_and_wimlib(self):
        import tempfile

        with tempfile.TemporaryDirectory() as source:
            os.makedirs(os.path.join(source, "sources"))
            path = os.path.join(source, "sources", "install.wim")
            with open(path, "wb") as handle:
                handle.truncate(5 * 1024**3)  # sparse, no real disk use

            job = engine.Job(image="x", device="/dev/fake", filesystem="FAT32", split_wim=False)
            with self.assertRaises(RuntimeError):
                engine.check_fat32_limits(source, images.ImageInfo(path="x", size=1, label=""), job,
                                          engine.Reporter(human=True))

            job.split_wim = True
            if util.which("wimlib-imagex"):
                skipped = engine.check_fat32_limits(
                    source, images.ImageInfo(path="x", size=1, label=""),
                    job, engine.Reporter(human=True))
                self.assertIn("sources/install.wim", skipped)
            else:
                with self.assertRaises(RuntimeError) as ctx:
                    engine.check_fat32_limits(
                        source, images.ImageInfo(path="x", size=1, label=""),
                        job, engine.Reporter(human=True))
                self.assertIn("wimlib", str(ctx.exception))

    def test_fat32_huge_linux_file_advises_dd(self):
        with tempfile.TemporaryDirectory() as source:
            os.makedirs(os.path.join(source, "casper"))
            path = os.path.join(source, "casper", "filesystem.squashfs")
            with open(path, "wb") as handle:
                handle.truncate(5 * 1024**3)
            job = engine.Job(image="x", device="/dev/fake", filesystem="FAT32")
            info = images.ImageInfo(path="x", size=1, label="", kind="linux")
            with self.assertRaises(RuntimeError) as ctx:
                engine.check_fat32_limits(source, info, job, engine.Reporter(human=True))
            self.assertIn("DD mode", str(ctx.exception))

    def test_partition_commands(self):
        recorded = []

        class FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run_step(rep, cancel, cmd, **kwargs):
            recorded.append([str(c) for c in cmd])
            return FakeProc()

        original_run_step = engine.run_step
        original_wait = engine.devices.wait_for_partition
        original_uefi = engine.install_uefi_ntfs
        engine.run_step = fake_run_step
        engine.devices.wait_for_partition = lambda device, number: "/dev/fake%d" % number
        engine.install_uefi_ntfs = lambda job, part, rep, cancel: recorded.append(
            ["install-uefi-ntfs", part]
        )
        try:
            job = engine.Job(
                image="x", device="/dev/fake", scheme="gpt", target="bios",
                filesystem="FAT32", label="TEST", split_wim=False,
            )
            main, support, index = engine.create_partitions(
                job, images.ImageInfo(path="x", size=1, label=""), engine.Reporter(human=True),
                engine.CancelToken(), windows=False,
            )
            self.assertEqual(index, 2)
            self.assertEqual(main, "/dev/fake2")
            self.assertIsNone(support)
            flat = [" ".join(cmd) for cmd in recorded]
            self.assertTrue(any("mklabel gpt" in line for line in flat))
            self.assertTrue(any("bios_grub on" in line for line in flat))
            self.assertTrue(any("mkfs.vfat" in line for line in flat))

            # NTFS + UEFI needs a trailing UEFI:NTFS partition that is at
            # least as large as the 1 MiB bootloader image.
            recorded.clear()
            job = engine.Job(
                image="x", device="/dev/fake", scheme="mbr", target="uefi",
                filesystem="NTFS", label="WIN", split_wim=False,
            )
            main, support, index = engine.create_partitions(
                job, images.ImageInfo(path="x", size=1, label=""), engine.Reporter(human=True),
                engine.CancelToken(), windows=True,
            )
            self.assertEqual(index, 1)
            self.assertEqual(main, "/dev/fake1")
            self.assertEqual(support, "/dev/fake2")
            mkpart = [cmd for cmd in recorded if "mkpart" in cmd]
            data_cmd = [c for c in mkpart if "4MiB" in c][0]
            self.assertIn("ntfs", data_cmd)
            self.assertIn("-8193s", data_cmd)
            self.assertEqual(data_cmd[data_cmd.index("4MiB") - 1], "--")
            support_cmd = [c for c in mkpart if "-8192s" in c][0]
            self.assertIn("-1s", support_cmd)
            self.assertEqual([c for c in recorded if c[0] == "install-uefi-ntfs"],
                             [["install-uefi-ntfs", "/dev/fake2"]])

            # On GPT the support partition must stop before the backup header.
            recorded.clear()
            job = engine.Job(
                image="x", device="/dev/fake", scheme="gpt", target="uefi",
                filesystem="NTFS", label="WIN", split_wim=False,
            )
            main, support, index = engine.create_partitions(
                job, images.ImageInfo(path="x", size=1, label=""), engine.Reporter(human=True),
                engine.CancelToken(), windows=True,
            )
            self.assertEqual((main, support, index), ("/dev/fake1", "/dev/fake2", 1))
            support_cmd = [c for c in recorded if "mkpart" in c and "-8192s" in c][0]
            self.assertIn("-34s", support_cmd)
            self.assertNotIn("-1s", support_cmd)
        finally:
            engine.run_step = original_run_step
            engine.devices.wait_for_partition = original_wait
            engine.install_uefi_ntfs = original_uefi

    def test_source_completeness_guard(self):
        entries = {
            "sources/install.wim": images.Entry("sources/install.wim", 100, False),
            "bootmgr": images.Entry("bootmgr", 10, False),
        }
        info = images.ImageInfo(
            path="x", size=1, label="", kind="windows",
            install_image="sources/install.wim", entries=entries, listing_ok=True,
        )
        with tempfile.TemporaryDirectory() as root:
            # A Windows ISO's ISO 9660 stub only contains README.TXT.
            with open(os.path.join(root, "README.TXT"), "w") as handle:
                handle.write("stub")
            self.assertFalse(engine._source_looks_complete(root, info))
            os.makedirs(os.path.join(root, "sources"))
            for name, payload in (("sources/install.wim", b"wim"), ("bootmgr", b"boot")):
                with open(os.path.join(root, name), "wb") as handle:
                    handle.write(payload)
            self.assertTrue(engine._source_looks_complete(root, info))

    def test_mount_prefers_udf(self):
        calls = []

        class FakeProc:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append([str(c) for c in cmd])
            if cmd[0] == "mount":
                mountpoint = cmd[-1]
                os.makedirs(mountpoint, exist_ok=True)
                with open(os.path.join(mountpoint, "marker"), "w") as handle:
                    handle.write("x")
            return FakeProc()

        original = engine.util.run
        engine.util.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path, mountpoint = engine.mount_source(
                    "/tmp/not-a-real.iso", tmp, engine.Reporter(human=True),
                    engine.CancelToken(), None,
                )
                self.assertEqual(path, mountpoint)
                mounts = [c for c in calls if c[0] == "mount"]
                self.assertTrue(mounts)
                self.assertEqual(mounts[0][:4], ["mount", "-t", "udf", "-o"])
        finally:
            engine.util.run = original


if __name__ == "__main__":
    unittest.main()
