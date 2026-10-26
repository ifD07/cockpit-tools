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
host_client = load("host-client")


class RuntimeTests(unittest.TestCase):
    def test_host_client_preserves_isolated_login_and_uses_own_cli_and_openssl(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            program = root / "usr/lib/chatgpt/ChatGPT"
            (program.parent / "resources").mkdir(parents=True)
            (program.parent / "resources/codex").touch()
            (root / "compat/openssl/lib").mkdir(parents=True)
            (root / "compat/openssl/lib/libssl.so.3").touch()
            env = host_client.host_environment(root, program, {
                "CODEX_HOME": "/isolated/home", "CODEX_ELECTRON_USER_DATA_PATH": "/isolated/data",
                "COCKPIT_KYLIN_HOST_PATH": "/host/bin:/usr/bin", "PATH": "/private/bin",
                "COCKPIT_KYLIN_RUNTIME": str(root), "LD_LIBRARY_PATH": "/wrong/libs",
                "NODE_OPTIONS": "--bad-option", "ELECTRON_RUN_AS_NODE": "1",
            })
            self.assertEqual(env["CODEX_HOME"], "/isolated/home")
            self.assertEqual(env["CODEX_ELECTRON_USER_DATA_PATH"], "/isolated/data")
            self.assertEqual(env["PATH"], "/host/bin:/usr/bin")
            self.assertEqual(env["CODEX_CLI_PATH"], str(program.parent / "resources/codex"))
            self.assertEqual(env["LD_LIBRARY_PATH"], str(root / "compat/openssl/lib"))
            self.assertNotIn("NODE_OPTIONS", env)
            self.assertNotIn("ELECTRON_RUN_AS_NODE", env)
            self.assertNotIn("COCKPIT_KYLIN_RUNTIME", env)

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

    @unittest.skipUnless(shutil.which("bwrap") and shutil.which("xdg-dbus-proxy"),
                         "Runtime namespace helpers are not installed")
    def test_host_bridge_restores_native_executable_and_writable_isolated_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            binaries = prefix / "runtime/usr/bin"
            binaries.mkdir(parents=True)
            (prefix / "runtime/etc").mkdir()
            (prefix / "host").mkdir()
            (prefix / "runtime/etc/ld.so.cache").symlink_to("/etc/ld.so.cache")
            for helper in ("bwrap", "xdg-dbus-proxy"):
                (binaries / helper).symlink_to(shutil.which(helper))
            (binaries / "cockpit-tools").write_text("host wrapper")
            (binaries / "cockpit-tools.real").symlink_to("/usr/bin/readlink")
            program = prefix / "client/usr/lib/chatgpt/ChatGPT"
            program.parent.mkdir(parents=True)
            shutil.copy2("/bin/sh", program)
            (prefix / "installation.json").write_text(json.dumps({"mounts": []}))
            installer.install_launchers(prefix, Path(__file__).parent, program)
            home = prefix / "isolated-home"
            home.mkdir()
            env = launcher.runtime_environment(prefix, dict(os.environ, CODEX_HOME=str(home),
                CODEX_ELECTRON_USER_DATA_PATH=str(prefix / "isolated-electron")))
            script = 'readlink /proc/$$/exe; printf "%s\\n" "$CODEX_HOME" "$CODEX_ELECTRON_USER_DATA_PATH"; mkdir "$CODEX_HOME/created"'
            result = subprocess.run(launcher.command(prefix, [str(program), "-c", script],
                shutil.which("bwrap")), env=env, text=True, capture_output=True)
            if result.returncode and "Operation not permitted" in result.stderr:
                self.skipTest("User namespaces are disabled on this test host")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [str(program), str(home),
                             str(prefix / "isolated-electron")])
            self.assertTrue((home / "created").is_dir())
            self.assertEqual(program.read_bytes()[:4], b"\x7fELF")

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

    @unittest.skipUnless(shutil.which("glib-compile-schemas") and shutil.which("gsettings"),
                         "GSettings tools are not installed")
    def test_schema_cache_includes_host_desktop_ids_and_runtime_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            prefix = Path(directory)
            host = prefix / "host-schemas"
            runtime = prefix / "runtime/usr/share/glib-2.0/schemas"
            merged = prefix / "merged/usr/share/glib-2.0/schemas"
            for folder in (host, runtime, merged):
                folder.mkdir(parents=True)
            template = '<schemalist><schema id="{}" path="/test/"><key name="value" type="s"><default>"ok"</default></key></schema></schemalist>'
            (host / "host.gschema.xml").write_text(template.format("org.cockpit.test.kylin"))
            (runtime / "runtime.gschema.xml").write_text(template.format("org.cockpit.test.runtime"))
            original = runtime / "gschemas.compiled"
            original.write_bytes(b"original cache remains unchanged")
            (merged / "gschemas.compiled").symlink_to(original)
            installer.rebuild_schema_cache(prefix, host)
            env = dict(os.environ, GSETTINGS_SCHEMA_DIR=str(prefix / "gsettings"),
                       GSETTINGS_BACKEND="memory")
            result = subprocess.run(["gsettings", "list-schemas"], env=env,
                                    text=True, capture_output=True, check=True)
            self.assertIn("org.cockpit.test.kylin", result.stdout.splitlines())
            self.assertIn("org.cockpit.test.runtime", result.stdout.splitlines())
            self.assertEqual(original.read_bytes(), b"original cache remains unchanged")
            self.assertEqual((merged / "gschemas.compiled").resolve(),
                             prefix / "gsettings/gschemas.compiled")

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
