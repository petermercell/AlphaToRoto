# AlphaToRoto

> Trace alpha-channel mattes into animated Roto shapes in Nuke, using embedded potrace.

AlphaToRoto is a Nuke NDK plugin that converts an alpha channel — from an AI matte, a key, a hand-painted roto, anything — into a real `Roto` node with editable Bezier shapes. Each shape gets a fixed number of control points, those points are aligned across frames via cyclic-rotation matching, and each control point's centre is keyframed per frame. The result is a lightweight, smoothly interpolating, tracker-friendly spline you can edit downstream.

The plugin is **pass-through on the image side** — your alpha goes in and out untouched. The only thing it adds is a visible **Convert to Animated Roto** button that walks the configured frame range, traces each frame through statically-linked [potrace 1.16](https://potrace.sourceforge.net/), and emits a new `Roto` node wired to its output.

## Features

- **Animated Roto from any matte.** Outer shapes plus holes are tracked separately. Each detected track becomes one `Track{N}` or `Hole{N}` shape with K keyframed control points across the entire range.
- **Cross-frame correspondence.** Each frame's polyline is resampled to a fixed point count, then cyclic-rotation-aligned against the previous frame's points so control point indices stay consistent. The NUMPY variant does this via FFT cross-correlation; the NO_NUMPY variant uses a pure-Python equivalent.
- **Per-frame execution via `nuke.execute()`.** The plugin inherits `Executable`, so Nuke handles all `OutputContext` propagation — no manual context juggling, and it works correctly on Nuke 17 (where the legacy `Op::input(int, const OutputContext*)` API was removed).
- **Crash-safety sidecar.** Before the heavy Python build phase, the traced curve blob is dumped next to your script as `.a2r.txt`, with a flushed/fsynced `.a2r.log` for post-mortem. If Nuke dies during shape construction on a long range, recover with `rebuild_animated_from_dump(<path>)` instead of re-tracing.
- **Cross-platform.** Pre-built binaries for Linux (`.so`), macOS (`.dylib`), and Windows (`.dll`) under `COMPILED/`, covering Nuke 14.1, 15.0, 15.1, 15.2, 16.0, 16.1, and 17.0 (macOS from 15.0 onwards).

## Repository layout

```
AlphaToRoto/
├── CMakeLists.txt                  Linux build
├── COMPILED/
│   ├── LINUX/{14.1..17.0}/         AlphaToRoto.so
│   ├── MAC/{15.0..17.0}/           AlphaToRoto.dylib
│   ├── MAC/CMakeLists.txt          macOS build
│   ├── WIN/{14.1..17.0}/           AlphaToRoto.dll
│   └── WIN/CMakeLists.txt          Windows build
├── Python/
│   ├── NUMPY/alphaToRoto_py.py     FFT-based cyclic alignment (faster)
│   └── NO_NUMPY/alphaToRoto_py.py  Pure-Python fallback (no deps)
├── src/AlphaToRoto.cpp
├── third_party/potrace/            potrace 1.16, vendored, statically linked
├── LICENCE                         GPL-3.0 + project header + third-party notices
├── LICENSING.md                    Plain-language licensing summary
├── building_step_by_step.txt
└── README.md
```

## Installation (using the pre-built binaries)

1. Pick the binary that matches your OS and Nuke version from `COMPILED/<OS>/<VERSION>/`.
2. Copy that file into your `~/.nuke` directory (or any path on `NUKE_PATH`).
3. Pick **one** Python companion and copy it into the same place:
   - `Python/NUMPY/alphaToRoto_py.py` if your Nuke has NumPy installed (default on most studio setups).
   - `Python/NO_NUMPY/alphaToRoto_py.py` if it does not — the script does cyclic alignment in pure Python instead.
4. Restart Nuke. The node appears under **Draw → AlphaToRoto**.

## Usage

1. Pipe a node with a usable alpha into `AlphaToRoto`.
2. Set:
   - **threshold** — alpha values above this are "inside" (default `0.5`).
   - **turdsize** — speckles ≤ N pixels are dropped (default `2`).
   - **alphamax** — corner detection: `0` = polygonal, `1.3334` = all smooth (default `1.0`).
   - **first / last** — frame range to trace.
3. Click **Convert to Animated Roto**.

The plugin runs `nuke.execute()` across the range. Each frame's traced shapes are assigned to outer/hole tracks, resampled to K control points, cyclic-aligned to the previous frame, and written as keyframes on a fresh `Roto` node connected to `AlphaToRoto`'s output. Console output reports per-track summaries:

```
AlphaToRoto: built animated Roto with 1 track and 2 holes across frames 1001..1100 (29800 keyframes total).
  Track1: 128 control points, 100 keyframed frames
  Hole1:  64 control points, 100 keyframed frames
  Hole2:  64 control points, 100 keyframed frames
```

### Recovering from a build-phase crash

The trace blob is dumped to `~/.nuke/<NodeName>_<first>-<last>_<timestamp>.a2r.txt` (or next to your `.nk` script) before shape construction begins. If Nuke crashes or you force-quit during the build, run from the script editor:

```python
import alphaToRoto_py
alphaToRoto_py.rebuild_animated_from_dump('/path/to/<NodeName>_1001-1100_<ts>.a2r.txt')
```

This skips the trace step entirely and just rebuilds the Roto.

## Building from source

Potrace 1.16 is already vendored under `third_party/potrace/` — there is nothing extra to download.

### Linux

```bash
rm -rf build && mkdir build && cd build
cmake .. -DNUKE_VERSION=17.0v1 -DNUKE_INSTALL_PATH=/opt/Nuke17.0v1
make
```

Produces `build/AlphaToRoto.so`. Copy it (and one of the Python companions) into `~/.nuke`.

### macOS

```bash
cd /path/to/AlphaToRoto-main
rm -rf build && mkdir build && cd build
cmake -S ../COMPILED/MAC -B . -DNUKE_VERSION=17.0v1
make
```

The macOS `CMakeLists.txt` lives under `COMPILED/MAC/` and resolves DDImage inside the versioned `.app` bundle (`/Applications/Nuke<VERSION>/Nuke<VERSION>.app/Contents/MacOS`). Architecture defaults to the host (`arm64` on Apple Silicon, `x86_64` on Intel); override with `-DCMAKE_OSX_ARCHITECTURES=...`.

### Windows

```bat
cd C:\path\to\AlphaToRoto-main
rmdir /s /q build 2>nul & mkdir build && cd build
cmake -S ..\COMPILED\WIN -B . -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CXX_COMPILER=cl -DNUKE_VERSION=17.0v1
cmake --build .
```

Run from a **x64 Native Tools Command Prompt for VS** so `cl.exe` is on `PATH`.

### Targeting other Nuke versions

Pass the version you want to `-DNUKE_VERSION=...`. The build script auto-detects the C++ ABI: Nuke 14.x and earlier are forced to the old GCC ABI (`_GLIBCXX_USE_CXX11_ABI=0`); Nuke 15.0+ uses the new ABI. macOS uses Clang/libc++, so the GCC ABI flag does not apply there.

## Notes and limitations

- **Hole shapes use stencil blend mode.** When the build sets a hole's blend mode it does so via the public Roto attributes API; on the rare Nuke version where that write is rejected, the shape will animate correctly but render additive. The console reports the failure and you can fix the blend mode manually in the Roto properties panel.
- **K is auto-picked** from the median polyline point count across frames in a track, clamped to `[32, 128]` for outers and `[128, 2048]` for holes by default. Pass `num_points=...` to `convert_range_animated()` from the script editor to override.
- **`opticurve` is forced off** in potrace's parameters because some Nuke versions of the rotopaint API don't persist Bezier tangents through optimised-and-merged segments — they end up rendering as straight polylines. Disabling the optimisation keeps every segment as its own Bezier, which the API stores correctly.
- **This is a contour tracer, not a tracker.** Each frame is traced independently and tracks are assigned by centroid Hungarian matching between consecutive frames. Fast topology changes (a hole opening/closing, a limb separating from a body) will cause shape re-assignment.

## Licence

GPL-3.0. See [`LICENCE`](./LICENCE) for the full text and [`LICENSING.md`](./LICENSING.md) for a plain-language summary — short version: yes, you can use it commercially in a studio. Studios may install, deploy, and internally modify the plugin without triggering any source-release obligation; GPL-3.0 controls redistribution of the plugin itself, not the geometry or the rendered frames you produce with it.

AlphaToRoto statically links potrace 1.16 by Peter Selinger, dual-licensed GPL-2.0-or-later / commercial. AlphaToRoto exercises potrace's "or later" option to combine it under GPL-3.0; full reconciliation is in [`LICENCE`](./LICENCE) and [`LICENSING.md`](./LICENSING.md). The "Potrace" name is Peter Selinger's trademark and is used here only for attribution — AlphaToRoto is not a fork or rebrand of Potrace; it embeds the potrace tracing library as a third-party dependency.

## Credits

- [potrace](https://potrace.sourceforge.net/) by Peter Selinger — the tracing engine doing all the actual contour work.
- Foundry, for the Nuke NDK.

## Author

**Peter Mercell** — independent VFX developer, Prague.

[petermercell.com](https://petermercell.com) · [Patreon](https://patreon.com/cw/PeterMercell) · [GitHub](https://github.com/petermercell)
