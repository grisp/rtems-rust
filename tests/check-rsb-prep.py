#!/usr/bin/env python3
"""Check real RSB preparation commands without installing or compiling tools."""

import argparse
import json
from pathlib import Path
import platform
import re
import runpy
import shlex
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def check_host(sdk, cache, host, directory, extract, reuse=False):
    rsb = sdk / "external/rtems-source-builder"
    directory.mkdir()
    log = directory / "rsb.log"
    command = [str(rsb / "source-builder/sb-set-builder"),
               "--dry-run", "--no-download", "--no-install", "--no-report",
               "--topdir=" + str(directory), "--prefix=" + str(directory / "sdk"),
               "--sourcedir=" + str(cache), "--patchdir=" + str(ROOT / "patches"),
               "--configdir=" + ":".join(str(p) for p in [
                   ROOT / "config", rsb / "source-builder/config",
                   rsb / "bare/config", rsb / "rtems/config"]),
               "--with-rust-project=" + str(ROOT), "--with-rust-host=" + host,
               "--with-rust-c-prefix=" + str(directory / "c-sdk"),
               "--with-rust-jobs=2", "--log=" + str(log), "grisp2/grisp2-rust-tools.bset"]
    if reuse:
        command.insert(-1, "--with-rust-reuse")
    result = subprocess.run(command, cwd=directory, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    lines = [match.group(1) for line in log.read_text().splitlines()
             if (match := re.match(r"script:\s*\d+: (.*)", line))]
    helper = next(line for line in lines if "/tools/bootstrap-config" in line)
    if '--target-cflags=-march=armv7-a -mfpu=neon -mfloat-abi=hard' not in shlex.split(helper):
        raise AssertionError("target C flags were not passed to preparation")
    if reuse:
        retained = directory / "build" / ("rtems-rust-" + host + "-1.100.0")
        scratch = Path(next(line for line in lines if line.startswith('SB_BUILD_DIR=')).split('=', 1)[1].strip('"'))
        if scratch != directory / "rsb" / retained.name:
            raise AssertionError("RSB would recreate the retained compiler directory")
        if any("sh install.sh" in line or " -dc " in line for line in lines):
            raise AssertionError("reuse unexpectedly extracts sources or reinstalls stage0")
        if '--reuse' not in shlex.split(helper):
            raise AssertionError("reuse must validate the retained inputs")
        for component in ("stage0/bin/cargo", "cargo-home"):
            if not any(str(retained / component) in line for line in lines):
                raise AssertionError("reuse does not retain " + component)
        print(host + ": source/objects retained with a separate RSB scratch directory", flush=True)
        return
    installs = [i for i, line in enumerate(lines) if line.startswith("sh install.sh ")]
    if len(installs) != 3:
        raise AssertionError(f"{host}: expected three stage0 installers, got {len(installs)}")
    entries = json.loads((ROOT / "sources.json").read_text())["stage0"][host]
    for entry, index in zip(entries, installs):
        previous_cd = next(line for line in reversed(lines[:index]) if line.startswith("cd "))
        expected = entry["archive"].removesuffix(".tar.xz")
        if shlex.split(previous_cd) != ["cd", expected]:
            raise AssertionError(f"{host}/{entry['component']}: installer follows {previous_cd!r}; "
                                 f"expected cd {expected}")
    if extract:
        # Execute only the generated archive preparation section. Each actual
        # installer invocation is replaced by an existence check, so this never
        # installs or runs a compiler, and only writes inside our temporary tree.
        start = next(i for i, line in enumerate(lines) if entries[0]["archive"] in line)
        start = max(i for i in range(start) if lines[i].startswith("cd /"))
        build_dir = Path(shlex.split(lines[start])[1])
        if not build_dir.is_relative_to(directory):
            raise AssertionError("preparation escaped the temporary test tree")
        build_dir.mkdir(parents=True)
        commands = ["test -f install.sh" if line.startswith("sh install.sh ") else line
                    for line in lines[start:installs[-1] + 1]]
        script = "set -eu\n" + "\n".join(commands) + "\n"
        with (directory / "extract.log").open("w") as output:
            subprocess.run(["sh", "-c", script], cwd=directory, stdout=output,
                           stderr=subprocess.STDOUT, check=True)
    print(host + ": stage0 installer directories verified" +
          ("; real archives extracted and installers found" if extract else ""), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--extract-native", action="store_true")
    args = parser.parse_args()
    native = runpy.run_path(str(ROOT / "tools/bootstrap-config"))["host_triple"](
        platform.system(), platform.machine())
    manifest = json.loads((ROOT / "sources.json").read_text())
    with tempfile.TemporaryDirectory(prefix="rtems-rust-rsb-prep-") as directory:
        for host in manifest["stage0"]:
            check_host(args.sdk.resolve(), args.cache.resolve(), host, Path(directory) / host,
                       args.extract_native and host == native)
            check_host(args.sdk.resolve(), args.cache.resolve(), host, Path(directory) / (host + "-reuse"),
                       False, reuse=True)


if __name__ == "__main__":
    main()
