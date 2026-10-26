import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = load("install")
launcher = load("launcher")


class RuntimeTests(unittest.TestCase):
    def test_merge_keeps_host_only_libraries_and_prefers_matching_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host, runtime, merged = (root / name for name in ("host", "runtime", "merged"))
            for tree in (host, runtime):
                (tree / "plugins").mkdir(parents=True)
                (tree / "plugins/common.so").write_text(tree.name)
            (host / "plugins/kylin-only.so").write_text("host")
            (runtime / "plugins/new.so").symlink_to("common.so")
            installer.merge_tree(host, runtime, merged, Path("/original/usr/lib"))
            self.assertEqual((merged / "plugins/common.so").read_text(), "runtime")
            self.assertEqual(os.readlink(merged / "plugins/kylin-only.so"),
                             "/original/usr/lib/plugins/kylin-only.so")
            self.assertEqual(os.readlink(merged / "plugins/new.so"), "common.so")
            self.assertEqual((merged / "plugins/new.so").read_text(), "runtime")

    def test_parent_electron_library_paths_do_not_leak_into_runtime(self):
        env = launcher.runtime_environment(Path("/app"), {
            "LD_LIBRARY_PATH": "/electron/openssl", "LD_PRELOAD": "/old/lib.so",
            "OPENSSL_MODULES": "/electron/modules", "PATH": "/usr/bin",
            "DISPLAY": ":0", "LIBGL_ALWAYS_SOFTWARE": "0", "HOME": "/home/alice",
        })
        for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "OPENSSL_MODULES"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PATH"], "/app/runtime/usr/bin:/usr/bin")
        self.assertEqual(env["HOME"], "/home/alice")
        self.assertEqual(env["LIBGL_ALWAYS_SOFTWARE"], "0")
        self.assertEqual(env["GDK_BACKEND"], "x11")

    def test_archive_cannot_write_through_an_absolute_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                link = tarfile.TarInfo("usr/escape")
                link.type, link.linkname = tarfile.SYMTYPE, "/tmp"
                tar.addfile(link)
                payload = tarfile.TarInfo("usr/escape/unexpected-file")
                payload.size = 4
                tar.addfile(payload, io.BytesIO(b"test"))
            with self.assertRaises(ValueError):
                installer.extract_runtime(archive, root / "out")

    def test_normal_runtime_links_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "good.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                link = tarfile.TarInfo("usr/lib/ssl/certs")
                link.type, link.linkname = tarfile.SYMTYPE, "/etc/ssl/certs"
                tar.addfile(link)
            installer.extract_runtime(archive, root / "out")
            self.assertEqual(os.readlink(root / "out/usr/lib/ssl/certs"), "/etc/ssl/certs")

    def test_archive_cannot_hardlink_to_host_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "hardlink.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                link = tarfile.TarInfo("usr/host-file")
                link.type, link.linkname = tarfile.LNKTYPE, "/etc/passwd"
                tar.addfile(link)
            with self.assertRaises(ValueError):
                installer.extract_runtime(archive, root / "out")

    @unittest.skipUnless(shutil.which("bwrap"), "Bubblewrap is not installed")
    def test_current_exe_path_is_a_host_launcher_and_an_elf_inside_namespace(self):
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / "entry"
            entry.write_text("host launcher")
            result = subprocess.run([
                "bwrap", "--bind", "/", "/", "--ro-bind", "/usr/bin/readlink", str(entry),
                "--", str(entry), "/proc/self/exe",
            ], text=True, capture_output=True)
            if result.returncode and "Operation not permitted" in result.stderr:
                self.skipTest("User namespaces are disabled on this test host")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), str(entry))
            self.assertEqual(entry.read_text(), "host launcher")

    def test_desktop_exec_escapes_field_codes_and_metacharacters(self):
        value = installer.desktop_exec(Path('/home/a name/100%/$tool"'))
        self.assertEqual(value, '"/home/a name/100%%/\\$tool\\\""')

    @unittest.skipUnless(shutil.which("bwrap") and shutil.which("xdg-dbus-proxy"),
                         "Runtime namespace helpers are not installed")
    def test_launch_command_preserves_dev_null_for_child_programs(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            binaries = prefix / "runtime/usr/bin"
            binaries.mkdir(parents=True)
            (prefix / "runtime/etc").mkdir()
            (prefix / "host").mkdir()
            (prefix / "installation.json").write_text(json.dumps({"mounts": []}))
            (prefix / "runtime/etc/ld.so.cache").symlink_to("/etc/ld.so.cache")
            for helper in ("bwrap", "xdg-dbus-proxy"):
                (binaries / helper).symlink_to(shutil.which(helper))
            (binaries / "cockpit-tools").write_text("host wrapper")
            (binaries / "cockpit-tools.real").symlink_to("/usr/bin/readlink")
            args = launcher.command(prefix, ["/usr/bin/python3", "-c",
                'with open("/dev/null", "rb") as handle: assert handle.read() == b""'],
                shutil.which("bwrap"))
            result = subprocess.run(args, text=True, capture_output=True)
            if result.returncode and "Operation not permitted" in result.stderr:
                self.skipTest("User namespaces are disabled on this test host")
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
