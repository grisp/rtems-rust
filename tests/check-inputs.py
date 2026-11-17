#!/usr/bin/env python3
"""Exercise source acquisition and staging without compiling an SDK or firmware."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = runpy.run_path(str(ROOT / "tools/bootstrap-config"))


class ConfigurationTests(unittest.TestCase):
    def test_build_environment_overrides_ambient_rust_settings(self):
        with tempfile.TemporaryDirectory(prefix="rust-build-env-") as temporary:
            directory = Path(temporary)
            inherited = {**os.environ, "CARGO_TARGET_DIR": "/wrong/target",
                "CARGO_BUILD_TARGET": "wrong-target", "RUSTFLAGS": "wrong-flags",
                "RUSTUP_TOOLCHAIN": "wrong-toolchain", "RUSTC_WRAPPER": "/wrong/wrapper",
                "RUSTC_STD_RTEMS_NEWLIB_NO_TLS_ERRNO": "wrong-errno",
                "HTTPS_PROXY": "test-proxy", "HOST_PKG_CONFIG": "test-pkg-config"}
            output = subprocess.check_output(["python3", ROOT / "tools/build-env",
                "--cargo-home", directory / "home", "--target-dir", directory / "target",
                "--jobs", "3", "--rustc", "/sdk/bin/rustc", "--", "python3", "-c",
                "import json,os;print(json.dumps(dict(os.environ)))"], env=inherited, text=True)
            env = json.loads(output)
            self.assertEqual(env["CARGO_TARGET_DIR"], str(directory / "target"))
            self.assertEqual(env["CARGO_BUILD_JOBS"], "3")
            self.assertEqual(env["CARGO_NET_OFFLINE"], "true")
            self.assertEqual(env["RUSTC"], "/sdk/bin/rustc")
            self.assertEqual(env["HTTPS_PROXY"], "test-proxy")
            self.assertEqual(env["HOST_PKG_CONFIG"], "test-pkg-config")
            for name in ("CARGO_BUILD_TARGET", "RUSTFLAGS", "RUSTUP_TOOLCHAIN",
                         "RUSTC_WRAPPER", "RUSTC_STD_RTEMS_NEWLIB_NO_TLS_ERRNO"):
                self.assertNotIn(name, env)

    def test_core_signature_tracks_std_c_sdk_and_target_flags_without_absolute_paths(self):
        helper = runpy.run_path(str(ROOT / "tools/install-support"))
        with tempfile.TemporaryDirectory(prefix="rust-core-identity-") as temporary:
            prefix = Path(temporary) / "sdk"
            for relative in ("bin/rustc", "bin/cargo", "lib/librustc_driver-test.so",
                             "lib/rustlib/armv7-rtems-eabihf/lib/libstd-test.rlib"):
                path = prefix / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"original")
            inputs = {"c_sdk_identity": "C-A", "bootstrap_sha256": "bootstrap"}
            build = helper["core_build"]
            first = build(prefix, inputs, "-march=armv7-a")
            relocated = Path(temporary) / "relocated sdk"
            shutil.copytree(prefix, relocated)
            self.assertEqual(first, build(relocated, inputs, "-march=armv7-a"))
            std = prefix / "lib/rustlib/armv7-rtems-eabihf/lib/libstd-test.rlib"
            stamp = std.stat()
            std.write_bytes(b"modified")
            os.utime(std, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
            self.assertNotEqual(first, build(prefix, inputs, "-march=armv7-a"))
            self.assertNotEqual(first, build(relocated, {**inputs, "c_sdk_identity": "C-B"}, "-march=armv7-a"))
            self.assertNotEqual(first, build(relocated, inputs, "-march=armv7-a -mfpu=neon"))
            stamp = Path(temporary) / "bootstrap-inputs.json"
            stamp.write_text(json.dumps(inputs))
            command = ["python3", ROOT / "tools/install-support", "core", "--prefix", relocated,
                "--host", "x86_64-unknown-linux-gnu", "--target", "armv7-rtems-eabihf",
                "--rtems-major", "5", "--c-target", "arm-rtems5", "--errno", "__errno",
                "--bootstrap-inputs", stamp, "--target-cflags=-march=armv7-a"]
            subprocess.run(command, check=True)
            metadata = relocated / "share/rtems/rust-support.json"
            original = json.loads(metadata.read_text())
            self.assertEqual(original["sources"], hashlib.sha256((ROOT / "sources.json").read_bytes()).hexdigest())
            self.assertNotEqual(original["identity"], original["sources"])
            stamp.write_text(json.dumps({**inputs, "c_sdk_identity": "C-B"}))
            subprocess.run(command, check=True)
            updated = json.loads(metadata.read_text())
            self.assertNotEqual(original["identity"], updated["identity"])
            self.assertEqual(original["sources"], updated["sources"])

    def test_native_hosts_and_unsupported_host(self):
        for system, machine, expected in [
            ("Linux", "x86_64", "x86_64-unknown-linux-gnu"),
            ("Linux", "aarch64", "aarch64-unknown-linux-gnu"),
            ("Darwin", "x86_64", "x86_64-apple-darwin"),
            ("Darwin", "arm64", "aarch64-apple-darwin")]:
            self.assertEqual(BOOTSTRAP["host_triple"](system, machine), expected)
        with self.assertRaises(ValueError):
            BOOTSTRAP["host_triple"]("Linux", "riscv64")

    def test_bootstrap_is_source_built_with_host_and_target_std(self):
        import tomllib
        config = tomllib.loads(BOOTSTRAP["configuration"](
            "aarch64-apple-darwin", "armv7-rtems-eabihf", Path("/sdk with spaces"),
            Path("/stage0"), "arm-rtems5", 2))
        self.assertEqual(config["build"]["target"], ["aarch64-apple-darwin", "armv7-rtems-eabihf"])
        self.assertFalse(config["llvm"]["download-ci-llvm"])
        self.assertFalse(config["rust"]["download-rustc"])
        self.assertEqual(config["llvm"]["targets"], "ARM;AArch64")
        self.assertEqual(config["target"]["armv7-rtems-eabihf"]["cc"], "/sdk with spaces/bin/arm-rtems5-gcc")

    def test_target_c_flags_do_not_change_host_flags(self):
        flags = "-march=armv7-a -mfpu=neon -mfloat-abi=hard"
        script = BOOTSTRAP["target_environment"]("armv7-rtems-eabihf", flags)
        output = subprocess.check_output(["sh", "-c", script +
            'printf "%s\\n" "$CFLAGS_armv7_rtems_eabihf" "$CXXFLAGS_armv7_rtems_eabihf"'], text=True)
        self.assertEqual(output.splitlines(), [flags, flags])
        self.assertNotIn("CFLAGS_x86_64", script)

    def test_host_pkg_config_uses_original_path_without_changing_target_lookup(self):
        with tempfile.TemporaryDirectory(prefix="rust-host-pkg-config-") as temporary:
            root = Path(temporary)
            for name, output in [("native", "native"), ("rsb", "wrapper")]:
                directory = root / name
                directory.mkdir()
                program = directory / "pkg-config"
                program.write_text("#!/bin/sh\nprintf '%s\\n' " + output + "\n")
                program.chmod(0o755)
            with mock.patch.dict(os.environ, {"SB_ORIG_PATH": str(root / "native"),
                                              "PATH": str(root / "rsb")}, clear=True):
                script = BOOTSTRAP["host_environment"]()
                output = subprocess.check_output(["/bin/sh", "-c", script +
                    '"$HOST_PKG_CONFIG" --version\npkg-config --version\n'
                    'test "${PKG_CONFIG-unset}" = unset\n'], text=True)
                self.assertEqual(output.splitlines(), ["native", "wrapper"])
            with mock.patch.dict(os.environ, {"SB_ORIG_PATH": str(root / "missing")}, clear=True):
                with self.assertRaises(ValueError):
                    BOOTSTRAP["host_environment"]()

    def test_reuse_rejects_changed_configuration_or_source_pins(self):
        with tempfile.TemporaryDirectory(prefix="rust-reuse-check-") as temporary:
            directory = Path(temporary)
            identity, sources, inputs = BOOTSTRAP["source_identity"]()
            config = BOOTSTRAP["configuration"]("x86_64-unknown-linux-gnu", "armv7-rtems-eabihf",
                Path("/sdk"), Path("/stage0"), "arm-rtems5", 2)
            check = BOOTSTRAP["check_reuse"]
            with self.assertRaises(ValueError):
                check(directory, config, identity, sources, inputs)
            (directory / "bootstrap.toml").write_text(config)
            (directory / "git-commit-hash").write_text(sources["components"]["rust"]["base"])
            check(directory, config, identity, sources, inputs)
            check(directory, config.replace("jobs = 2", "jobs = 4"), identity, sources, inputs)
            check(directory, config.replace('"/sdk"', '"/another-sdk"'), identity, sources, inputs)
            with self.assertRaises(ValueError):
                check(directory, config.replace('/sdk/bin/', '/other-c-sdk/bin/'), identity, sources, inputs)
            stamp = directory / ".rtems-rust-inputs.json"
            stamp.write_text(json.dumps({"sources_sha256": "changed"}))
            with self.assertRaises(ValueError):
                check(directory, config, identity, sources, inputs)
            stamp.write_text(json.dumps({"sources_sha256": identity,
                "bootstrap_sha256": BOOTSTRAP["bootstrap_identity"](sources)}))
            updated = copy.deepcopy(sources)
            updated["components"]["rustler"]["commit"] = "updated NIF support"
            check(directory, config, "new sdk identity", updated, inputs)
            updated["components"]["libc"]["commit"] = "updated std ABI"
            with self.assertRaises(ValueError):
                check(directory, config, "new sdk identity", updated, inputs)

    def test_notices_do_not_create_or_overwrite_a_cargo_package(self):
        with tempfile.TemporaryDirectory(prefix="rust-notices-") as temporary:
            root = Path(temporary)
            dependency = root / "libc"
            dependency.mkdir()
            (dependency / "Cargo.toml").write_text('[package]\nname="libc"\n')
            (dependency / "LICENSE-MIT").write_text("test notice\n")
            (dependency / "src").mkdir()
            (dependency / "src/lib.rs").write_text("source must not be copied\n")
            source = root / "rust"
            stage = BOOTSTRAP["stage_dependency_notices"]
            stage(source, dependency, "libc", "0.2.191")
            target = source / "vendor/libc-0.2.191"
            self.assertEqual([p.name for p in target.iterdir()], ["LICENSE-MIT"])
            self.assertEqual((target / "LICENSE-MIT").read_bytes(), (dependency / "LICENSE-MIT").read_bytes())
            modified = (target / "LICENSE-MIT").stat().st_mtime_ns
            stage(source, dependency, "libc", "0.2.191")
            self.assertEqual((target / "LICENSE-MIT").stat().st_mtime_ns, modified)
            (target / "Cargo.toml").write_text("existing vendor manifest\n")
            with self.assertRaises(ValueError):
                stage(source, dependency, "libc", "0.2.191")
            self.assertEqual((target / "Cargo.toml").read_text(), "existing vendor manifest\n")


def extract(archive, destination, selected=None):
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as source:
        for member in source:
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue
            relative = str(Path(*parts[1:]))
            if selected is not None and relative not in selected:
                continue
            member.name = relative
            source.extract(member, destination, filter="data")


def archives(cache, output):
    if output.exists() and any(output.iterdir()):
        raise ValueError("archive-check output directory must be empty: " + str(output))
    manifest = json.loads((ROOT / "sources.json").read_text())
    for name, item in manifest["components"].items():
        archive = cache / item["archive"]
        if hashlib.sha256(archive.read_bytes()).hexdigest() != item["source_sha256"]:
            raise ValueError(f"{archive}: checksum differs")
        directory = output / name
        patch = ROOT / "patches" / (name + ".patch")
        patches = [patch, *[ROOT / "patches" / b["patch"] for b in item.get("backports", [])]]
        selected = None
        if name == "rust":
            selected = {line.split(" b/", 1)[1] for line in patch.read_text().splitlines()
                        if line.startswith("diff --git ")}
            selected.add("library/Cargo.toml")
            selected.add("src/stage0")
        extract(archive, directory, selected)
        for _ in range(2):
            for source_patch in patches:
                subprocess.run(["git", "apply", "--check", str(source_patch)], cwd=directory, check=True)
                subprocess.run(["git", "apply", str(source_patch)], cwd=directory, check=True)
            for source_patch in reversed(patches):
                subprocess.run(["git", "apply", "--reverse", "--check", str(source_patch)], cwd=directory, check=True)
                subprocess.run(["git", "apply", "--reverse", str(source_patch)], cwd=directory, check=True)
        for source_patch in patches:
            subprocess.run(["git", "apply", str(source_patch)], cwd=directory, check=True)
        print(name + ": upstream archive checksum and two patch cycles passed", flush=True)
        if name == "rust":
            stage0 = dict(line.split("=", 1) for line in (directory / "src/stage0").read_text().splitlines()
                          if "=" in line and not line.startswith("#"))
            for entries in manifest["stage0"].values():
                for entry in entries:
                    path = entry["url"].removeprefix(stage0["dist_server"] + "/")
                    if stage0[path] != entry["sha256"]:
                        raise ValueError("stage0 input differs from the official source: " + path)
    vendor = output / "vendor"
    for package in json.loads((ROOT / "config/rust/nif-packages.json").read_text()):
        name = package["name"] + "-" + package["version"]
        archive = cache / (name + ".crate")
        if hashlib.sha256(archive.read_bytes()).hexdigest() != package["checksum"]:
            raise ValueError(f"{archive}: checksum differs")
        extract(archive, vendor / name)
    subprocess.run(["python3", ROOT / "tools/install-support", "vendor", "--prefix", vendor], check=True)
    rustler = output / "rustler"
    shutil.copy2(ROOT / "config/rust/nif.Cargo.lock", rustler / "Cargo.lock")
    (rustler / ".cargo").mkdir(exist_ok=True)
    (rustler / ".cargo/config.toml").write_text(
        '[source.crates-io]\nreplace-with = "sdk-vendor"\n[source.sdk-vendor]\ndirectory = ' +
        json.dumps(str(vendor)) + '\n[patch.crates-io]\ninventory = { path = ' +
        json.dumps(str(output / "inventory")) + ' }\n')
    print("Patched sources and frozen vendor inputs prepared in " + str(output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ConfigurationTests)
    if not unittest.TextTestRunner().run(suite).wasSuccessful():
        raise SystemExit(1)
    if args.cache:
        archives(args.cache.resolve(), (args.output or Path(tempfile.mkdtemp(prefix="rtems-rust-inputs-"))).resolve())
