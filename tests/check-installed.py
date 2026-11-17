#!/usr/bin/env python3
"""Compile against an installed, relocated read-only Rust SDK without firmware."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True, type=Path)
    parser.add_argument("--c-prefix", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--skip-host-nifs", action="store_true",
                        help="skip host NIF loading, which requires an Erlang installation")
    args = parser.parse_args()
    output = args.output.resolve()
    if any((parent / ".cargo" / name).exists()
           for parent in output.parents for name in ("config", "config.toml")):
        parser.error("use an output directory outside existing Cargo configuration trees")
    output.mkdir(parents=True, exist_ok=True)
    sdk = output / "relocated sdk"
    if sdk.exists():
        parser.error("use a fresh output directory")
    shutil.copytree(args.prefix.resolve(), sdk, symlinks=True)
    for path in [*sdk.rglob("*"), sdk]:
        if not path.is_symlink():
            path.chmod(path.stat().st_mode & ~0o222)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("CARGO_", "RUST", "GRISP_RUST", "CC_", "CFLAGS_", "CXXFLAGS_"))}
    env.update(PATH=str(sdk / "bin") + os.pathsep + env["PATH"],
               RUSTC=str(sdk / "bin/rustc"), CARGO_HOME=str(output / "cargo-home"),
               CARGO_TARGET_DIR=str(output / "target"), CARGO_NET_OFFLINE="true")

    def run(command, **kwargs):
        result = subprocess.run(list(map(str, command)), cwd=output, env=env,
                                capture_output=True, text=True, **kwargs)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return result.stdout

    assert Path(run([sdk / "bin/rustc", "--print", "sysroot"]).strip()) == sdk
    print(run([sdk / "bin/rustc", "-Vv"]), end="", flush=True)
    print(run([sdk / "bin/cargo", "-V"]), end="", flush=True)
    print(run([sdk / "bin/rustler-bundler", "--version"]), end="", flush=True)
    core = json.loads((sdk / "share/rtems/rust-support.json").read_text())
    nif = json.loads((sdk / "share/rust-nif/support.json").read_text())
    identity = hashlib.sha256((sdk / "share/rust-nif/sources.json").read_bytes()).hexdigest()
    assert core["identity"] == nif["identity"]
    assert core["sources"] == identity
    source = output / "host.rs"
    source.write_text('fn main() { println!("host std works"); }\n')
    run([sdk / "bin/rustc", source, "-o", output / "host"])
    assert run([output / "host"]).strip() == "host std works"
    source = output / "target.rs"
    source.write_text('''#[no_mangle]
pub extern "C" fn sdk_std_probe() -> usize {
    let values = std::sync::Mutex::new(vec![1_u32, 2, 3]);
    std::panic::catch_unwind(|| values.lock().unwrap().len()).unwrap()
}
''')
    run([sdk / "bin/rustc", source, "--crate-type=staticlib", "--target=armv7-rtems-eabihf",
         "-Cpanic=unwind", "-o", output / "libstd_probe.a"])
    base = sdk / "share/rust-nif"
    (output / ".cargo").mkdir()
    (output / ".cargo/config.toml").write_text(
        '[source.crates-io]\nreplace-with="sdk-vendor"\n[source.sdk-vendor]\ndirectory=' +
        json.dumps(str(base / "vendor")) + '\n')
    roots = []
    for name in ("one", "two"):
        directory = output / name
        (directory / "src").mkdir(parents=True)
        (directory / "Cargo.toml").write_text(
            '[package]\nname=' + json.dumps(name) + '\nversion="0.1.0"\nedition="2021"\n'
            '[lib]\ncrate-type=["rlib","cdylib"]\n[dependencies]\nrustler="=0.39.0"\n')
        (directory / "src/lib.rs").write_text(
            '#[rustler::nif]\nfn value() -> u64 { 42 }\nrustler::init!(' + json.dumps(name) + ');\n')
        roots.append({"manifest_path": str(directory / "Cargo.toml"),
                      "package": name, "library": name})
    spec = {"schema": 1, "out_dir": str(output / "generated bundle"), "roots": roots,
            "target": "armv7-rtems-eabihf", "release": True, "offline": True,
            "cargo": [str(sdk / "bin/cargo"), "--config", str(base / "cargo/config.toml")]}
    spec_path = output / "bundle.json"
    spec_path.write_text(json.dumps(spec))
    messages = run([sdk / "bin/rustler-bundler", "build", "--spec", spec_path])
    (output / "bundle-messages.jsonl").write_text(messages)
    bundle = json.loads(messages.splitlines()[-1])
    assert [entry["init"] for entry in bundle["entries"]] == ["one_nif_init", "two_nif_init"]
    archive = Path(bundle["archive"])
    symbols = run([args.c_prefix.resolve() / "bin/arm-rtems5-nm", "-g", "--defined-only", archive])
    assert all(" T " + name + "_nif_init" in symbols for name in ("one", "two"))
    metadata = json.loads(run([*spec["cargo"], "metadata", "--offline", "--format-version=1",
                              "--manifest-path", output / "generated bundle/Cargo.toml"]))
    for name in ("rustler", "rustler_codegen", "inventory"):
        package = next(p for p in metadata["packages"] if p["name"] == name)
        assert Path(package["manifest_path"]).is_relative_to(base)
    print("PASS read-only SDK relocation, host std execution, ARM std/unwind staticlib and offline two-NIF ARM bundle", flush=True)
    if args.skip_host_nifs:
        print("Evidence: " + str(output), flush=True)
        return
    spec.pop("target")
    spec["out_dir"] = str(output / "host bundle")
    spec_path.write_text(json.dumps(spec))
    messages = run([sdk / "bin/rustler-bundler", "build", "--spec", spec_path])
    bundle = json.loads(messages.splitlines()[-1])
    run(["cc", "-shared", "-o", output / "libbundle.so", "-Wl,--whole-archive",
         bundle["archive"], "-Wl,--no-whole-archive", "-ldl", "-lpthread", "-lm"])
    for name in ("one", "two"):
        loader = output / (name + ".c")
        loader.write_text(f'extern void *{name}_nif_init(void);\n'
                          f'void *nif_init(void) {{ return {name}_nif_init(); }}\n')
        run(["cc", "-shared", "-fPIC", loader, "-L" + str(output), "-Wl,-rpath,$ORIGIN",
             "-lbundle", "-o", output / (name + ".so")])
        module = output / (name + ".erl")
        module.write_text(f'-module({name}).\n-export([value/0]).\n-on_load(init/0).\n'
                          'init() -> erlang:load_nif(' + json.dumps(str(output / name)) + ', 0).\n'
                          'value() -> erlang:nif_error(not_loaded).\n')
        run(["erlc", "-o", output, module])
    run(["erl", "-noshell", "-pa", output, "-eval",
         '42 = one:value(), 42 = two:value(), halt(0).'])
    print("PASS both host NIFs load and return 42 from a shared Rust runtime", flush=True)
    print("Evidence: " + str(output), flush=True)


if __name__ == "__main__":
    main()
