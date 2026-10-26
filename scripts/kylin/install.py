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


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def merge_tree(host, runtime, target, mirror):
    """Prefer runtime files and retain host-only libraries and resources.

    Host fallback links point at a read-only view of the original filesystem,
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--sha256", required=True, help="SHA256 from the trusted build output")
    parser.add_argument("--prefix", type=Path, help="New, unused installation directory")
    args = parser.parse_args()
    if os.geteuid() == 0:
        parser.error("Run this installer as the desktop user, without sudo.")
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
    shutil.copy2(source / "launcher.py", prefix / "launcher.py")
    (prefix / "launcher.py").chmod(0o755)
    app = runtime / "usr/bin/cockpit-tools"
    app.rename(app.with_name("cockpit-tools.real"))
    app.write_text("#!/bin/sh\nexec " + shlex.quote(str(prefix / "launcher.py")) + ' "$@"\n')
    app.chmod(0o755)
    (prefix / "installation.json").write_text(json.dumps({
        "upstream": upstream, "runtimeSha256": args.sha256, "mounts": mounts,
        "method": "bubblewrap-private-runtime", "systemLibrariesChanged": False,
    }, indent=2) + "\n")
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
