# RTEMS Rust integration into GRiSP SDK

RSB project sets for a source-built Rust compiler, its matching Cargo and LLVM,
prebuilt ARMv7 RTEMS standard libraries, and static Rustler NIF support.
The enclosing SDK selects the C tool prefix, RTEMS major, errno ABI and Rust
target. CPU and BSP policy belongs to its platform overlays.
The GRiSP2 build-set wrappers live in `config/grisp2/`; shared recipes remain
in `config/rust/`.

The source inputs are immutable upstream archives with checksummed patches.
`sources.json` records the upstream bases, local feature commits, complete
patched trees and Rust's Cargo/LLVM submodule identities. Rustler is based on
release 0.39.0; other Rustler release lines are not included.

## Building through the GRiSP SDK

The enclosing SDK's ordinary `make install` builds the C/RTEMS SDK. To add or
update Rust components afterward, run `make rust-support`; no prefix overrides
are needed, and the default destination is the same `rtems/5` prefix. During
development the narrower targets are also available:

```sh
make rust-check         # recipe dependency checks; no compilation
make rust-sources       # checksummed prefetch; no compilation
make rust-support       # all Rust components; reuse the installed C SDK
make rust-toolchain     # Rust/Cargo/LLVM and host/target std
make rust-nif-support   # bundler and patched source payload only
```

`NUMCORE` defaults to the host CPU count and controls both C and Rust build
parallelism. A lower value is useful when RAM is limited.

`PREFIX` selects the output SDK. `RUST_C_PREFIX` defaults to `PREFIX` and can
point at an existing C SDK while testing Rust in a separate output prefix.
The ordinary targets skip installed packages whose recipe and C SDK inputs are
unchanged. `RUST_RSB_FLAGS` defaults to `--no-clean` to retain compiler work after
failures and successful builds. If a package needs updating, compatible Rust
source, LLVM and compiler objects are reused automatically. RSB still recreates
its own scratch directory under `build/rust/rsb`; its `build/rust/build/tmp`
directories are normal package-install staging. Changing compiler/libc/bootstrap
pins or the C SDK starts a fresh compiler build. Changing only NIF source pins,
the installation prefix or job count can reuse compiler work. Set
`RUST_REBUILD=1` to force a fresh build.

For a trial installation in a separate prefix, `RUST_C_PREFIX` selects the
existing C compiler and libraries; it does not copy them to the output prefix.
To add Rust to the default C SDK afterward, run `make rust-support` without the
trial prefix overrides. This installs only the Rust packages and leaves the
enclosing C toolchain build targets uninvoked.

The two RSB packages separate compiler/Cargo/LLVM/std from the host bundler and
patched NIF source payload. This permits independent package maintenance; the
aggregate target installs both. The shared C SDK still contains its IMX7 and
ATSAMV BSPs and GCC multilibs. The Rust ARMv7-A runtime is additional target data;
it does not change the Cortex-M7 C runtime or provide Rust support for ATSAMV.
The consuming GRiSP platform overlay enables Rust only on GRiSP2.

Docker builds omit Rust by default. Add `--build-arg RUST_SUPPORT=1` and optionally
`--build-arg NUMCORE=2` to build the Rust-enabled image with the existing
Dockerfile. The enclosing toolchain's publication workflow always enables Rust
for its native amd64 and arm64 builds. It runs `tests/check-installed.py` inside
each final runtime image with `--skip-host-nifs` before pushing; this retains
relocation, host Rust execution and offline ARM bundle checks without requiring
Erlang inside the toolchain image. The default check still includes host NIF
loading when Erlang is available.
The image's installed SDK is self-contained; no build-tree mounts
or development Rust installation are required by a consumer.

The installed core capability identity includes compiler/std contents, source
pins, recipe identity, target C flags and the C SDK used for std. The NIF package
records that same core identity. Copying an installed SDK preserves its identity;
rebuilding std or changing its inputs invalidates consumer target caches.
Internal builds clear inherited Cargo/Rust compiler settings and use private
Cargo home/target directories with the selected job limit and frozen inputs.

Installed SDK configuration uses relative paths and capability JSON is pretty
printed. Build-cache JSON is also pretty printed; its installation prefix is
relative to the Rust build root. Bootstrap TOML remains specific to its local
compiler workspace. Rustler's current bundle specification requires absolute
input/output paths; generated specification files are pretty printed, while
stdout retains its streaming JSON protocol.

The ARMv7 RTEMS wrapper supplies target-scoped C/C++ flags matching the Rust
target's ARMv7-A, NEON/VFPv3 and hard-float ABI. They are also used when selecting
the C runtime archive for the errno check; they do not select a board CPU or
alter host LLVM/compiler flags.

Native Cargo dependencies use `HOST_PKG_CONFIG` from the path saved before RSB
adds its own wrappers (`SB_ORIG_PATH`). RSB's small pkg-config implementation
does not cover every host library search path, including Ubuntu's multiarch
OpenSSL directory. This selection leaves target package discovery unchanged.

Preparation copies the external patched libc's license notices into a
notice-only `vendor/libc-<version>` directory for Rust's distribution copyright
generator. It contains no Cargo manifest or crate sources, so Cargo keeps using
the explicit libc path patch and existing vendor checksums remain unchanged.

Native bootstrap hosts are Linux x86_64/AArch64 and macOS x86_64/AArch64.
`--with-rust-host=<triple>` is available for recipe checks and source prefetch.
The bootstrap helper rejects a non-native host when actually preparing a build.

## Maintaining source pins

`tools/export-patches --workspace <source-checkouts>` verifies the patch hashes,
complete tree identities and two forward/reverse patch cycles. `--refresh`
snapshots local sources with a temporary Git index and preserves the last
commit's author and message. For a release port, pass `--source NAME=<ported-tree>`;
do not export unrelated changes from the feature branch's upstream baseline.
Use `--component NAME` to restrict verification or refresh to one component.
Additional upstream backports are recorded separately in `sources.json` and
applied after the feature patch. Refresh removes them from the temporary index
before exporting the feature patch, preserving that patch's scope and metadata.
Rustler 0.39.0 includes a checked-integer backport from upstream commit
`bd34ad27831482b0d0dec19a87cdddda91556f06`; it rejects narrowing overflows,
including `u64` terms decoded as `usize` on 32-bit targets.

After reviewing patches and the frozen `config/rust/nif.Cargo.lock`, run:

```sh
python3 tools/pin-sources --rust-source <pinned-rust-source> --cache <archive-cache>
python3 tests/check-inputs.py --cache <rsb-source-cache>
python3 tests/check-rsb-prep.py --sdk <enclosing-sdk> --cache <rsb-source-cache> --extract-native
python3 tests/check-incremental.py --sdk <enclosing-sdk>
python3 tests/check-nif-prep.py --sdk <enclosing-sdk> --cache <rsb-source-cache> --cargo <host-cargo> --rustc <host-rustc> --test-bundler
python3 tests/check-installed.py --prefix <installed-rust-sdk> --c-prefix <installed-c-sdk> --output <fresh-directory-outside-any-cargo-project>
```

These maintenance checks use Python 3.12 or later. Build/install helpers do not
require `tomllib`. Pinned stage0 binaries and unchanged NIF registry crates are
declared as RSB inputs; Cargo builds use frozen local vendor inputs.
The installed check also requires native `cc`, `erlc` and `erl` executables. It
copies the SDK to a read-only path containing spaces, builds offline host and ARM
probes, and loads two host NIFs through a shared Rust runtime. Its output directory
must be outside existing Cargo configuration trees to avoid development patches.
The NIF check executes the actual RSB prep script in a temporary tree, verifies
offline resolution and release output/staging, and runs the bundler tests.
Use an installed compiler or a development stage1 compiler with its host std;
a stage2 build sysroot can be incomplete during a failed distribution install.

The installed Cargo configuration selects patched crate sources without
globally replacing the application's registry. `share/rust-nif/vendor` contains
the support package's frozen dependencies; arbitrary application dependencies
still require normal acquisition or their own complete offline vendor set.

## Build helpers and checks

Normal SDK users invoke the Make targets above. These helpers are internal or
source-maintenance tools, not extra steps required by an Erlang application:

| Helper | Purpose and use |
| --- | --- |
| `tools/bootstrap-config` | RSB prep configures the native Rust bootstrap, selects the C compiler/libc and verifies errno. Reuse mode validates retained compiler inputs. |
| `tools/build-state` | Make records current inputs and asks whether retained compiler work can be reused. |
| `tools/build-env` | RSB runs Rust build/install commands with controlled Cargo/compiler environment and private output paths. |
| `tools/install-support` | RSB stages core capability identity, patched NIF sources/configuration, and vendor integrity inventories. |
| `tools/export-patches` | Maintainers verify source trees, patch hashes and repeated forward/reverse application. `--refresh` exports reviewed local changes without committing. |
| `tools/pin-sources` | Maintainers refresh immutable source/bootstrap declarations and the frozen NIF dependency inventory after intentionally updating source pins. |

Keep `tests/` in the source repository. The small input/environment/identity and
incremental checks cover regressions that successful board tests cannot detect.
The RSB prep checks verify package assembly; the installed smoke check verifies
relocation and host/ARM consumption. Run the heavier installed/NIF checks when
changing recipes or preparing a release. These tests are not installed into the
SDK or copied into the runtime image.
