# AlphaToRoto

> Trace alpha-channel mattes into animated Roto shapes in Nuke, using embedded potrace.

## 🚧 Work in progress

This repository is being prepared for public release. Source code, build instructions, and a full README are coming soon.

## What it does

AlphaToRoto is a Nuke NDK plugin that converts alpha-channel mattes into vector Roto geometry. Drop it after any node with a usable alpha — an AI matte, a key, a hand-painted roto — set the frame range, and click **Convert to Animated Roto**. The plugin traces each frame's alpha through embedded potrace, resamples the outer contour to a fixed number of control points, aligns those points between frames via cyclic cross-correlation, and emits a single Roto shape with per-frame keyframes on each control point — producing a lightweight, tracker-friendly spline you can edit downstream.

## Coming soon

- Source code (`AlphaToRoto.cpp`, `alphaToRoto_py.py`)
- Build instructions for Linux, macOS, and Windows

## Licence

GPL-3.0. See [`LICENCE`](./LICENCE) for the full text and [`LICENSING.md`](./LICENSING.md) for a plain-language summary of what GPL-3.0 means in practice — short version: yes, you can use it commercially in a studio.

## Author

**Peter Mercell** — independent VFX developer, Prague.

[petermercell.com](https://petermercell.com) · [Patreon](https://patreon.com/cw/PeterMercell) · [GitHub](https://github.com/petermercell)
