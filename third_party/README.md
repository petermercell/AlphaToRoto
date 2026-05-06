# Vendored potrace 1.16

This is potrace 1.16 by Peter Selinger, vendored as a third-party dependency of AlphaToRoto.

## What's included

- `src/`        — full library and CLI source (only library files are compiled)
- `COPYING`     — potrace's GPL-2.0-or-later licence text
- `AUTHORS`     — upstream contributor list
- `README`      — upstream README
- `ChangeLog`   — upstream changelog
- `NEWS`        — upstream release notes

## What's omitted (not needed for AlphaToRoto's build)

- `configure` / autoconf machinery (AlphaToRoto uses CMake)
- `doc/`, `m4/`, `tests/`
- `mkbitmap` utility

## Status

No source files have been modified.

## Upstream

- <https://potrace.sourceforge.net/>
- <https://sourceforge.net/projects/potrace/files/>

## To upgrade

Download the new release tarball from upstream and replace this directory wholesale; AlphaToRoto's `CMakeLists.txt` only references `src/potracelib.c`, `src/curve.c`, `src/decompose.c`, and `src/trace.c`.

## Licensing

potrace is licensed under "GPL version 2, or (at your option) any later version" (GPL-2.0-or-later). AlphaToRoto exercises the "or later" option to combine the potrace library under GPL-3.0. See the [`LICENCE`](../../LICENCE) file at the repository root for the full reconciliation note, and [`COPYING`](./COPYING) for potrace's original GPL-2.0 licence text.

The "Potrace" name is Peter Selinger's trademark and is used here only for attribution. AlphaToRoto is not a fork or rebrand of Potrace; it embeds the potrace tracing library as a third-party dependency.
