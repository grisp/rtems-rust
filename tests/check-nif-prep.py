#!/usr/bin/env python3
"""Run NIF recipe preparation and frozen metadata checks on temporary sources."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--cargo", type=Path, required=True)
    parser.add_argument("--rustc", type=Path, required=True)
    parser.add_argument("--test-bundler", action="store_true")
    args = parser.parse_args()
    sdk, cache = args.sdk.resolve(), args.cache.resolve()
    rsb = sdk / "external/rtems-source-builder"
    pins = json.loads((ROOT / "sources.json").read_text())
    with tempfile.TemporaryDirectory(prefix="rtems-rust-nif-prep-") as temporary:
        directory = Path(temporary)
        log = directory / "rsb.log"
        command = [str(rsb / "source-builder/sb-set-builder"),
                   "--dry-run", "--no-download", "--no-install", "--no-report",
                   "--topdir=" + str(directory), "--prefix=" + str(directory / "sdk"),
                   "--sourcedir=" + str(cache), "--patchdir=" + str(ROOT / "patches"),
                   "--configdir=" + ":".join(str(p) for p in [
                       ROOT / "config", rsb / "source-builder/config",
                       rsb / "bare/config", rsb / "rtems/config"]),
                   "--with-rust-project=" + str(ROOT), "--with-rust-jobs=2", "--log=" + str(log),
                   "grisp2/grisp2-rust-nif-support.bset"]
        result = subprocess.run(command, cwd=directory, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        lines = [match.group(1) for line in log.read_text().splitlines()
                 if (match := re.match(r"script:\s*\d+: (.*)", line))]
        boundary = next(i for i, line in enumerate(lines) if line.startswith('echo "==> clean'))
        prep = lines[:boundary]
        build_dir = Path(next(line for line in lines if line.startswith("SB_BUILD_DIR=")).split("=", 1)[1].strip('"'))
        if not build_dir.is_relative_to(directory):
            raise AssertionError("preparation escaped the temporary tree")
        build_dir.mkdir(parents=True)
        with (directory / "prep.log").open("w") as output:
            result = subprocess.run(["/bin/sh", "-ex", "-c", "\n".join(prep)], cwd=directory,
                                    stdout=output, stderr=subprocess.STDOUT)
        if result.returncode:
            raise RuntimeError((directory / "prep.log").read_text()[-6000:])
        rustler = build_dir / ("rustler-" + pins["components"]["rustler"]["base"])
        inventory = build_dir / ("inventory-" + pins["components"]["inventory"]["base"])
        env = {**os.environ, "CARGO_HOME": str(directory / "cargo-home"),
               "CARGO_TARGET_DIR": str(directory / "target"), "CARGO_NET_OFFLINE": "true",
               "RUSTC": str(args.rustc.resolve()),
               "PATH": str(args.cargo.resolve().parent) + os.pathsep + os.environ.get("PATH", os.defpath)}
        cargo = str(args.cargo.resolve())
        metadata = json.loads(subprocess.check_output(
            [cargo, "metadata", "--frozen", "--format-version", "1"], cwd=rustler, env=env))
        package = next(p for p in metadata["packages"] if p["name"] == "inventory")
        if package["source"] is not None or Path(package["manifest_path"]).parent != inventory:
            raise AssertionError("prepared NIF graph does not select the patched inventory")
        print("Real RSB NIF preparation and frozen offline metadata checks passed", flush=True)
        if args.test_bundler:
            contaminated = {**env, "CARGO_TARGET_DIR": str(directory / "wrong-target"),
                "CARGO_BUILD_TARGET": "wrong-target", "RUSTFLAGS": "wrong-flags",
                "RUSTC_WRAPPER": "/wrong/wrapper", "RUSTUP_TOOLCHAIN": "wrong-toolchain"}
            subprocess.run(["python3", str(ROOT / "tools/build-env"),
                "--cargo-home", str(directory / "cargo-home"),
                "--target-dir", str(directory / "target"), "--jobs", "2",
                "--rustc", str(args.rustc.resolve()), "--",
                cargo, "build", "--frozen", "--release", "-p", "rustler_bundler"],
                cwd=rustler, env=contaminated, check=True)
            binary = directory / "target/release/rustler-bundler"
            if not binary.is_file():
                raise AssertionError("release bundler is missing from the package's expected output path")
            stage = directory / "stage"
            (stage / "bin").mkdir(parents=True)
            shutil.copy2(binary, stage / "bin/rustler-bundler")
            # Exercise packaging against the newly pinned sources without
            # rebuilding the compiler or modifying the installed SDK.
            installed = args.cargo.resolve().parent.parent
            core = json.loads((installed / "share/rtems/rust-support.json").read_text())
            for program in ("cargo", "rustc"):
                (stage / "bin" / program).symlink_to(installed / "bin" / program)
            (stage / "lib").symlink_to(installed / "lib", target_is_directory=True)
            inputs = directory / "core-inputs.json"
            inputs.write_text(json.dumps({"c_sdk_identity": core["build"]["c_sdk"],
                "bootstrap_sha256": core["build"]["bootstrap"]}))
            subprocess.run(["python3", str(ROOT / "tools/install-support"), "core",
                "--prefix", str(stage), "--host", core["host"],
                "--target", "armv7-rtems-eabihf", "--rtems-major", "5",
                "--c-target", "arm-rtems5", "--errno", "__errno",
                "--bootstrap-inputs", str(inputs),
                "--target-cflags=" + core["build"]["target_cflags"]], check=True)
            subprocess.run(["python3", str(ROOT / "tools/install-support"), "nif",
                "--prefix", str(stage), "--core-prefix", str(stage),
                "--rustler", str(rustler), "--inventory", str(inventory),
                "--libc", str(build_dir / ("libc-" + pins["components"]["libc"]["base"])),
                "--vendor", str(build_dir / "rust-nif-vendor")], check=True)
            subprocess.run([str(stage / "bin/rustler-bundler"), "--version"], check=True)
            if not (stage / "share/rust-nif/cargo/config.toml").is_file():
                raise AssertionError("staged SDK Cargo configuration is missing")
            if (stage / "share/rust-nif/crates/rustler/0.39.0/.cargo").exists():
                raise AssertionError("build-specific Cargo configuration leaked into staged sources")
            print("Release bundler output and staged NIF payload checks passed", flush=True)
            # Keep the CLI's generated fixtures beneath the prepared .cargo
            # configuration so their subprocesses use the same offline sources.
            fixtures = rustler / "test-tmp"
            fixtures.mkdir()
            env["TMPDIR"] = str(fixtures)
            subprocess.run([cargo, "test", "--frozen", "-p", "rustler_bundler"],
                           cwd=rustler, env=env, check=True)


if __name__ == "__main__":
    main()
