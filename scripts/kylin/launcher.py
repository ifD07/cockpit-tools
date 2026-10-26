#!/usr/bin/python3
"""Run the upstream app with private libraries in a Bubblewrap mount namespace.

This is a compatibility environment, not a security sandbox. The application
keeps normal access to the user's files, desktop bus, display and network.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def runtime_environment(prefix, source=None):
    env = dict(os.environ if source is None else source)
    # A launcher started by an Electron app can inherit its private OpenSSL.
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "OPENSSL_MODULES",
                "GIO_EXTRA_MODULES", "GTK_PATH", "GTK_EXE_PREFIX",
                "GTK_DATA_PREFIX", "GDK_PIXBUF_MODULE_FILE"):
        env.pop(key, None)
    env["PATH"] = str(prefix / "runtime/usr/bin") + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    env["GTK_MODULES"] = ""
    env.setdefault("GTK_THEME", "Adwaita")
    env.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")
    # Stable on the older Mesa/Hygon stack shipped with Kylin V10.
    env.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
    if env.get("DISPLAY"):
        env.setdefault("GDK_BACKEND", "x11")
    env["COCKPIT_KYLIN_RUNTIME"] = str(prefix)
    return env


def command(prefix, arguments, bwrap):
    with (prefix / "installation.json").open() as handle:
        installation = json.load(handle)
    args = [bwrap, "--bind", "/", "/", "--ro-bind", "/", str(prefix / "host"),
            "--dev-bind", "/dev", "/dev"]
    for target in installation["mounts"]:
        args.extend(["--ro-bind", str(prefix / "merged" / target.lstrip("/")), target])
    args.extend(["--ro-bind", str(prefix / "runtime/etc/ld.so.cache"), "/etc/ld.so.cache"])
    # WebKit launches these sandbox helpers by their compiled absolute paths.
    for helper in ("bwrap", "xdg-dbus-proxy"):
        args.extend(["--ro-bind", str(prefix / "runtime/usr/bin" / helper), "/usr/bin/" + helper])
    # The host-visible path is a wrapper; inside the namespace it is the ELF.
    # Tauri's current_exe/deep-link/relaunch paths therefore remain launchable
    # from the host desktop without inheriting these private library mounts.
    app = prefix / "runtime/usr/bin/cockpit-tools"
    args.extend(["--ro-bind", str(app.with_name("cockpit-tools.real")), str(app)])
    args.extend(["--", *arguments])
    return args


def main():
    prefix = Path(__file__).resolve().parent
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise SystemExit("Bubblewrap is required. Install the Kylin 'bubblewrap' package.")
    env = runtime_environment(prefix)
    app = str(prefix / "runtime/usr/bin/cockpit-tools")
    if sys.argv[1:] == ["--kylin-check"]:
        loader = prefix / "runtime/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
        probe = [str(loader), "--list", app]
        result = subprocess.run(command(prefix, probe, bwrap), env=env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        print(result.stdout, end="")
        if result.returncode or "not found" in result.stdout:
            raise SystemExit(result.returncode or 1)
        print("Kylin runtime dependencies: OK")
        return
    args = command(prefix, [app, *sys.argv[1:]], bwrap)
    os.execve(bwrap, args, env)


if __name__ == "__main__":
    main()
