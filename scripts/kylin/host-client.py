#!/usr/bin/python3
"""Launch a managed Codex desktop client outside Cockpit's private libraries."""
import json
import os
from pathlib import Path
import sys


def host_environment(prefix, program, source):
    env = dict(source)
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "OPENSSL_MODULES",
                "GIO_EXTRA_MODULES", "GTK_PATH", "GTK_EXE_PREFIX", "GTK_DATA_PREFIX",
                "GDK_PIXBUF_MODULE_FILE", "GTK_MODULES", "GTK_THEME", "GDK_BACKEND",
                "LIBGL_ALWAYS_SOFTWARE", "WEBKIT_DISABLE_DMABUF_RENDERER",
                "NODE_OPTIONS", "NODE_PATH", "NODE_ENV", "ELECTRON_RUN_AS_NODE",
                "ELECTRON_NO_ASAR"):
        env.pop(key, None)
    env["PATH"] = env.pop("COCKPIT_KYLIN_HOST_PATH", "/usr/local/bin:/usr/bin:/bin")
    env.pop("COCKPIT_KYLIN_RUNTIME", None)
    # The isolated CODEX_HOME and Electron profile supplied by Cockpit must
    # survive. Never substitute a parallel launcher's shared account directory.
    resources = program.parent / "resources/codex"
    if resources.is_file():
        env["CODEX_CLI_PATH"] = str(resources)
    else:
        env.pop("CODEX_CLI_PATH", None)
    openssl = program.parents[3] / "compat/openssl/lib"
    if (openssl / "libssl.so.3").is_file():
        env["LD_LIBRARY_PATH"] = str(openssl)
        env["OPENSSL_MODULES"] = str(openssl / "ossl-modules")
    return env


def main():
    prefix = Path(os.environ["COCKPIT_KYLIN_RUNTIME"])
    on_host = sys.argv[1:2] == ["--on-host"]
    program = Path(sys.argv[2] if on_host else sys.argv[0])
    with (prefix / "installation.json").open() as handle:
        allowed = json.load(handle).get("hostClients", [])
    if str(program) not in allowed:
        raise SystemExit("Desktop client is not registered in installation.json")
    if not on_host:
        args = ["/usr/bin/bwrap", "--bind", str(prefix / "host"), "/",
                "--dev-bind", "/dev", "/dev", "--", "/usr/bin/python3",
                str(prefix / "host-client.py"), "--on-host", str(program), *sys.argv[1:]]
        os.execv(args[0], args)
    env = host_environment(prefix, program, os.environ)
    arguments = sys.argv[3:]
    # Kylin V10's GTK and older Mesa require the GTK3/software path.
    for flag in ("--gtk-version=3", "--disable-setuid-sandbox", "--disable-gpu"):
        if flag not in arguments:
            arguments.append(flag)
    os.execve(str(program), [str(program), *arguments], env)


if __name__ == "__main__":
    main()
