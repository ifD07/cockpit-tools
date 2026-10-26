#!/usr/bin/python3
"""Run the production Linux process scanner tests without linking the GUI.

Run in the Rust builder after cargo build has produced sysinfo/tempfile rlibs.
Functions and tests are extracted verbatim; no mock process scanner is used.
"""
import argparse
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


def extract(text, name, module=False):
    kind = "mod" if module else r"(?:pub )?fn"
    match = re.search(r"(?ms)^" + kind + r" " + re.escape(name)
                      + r"\b.*?^\}", text)
    if not match:
        raise RuntimeError("Production function/test not found: " + name)
    return match.group(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deps", type=Path, default=Path("target/x86_64-unknown-linux-gnu/release/deps"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    modules = root / "src-tauri/src/modules"
    paths = (modules / "process_path_resolution.rs").read_text()
    lifecycle = (modules / "process_close_lifecycle.rs").read_text()
    source = '\n'.join([
        'use std::{thread, process::Command, time::{Duration, Instant}, collections::{HashMap, HashSet}};',
        'use sysinfo::{System, ProcessRefreshKind, UpdateKind};',
        'include!("' + str(modules / "process_codex_linux_guard.rs") + '");',
        extract(paths, "normalize_path_for_compare"),
        extract(paths, "is_linux_chromium_helper_tokens"),
        extract(paths, "is_pid_running"),
        extract(paths, "split_command_tokens"),
        'include!("' + str(modules / "process_codex_app_server.rs") + '");',
        extract(lifecycle, "collect_codex_process_entries_linux_for_launch"),
        extract(lifecycle, "linux_codex_process_scan_tests", module=True),
    ])
    compiler = shutil.which("rustc")
    if not compiler:
        raise SystemExit("rustc is required")
    dependencies = args.deps.resolve()
    with tempfile.TemporaryDirectory(prefix="cockpit-linux-guard-") as directory:
        stage = Path(directory)
        harness = stage / "guard.rs"
        harness.write_text(source)
        command = [compiler, "--edition=2021", "--test", str(harness),
                   "-L", "dependency=" + str(dependencies), "-o", str(stage / "tests")]
        for name in ("sysinfo", "tempfile"):
            files = sorted(dependencies.glob("lib" + name + "-*.rlib"))
            if len(files) != 1:
                raise RuntimeError("Expected one compiled " + name + " rlib in " + str(dependencies))
            command += ["--extern", name + "=" + str(files[0])]
        subprocess.run(command, check=True)
        subprocess.run([str(stage / "tests"), "--test-threads=1"], check=True)


if __name__ == "__main__":
    main()
