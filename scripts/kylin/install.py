#!/usr/bin/python3
"""Install a verified private runtime without root or changing system libraries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import tarfile
import tempfile


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def merge_tree(host, runtime, target, mirror):
    """Prefer runtime files and retain host-only libraries and resources.

    Host fallback links point at a view of the original filesystem,
    avoiding self-referential links after the merged directory is mounted.
    """
    target.mkdir(parents=True, exist_ok=True)
    host_names = {item.name for item in host.iterdir()} if host.is_dir() else set()
    runtime_names = {item.name for item in runtime.iterdir()} if runtime.is_dir() else set()
    for name in sorted(host_names | runtime_names):
        h, r, dest = host / name, runtime / name, target / name
        if name not in runtime_names:
            dest.symlink_to(mirror / name)
        elif r.is_symlink():
            dest.symlink_to(os.readlink(r))
        elif r.is_dir():
            merge_tree(h, r, dest, mirror / name)
        else:
            dest.symlink_to(r)


def extract_runtime(archive, destination):
    # Only extract trusted builder outputs. Refuse traversal, special files and
    # links escaping the private directory (except normal absolute /usr links).
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        regular_files = {str(Path(m.name)) for m in members if m.isfile()}
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Unsafe runtime member: " + member.name)
            if not (member.isdir() or member.isfile() or member.issym() or member.islnk()):
                raise ValueError("Unsupported runtime member: " + member.name)
            if member.islnk():
                link = Path(member.linkname)
                if link.is_absolute() or ".." in link.parts or str(link) not in regular_files:
                    raise ValueError("Unsafe runtime hardlink: " + member.name)
            if member.issym():
                link = Path(member.linkname)
                if ".." in link.parts:
                    # Ubuntu uses relative links such as ../../../etc/ssl.
                    # They are allowed only if they do not resolve above root.
                    base = Path("/") / path.parent
                    normalized = Path(os.path.normpath(str(base / link)))
                    if not str(normalized).startswith(("/usr/", "/etc/", "/lib")):
                        raise ValueError("Unsafe runtime link: " + member.name)
        # Ubuntu's absolute links must not be followed by a later archive entry.
        links = {str(Path(m.name)) for m in members if m.issym()}
        for member in members:
            if any(str(parent) in links for parent in Path(member.name).parents):
                raise ValueError("Runtime entry traverses a symlink: " + member.name)
        tar.extractall(destination, members=members)


def desktop_exec(path):
    # Desktop Entry quoting differs from shell quoting; double '%' field codes.
    escaped = str(path).replace("%", "%%")
    for char in ("\\", '"', "`", "$"):
        escaped = escaped.replace(char, "\\" + char)
    return '"' + escaped + '"'


def rebuild_schema_cache(prefix, host_schemas=Path("/usr/share/glib-2.0/schemas")):
    """Compile the merged XML set: a precompiled Ubuntu cache drops Kylin IDs."""
    compiler = shutil.which("glib-compile-schemas")
    if not compiler:
        raise RuntimeError("Install the libglib2.0-bin package (glib-compile-schemas).")
    runtime_schemas = prefix / "runtime/usr/share/glib-2.0/schemas"
    merged_schemas = prefix / "merged/usr/share/glib-2.0/schemas"
    if not runtime_schemas.is_dir() or not merged_schemas.is_dir():
        raise RuntimeError("Not a Kylin runtime installation: " + str(prefix))
    cache_dir = prefix / "gsettings"
    cache_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".schema-build-", dir=prefix) as directory:
        stage = Path(directory)
        # Match merge_tree's precedence, but compile every host-only schema too.
        for source in (host_schemas, runtime_schemas):
            for pattern in ("*.gschema.xml", "*.gschema.override", "*.enums.xml"):
                for path in source.glob(pattern):
                    shutil.copyfile(path, stage / path.name)
        result = subprocess.run([compiler, "--strict", str(stage)], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError("GSettings schema compilation failed:\n" + result.stdout)
        pending = cache_dir / "gschemas.compiled.new"
        shutil.copyfile(stage / "gschemas.compiled", pending)
        os.replace(pending, cache_dir / "gschemas.compiled")
    pending_link = merged_schemas / ".gschemas.compiled.kylin"
    pending_link.symlink_to(cache_dir / "gschemas.compiled")
    os.replace(pending_link, merged_schemas / "gschemas.compiled")


def install_launchers(prefix, source, codex_client=None):
    metadata = prefix / "installation.json"
    installation = json.loads(metadata.read_text())
    if codex_client:
        program = codex_client.resolve(strict=True)
        with program.open("rb") as handle:
            if handle.read(4) != b"\x7fELF":
                raise ValueError("--codex-client must point to the native ELF, not a shell launcher")
        if not os.access(program, os.X_OK):
            raise ValueError("Codex client is not executable: " + str(program))
        installation["hostClients"] = [str(program)]
    for name in ("launcher.py", "host-client.py"):
        pending = prefix / (name + ".new")
        shutil.copy2(source / name, pending)
        pending.chmod(0o755)
        os.replace(pending, prefix / name)
    pending = prefix / "installation.json.new"
    pending.write_text(json.dumps(installation, indent=2) + "\n")
    os.replace(pending, metadata)


def rebuild_input_method_cache(prefix):
    """Query the merged GTK modules using the same libraries as the app."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("kylin_launcher", prefix / "launcher.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    relative = Path("usr/lib/x86_64-linux-gnu/gtk-3.0/3.0.0")
    modules = sorted((prefix / "merged" / relative / "immodules").glob("*.so"))
    query = "/usr/lib/x86_64-linux-gnu/libgtk-3-0/gtk-query-immodules-3.0"
    arguments = [query, *["/" + str(relative / "immodules" / path.name) for path in modules]]
    result = subprocess.run(launcher.command(prefix, arguments, shutil.which("bwrap")),
                            env=launcher.runtime_environment(prefix), text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    # Querying can return success even when one module fails to load.
    if '"fcitx"' not in result.stdout and any(p.name == "im-fcitx.so" for p in modules):
        raise RuntimeError("Fcitx GTK module failed to load: " + result.stderr)
    cache = prefix / "gtk-immodules.cache"
    pending = prefix / "gtk-immodules.cache.new"
    pending.write_text(result.stdout)
    os.replace(pending, cache)
    merged_cache = prefix / "merged" / relative / "immodules.cache"
    pending_link = merged_cache.with_name(".immodules.cache.kylin")
    pending_link.symlink_to(cache)
    os.replace(pending_link, merged_cache)


def install_built_app(prefix, binary, expected_sha256):
    if not expected_sha256 or sha256(binary) != expected_sha256.lower():
        raise ValueError("Built app SHA256 mismatch (use --app-sha256)")
    with binary.open("rb") as handle:
        if handle.read(4) != b"\x7fELF":
            raise ValueError("Built app must be an ELF executable")
    import importlib.util
    spec = importlib.util.spec_from_file_location("kylin_launcher", prefix / "launcher.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    app = prefix / "runtime/usr/bin/cockpit-tools.real"
    pending = app.with_name(app.name + ".new")
    shutil.copy2(binary, pending)
    pending.chmod(0o755)
    loader = prefix / "runtime/usr/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
    probe = subprocess.run(launcher.command(prefix, [str(loader), "--list", str(pending)],
                           shutil.which("bwrap")), env=launcher.runtime_environment(prefix),
                           text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if probe.returncode or "not found" in probe.stdout:
        raise RuntimeError("Built app dependency check failed:\n" + probe.stdout)
    backup = app.with_name(app.name + ".backup-" + sha256(app)[:12])
    if not backup.exists():
        shutil.copy2(app, backup)
    os.replace(pending, app)
    metadata = prefix / "installation.json"
    installation = json.loads(metadata.read_text())
    installation["localBinarySha256"] = expected_sha256.lower()
    metadata.write_text(json.dumps(installation, indent=2) + "\n")
    print("Built app installed; rollback binary:", backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path, nargs="?")
    parser.add_argument("--sha256", help="SHA256 from the trusted build output")
    parser.add_argument("--prefix", type=Path, help="New, unused installation directory")
    parser.add_argument("--refresh-schemas", type=Path, help="Repair an existing installation's GSettings cache")
    parser.add_argument("--codex-client", type=Path, help="Native Codex/ChatGPT ELF (not a parallel-profile launcher)")
    parser.add_argument("--repair", type=Path, help="Update launchers and schemas of an existing installation")
    parser.add_argument("--app-binary", type=Path, help="Install a locally compiled app during --repair (exit Cockpit first)")
    parser.add_argument("--app-sha256", help="SHA256 of --app-binary")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run this installer as the desktop user, without sudo.")
    if args.refresh_schemas or args.repair:
        prefix = (args.repair or args.refresh_schemas).resolve()
        if not (prefix / "installation.json").is_file():
            parser.error("Missing installation.json: " + str(prefix))
        rebuild_schema_cache(prefix)
        if args.repair:
            install_launchers(prefix, Path(__file__).resolve().parent, args.codex_client)
            rebuild_input_method_cache(prefix)
            if args.app_binary:
                install_built_app(prefix, args.app_binary.resolve(), args.app_sha256)
        print("Merged GSettings cache rebuilt:", prefix)
        return
    if not args.runtime or not args.sha256:
        parser.error("runtime and --sha256 are required for a new installation.")
    if platform.machine() != "x86_64":
        parser.error("Only x86_64 / Hygon x86 is currently supported.")
    if not shutil.which("bwrap"):
        parser.error("Install Kylin's bubblewrap package first.")
    subprocess.run(["bwrap", "--ro-bind", "/", "/", "--", "/usr/bin/true"], check=True)
    if sha256(args.runtime) != args.sha256.lower():
        parser.error("Runtime SHA256 mismatch.")
    source = Path(__file__).resolve().parent
    with (source / "upstream.json").open() as handle:
        upstream = json.load(handle)
    prefix = (args.prefix or Path.home() / ".local/opt/cockpit-tools" / (upstream["version"] + "-kylin1")).resolve()
    if prefix.exists():
        parser.error("Installation directory already exists; choose a new --prefix: " + str(prefix))
    link = Path.home() / ".local/bin/cockpit-tools"
    if link.exists() or link.is_symlink():
        parser.error("Launcher already exists: " + str(link))
    if Path("/lib").resolve() != Path("/usr/lib") or Path("/lib64").resolve() != Path("/usr/lib64"):
        parser.error("This installer requires a merged-/usr layout (/lib -> /usr/lib).")
    if not Path("/usr/libexec").is_dir() or not Path("/usr/bin/xdg-dbus-proxy").is_file():
        parser.error("The host needs /usr/libexec and the xdg-dbus-proxy package.")
    prefix.mkdir(parents=True, mode=0o700)
    runtime = prefix / "runtime"
    extract_runtime(args.runtime, runtime)
    # /lib and /lib64 are /usr symlinks on Kylin 2403 and Ubuntu 22.04.
    # Refuse a different layout instead of silently loading mismatched libc.
    mounts = ["/usr/lib", "/usr/lib64", "/usr/libexec", "/usr/share"]
    (prefix / "host").mkdir()
    for target in mounts:
        relative = target.lstrip("/")
        merge_tree(Path(target), runtime / relative, prefix / "merged" / relative,
                   prefix / "host" / relative)
    rebuild_schema_cache(prefix)
    app = runtime / "usr/bin/cockpit-tools"
    app.rename(app.with_name("cockpit-tools.real"))
    app.write_text("#!/bin/sh\nexec " + shlex.quote(str(prefix / "launcher.py")) + ' "$@"\n')
    app.chmod(0o755)
    (prefix / "installation.json").write_text(json.dumps({
        "upstream": upstream, "runtimeSha256": args.sha256, "mounts": mounts,
        "method": "bubblewrap-private-runtime", "systemLibrariesChanged": False,
    }, indent=2) + "\n")
    install_launchers(prefix, source, args.codex_client)
    rebuild_input_method_cache(prefix)
    subprocess.run([str(prefix / "launcher.py"), "--kylin-check"], check=True)
    bin_dir = Path.home() / ".local/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    link.symlink_to(prefix / "launcher.py")
    applications = Path.home() / ".local/share/applications"
    applications.mkdir(parents=True, exist_ok=True)
    desktop = applications / "cockpit-tools-kylin.desktop"
    icon = runtime / "usr/share/icons/hicolor/128x128/apps/cockpit-tools.png"
    if not icon.exists():
        icon = next((runtime / "usr/share/icons").rglob("cockpit-tools.png"))
    desktop.write_text("\n".join([
        "[Desktop Entry]", "Type=Application", "Name=Cockpit Tools", "Name[zh_CN]=Cockpit Tools",
        "Comment=AI IDE account manager (Kylin compatible runtime)",
        "Exec=" + desktop_exec(link) + " %u", "Icon=" + str(icon),
        "Terminal=false", "Categories=Development;Utility;", "StartupWMClass=cockpit-tools",
        "MimeType=x-scheme-handler/cockpit-tools;x-scheme-handler/cockpittools;x-scheme-handler/zcode;", "",
    ]))
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", str(applications)], check=False)
    print("Installed:", prefix)
    print("Launch:", link)


if __name__ == "__main__":
    main()
