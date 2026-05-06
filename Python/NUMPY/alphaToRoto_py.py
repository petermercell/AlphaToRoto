# SPDX-License-Identifier: GPL-3.0-only
#
# AlphaToRoto -- Trace alpha-channel mattes into Roto shapes in Nuke
# Copyright (C) 2026 Peter Mercell
#
# This file is part of AlphaToRoto.
#
# AlphaToRoto is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, version 3 of the License.
#
# AlphaToRoto is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License
# for more details.
#
# You should have received a copy of the GNU General Public License along
# with AlphaToRoto. If not, see <https://www.gnu.org/licenses/>.
#
# AlphaToRoto links statically against potrace (Copyright (C) 2001-2019
# Peter Selinger), which is licensed under "GPL version 2, or (at your
# option) any later version". AlphaToRoto exercises the "or later" option
# to combine potrace under GPL-3.0. See the LICENCE file at the repository
# root and third_party/potrace/COPYING for full details.

"""
alphaToRoto_py
--------------

Python companion for the AlphaToRoto NDK plugin.

Flow:
    1. The plugin's visible "Convert to Roto" button is a PyScript_knob
       that calls alphaToRoto_py.convert(nuke.thisNode()).
    2. convert() dispatches on the 'use_frame_range' knob:
         - False  -> convert_single_frame(): trace the current frame only
         - True   -> convert_range(): nuke.execute across [first, last],
                     build a Roto with per-frame shape lifetimes
    3. Both paths:
         a. bump the plugin's hidden _reset_trigger to clear its C++-side
            accumulator (fires knob_changed -> clears _accum_buf + curve_data)
         b. call nuke.execute(plugin_node, first, last) -- Nuke's render
            pipeline then calls the plugin's _execute() once per frame with
            the correct OutputContext, and each call appends that frame's
            serialized curves (prefixed with "F <frame>\\n") to curve_data
         c. read back the accumulated curve_data, parse by F-markers, and
            build rp.Shape objects on a new Roto node wired to the plugin.

Per-frame serialization format (produced by AlphaToRoto.cpp):
    F <frame>                            frame marker (prefixes each block)
    S <idx> <sign> <n_segments>          shape header ('+' or '-')
    M <x> <y>                            start point
    C <c1x> <c1y> <c2x> <c2y> <ex> <ey>  cubic bezier segment (absolute)
    L <x> <y>                            line segment (from corner)
    E                                    end shape

All coordinates are Nuke absolute pixel coords, Y-up origin bottom-left.

Range-mode caveat:
    This is NOT a tracked animated spline. Each frame's shapes are
    independently traced -- point count, point ordering, and even the
    number of shapes can change frame to frame. What you get is a Roto
    where each frame has its own shapes, visible only on that frame via
    single-frame lifetime. It renders correctly but isn't "keyframeable".
    For a true animated spline from a matte, use Mocha Pro's magnetic
    spline or Silhouette's shape tracker instead.
"""

from __future__ import annotations

import os
import time
import tempfile

import numpy as np
import nuke


# ---------------------------------------------------------------------------
# Debug logging
# ---------------------------------------------------------------------------
# Writes to a sidecar .log file next to the .a2r.txt dump, flushing + fsyncing
# on every line so the log survives an OOM kill / SIGKILL / hard crash.
# Runs silently on the terminal -- the log file is only for post-mortems.
#
# Read the log after a crash with:
#   tail -50 ~/.nuke/<NodeName>_<first>-<last>_<timestamp>.a2r.log
# ---------------------------------------------------------------------------
_DBG_FILE = None      # open file handle
_DBG_PATH = None      # path for messages
_DBG_T0   = None      # start time (seconds)


def _dbg_rss_mb():
    """Return current process RSS in MB, or None if /proc unavailable."""
    try:
        with open('/proc/self/status', 'r') as fh:
            for line in fh:
                if line.startswith('VmRSS:'):
                    # e.g. "VmRSS:\t  123456 kB"
                    return int(line.split()[1]) / 1024.0
    except Exception:
        return None
    return None


def _dbg_open(path):
    """Open the sidecar debug log at `path`. Safe to call multiple times."""
    global _DBG_FILE, _DBG_PATH, _DBG_T0
    _dbg_close()
    try:
        _DBG_FILE = open(path, 'w', buffering=1)  # line-buffered
        _DBG_PATH = path
        _DBG_T0 = time.time()
        _dbg("=== AlphaToRoto debug log opened: {} ===".format(path))
    except Exception:
        # Silent failure -- debug log is non-critical.
        _DBG_FILE = None
        _DBG_PATH = None


def _dbg_close():
    global _DBG_FILE, _DBG_PATH, _DBG_T0
    if _DBG_FILE is not None:
        try:
            _DBG_FILE.flush()
            os.fsync(_DBG_FILE.fileno())
            _DBG_FILE.close()
        except Exception:
            pass
    _DBG_FILE = None
    _DBG_PATH = None
    _DBG_T0 = None


def _dbg(msg):
    """Timestamped debug line -> sidecar log, flushed + fsynced.

    Writes only to the sidecar .log file (if opened via _dbg_open) so the
    terminal stays quiet during normal runs. The log is fsynced on every
    write so it survives an OOM kill / SIGKILL.
    """
    if _DBG_FILE is None:
        return
    rss = _dbg_rss_mb()
    elapsed = (time.time() - _DBG_T0) if _DBG_T0 is not None else 0.0
    rss_s = "{:7.1f}MB".format(rss) if rss is not None else "   ?.?MB"
    line = "[a2r +{:7.2f}s {}] {}".format(elapsed, rss_s, msg)
    try:
        _DBG_FILE.write(line + "\n")
        _DBG_FILE.flush()
        os.fsync(_DBG_FILE.fileno())
    except Exception:
        pass

try:
    import nuke.rotopaint as rp
except Exception as exc:  # pragma: no cover
    rp = None
    _rp_import_error = exc
else:
    _rp_import_error = None


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _parse_curve_data(text):
    """Parse the serialized curve blob into a list of shape dicts."""
    shapes = []
    cur = None
    for raw in text.splitlines():
        parts = raw.split()
        if not parts:
            continue
        tag = parts[0]

        if tag == 'S':
            if cur is not None:
                shapes.append(cur)
            sign = parts[2] if len(parts) >= 3 else '+'
            cur = {'sign': sign, 'segments': []}

        elif tag == 'M' and cur is not None:
            cur['segments'].append(('move', float(parts[1]), float(parts[2])))

        elif tag == 'C' and cur is not None:
            cur['segments'].append((
                'curve',
                float(parts[1]), float(parts[2]),
                float(parts[3]), float(parts[4]),
                float(parts[5]), float(parts[6]),
            ))

        elif tag == 'L' and cur is not None:
            cur['segments'].append(('line', float(parts[1]), float(parts[2])))

        elif tag == 'E':
            if cur is not None:
                shapes.append(cur)
                cur = None

    if cur is not None:
        shapes.append(cur)
    return shapes


def _parse_frame_keyed_data(text):
    """Parse an F-marker-delimited multi-frame blob.

    Input format: one or more blocks, each starting with "F <frame>" on its
    own line, followed by lines in the _parse_curve_data format.

    Returns: {int frame: [shape_dict, ...]}  (dict insertion order preserved)
    """
    frames = {}
    current_frame = None
    current_lines = []

    def _flush():
        if current_frame is None:
            return
        shapes = _parse_curve_data('\n'.join(current_lines))
        # Merge if the same frame appears more than once (shouldn't happen
        # in normal use but tolerate it):
        if current_frame in frames:
            frames[current_frame].extend(shapes)
        else:
            frames[current_frame] = shapes

    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith('F '):
            _flush()
            try:
                current_frame = int(stripped.split(None, 1)[1])
            except (ValueError, IndexError):
                current_frame = None
            current_lines = []
        else:
            if current_frame is not None:
                current_lines.append(raw)

    _flush()
    return frames


# ---------------------------------------------------------------------------
# Defensive tangent API helpers
#
# nuke.rotopaint.ShapeControlPoint.leftTangent is a method in some Nuke
# versions and a property in others. The underlying tangent object has
# setXY / setPosition / direct .x,.y attribute access depending on
# version. Wrap this mess so the caller doesn't care.
# ---------------------------------------------------------------------------
def _get_tangent_obj(cp, which):
    """which: 'leftTangent' or 'rightTangent'."""
    attr = getattr(cp, which)
    if callable(attr):
        return attr()
    return attr


def _set_tangent_xy(tangent, dx, dy):
    """Set the tangent offset (relative to its control point).

    Try direct attribute assignment FIRST. That's the idiom that works
    reliably across Nuke 13+ community scripts. In Nuke 17, setXY() on
    the tangent CubicCurve appears to succeed silently without persisting
    the value (the returned object is detached from the ShapeControlPoint),
    which was making all tangents effectively zero and turning smooth
    Bezier curves into polygonal line segments.
    """
    # Path 1: direct attribute assignment (the robust one)
    try:
        tangent.x = dx
        tangent.y = dy
        return True
    except Exception:
        pass

    # Path 2: setXY method (may silently no-op on Nuke 17)
    if hasattr(tangent, 'setXY'):
        try:
            tangent.setXY(dx, dy)
            return True
        except Exception:
            pass

    # Path 3: setPosition method
    if hasattr(tangent, 'setPosition'):
        try:
            tangent.setPosition(dx, dy)
            return True
        except Exception:
            pass

    return False


def _make_cp(x, y, ltx=0.0, lty=0.0, rtx=0.0, rty=0.0):
    """Create a ShapeControlPoint with given center and relative tangents."""
    cp = rp.ShapeControlPoint(x, y)
    _set_tangent_xy(_get_tangent_obj(cp, 'leftTangent'),  ltx, lty)
    _set_tangent_xy(_get_tangent_obj(cp, 'rightTangent'), rtx, rty)
    return cp


# ---------------------------------------------------------------------------
# Shape construction
# ---------------------------------------------------------------------------
def _build_shape(curves_knob, sign, segments):
    """Build one rp.Shape from parsed segments.

    The trick here is that potrace gives ABSOLUTE handle positions per
    segment, while Nuke wants tangent OFFSETS from the control point.

    For a cubic segment ending at point P with control handles (c1, c2):
        - c1 is the RIGHT tangent of the PREVIOUS control point
        - c2 is the LEFT  tangent of the CURRENT (new) control point

    Corners are emitted as two line segments with zero tangents -- sharp.
    """
    shape = rp.Shape(curves_knob, type='bezier')

    if not segments or segments[0][0] != 'move':
        return shape

    # cps: list of [cx, cy, right_dx, right_dy, left_dx, left_dy]
    sx, sy = segments[0][1], segments[0][2]
    cps = [[sx, sy, 0.0, 0.0, 0.0, 0.0]]

    for seg in segments[1:]:
        kind = seg[0]
        if kind == 'curve':
            _, c1x, c1y, c2x, c2y, ex, ey = seg
            # Previous CP's right tangent = c1 - prev_center
            cps[-1][2] = c1x - cps[-1][0]
            cps[-1][3] = c1y - cps[-1][1]
            # New CP with left tangent = c2 - (ex,ey)
            cps.append([ex, ey, 0.0, 0.0, c2x - ex, c2y - ey])
        elif kind == 'line':
            _, ex, ey = seg
            cps.append([ex, ey, 0.0, 0.0, 0.0, 0.0])

    # For a closed path, the final CP coincides with the first. Merge:
    # transfer the final CP's left tangent onto the first CP, then drop
    # the duplicate. The first CP's right tangent was already set by the
    # first segment, so nothing to do there.
    if len(cps) >= 2:
        last = cps[-1]
        first = cps[0]
        if abs(last[0] - first[0]) < 1e-4 and abs(last[1] - first[1]) < 1e-4:
            first[4] = last[4]
            first[5] = last[5]
            cps.pop()

    for cx, cy, rtx, rty, ltx, lty in cps:
        shape.append(_make_cp(cx, cy, ltx, lty, rtx, rty))

    # Hole shapes ('-' sign from potrace): set blending mode = stencil so
    # the shape actually subtracts its alpha from the outer shape stacked
    # below it in the same Roto layer. This is the real cutout path -- the
    # old "set color.a = 0" trick made the hole contribute zero alpha to
    # the union, which is NOT the same as subtracting from the outer.
    #
    # potrace emits outer-then-its-holes, so insertion order into the root
    # layer is already correct: the hole follows the outer it punches.
    if sign == '-':
        _flag_as_hole(shape)

    return shape


# Set on the first successful _flag_as_hole() call, then printed once so
# the user can see which AnimAttributes signature actually wrote the
# blend mode for their Nuke build. Same idea as _ANIMATE_CP_API_DESC.
_HOLE_API_DESC = None


def _flag_as_hole(shape):
    """Mark `shape` as a hole by setting its blending mode to stencil.

    A stencil shape removes its alpha from any shape stacked beneath it
    in the same Roto layer -- exactly potrace's negative / inner-contour
    semantics. The hole MUST be appended AFTER the outer shape it cuts
    (potrace already emits in that order, so just preserve insertion).

    The AnimAttributes key + enum value are resolved from class
    constants when exposed (mirroring _apply_single_frame_lifetime),
    with sensible string/integer fallbacks for older builds. The
    resolved signature is announced once on first success.

    Returns True on apparent success, False otherwise. A failure here
    typically means the AnimAttributes API differs in this Nuke build;
    run diagnose_shape_attrs() on a hole shape and adjust the K_BLEND /
    BLEND_STENCIL fallbacks below.
    """
    global _HOLE_API_DESC

    try:
        attrs = shape.getAttributes()
    except Exception:
        return False

    # Resolve attribute key from class constants, fall back to 'bm'.
    # 'bm' is the documented short-form key for blending mode in
    # nuke.rotopaint.AnimAttributes across recent builds.
    K_BLEND = getattr(attrs, 'kBlendingModeAttribute', None)
    if K_BLEND is None:
        K_BLEND = getattr(attrs, 'kBlendModeAttribute', None)
    if K_BLEND is None:
        K_BLEND = 'bm'

    # Resolve the "stencil" enum value. In current Nuke RotoPaint
    # BlendingMode enums, eBlendStencil is index 1. If your build orders
    # them differently and the result looks additive instead of
    # subtractive, change the integer fallback below to 2 or 3.
    BLEND_STENCIL = getattr(attrs, 'eBlendStencil', None)
    if BLEND_STENCIL is None:
        BLEND_STENCIL = 1
    BLEND_STENCIL = int(BLEND_STENCIL)

    # Try the 3-arg animated setter first (matches the lifetime path),
    # fall back to a 2-arg static setter on older builds.
    wrote = False
    try:
        attrs.set(1.0, K_BLEND, BLEND_STENCIL)
        wrote = True
    except Exception:
        try:
            attrs.set(K_BLEND, BLEND_STENCIL)
            wrote = True
        except Exception:
            return False

    # Verify via read-back when supported. If we can read back but the
    # value didn't take, report failure so the caller can warn.
    try:
        got = attrs.getValue(1.0, K_BLEND)
        if abs(float(got) - BLEND_STENCIL) > 0.01:
            return False
    except Exception:
        pass  # No read-back available; trust the setter.

    if _HOLE_API_DESC is None and wrote:
        _HOLE_API_DESC = "key={!r} value={}".format(K_BLEND, BLEND_STENCIL)
        try:
            print("AlphaToRoto: hole blend mode API = {}".format(_HOLE_API_DESC))
        except Exception:
            pass

    return True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def build_roto_from_plugin(plugin_node):
    """Read curve_data from plugin_node and construct a Roto node.

    Intended for single-frame output. curve_data is now always
    F-marker-delimited (even with one frame), so we parse via
    _parse_frame_keyed_data and flatten the shapes from all frames
    (there should only be one).
    """
    if rp is None:
        nuke.message("AlphaToRoto: nuke.rotopaint unavailable ({}).".format(
            _rp_import_error))
        return None

    try:
        data_knob = plugin_node['curve_data']
    except (NameError, KeyError):
        nuke.message("AlphaToRoto: plugin node has no curve_data knob.")
        return None

    data = data_knob.getValue() if hasattr(data_knob, 'getValue') else ''
    if not data or not data.strip():
        nuke.message("AlphaToRoto: no curve data. Check the input alpha, "
                     "threshold, and turdsize knobs, then press Convert again.")
        return None

    frames = _parse_frame_keyed_data(data)
    if not frames:
        nuke.message("AlphaToRoto: could not parse curve data.")
        return None

    # Flatten all shapes from all frames (single-frame case has one key).
    shapes_data = []
    for f in sorted(frames.keys()):
        shapes_data.extend(frames[f])

    if not shapes_data:
        nuke.message("AlphaToRoto: parsed 0 shapes from curve data.")
        return None

    roto = nuke.nodes.Roto(
        inputs=[plugin_node],
        xpos=plugin_node.xpos() + 120,
        ypos=plugin_node.ypos() + 50,
    )
    try:
        roto['label'].setValue('from AlphaToRoto')
    except Exception:
        pass

    curves_knob = roto['curves']
    root_layer = curves_knob.rootLayer

    outer = holes = 0
    for sd in shapes_data:
        shape = _build_shape(curves_knob, sd['sign'], sd['segments'])
        root_layer.append(shape)
        if sd['sign'] == '+':
            outer += 1
        else:
            holes += 1

    curves_knob.changed()
    print("AlphaToRoto: built Roto with {} outer shapes, {} holes.".format(
        outer, holes))
    return roto


def convert(plugin_node):
    """Single entry point called by the plugin's 'Convert to Roto' button.

    Dispatches to convert_single_frame() or convert_range() based on the
    'use_frame_range' knob on the plugin.
    """
    use_range = False
    try:
        use_range = bool(plugin_node['use_frame_range'].getValue())
    except (NameError, KeyError):
        pass

    if use_range:
        try:
            first = int(plugin_node['first_frame'].getValue())
            last  = int(plugin_node['last_frame'].getValue())
        except (NameError, KeyError):
            nuke.message("AlphaToRoto: frame range knobs missing. "
                         "Rebuild the plugin against the current source.")
            return None
        if last < first:
            nuke.message("AlphaToRoto: 'last' frame ({}) is before 'first' ({}). "
                         "Check the frame range knobs.".format(last, first))
            return None
        return convert_range(plugin_node, first, last)

    return convert_single_frame(plugin_node)


def _reset_plugin_accumulator(plugin_node):
    """Clear the plugin's C++-side accumulator (and curve_data knob) by
    bumping the hidden _reset_trigger Int_knob, which fires knob_changed
    synchronously on the main thread."""
    try:
        trig = plugin_node['_reset_trigger']
    except (NameError, KeyError):
        raise RuntimeError(
            "AlphaToRoto: plugin is missing _reset_trigger knob -- the "
            "compiled plugin is out of date. Rebuild against the current "
            "AlphaToRoto.cpp."
        )
    try:
        current = int(trig.getValue())
    except Exception:
        current = 0
    trig.setValue(current + 1)


def convert_single_frame(plugin_node):
    """Trace the current frame and build a single-frame Roto."""
    f = int(nuke.frame())

    try:
        _reset_plugin_accumulator(plugin_node)
    except RuntimeError as exc:
        nuke.message(str(exc))
        return None

    # nuke.execute(node, first, last) asks Nuke to run the node's _execute()
    # per frame with the correct OutputContext. For a single frame we just
    # pass the same frame for first and last.
    try:
        nuke.execute(plugin_node, f, f)
    except RuntimeError as exc:
        nuke.message("AlphaToRoto: nuke.execute failed at frame {}: {}".format(
            f, exc))
        return None

    return build_roto_from_plugin(plugin_node)


def convert_range(plugin_node, first, last):
    """Trace every frame in [first, last] via a single nuke.execute() call
    and build a Roto with per-frame shape lifetimes. Returns the new Roto,
    or None on error/cancel.
    """
    if first == last:
        return convert_single_frame(plugin_node)

    if rp is None:
        nuke.message("AlphaToRoto: nuke.rotopaint unavailable ({}).".format(
            _rp_import_error))
        return None

    try:
        curve_data_knob = plugin_node['curve_data']
    except (NameError, KeyError):
        nuke.message("AlphaToRoto: curve_data knob missing. "
                     "Rebuild the plugin against the current source.")
        return None

    try:
        _reset_plugin_accumulator(plugin_node)
    except RuntimeError as exc:
        nuke.message(str(exc))
        return None

    # Nuke's render pipeline handles the per-frame loop AND context
    # propagation for us. Our plugin's _execute() fires once per frame and
    # appends "F <frame>\n<curves...>\n" to curve_data. A render dialog
    # shows progress -- harmless flicker for tiny ranges, useful for long.
    try:
        nuke.execute(plugin_node, first, last)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if 'cancel' in msg or 'abort' in msg:
            return None
        nuke.message("AlphaToRoto: nuke.execute failed: {}".format(exc))
        return None

    data = curve_data_knob.getValue() if hasattr(curve_data_knob, 'getValue') else ''
    if not data or not data.strip():
        nuke.message("AlphaToRoto: no curves were traced across frames "
                     "{}..{}. Check the input alpha and threshold.".format(
                     first, last))
        return None

    per_frame_map = _parse_frame_keyed_data(data)
    if not per_frame_map:
        nuke.message("AlphaToRoto: could not parse accumulated curve data "
                     "(no F-markers found).")
        return None

    total_shapes = sum(len(sd) for sd in per_frame_map.values())
    if total_shapes == 0:
        nuke.message("AlphaToRoto: parsed 0 shapes across frames "
                     "{}..{}.".format(first, last))
        return None

    # Build the Roto with all the collected frames.
    roto = nuke.nodes.Roto(
        inputs=[plugin_node],
        xpos=plugin_node.xpos() + 120,
        ypos=plugin_node.ypos() + 50,
    )
    try:
        roto['label'].setValue('from AlphaToRoto\n[{}..{}]'.format(first, last))
    except Exception:
        pass

    curves_knob = roto['curves']
    root_layer  = curves_knob.rootLayer

    # Build shapes and set per-shape lifetime in a single pass.
    # _apply_single_frame_lifetime() writes directly to the shape's own
    # AnimAttributes via the correct 3-arg set(time, key, value) signature,
    # using the real attribute keys (ltt/ltm/ltn) resolved from Nuke's
    # class constants. No selection dance, no visibility animation.
    lifetime_failed    = 0
    first_failed_shape = None
    total_placed       = 0

    for f in sorted(per_frame_map.keys()):
        for sd in per_frame_map[f]:
            shape = _build_shape(curves_knob, sd['sign'], sd['segments'])
            root_layer.append(shape)
            total_placed += 1
            if not _apply_single_frame_lifetime(shape, f):
                # Fall back to animated visibility if lifetime write failed
                # (shouldn't happen on Nuke 13+; safety net only).
                if not _apply_single_frame_visibility(shape, f):
                    lifetime_failed += 1
                    if first_failed_shape is None:
                        first_failed_shape = shape

    curves_knob.changed()

    print("AlphaToRoto: built Roto with {} shapes across frames {}..{} "
          "({} distinct frames).".format(
              total_placed, first, last, len(per_frame_map)))
    if lifetime_failed:
        print("AlphaToRoto: WARNING -- could not set per-frame lifetime on "
              "{} of {} shapes. Those shapes will render on every frame. "
              "Auto-running diagnose_shape_attrs() on the first failing "
              "shape -- paste the output below to the plugin author to "
              "fix:".format(lifetime_failed, total_placed))
        try:
            diagnose_shape_attrs(first_failed_shape)
        except Exception as exc:
            print("(diagnostic failed: {})".format(exc))
    return roto


def _apply_single_frame_lifetime(shape, frame):
    """Set `shape`'s lifetime to 'single frame' at `frame`.

    Uses the AnimAttributes.set(time, key, value) animated-setter API,
    which is the real way to write lifetime in Nuke's rotopaint. Previous
    iterations of this code guessed at keys ('lt'/'ls'/'le') and used
    the 2-arg setter -- both were wrong. The correct keys are resolved
    from class constants, and the correct setter is the 3-arg form:

        attrs.set(time, key, value)

    Attribute keys (confirmed by reading the class constants in Nuke 17):
      kLifeTimeTypeAttribute -> 'ltt'   (lifetime type enum)
      kLifeTimeMAttribute    -> 'ltm'   (lifetime M = start/from)
      kLifeTimeNAttribute    -> 'ltn'   (lifetime N = end/to)

    Lifetime type enum (confirmed against the Roto UI's Lifetime tab):
      0 = all frames (default)
      1 = single frame         <-- our target
      2 = from start to frame
      3 = from frame to end
      4 = frame range

    We place a single keyframe at time=1.0. Since lifetime isn't animated
    over time, one keyframe is sufficient -- the value holds at all times.
    """
    try:
        attrs = shape.getAttributes()
    except Exception:
        return False

    # Resolve the real attribute names from class constants. Fall back to
    # the string literals if the constants aren't exposed for some reason.
    K_TYPE = getattr(attrs, 'kLifeTimeTypeAttribute', 'ltt')
    K_M    = getattr(attrs, 'kLifeTimeMAttribute',    'ltm')
    K_N    = getattr(attrs, 'kLifeTimeNAttribute',    'ltn')

    LIFETIME_SINGLE_FRAME = 1

    try:
        attrs.set(1.0, K_TYPE, LIFETIME_SINGLE_FRAME)
        attrs.set(1.0, K_M,    float(frame))
        attrs.set(1.0, K_N,    float(frame))
    except Exception:
        return False

    # Verify via read-back.
    try:
        t = attrs.getValue(1.0, K_TYPE)
        m = attrs.getValue(1.0, K_M)
        n = attrs.getValue(1.0, K_N)
        if abs(t - LIFETIME_SINGLE_FRAME) > 0.01:
            return False
        if abs(m - frame) > 0.01 or abs(n - frame) > 0.01:
            return False
    except Exception:
        # If read-back isn't available, trust the setters
        pass

    return True


# Kept for backward compatibility / diagnostic purposes.
def _apply_single_frame_visibility(shape, frame):
    """Fallback: animate shape visibility instead of lifetime.

    Uses Shape.setVisible(value, time). Same visual effect as
    _apply_single_frame_lifetime but leaves Life="all" in the scene graph.
    Only used if the real lifetime approach fails.
    """
    try:
        shape.setVisible(0.0, frame - 1)
        shape.setVisible(1.0, frame)
        shape.setVisible(0.0, frame + 1)
    except Exception:
        return False
    return True


def _try_set_selected(obj, value):
    """Kept for the diagnostic path."""
    ok = False
    if hasattr(obj, 'selected'):
        try:
            obj.selected = bool(value); ok = True
        except (AttributeError, TypeError):
            pass
    if not ok and hasattr(obj, 'setSelected'):
        try:
            obj.setSelected(bool(value)); ok = True
        except Exception:
            pass
    return ok


_LIFETIME_TYPE_SINGLE_FRAME = 1
_LIFETIME_TYPE_FRAME_RANGE  = 4


def _set_shape_lifetime(shape, start_frame, end_frame):
    """DEPRECATED: kept only so diagnose_shape_attrs() has a reference
    entry point. The real path is _apply_single_frame_lifetime() which
    uses the correct 3-arg attrs.set(time, key, value) signature.
    """
    return _apply_single_frame_lifetime(shape, start_frame)


def diagnose_shape_attrs(shape=None):
    """Print everything we can discover about a rotopaint Shape's attribute
    API, to help tune _set_shape_lifetime for this Nuke build.

    Usage from the Script Editor (with a Roto node selected that contains
    at least one shape):

        import alphaToRoto_py
        alphaToRoto_py.diagnose_shape_attrs()

    Paste the printed output back to the plugin author.
    """
    if shape is None:
        try:
            n = nuke.selectedNode()
            shape = n['curves'].rootLayer[0]
        except Exception as exc:
            print("Select a Roto node with at least one shape first.")
            print("Error: {}".format(exc))
            return

    print("=" * 70)
    print("AlphaToRoto shape-attr diagnostic")
    print("=" * 70)
    print("Shape object: {}".format(shape))
    print("Shape type  : {}".format(type(shape).__name__))
    public = [a for a in dir(shape) if not a.startswith('_')]
    print("Shape dir (public): {}".format(public))
    print()

    try:
        attrs = shape.getAttributes()
    except Exception as exc:
        print("shape.getAttributes() raised: {}".format(exc))
        return

    print("Attributes object: {}".format(attrs))
    print("Attributes type  : {}".format(type(attrs).__name__))
    attrs_public = [a for a in dir(attrs) if not a.startswith('_')]
    print("Attributes dir (public): {}".format(attrs_public))
    print()

    # Probe every plausible lifetime-related key to see what raises and
    # what returns a value.
    probe_keys = [
        # lifetime
        'lt', 'ltm', 'life_type', 'lifetime_type', 'lifeType',
        'ls', 'life_start', 'lifetime_start', 'lifeStart',
        'le', 'life_end', 'lifetime_end', 'lifeEnd',
        # well-known ones that should succeed (sanity check)
        'r', 'g', 'b', 'a', 'op', 'opc',
    ]
    print("Probing attribute keys (via attrs.get() if available):")
    get_fn = getattr(attrs, 'get', None)
    for k in probe_keys:
        if get_fn is None:
            print("  attrs has no get() method; can't probe {!r}".format(k))
            break
        try:
            v = get_fn(k)
            print("  {!r:30s} = {!r}".format(k, v))
        except Exception as exc:
            print("  {!r:30s} raised {}: {}".format(k, type(exc).__name__, exc))

    # Try to write each candidate key with a benign test value and report.
    print()
    print("Probing attribute keys (via attrs.set() with a test value):")
    set_fn = getattr(attrs, 'set', None)
    for k in probe_keys:
        if set_fn is None:
            print("  attrs has no set() method; can't probe {!r}".format(k))
            break
        try:
            set_fn(k, 1)
            print("  set({!r}, 1) succeeded".format(k))
        except Exception as exc:
            print("  set({!r}, 1) raised {}: {}".format(k, type(exc).__name__, exc))

    print("=" * 70)
    print("END diagnostic -- please paste the above back to plugin author.")


# ===========================================================================
# Option B: N animated shapes with K keyframed control points each
# ---------------------------------------------------------------------------
#
# Unlike convert_range() which creates N shapes with single-frame lifetime
# (heavy Roto, no interpolation), convert_range_animated() creates one
# animated shape per persistent "track" across frames. A track is a
# distinct outer contour whose identity is followed across frames by
# centroid-distance assignment -- so two wheels moving around the screen
# become two separate animated shapes (Track1, Track2) rather than one
# shape flickering between positions.
#
# Pipeline:
#   1. nuke.execute(plugin_node, first, last) -- reuses Option A's frame-
#      by-frame tracing
#   2. For each frame, collect ALL outer (+) shapes (not just the biggest)
#   3. Determine num_tracks = max outer-shape count across frames
#   4. Initialize tracks from frame 1 (sorted left-to-right by centroid x)
#   5. For each subsequent frame, solve the optimal polygon->track
#      assignment by minimum total centroid distance (brute force for
#      small N, greedy fallback above N=8)
#   6. For each track independently: flatten beziers, arc-length resample
#      to K points, cyclic-align per frame, build a Roto shape with K
#      ShapeControlPoints whose centers are keyframed per frame
#
# Usage (button "Convert to Animated Roto", or from Script Editor):
#
#     import alphaToRoto_py
#     alphaToRoto_py.convert_range_animated(nuke.toNode('AlphaToRoto1'))
#
# Caveats:
#   * Holes (- shapes) are also tracked across frames in their own
#     parallel pass, then animated as stencil shapes appended after the
#     outer shapes in the same root layer (so they cut through). Hole
#     identity is matched by centroid distance independently of outers.
#   * Tracks can gain/lose frames if a shape temporarily disappears or
#     merges with another; Nuke holds the last keyframe value on missing
#     frames.
#   * Shape identity is geometric (centroid distance); shapes that cross
#     paths may swap identity. For complex crossings, Option A is safer.
# ===========================================================================

# Module-level cache for the probed cp.center keyframe API. Set on first call.
_ANIMATE_CP_API = None
_ANIMATE_CP_API_DESC = None


def _cubic_bezier(p0, p1, p2, p3, t):
    """Evaluate a cubic Bezier at parameter t in [0, 1]."""
    u = 1.0 - t
    b0 = u * u * u
    b1 = 3.0 * u * u * t
    b2 = 3.0 * u * t * t
    b3 = t * t * t
    return (b0 * p0[0] + b1 * p1[0] + b2 * p2[0] + b3 * p3[0],
            b0 * p0[1] + b1 * p1[1] + b2 * p2[1] + b3 * p3[1])


# ---------------------------------------------------------------------------
# Module-level Bernstein basis cache, keyed by samples_per_bezier.
# For the default N=16 this holds a single (16, 4) matrix -- ~512 bytes.
# ---------------------------------------------------------------------------
_BASIS_CACHE = {}


def _get_bezier_basis(samples_per_bezier):
    """Return a cached (N, 4) Bernstein basis matrix evaluated at
    t = 1/N, 2/N, ..., 1.0 (i.e. the same sample points as the reference
    Python implementation -- start point is contributed by the previous
    segment, so we skip t=0).
    """
    basis = _BASIS_CACHE.get(samples_per_bezier)
    if basis is not None:
        return basis
    N = samples_per_bezier
    t = np.linspace(1.0 / N, 1.0, N, dtype=np.float64)
    u = 1.0 - t
    basis = np.stack([u*u*u, 3*u*u*t, 3*u*t*t, t*t*t], axis=1)  # (N, 4)
    _BASIS_CACHE[samples_per_bezier] = basis
    return basis


def _flatten_shape_to_polyline(segments, samples_per_bezier=16):
    """Convert parsed shape segments (move/curve/line) to a flat polyline.

    The result is a CLOSED polyline -- the last point equals the first
    logically, but we don't duplicate it in the list.

    Vectorized implementation: all cubic bezier segments in the shape are
    batched into a single (M, 4, 2) @ (N, 4) matmul rather than one
    Python-level call per segment. ~2x faster than the previous per-segment
    loop on realistic mattes, and numerically equivalent to within float
    round-off.
    """
    if not segments or segments[0][0] != 'move':
        return []

    basis = _get_bezier_basis(samples_per_bezier)

    # Pass 1: walk segments once, collecting:
    #   tokens     - ordered polyline structure; each is ('pt', x, y)
    #                or ('curve', curve_idx) referring to the batched samples
    #   curve_cps  - flat list of (4, 2) control-point lists for one batch call
    _, sx, sy = segments[0]
    tokens = [('pt', sx, sy)]
    curve_cps = []
    prev = (sx, sy)
    for seg in segments[1:]:
        kind = seg[0]
        if kind == 'curve':
            _, c1x, c1y, c2x, c2y, ex, ey = seg
            curve_cps.append([[prev[0], prev[1]],
                              [c1x, c1y],
                              [c2x, c2y],
                              [ex, ey]])
            tokens.append(('curve', len(curve_cps) - 1))
            prev = (ex, ey)
        elif kind == 'line':
            _, ex, ey = seg
            tokens.append(('pt', ex, ey))
            prev = (ex, ey)

    # Pass 2: single batched matmul across all cubic segments in the shape.
    #   cps shape     : (M, 4, 2)
    #   basis shape   : (N, 4)  -- broadcasts
    #   result shape  : (M, N, 2)
    curve_samples = None
    if curve_cps:
        cps = np.asarray(curve_cps, dtype=np.float64)
        curve_samples = basis @ cps

    # Pass 3: weave straight points and curve-sample runs into the output.
    # Convert each numpy block to a Python list once (via .tolist()) rather
    # than per-point to avoid numpy scalar -> Python float overhead.
    result = []
    for tok in tokens:
        if tok[0] == 'pt':
            result.append((float(tok[1]), float(tok[2])))
        else:
            samples = curve_samples[tok[1]].tolist()
            result.extend((pt[0], pt[1]) for pt in samples)

    # If the polyline closes back to its start, drop the duplicate last
    # point so our arc-length math handles it cleanly as a closed loop.
    if len(result) >= 2:
        if (abs(result[0][0] - result[-1][0]) < 0.1
                and abs(result[0][1] - result[-1][1]) < 0.1):
            result.pop()
    return result


def _polyline_perimeter(points):
    """Total arc length of a closed polyline."""
    if len(points) < 2:
        return 0.0
    total = 0.0
    for i in range(len(points)):
        j = (i + 1) % len(points)
        dx = points[j][0] - points[i][0]
        dy = points[j][1] - points[i][1]
        total += (dx * dx + dy * dy) ** 0.5
    return total


def _resample_polyline_to_K(points, K):
    """Resample a closed polyline to exactly K evenly-spaced points (by
    arc length). Returns a list of K (x, y) tuples.
    """
    if K <= 0:
        return []
    if len(points) < 2:
        return [points[0]] * K if points else [(0.0, 0.0)] * K

    # Treat as closed: final virtual point == first
    closed = list(points) + [points[0]]

    # Cumulative arc length at each vertex
    lengths = [0.0]
    for i in range(len(closed) - 1):
        dx = closed[i + 1][0] - closed[i][0]
        dy = closed[i + 1][1] - closed[i][1]
        lengths.append(lengths[-1] + (dx * dx + dy * dy) ** 0.5)

    total = lengths[-1]
    if total < 1e-6:
        return [points[0]] * K

    step = total / K
    result = []
    j = 0  # current segment index
    for i in range(K):
        target = i * step
        while j + 1 < len(lengths) and lengths[j + 1] < target:
            j += 1
        if j + 1 >= len(lengths):
            result.append(closed[-1])
            continue
        seg_len = lengths[j + 1] - lengths[j]
        if seg_len < 1e-6:
            result.append(closed[j])
        else:
            t = (target - lengths[j]) / seg_len
            x = closed[j][0] + t * (closed[j + 1][0] - closed[j][0])
            y = closed[j][1] + t * (closed[j + 1][1] - closed[j][1])
            result.append((x, y))
    return result


def _cyclic_align(curr, prev):
    """Rotate `curr` cyclically so its points best match `prev`.

    Both lists must be the same length K. Returns a new list of length K.

    Math: we want argmin_offset sum_i |curr[(i+offset) % K] - prev[i]|^2.
    Expanding the quadratic, this is equivalent to
        argmax_offset sum_i curr[(i+offset) % K] . prev[i],
    which is the circular cross-correlation of curr and prev evaluated
    at lag `offset`. Computed via FFT:
        corr[offset] = ifft(fft(curr_x) * conj(fft(prev_x)))[offset]
                     + ifft(fft(curr_y) * conj(fft(prev_y)))[offset]
    and we return the offset at the peak.

    Complexity O(K log K) vs the previous O(K^2) exhaustive search.
    At K=2048 this takes ~3ms per call vs ~60ms for the Python reference,
    so across ~100 frames the align phase drops from seconds to well
    under a second.

    Uses rfft (real-input FFT) since the coordinates are real-valued --
    saves memory by exploiting Hermitian symmetry in the spectrum.
    """
    K = len(curr)
    if K == 0 or K != len(prev):
        return list(curr)
    if K == 1:
        return list(curr)

    c = np.asarray(curr, dtype=np.float64)   # (K, 2)
    p = np.asarray(prev, dtype=np.float64)   # (K, 2)

    # Per-axis circular cross-correlation, summed across x and y
    Cx = np.fft.rfft(c[:, 0]); Px = np.fft.rfft(p[:, 0])
    Cy = np.fft.rfft(c[:, 1]); Py = np.fft.rfft(p[:, 1])
    corr = (np.fft.irfft(Cx * np.conj(Px), n=K)
          + np.fft.irfft(Cy * np.conj(Py), n=K))

    best_offset = int(np.argmax(corr))
    if best_offset == 0:
        return list(curr)
    rolled = np.roll(c, -best_offset, axis=0)
    return [(float(x), float(y)) for x, y in rolled]


def _polygon_centroid(poly):
    """Arithmetic-mean centroid of a polygon's vertices.

    Not the true area-weighted centroid (which we'd want for irregular
    shapes), but good enough for the shape-identity tracking use case.
    Roto shapes we generate from arc-length-resampled polylines have
    uniformly-spaced vertices, so the arithmetic mean is close to the
    area centroid anyway.
    """
    if not poly:
        return (0.0, 0.0)
    sx = 0.0
    sy = 0.0
    for p in poly:
        sx += p[0]
        sy += p[1]
    n = float(len(poly))
    return (sx / n, sy / n)


def _hungarian_min_cost(cost):
    """Rectangular Hungarian (Kuhn-Munkres) assignment.

    Finds the min-cost one-to-one assignment of rows to columns. Works for
    rectangular cost[n_rows][n_cols] with n_rows <= n_cols. Returns a list
    `row_to_col` of length n_rows: row_to_col[r] is the column assigned
    to row r (or -1 if the row got no column -- shouldn't happen when
    n_rows <= n_cols).

    O(n_rows^2 * n_cols). Pure Python; no scipy dependency. Used instead
    of the factorial brute force, which was O(N! * N!) and blew up at N=8.
    """
    INF = float('inf')
    n = len(cost)
    if n == 0:
        return []
    m = len(cost[0])
    assert n <= m, "Hungarian requires n_rows ({}) <= n_cols ({})".format(n, m)

    # Pad to square with zero-cost dummy rows so we can use the square
    # algorithm. Dummy rows will "absorb" excess columns.
    size = m
    # Build augmented square matrix
    a = [[0.0] * size for _ in range(size)]
    for i in range(n):
        for j in range(m):
            a[i][j] = cost[i][j]
    # Rows n..size-1 stay all-zero (dummies)

    u = [0.0] * (size + 1)
    v = [0.0] * (size + 1)
    p = [0]   * (size + 1)   # p[j] = row assigned to column j (1-indexed)
    way = [0] * (size + 1)

    for i in range(1, size + 1):
        p[0] = i
        j0 = 0
        minv = [INF] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = INF
            j1 = -1
            for j in range(1, size + 1):
                if used[j]:
                    continue
                cur = a[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j]  = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(size + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j]    -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0 != 0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1

    # Extract: col_to_row, then invert for real rows only
    row_to_col = [-1] * n
    for j in range(1, size + 1):
        row = p[j] - 1
        if 0 <= row < n:
            row_to_col[row] = j - 1
    return row_to_col


def _solve_assignment(poly_centroids, track_centroids,
                      poly_sizes=None, track_sizes=None,
                      size_weight=2.0):
    """Assign each polygon (by index) to at most one track (by index).

    Inputs:
        poly_centroids : list of (x, y) -- the polygons on the CURRENT frame
        track_centroids: list of (x, y) or None -- each track's last-known
                         centroid; None for inactive/empty tracks
        poly_sizes     : optional list of float (perimeters) matching polys
        track_sizes    : optional list of float (last-known perimeters)
                         matching tracks, or None for inactive tracks
        size_weight    : strength of the size-compatibility penalty.
                         Set to 0 to disable (purely centroid-based).

    Output:
        list `assignment` of length len(poly_centroids); assignment[i] is
        either the track index to which polygon i is assigned, or -1 if
        polygon i has no good match.

    Cost model:
        cost = (sq_centroid_dist + 1) * ratio^size_weight

        where ratio = max(poly_size, track_size) / min(...)  (>= 1).

        With size_weight=2.0: a 2x size mismatch multiplies cost by 4;
        a 10x mismatch by 100; a 50x mismatch by 2500. That's what stops
        a tiny foot-speckle from being assigned to a whole-body track
        just because its centroid landed closer.

        Multiplicative form is necessary because sq_dist scales with
        pixels^2 (magnitudes of 100s to 10000s) while log(ratio) only
        reaches ~5 for a 150x mismatch; an additive log-based penalty
        gets drowned out. ratio^weight keeps both terms comparable.

    Strategy:
        - N<=4: brute force (576 ops max) -- tiny, guaranteed optimal
        - N>4 : Hungarian O(N^3) -- optimal and fast
    """
    n_polys = len(poly_centroids)
    n_tracks = len(track_centroids)
    if n_polys == 0:
        return []

    active_tracks = [(i, c) for i, c in enumerate(track_centroids) if c is not None]
    if not active_tracks:
        # First frame case: assign polys 0..min(n, n_tracks)-1 to tracks
        # 0..min(n, n_tracks)-1; leftover polys get -1 (unassigned).
        out = [-1] * n_polys
        for i in range(min(n_polys, n_tracks)):
            out[i] = i
        return out

    n_active = len(active_tracks)

    def sq_dist(a, b):
        dx = a[0] - b[0]
        dy = a[1] - b[1]
        return dx * dx + dy * dy

    def edge_cost(p_idx, t_idx):
        """Combined centroid + size-compatibility cost."""
        d = sq_dist(poly_centroids[p_idx], track_centroids[t_idx])
        if (size_weight <= 0.0 or poly_sizes is None or track_sizes is None
                or track_sizes[t_idx] is None):
            return d
        ps = poly_sizes[p_idx]
        ts = track_sizes[t_idx]
        if ps <= 0 or ts <= 0:
            return d
        ratio = max(ps, ts) / min(ps, ts)  # >= 1
        # Multiplicative penalty: (sq_dist + 1) * ratio^weight
        return (d + 1.0) * (ratio ** size_weight)

    track_idx_list = [i for i, _ in active_tracks]

    # --- Brute force path (N<=4) ---------------------------------------
    # Threshold dropped from 8 to 4: at 8, the two nested permutations
    # hit 40320 * 40320 = 1.6B iterations per frame, which pegged CPU
    # for minutes and eventually got us SIGKILL'd. At 4 the worst case
    # is 4! * 4! = 576 iterations -- free.
    if n_polys <= 4 and n_active <= 4:
        from itertools import permutations as _p
        k = min(n_polys, n_active)
        best_cost = float('inf')
        best = None
        poly_indices = list(range(n_polys))
        cost = [[edge_cost(p, t) for t in track_idx_list] for p in poly_indices]
        for poly_subset in _p(poly_indices, k):
            for track_perm in _p(range(n_active), k):
                c = 0.0
                for i in range(k):
                    c += cost[poly_subset[i]][track_perm[i]]
                    if c >= best_cost:
                        break
                if c < best_cost:
                    best_cost = c
                    best = (poly_subset, track_perm)
        out = [-1] * n_polys
        if best is not None:
            poly_subset, track_perm = best
            for i, p in enumerate(poly_subset):
                out[p] = track_idx_list[track_perm[i]]
        return out

    # --- Hungarian path (N>4) ------------------------------------------
    cost = [[edge_cost(p, t) for t in track_idx_list] for p in range(n_polys)]

    if n_polys <= n_active:
        row_to_col = _hungarian_min_cost(cost)
        out = [-1] * n_polys
        for p, col in enumerate(row_to_col):
            if col >= 0:
                out[p] = track_idx_list[col]
        return out
    else:
        # Transpose so rows=tracks, cols=polys. Solve, then invert.
        n = n_active
        m = n_polys
        cost_t = [[cost[p][t] for p in range(m)] for t in range(n)]
        track_to_poly = _hungarian_min_cost(cost_t)
        out = [-1] * n_polys
        for t, p in enumerate(track_to_poly):
            if p >= 0:
                out[p] = track_idx_list[t]
        return out


def _build_tracks_from_per_frame(per_frame_outer_polylines, num_tracks,
                                 min_relative_size=0.1):
    """Group per-frame outer polylines into `num_tracks` persistent tracks.

    Input:  {frame: [polyline, polyline, ...]}  (all outer + shapes per frame)
    Output: list of `num_tracks` dicts, each {frame: polyline}

    Tracks are initialized from the first frame's polylines (sorted
    left-to-right by centroid x, so tracks are indexed deterministically).
    Subsequent frames' polylines are assigned to tracks by the optimal
    minimum-cost permutation, where cost combines centroid distance and
    size compatibility (perimeter ratio).

    Args:
        min_relative_size: Drop any polyline whose perimeter is less than
            this fraction of the biggest polyline on the same frame. 0.1
            removes tiny fragments / speckles / shadow bits that would
            otherwise become their own tracks OR steal identity from a
            real track when the Hungarian centroid lands close. Set to 0
            to disable.
    """
    # --- Per-frame relative-size filter ---------------------------------
    # Without this, a small foot-fragment contour on some frame can get
    # assigned to a big-subject track just because its centroid happened
    # to be close, producing "animated surfer from small to normal size"
    # visual artifacts. The size-aware Hungarian cost below also helps,
    # but dropping the noise up front is cleaner.
    if min_relative_size > 0:
        filtered = {}
        total_dropped = 0
        total_kept = 0
        for f, polys in per_frame_outer_polylines.items():
            if not polys:
                continue
            perims = [_polyline_perimeter(p) for p in polys]
            max_p = max(perims)
            threshold = min_relative_size * max_p
            kept = [p for p, per in zip(polys, perims) if per >= threshold]
            dropped = len(polys) - len(kept)
            total_kept += len(kept)
            total_dropped += dropped
            if kept:
                filtered[f] = kept
        _dbg("  size filter (min_relative_size={}): kept {}, dropped {} "
             "sub-threshold polylines".format(
                 min_relative_size, total_kept, total_dropped))
        per_frame_outer_polylines = filtered
        if not per_frame_outer_polylines:
            return []
        # num_tracks may shrink after filter
        num_tracks = max(len(polys) for polys in per_frame_outer_polylines.values())
        _dbg("  after filter, num_tracks = {}".format(num_tracks))

    sorted_frames = sorted(per_frame_outer_polylines.keys())
    if not sorted_frames:
        return []

    # Initialize tracks from first frame, left-to-right by centroid x
    first_f = sorted_frames[0]
    first_polys = per_frame_outer_polylines[first_f]
    first_centroids = [_polygon_centroid(p) for p in first_polys]
    order = sorted(range(len(first_polys)), key=lambda i: first_centroids[i][0])

    tracks = []
    track_sizes = []  # perimeter of each track's last-known polyline
    for rank in range(num_tracks):
        if rank < len(order):
            poly = first_polys[order[rank]]
            tracks.append({first_f: poly})
            track_sizes.append(_polyline_perimeter(poly))
        else:
            tracks.append({})
            track_sizes.append(None)

    # Assign subsequent frames' polygons to tracks
    frames_done = 0
    total_frames = len(sorted_frames) - 1
    for f in sorted_frames[1:]:
        polys = per_frame_outer_polylines[f]
        if not polys:
            continue
        poly_centroids = [_polygon_centroid(p) for p in polys]
        poly_sizes = [_polyline_perimeter(p) for p in polys]
        # Each track's last-known centroid (None if track is empty so far)
        track_centroids = []
        track_last_sizes = []
        for ti, t in enumerate(tracks):
            if t:
                last_f = max(t.keys())
                track_centroids.append(_polygon_centroid(t[last_f]))
                track_last_sizes.append(track_sizes[ti])
            else:
                track_centroids.append(None)
                track_last_sizes.append(None)
        assignment = _solve_assignment(
            poly_centroids, track_centroids,
            poly_sizes=poly_sizes, track_sizes=track_last_sizes)
        for poly_idx, track_idx in enumerate(assignment):
            if track_idx >= 0:
                tracks[track_idx][f] = polys[poly_idx]
                track_sizes[track_idx] = poly_sizes[poly_idx]
        frames_done += 1
        if frames_done == 1 or frames_done % 20 == 0:
            n_active = sum(1 for c in track_centroids if c is not None)
            _dbg("  track assignment: frame {} ({}/{}), n_polys={}, "
                 "n_active={}".format(f, frames_done, total_frames,
                                      len(polys), n_active))

    return tracks


def _probe_and_cache_cp_animate_api(cp):
    """Discover which AnimControlPoint keyframe method this Nuke build uses.

    Writes a test keyframe on cp.center at time=-1000 (outside any likely
    playback range), reads it back, and caches the working signature in
    _ANIMATE_CP_API / _ANIMATE_CP_API_DESC for subsequent calls.

    Returns True on success.
    """
    global _ANIMATE_CP_API, _ANIMATE_CP_API_DESC

    center = cp.center
    probe_time = -1000.0
    probe_pt = (12345.0, 67890.0)

    candidates = [
        ('addPositionKey(time, point)',
         lambda c, t, pt: c.addPositionKey(t, pt)),
        ('addPositionKey(time, x, y)',
         lambda c, t, pt: c.addPositionKey(t, pt[0], pt[1])),
        ('addPositionKey(point, time)',
         lambda c, t, pt: c.addPositionKey(pt, t)),
        ('setPositionKey(time, point)',
         lambda c, t, pt: c.setPositionKey(t, pt)),
        ('setPosition(point, time)',
         lambda c, t, pt: c.setPosition(pt, t)),
        ('setPosition(x, y, time)',
         lambda c, t, pt: c.setPosition(pt[0], pt[1], t)),
    ]

    def _read_back(c, t):
        """Try various reader method names. Return (x, y) or None."""
        for getter_name in ('getPositionAtTime', 'getPosition', 'evaluate'):
            getter = getattr(c, getter_name, None)
            if getter is None:
                continue
            try:
                val = getter(t)
            except Exception:
                continue
            # val might be a tuple, or an object with .x/.y
            if hasattr(val, 'x') and hasattr(val, 'y'):
                return (float(val.x), float(val.y))
            try:
                return (float(val[0]), float(val[1]))
            except Exception:
                continue
        return None

    for desc, fn in candidates:
        try:
            fn(center, probe_time, probe_pt)
        except Exception:
            continue
        # If the call didn't raise, verify via readback when possible
        got = _read_back(center, probe_time)
        if got is None:
            # Can't verify; accept this one optimistically. It's the first
            # candidate that didn't raise -- most likely to be correct.
            _ANIMATE_CP_API = fn
            _ANIMATE_CP_API_DESC = desc + ' (unverified -- no readback method)'
            return True
        if abs(got[0] - probe_pt[0]) < 0.5 and abs(got[1] - probe_pt[1]) < 0.5:
            _ANIMATE_CP_API = fn
            _ANIMATE_CP_API_DESC = desc
            return True

    return False


def _animate_cp_center(cp, frame, x, y):
    """Add a keyframe on cp.center at `frame` with position (x, y).

    Caller must ensure _ANIMATE_CP_API has already been cached via
    _probe_and_cache_cp_animate_api() on a throwaway CP.
    """
    if _ANIMATE_CP_API is None:
        return False
    try:
        _ANIMATE_CP_API(cp.center, float(frame), (float(x), float(y)))
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Crash-safety: dump curve_data to disk BEFORE the expensive Python build
# phase starts. If Nuke crashes, dies, or the user force-quits, the trace
# data survives and can be rebuilt via rebuild_animated_from_dump() without
# re-tracing. For a 741-frame 4K run that's the difference between "rerun
# the button in 30 seconds" and "retrace for 10 minutes again".
# ---------------------------------------------------------------------------
def _dump_curve_data_sidecar(data, plugin_node, first, last):
    """Write curve_data to a sidecar .a2r file on disk and return the path.

    Tries three locations in order:
      1. next to the .nk script (<script>.a2r_<first>-<last>_<ts>.txt)
      2. ~/.nuke
      3. system temp dir

    Returns the path on success, or None if nothing worked. Failure is
    non-fatal -- we still proceed with the build.
    """
    ts = time.strftime('%Y%m%d-%H%M%S')
    node_name = plugin_node.name() if plugin_node else 'AlphaToRoto'
    filename = '{}_{}-{}_{}.a2r.txt'.format(node_name, first, last, ts)

    candidates = []
    try:
        script_path = nuke.root().name()
        if script_path and script_path != 'Root':
            script_dir = os.path.dirname(os.path.abspath(script_path))
            if script_dir:
                candidates.append(os.path.join(script_dir, filename))
    except Exception:
        pass

    nuke_dir = os.path.expanduser('~/.nuke')
    if os.path.isdir(nuke_dir):
        candidates.append(os.path.join(nuke_dir, filename))

    candidates.append(os.path.join(tempfile.gettempdir(), filename))

    for path in candidates:
        try:
            with open(path, 'w') as fh:
                fh.write(data)
            return path
        except Exception:
            continue
    return None


def rebuild_animated_from_dump(dump_path, plugin_node=None,
                               num_points=None, samples_per_bezier=16):
    """Rebuild an animated Roto from a sidecar .a2r.txt dump.

    Use this if Nuke crashed or you force-quit during the Python build
    phase of convert_range_animated(). No retrace required -- reads the
    dumped curve_data blob and runs the same build pipeline.

    Args:
        dump_path: Path to the .a2r.txt file written by a previous
                   convert_range_animated() call (path is printed to the
                   script editor on every run).
        plugin_node: AlphaToRoto node to wire the new Roto to. If None,
                     uses the selected node.
        num_points, samples_per_bezier: Same as convert_range_animated().
    """
    if rp is None:
        nuke.message("AlphaToRoto: nuke.rotopaint unavailable ({}).".format(
            _rp_import_error))
        return None

    if not os.path.isfile(dump_path):
        nuke.message("rebuild_animated_from_dump: file not found: {}".format(
            dump_path))
        return None

    if plugin_node is None:
        try:
            plugin_node = nuke.selectedNode()
        except Exception:
            nuke.message("rebuild_animated_from_dump: no node selected. "
                         "Select an AlphaToRoto node or pass plugin_node.")
            return None

    try:
        with open(dump_path, 'r') as fh:
            data = fh.read()
    except Exception as exc:
        nuke.message("rebuild_animated_from_dump: could not read {}: {}".format(
            dump_path, exc))
        return None

    if not data or not data.strip():
        nuke.message("rebuild_animated_from_dump: dump file is empty.")
        return None

    # Mirror the debug log next to the dump file
    log_path = dump_path.replace('.a2r.txt', '.a2r.log')
    if log_path == dump_path:
        log_path = dump_path + '.log'
    _dbg_open(log_path)
    _dbg("rebuild_animated_from_dump: source={} ({} bytes)".format(
        dump_path, len(data)))

    # Reuse the same build pipeline as convert_range_animated, skipping the
    # trace step. We manually push the data into the plugin's curve_data
    # knob so the rest of convert_range_animated's body can run unchanged.
    try:
        plugin_node['curve_data'].setValue(data)
    except Exception:
        pass  # not fatal, we pass `data` through directly below

    print("AlphaToRoto: rebuilding from dump {} ({} bytes)".format(
        dump_path, len(data)))
    result = _build_animated_roto_from_blob(
        plugin_node, data,
        num_points=num_points,
        samples_per_bezier=samples_per_bezier,
        skipped_trace=True,
    )
    _dbg("rebuild_animated_from_dump DONE (result={})".format(
        "roto" if result is not None else "None"))
    _dbg_close()
    return result


def convert_range_animated(plugin_node=None, first=None, last=None,
                           num_points=None, samples_per_bezier=16):
    """Option B: build ONE Roto shape with K keyframed control points,
    one keyframe per traced frame.

    Args:
        plugin_node: The AlphaToRoto node. If None, uses the selected node.
        first, last: Frame range. If None, reads from plugin_node's
                     first_frame/last_frame knobs.
        num_points: K, the number of control points. If None, picks the
                    median polyline point count across frames, clamped
                    to [32, 128].
        samples_per_bezier: How densely to flatten potrace's cubic beziers
                            into the intermediate polyline. 16 is fine
                            for most shots.

    Returns:
        The new Roto node, or None on error.
    """
    if rp is None:
        nuke.message("AlphaToRoto: nuke.rotopaint unavailable ({}).".format(
            _rp_import_error))
        return None

    if plugin_node is None:
        try:
            plugin_node = nuke.selectedNode()
        except Exception:
            nuke.message("convert_range_animated: no node selected and no "
                         "plugin_node passed. Select an AlphaToRoto node or "
                         "pass one explicitly.")
            return None

    # Resolve frame range from knobs if not given
    if first is None:
        try:
            first = int(plugin_node['first_frame'].getValue())
        except Exception:
            first = int(nuke.root().firstFrame())
    if last is None:
        try:
            last = int(plugin_node['last_frame'].getValue())
        except Exception:
            last = int(nuke.root().lastFrame())
    if last < first:
        nuke.message("convert_range_animated: last ({}) < first ({}).".format(
            last, first))
        return None

    # Open the debug log BEFORE we do anything heavy so we have a trail.
    # Put it in the same directory the dump will go to.
    ts = time.strftime('%Y%m%d-%H%M%S')
    node_name = plugin_node.name() if plugin_node else 'AlphaToRoto'
    log_basename = '{}_{}-{}_{}.a2r.log'.format(node_name, first, last, ts)
    log_candidates = []
    try:
        script_path = nuke.root().name()
        if script_path and script_path != 'Root':
            script_dir = os.path.dirname(os.path.abspath(script_path))
            if script_dir:
                log_candidates.append(os.path.join(script_dir, log_basename))
    except Exception:
        pass
    nuke_dir = os.path.expanduser('~/.nuke')
    if os.path.isdir(nuke_dir):
        log_candidates.append(os.path.join(nuke_dir, log_basename))
    log_candidates.append(os.path.join(tempfile.gettempdir(), log_basename))
    for cand in log_candidates:
        _dbg_open(cand)
        if _DBG_FILE is not None:
            print("AlphaToRoto: debug log at {}".format(cand))
            break

    _dbg("convert_range_animated: node={} first={} last={} ({} frames)".format(
        node_name, first, last, last - first + 1))

    # Reset accumulator and run the plugin across the range
    try:
        _reset_plugin_accumulator(plugin_node)
    except RuntimeError as exc:
        _dbg("FATAL: reset accumulator failed: {}".format(exc))
        nuke.message(str(exc))
        _dbg_close()
        return None

    _dbg("phase 1/4: nuke.execute (tracing) -- entering")
    try:
        nuke.execute(plugin_node, first, last)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if 'cancel' in msg or 'abort' in msg:
            _dbg("nuke.execute cancelled by user")
            _dbg_close()
            return None
        _dbg("FATAL: nuke.execute failed: {}".format(exc))
        nuke.message("convert_range_animated: nuke.execute failed: {}".format(exc))
        _dbg_close()
        return None
    _dbg("phase 1/4: nuke.execute returned")

    _dbg("phase 2/4: reading curve_data from plugin knob")
    data = plugin_node['curve_data'].getValue()
    if not data or not data.strip():
        _dbg("FATAL: curve_data is empty")
        nuke.message("convert_range_animated: no curves were traced.")
        _dbg_close()
        return None
    _dbg("curve_data size: {:.2f} MB ({} bytes, {} lines)".format(
        len(data) / (1024.0 * 1024.0), len(data), data.count('\n')))

    # --- Crash-safety checkpoint -----------------------------------------
    # Dump the traced blob to disk BEFORE the heavy Python build phase.
    # If Nuke hangs/dies during shape construction, re-run the build via
    # rebuild_animated_from_dump(<path>) instead of re-tracing.
    _dbg("phase 3/4: writing sidecar dump")
    dump_path = _dump_curve_data_sidecar(data, plugin_node, first, last)
    if dump_path:
        _dbg("dump written: {}".format(dump_path))
        print("AlphaToRoto: trace dumped to {}".format(dump_path))
        print("AlphaToRoto: if the build phase below crashes, recover via:")
        print("  alphaToRoto_py.rebuild_animated_from_dump({!r})".format(dump_path))
    else:
        _dbg("WARNING: sidecar dump failed; a crash during build will lose the trace")
        print("AlphaToRoto: WARNING -- could not write sidecar dump; "
              "a crash during build would lose the trace.")

    _dbg("phase 4/4: handing off to _build_animated_roto_from_blob")
    result = _build_animated_roto_from_blob(
        plugin_node, data,
        num_points=num_points,
        samples_per_bezier=samples_per_bezier,
        skipped_trace=False,
    )
    _dbg("convert_range_animated DONE (result={})".format(
        "roto" if result is not None else "None"))
    _dbg_close()
    return result


def _build_animated_roto_from_blob(plugin_node, data,
                                   num_points=None,
                                   samples_per_bezier=16,
                                   skipped_trace=False):
    """Shared build pipeline used by both convert_range_animated() (live
    trace) and rebuild_animated_from_dump() (recovery from sidecar).

    Performance notes:
      - Wraps the keyframe-writing loops in nuke.Undo().disable(). Each
        addPositionKey call would otherwise log an undo entry, multiplying
        main-thread cost by 5-20x on large ranges.
      - Uses nuke.ProgressTask to pump the event loop during the write so
        Nuke doesn't show 'not responding' and the user gets a cancel
        button.
    """
    _dbg("build: entering _build_animated_roto_from_blob (data={} bytes, "
         "skipped_trace={})".format(len(data) if data else 0, skipped_trace))

    _dbg("build: parsing frame-keyed blob")
    frames_data = _parse_frame_keyed_data(data)
    if not frames_data:
        _dbg("FATAL: parse returned empty")
        nuke.message("AlphaToRoto: could not parse curve data.")
        return None
    _total_shapes = sum(len(v) for v in frames_data.values())
    _dbg("build: parsed {} frames, {} shapes total".format(
        len(frames_data), _total_shapes))

    # Derive first/last from the blob (covers the rebuild-from-dump case
    # where the caller didn't supply them).
    all_frames = sorted(frames_data.keys())
    first = all_frames[0]
    last  = all_frames[-1]

    # Extract per-frame polylines for both outer (+) and hole (-) shapes.
    # Outers feed the main animated tracks; holes feed a parallel set of
    # stencil tracks built the same way, then appended after the outers
    # in the same root layer so they cut through.
    _dbg("build: flattening beziers to polylines (samples_per_bezier={})".format(
        samples_per_bezier))
    per_frame_polylines      = {}  # outers   {frame: [polyline, ...]}
    per_frame_hole_polylines = {}  # holes    {frame: [polyline, ...]}
    _total_points = 0
    _max_points_per_frame = 0
    _total_hole_points = 0
    for f in sorted(frames_data.keys()):
        outer_polys = []
        hole_polys  = []
        for s in frames_data[f]:
            poly = _flatten_shape_to_polyline(s['segments'], samples_per_bezier)
            if not (poly and _polyline_perimeter(poly) > 1.0):  # reject specks
                continue
            if s['sign'] == '+':
                outer_polys.append(poly)
            else:
                hole_polys.append(poly)
        if outer_polys:
            per_frame_polylines[f] = outer_polys
            pts_this_frame = sum(len(p) for p in outer_polys)
            _total_points += pts_this_frame
            if pts_this_frame > _max_points_per_frame:
                _max_points_per_frame = pts_this_frame
        if hole_polys:
            per_frame_hole_polylines[f] = hole_polys
            _total_hole_points += sum(len(p) for p in hole_polys)

    _dbg("build: flattened to {} outer polylines ({} points, max {}/frame); "
         "{} hole polylines ({} points)".format(
             sum(len(v) for v in per_frame_polylines.values()),
             _total_points, _max_points_per_frame,
             sum(len(v) for v in per_frame_hole_polylines.values()),
             _total_hole_points))

    # Release the parsed dict -- keeping frames_data AND per_frame_polylines
    # in memory at once is wasteful on 4K/long ranges.
    del frames_data
    _dbg("build: released frames_data")

    if not per_frame_polylines:
        nuke.message("convert_range_animated: no outer shapes found "
                     "across frames {}..{}.".format(first, last))
        _dbg("FATAL: no outer shapes")
        return None

    # Number of tracks = max count of outer shapes observed on any frame.
    # Frames with fewer shapes leave some tracks un-keyframed that frame
    # (Nuke holds the previous keyframe).
    num_tracks = max(len(polys) for polys in per_frame_polylines.values())
    _dbg("build: num_tracks = {}".format(num_tracks))

    # Assign polygons to tracks across frames by centroid-distance matching.
    # Complexity is O(frames * N^3) using Hungarian algorithm, where N is
    # min(polys_per_frame, active_tracks). Typical N=2..14 --> trivial.
    _dbg("build: assigning polygons to tracks (Hungarian O(frames * N^3))")
    tracks = _build_tracks_from_per_frame(per_frame_polylines, num_tracks)
    _dbg("build: tracks built")

    # Release per_frame_polylines -- the track dicts hold references to the
    # polylines we actually need, so this is safe.
    del per_frame_polylines
    _dbg("build: released per_frame_polylines")

    # Skip tracks that never had enough frames to be meaningful
    tracks = [t for t in tracks if len(t) >= 1]
    if not tracks:
        _dbg("FATAL: no usable tracks after filter")
        nuke.message("convert_range_animated: no usable tracks after "
                     "assignment.")
        return None
    _dbg("build: {} usable tracks, frame counts: {}".format(
        len(tracks), [len(t) for t in tracks]))

    # Parallel pass for holes. Empty per_frame_hole_polylines is fine and
    # very common (most mattes have no holes); just skip gracefully.
    hole_tracks = []
    if per_frame_hole_polylines:
        num_hole_tracks = max(
            len(polys) for polys in per_frame_hole_polylines.values())
        _dbg("build: num_hole_tracks = {}".format(num_hole_tracks))
        _dbg("build: assigning hole polygons to tracks (Hungarian)")
        hole_tracks = _build_tracks_from_per_frame(
            per_frame_hole_polylines, num_hole_tracks)
        del per_frame_hole_polylines
        _dbg("build: released per_frame_hole_polylines")
        hole_tracks = [t for t in hole_tracks if len(t) >= 1]
        _dbg("build: {} usable hole tracks, frame counts: {}".format(
            len(hole_tracks), [len(t) for t in hole_tracks]))
    else:
        del per_frame_hole_polylines
        _dbg("build: no holes to track")

    # Probe the CP-center animation API on a throwaway ShapeControlPoint
    # BEFORE building any real shape. (Probe writes a keyframe at t=-1000
    # to verify its signature, which would pollute any CP it ran on.)
    if _ANIMATE_CP_API is None:
        probe_cp = rp.ShapeControlPoint(0, 0)
        if not _probe_and_cache_cp_animate_api(probe_cp):
            nuke.message(
                "convert_range_animated: could not detect the "
                "AnimControlPoint keyframe API for this Nuke build. "
                "Animation cannot proceed. Please report this to the "
                "plugin author.")
            return None

    # Build the output Roto with one shape per track
    roto = nuke.nodes.Roto(
        inputs=[plugin_node],
        xpos=plugin_node.xpos() + 120,
        ypos=plugin_node.ypos() + 50,
    )
    try:
        label = 'from AlphaToRoto\n[{}..{}] animated\n{} track{}'.format(
            first, last, len(tracks), '' if len(tracks) == 1 else 's')
        if hole_tracks:
            label += ', {} hole{}'.format(
                len(hole_tracks), '' if len(hole_tracks) == 1 else 's')
        roto['label'].setValue(label)
    except Exception:
        pass

    curves_knob = roto['curves']
    total_keyframes = 0
    total_failures  = 0
    track_summaries = []
    hole_summaries  = []
    hole_blend_failures = 0

    # --- Performance hot zone --------------------------------------------
    # Disable the undo stack: every addPositionKey() call logs an undo
    # entry by default, and for tens of thousands of writes that dominates
    # wall time. The user can still undo the whole Roto via its node
    # creation; they just can't individually undo the keyframes (which
    # they wouldn't want to anyway).
    #
    # Use ProgressTask so Nuke pumps the event loop during the write --
    # this is what stops the OS "not responding" dialog from appearing,
    # and gives the user a visible progress bar + cancel button.
    undo = nuke.Undo()
    undo.disable()
    task = nuke.ProgressTask("AlphaToRoto: building animated Roto")
    cancelled = False
    _dbg("build: entering hot write loop (undo disabled, progress task created)")
    try:
        # Estimate total work units for the progress bar.
        total_units = 0
        for track_polys in tracks:
            total_units += len(track_polys)  # one unit per (track, frame)
        for track_polys in hole_tracks:
            total_units += len(track_polys)  # holes contribute too
        units_done = 0
        _dbg("build: total work units = {} ({} outer + {} hole)".format(
            total_units,
            sum(len(t) for t in tracks),
            sum(len(t) for t in hole_tracks)))

        for track_idx, track_polys in enumerate(tracks):
            if task.isCancelled():
                cancelled = True
                break

            # Determine K for THIS track (median of this track's point counts,
            # clamped). Each track can independently choose an appropriate K.
            #
            # Old cap was [32, 128], which was the noise floor -- at 128 CPs
            # a 4K hero matte loses fingers, chin notches, hair wisps: any
            # feature smaller than ~200 pixels of perimeter collapses to a
            # straight line. New cap [128, 2048] gives ~1 CP per 20 pixels
            # of perimeter on a typical 4K silhouette, which resolves
            # finger-scale features. 2048 CPs x 100 frames is ~200k
            # keyframes, which completes in under a second with undo
            # disabled.
            this_track_counts = sorted(len(p) for p in track_polys.values())
            src_median = this_track_counts[len(this_track_counts) // 2]
            src_max    = this_track_counts[-1]
            if num_points is None:
                K = max(128, min(2048, src_median))
            else:
                K = max(4, int(num_points))

            _dbg("track {}/{}: {} frames, source polylines {}..{} points "
                 "(median {}), K={} control points".format(
                 track_idx + 1, len(tracks), len(track_polys),
                 this_track_counts[0], src_max, src_median, K))

            # Resample each frame's polyline in this track to K points
            resampled = {
                f: _resample_polyline_to_K(poly, K)
                for f, poly in track_polys.items()
            }

            # Cyclic-align across frames within this track
            track_sorted_frames = sorted(resampled.keys())
            prev_pts = resampled[track_sorted_frames[0]]
            for f in track_sorted_frames[1:]:
                aligned = _cyclic_align(resampled[f], prev_pts)
                resampled[f] = aligned
                prev_pts = aligned
            _dbg("track {}: resampled + aligned".format(track_idx + 1))

            # Build the shape
            shape = rp.Shape(curves_knob, type='bezier')
            for (px, py) in resampled[track_sorted_frames[0]]:
                shape.append(rp.ShapeControlPoint(px, py))
            curves_knob.rootLayer.append(shape)
            try:
                shape.name = 'Track{}'.format(track_idx + 1)
            except Exception:
                pass
            _dbg("track {}: shape created + appended".format(track_idx + 1))

            # Keyframe each CP's center per frame
            failures = 0
            frames_written = 0
            for f in track_sorted_frames:
                if task.isCancelled():
                    cancelled = True
                    break
                pts = resampled[f]
                for i, (px, py) in enumerate(pts):
                    if _animate_cp_center(shape[i], f, px, py):
                        total_keyframes += 1
                    else:
                        failures += 1
                units_done += 1
                frames_written += 1
                # Progress update + event pump. Updating every frame is
                # fine -- setProgress is cheap and setMessage gives the
                # user feedback on long ranges.
                if total_units > 0:
                    task.setProgress(int(units_done * 100 / total_units))
                task.setMessage(
                    "Track {}/{}  frame {}  ({} keyframes)".format(
                        track_idx + 1, len(tracks), f, total_keyframes))
                # Log every 50 frames (and on first frame) so we can see
                # RSS growing / where a kill landed.
                if frames_written == 1 or frames_written % 50 == 0:
                    _dbg("track {}: wrote frame {} ({}/{}), total_keyframes={}, "
                         "failures={}".format(track_idx + 1, f, frames_written,
                                              len(track_sorted_frames),
                                              total_keyframes, failures))

            _dbg("track {}: done, {} frames written, {} failures".format(
                track_idx + 1, frames_written, failures))

            total_failures += failures
            track_summaries.append((track_idx + 1, K,
                                    len(track_sorted_frames), failures))

            if cancelled:
                break

        # ----------------------------------------------------------------
        # Parallel pass: animated HOLE shapes (stencil blend mode).
        # Same algorithm as the outer pass -- resample to K, cyclic-align,
        # keyframe centers per frame -- but each shape gets flagged as a
        # stencil so it cuts through the outer shapes below it in the
        # root layer. Order matters: holes are appended AFTER the outers
        # so the layer composition has them on top to subtract.
        # ----------------------------------------------------------------
        for hole_idx, track_polys in enumerate(hole_tracks):
            if cancelled or task.isCancelled():
                cancelled = True
                break

            this_track_counts = sorted(len(p) for p in track_polys.values())
            src_median = this_track_counts[len(this_track_counts) // 2]
            src_max    = this_track_counts[-1]
            if num_points is None:
                K = max(128, min(2048, src_median))
            else:
                K = max(4, int(num_points))

            _dbg("hole {}/{}: {} frames, source polylines {}..{} points "
                 "(median {}), K={} control points".format(
                 hole_idx + 1, len(hole_tracks), len(track_polys),
                 this_track_counts[0], src_max, src_median, K))

            resampled = {
                f: _resample_polyline_to_K(poly, K)
                for f, poly in track_polys.items()
            }

            track_sorted_frames = sorted(resampled.keys())
            prev_pts = resampled[track_sorted_frames[0]]
            for f in track_sorted_frames[1:]:
                aligned = _cyclic_align(resampled[f], prev_pts)
                resampled[f] = aligned
                prev_pts = aligned
            _dbg("hole {}: resampled + aligned".format(hole_idx + 1))

            shape = rp.Shape(curves_knob, type='bezier')
            for (px, py) in resampled[track_sorted_frames[0]]:
                shape.append(rp.ShapeControlPoint(px, py))
            curves_knob.rootLayer.append(shape)
            try:
                shape.name = 'Hole{}'.format(hole_idx + 1)
            except Exception:
                pass

            # The cutout itself: flip blending mode to stencil. Must be
            # AFTER the shape is appended to the layer so getAttributes()
            # sees the layer-attached state. If this returns False the
            # shape will animate but render as additive instead of
            # subtractive -- in that case the user can fix it manually
            # in the Roto blend-mode dropdown.
            if not _flag_as_hole(shape):
                hole_blend_failures += 1
                _dbg("hole {}: WARNING blend-mode write failed".format(
                    hole_idx + 1))

            _dbg("hole {}: shape created + appended".format(hole_idx + 1))

            failures = 0
            frames_written = 0
            for f in track_sorted_frames:
                if task.isCancelled():
                    cancelled = True
                    break
                pts = resampled[f]
                for i, (px, py) in enumerate(pts):
                    if _animate_cp_center(shape[i], f, px, py):
                        total_keyframes += 1
                    else:
                        failures += 1
                units_done += 1
                frames_written += 1
                if total_units > 0:
                    task.setProgress(int(units_done * 100 / total_units))
                task.setMessage(
                    "Hole {}/{}  frame {}  ({} keyframes)".format(
                        hole_idx + 1, len(hole_tracks), f, total_keyframes))
                if frames_written == 1 or frames_written % 50 == 0:
                    _dbg("hole {}: wrote frame {} ({}/{}), "
                         "total_keyframes={}, failures={}".format(
                             hole_idx + 1, f, frames_written,
                             len(track_sorted_frames),
                             total_keyframes, failures))

            _dbg("hole {}: done, {} frames written, {} failures".format(
                hole_idx + 1, frames_written, failures))

            total_failures += failures
            hole_summaries.append((hole_idx + 1, K,
                                   len(track_sorted_frames), failures))

            if cancelled:
                break
    finally:
        _dbg("build: exiting hot write loop (re-enabling undo, closing task)")
        del task           # closes the progress dialog
        undo.enable()
    # --- End performance hot zone ---------------------------------------

    if cancelled:
        print("AlphaToRoto: build cancelled by user after {} keyframes. "
              "Partial Roto left in the graph.".format(total_keyframes))

    curves_knob.changed()

    if _ANIMATE_CP_API_DESC is not None:
        print("AlphaToRoto: cp.center animation API = {}".format(_ANIMATE_CP_API_DESC))

    print("AlphaToRoto: built animated Roto with {} track{} and {} hole{} "
          "across frames {}..{} ({} keyframes total).".format(
              len(tracks),       '' if len(tracks)       == 1 else 's',
              len(hole_tracks),  '' if len(hole_tracks)  == 1 else 's',
              first, last, total_keyframes))
    for idx, K, nframes, fail in track_summaries:
        suffix = " [{} keyframe failures]".format(fail) if fail else ""
        print("  Track{}: {} control points, {} keyframed frames{}".format(
            idx, K, nframes, suffix))
    for idx, K, nframes, fail in hole_summaries:
        suffix = " [{} keyframe failures]".format(fail) if fail else ""
        print("  Hole{}:  {} control points, {} keyframed frames{}".format(
            idx, K, nframes, suffix))

    if hole_blend_failures > 0:
        print("AlphaToRoto: WARNING -- {} hole shape(s) could not be set "
              "to stencil blend mode. They will appear additive; fix the "
              "blend mode manually in the Roto properties panel, or run "
              "diagnose_shape_attrs() on a hole shape and report back."
              .format(hole_blend_failures))

    if total_failures > 0:
        print("AlphaToRoto: WARNING -- {} total keyframe writes failed. "
              "Some track motion may be incomplete.".format(total_failures))

    return roto
