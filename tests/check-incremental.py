#!/usr/bin/env python3
"""Exercise the enclosing Makefile with a fake RSB, without building the SDK."""

import argparse
import json
from pathlib import Path
import runpy
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="rtems-rust-make-") as temporary:
        directory = Path(temporary)
        project = directory / "project"
        shutil.copytree(ROOT, project)
        c_prefix = directory / "c-sdk"
        (c_prefix / "bin").mkdir(parents=True)
        (c_prefix / "bin/arm-rtems5-gcc").write_text("fake compiler\n")
        prefix = directory / "sdk"
        build = directory / "build"
        calls = directory / "calls"
        fake = directory / "fake-rsb.py"
        fake.write_text('''import json, pathlib, sys
prefix, calls = map(pathlib.Path, sys.argv[1:3])
args = sys.argv[3:]
kind = "core" if args[-1] == "grisp2/grisp2-rust-tools.bset" else "nif"
with calls.open("a") as output:
    output.write(json.dumps([kind, "--with-rust-reuse" in args]) + "\\n")
path = prefix / ("share/rtems/rust-support.json" if kind == "core" else "share/rust-nif/support.json")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text("{}\\n")
''')
        guard = directory / "no-c-build.mk"
        guard.write_text("toolchain submodule-update bootstrap bsp bsp-grisp1 libbsd libgrisp "
                         "libinih cryptoauthlib blas barebox-install toolchain-revision fdt:\n"
                         "\t$(error Rust-only targets must not invoke C SDK builds)\n")
        command = ["make", "-s", "-f", str(args.sdk.resolve() / "Makefile"),
                   "-f", str(guard),
                   "rust-support", "PREFIX=" + str(prefix),
                   "RUST_C_PREFIX=" + str(c_prefix), "BUILD_RUST=" + str(build),
                   "SRC_RTEMS_RUST=" + str(project),
                   "RUST_RSB=python3 " + str(fake) + " " + str(prefix) + " " + str(calls)]

        def invoke(*extra):
            result = subprocess.run(command + list(extra), cwd=directory,
                                    capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
            return [json.loads(line) for line in calls.read_text().splitlines()]

        assert invoke() == [["core", False], ["nif", False]]
        assert len(invoke()) == 2, "unchanged SDK unexpectedly rebuilt"
        assert len(invoke("NUMCORE=4")) == 2, "job count invalidated compiled artifacts"

        # Simulate a compatible failed/completed bootstrap for the next update.
        helper = runpy.run_path(str(project / "tools/bootstrap-config"))
        context = json.loads((build / "inputs.json").read_text())
        assert not Path(context["prefix"]).is_absolute()
        assert (build / context["prefix"]).resolve() == prefix.resolve()
        sources = json.loads((project / "sources.json").read_text())
        work = build / "build" / ("rtems-rust-" + context["host"] + "-1.100.0")
        source = work / "rustc-1.100.0-src"
        source.mkdir(parents=True)
        (source / ".rtems-rust-inputs.json").write_text(json.dumps(context))
        (source / "bootstrap.toml").write_text(helper["configuration"](
            context["host"], "armv7-rtems-eabihf", prefix, work / "stage0",
            "arm-rtems5", 2, c_prefix))
        tool = project / "tools/install-support"
        tool.write_text(tool.read_text() + "\n# changed packaging input\n")
        assert invoke()[-2:] == [["core", True], ["nif", False]]
        assert len(invoke()) == 4
        assert invoke("PREFIX=" + str(directory / "other-sdk"))[-2:] == [["core", True], ["nif", False]]
        (c_prefix / "bin/arm-rtems5-gcc").write_text("changed compiler\n")
        assert invoke()[-2:] == [["core", False], ["nif", False]]
        assert invoke("RUST_REBUILD=1")[-2:] == [["core", False], ["nif", False]]

        # A normal SDK checkout needs neither PREFIX nor RUST_C_PREFIX overrides.
        checkout = directory / "default-location"
        checkout.mkdir()
        shutil.copy2(args.sdk.resolve() / "Makefile", checkout / "Makefile")
        installed = checkout / "rtems/5"
        (installed / "bin").mkdir(parents=True)
        (installed / "bin/arm-rtems5-gcc").write_text("fake installed C compiler\n")
        default_calls = directory / "default-calls"
        default_command = ["make", "-s", "-j2", "-C", str(checkout), "-f", "Makefile",
                           "-f", str(guard), "rust-support", "SRC_RTEMS_RUST=" + str(project),
                           "RUST_RSB=python3 " + str(fake) + " " + str(installed) + " " + str(default_calls)]
        for _ in range(2):
            result = subprocess.run(default_command, capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stdout + result.stderr)
        assert (installed / "share/rtems/rust-support.json").is_file()
        assert (installed / "share/rust-nif/support.json").is_file()
        assert len(default_calls.read_text().splitlines()) == 2
        print("PASS Make skips unchanged packages, reuses compatible work, handles prefix/C SDK changes and forces fresh builds")
        print("PASS rust-support installs both packages under default rtems/5, skips a second run and invokes no C SDK builds")


if __name__ == "__main__":
    main()
