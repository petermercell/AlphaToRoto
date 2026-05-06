// SPDX-License-Identifier: GPL-3.0-only
//
// AlphaToRoto -- Trace alpha-channel mattes into Roto shapes in Nuke
// Copyright (C) 2026 Peter Mercell
//
// This file is part of AlphaToRoto.
//
// AlphaToRoto is free software: you can redistribute it and/or modify it
// under the terms of the GNU General Public License as published by the
// Free Software Foundation, version 3 of the License.
//
// AlphaToRoto is distributed in the hope that it will be useful, but
// WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
// or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public License
// for more details.
//
// You should have received a copy of the GNU General Public License along
// with AlphaToRoto. If not, see <https://www.gnu.org/licenses/>.
//
// AlphaToRoto links statically against potrace (Copyright (C) 2001-2019
// Peter Selinger), which is licensed under "GPL version 2, or (at your
// option) any later version". AlphaToRoto exercises the "or later" option
// to combine potrace under GPL-3.0. See the LICENCE file at the repository
// root and third_party/potrace/COPYING for full details.
//
// AlphaToRoto.cpp
//
// Nuke NDK plugin that traces the alpha channel into a vector Roto node
// using embedded potrace. Pass-through Iop with a "Convert to Animated Roto"
// button.
//
// Flow:
//   1) User clicks 'Convert to Roto' button -> Python runs:
//        a. bumps the hidden _reset_trigger knob, which fires knob_changed()
//           -> clears _accum_buf and curve_data
//        b. calls nuke.execute(thisNode, first, last) -- Nuke's render
//           pipeline then calls _execute() per frame with the correct
//           OutputContext
//   2) Our _execute() runs per frame:
//        - validate(true) propagates the context upstream
//        - build_bitmap() reads input0's alpha (which inherits context)
//        - potrace_trace() with the user's params
//        - serialize_paths() -> "F <frame>\n<S/M/C/L/E lines>\n"
//        - that block is appended to _accum_buf and pushed to curve_data
//   3) After nuke.execute() returns, Python reads the accumulated
//      curve_data, splits on F-markers, and constructs a nuke.rotopaint
//      Shape tree on a new Roto node wired to this plugin's output.
//
// Nuke 17 note: Op::input(int, const OutputContext*) was removed entirely
// in Nuke 17; we used to manipulate OutputContext directly inside a knob
// callback but that API is gone. Driving per-frame work through _execute()
// is the blessed modern idiom -- Nuke handles all context propagation.
//
// License note: potrace is dual-licensed GPL / commercial. Statically
// linking it makes this plugin GPL-encumbered unless a commercial license
// is obtained from Peter Selinger. Plan accordingly.

#include <DDImage/Iop.h>
#include <DDImage/Row.h>
#include <DDImage/Knobs.h>
#include <DDImage/Knob.h>
#include <DDImage/ChannelSet.h>
#include <DDImage/Channel.h>
#include <DDImage/OutputContext.h>
#include <DDImage/Executable.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <sstream>
#include <string>

extern "C" {
#include "potracelib.h"
#include "bitmap.h"     // BM_PUT / BM_UPUT / bm_new / bm_free / bm_clear
}

using namespace DD::Image;

// ---------------------------------------------------------------------------
// The PyScript that the visible "Convert to Animated Roto" button runs.
// Python calls nuke.execute(node, first, last), which makes Nuke's render
// pipeline call _execute() on this op once per frame with the correct
// OutputContext. Each _execute() appends that frame's serialized curves to
// curve_data; Python reads the accumulated blob and builds one Roto shape
// with K control points keyframed per frame.
// ---------------------------------------------------------------------------
static const char* const kConvertAnimatedScript =
    "import alphaToRoto_py\n"
    "alphaToRoto_py.convert_range_animated(nuke.thisNode())\n";


class AlphaToRoto : public Iop, public Executable {
public:
    explicit AlphaToRoto(Node* n);
    ~AlphaToRoto() override = default;

    // Iop overrides
    void _validate(bool for_real) override;
    void _request(int x, int y, int r, int t, ChannelMask m, int count) override;
    void engine(int y, int x, int r, ChannelMask m, Row& out) override;

    // Op override: announce that this op is executable (multi-inherits
    // Executable). Nuke's nuke.execute() path routes through this.
    Executable* executable() override { return this; }

    // Executable override: called per-frame by Nuke when nuke.execute() is
    // invoked from Python. outputContext() is set to the current frame and
    // our upstream ops have been validated at that context, so input0() reads
    // the correct animated input automatically.
    void execute() override;

    void knobs(Knob_Callback f) override;
    int  knob_changed(Knob* k) override;

    const char* Class()     const override { return desc.name; }
    const char* node_help() const override;

    static const Iop::Description desc;

private:
    // Knob-backed state
    float       _threshold;
    int         _turdsize;
    float       _alphamax;
    const char* _curve_data_buf;  // String_knob initial-value storage
    int         _reset_trigger;   // bumped by Python to clear the accumulator
                                  // before a new nuke.execute() run
    int         _first_frame;
    int         _last_frame;

    // Runtime-only (not a knob): accumulated serialized curves across
    // successive _execute() calls within a single nuke.execute() invocation.
    // Python clears this by bumping _reset_trigger before calling nuke.execute.
    std::string _accum_buf;

    // Implementation
    bool build_bitmap(potrace_bitmap_t& bm, int& out_origin_x, int& out_origin_y);
    void trace_current_frame_and_append();
    static std::string serialize_paths(potrace_state_t* st,
                                       int origin_x, int origin_y);
};


// ---------------------------------------------------------------------------
// Construction / static registration
// ---------------------------------------------------------------------------
AlphaToRoto::AlphaToRoto(Node* n)
    : Iop(n)
    , Executable(this)
    , _threshold(0.5f)
    , _turdsize(2)
    , _alphamax(1.0f)
    , _curve_data_buf("")
    , _reset_trigger(0)
    , _first_frame(1)
    , _last_frame(100)
    , _accum_buf()
{}

static Iop* build_AlphaToRoto(Node* n) { return new AlphaToRoto(n); }

const Iop::Description AlphaToRoto::desc(
    "AlphaToRoto",
    "Draw/AlphaToRoto",
    build_AlphaToRoto);

const char* AlphaToRoto::node_help() const {
    return
        "AlphaToRoto\n\n"
        "Traces the input's alpha channel into an animated Roto node\n"
        "using potrace. The output of this node is a pass-through; click\n"
        "'Convert to Animated Roto' to generate a new Roto node wired to\n"
        "this node's output.\n\n"
        "Knobs:\n"
        "  threshold  - alpha threshold for binarization (0..1)\n"
        "  turdsize   - suppress speckles <= N pixels\n"
        "  alphamax   - corner threshold; 0 = polygons, 1.3334 = all smooth\n";
}


// ---------------------------------------------------------------------------
// Pass-through image processing
// ---------------------------------------------------------------------------
void AlphaToRoto::_validate(bool /*for_real*/) {
    copy_info();                    // identical bbox / format / channels as input
    set_out_channels(Mask_All);     // we emit everything input does
}

void AlphaToRoto::_request(int x, int y, int r, int t,
                           ChannelMask m, int count) {
    // Nuke 17: input0() returns Iop&, not Iop*.
    input0().request(x, y, r, t, m, count);
}

void AlphaToRoto::engine(int y, int x, int r, ChannelMask m, Row& out) {
    input0().get(y, x, r, m, out);
}


// ---------------------------------------------------------------------------
// Knobs
// ---------------------------------------------------------------------------
void AlphaToRoto::knobs(Knob_Callback f) {
    Float_knob(f, &_threshold, "threshold", "threshold");
    SetRange(f, 0.0, 1.0);
    Tooltip(f, "Alpha values above this are treated as inside.");

    Divider(f);

    Int_knob(f, &_turdsize, "turdsize", "turdsize");
    SetRange(f, 0, 100);
    Tooltip(f, "Suppress speckles <= this many pixels.");

    Float_knob(f, &_alphamax, "alphamax", "alphamax");
    SetRange(f, 0.0, 1.3334);
    Tooltip(f, "Corner detection threshold.\n"
               "0 = polygonal (all corners), 1.3334 = all smooth.");

    Divider(f);

    // Frame range for the animated-Roto conversion.
    Int_knob(f, &_first_frame, "first_frame", "first");
    SetRange(f, 1, 10000);
    Tooltip(f, "First frame for range-mode conversion.");

    Int_knob(f, &_last_frame, "last_frame", "last");
    SetRange(f, 1, 10000);
    Tooltip(f, "Last frame for range-mode conversion.");

    Divider(f);

    // Visible button. PyScript_knob runs Python which:
    //   1. bumps _reset_trigger to clear the C++ accumulator
    //   2. calls nuke.execute(thisNode, first, last) -- Nuke's render
    //      pipeline then calls our _execute() once per frame with the
    //      correct frame context, each call appending its frame's curves
    //      to curve_data
    //   3. reads the accumulated curve_data and builds a single Roto shape
    //      with K control points keyframed per frame
    PyScript_knob(f, kConvertAnimatedScript, "convert_animated_btn",
                  "Convert to Animated Roto");
    Tooltip(f, "Trace the frame range [first, last] and create a single\n"
               "Roto shape with K control points whose positions are\n"
               "keyframed per frame. Produces a lightweight, smoothly\n"
               "interpolating, tracker-friendly Roto -- at the cost of\n"
               "simplifying to one outer contour per frame (holes and\n"
               "smaller shapes are discarded). Correspondence between\n"
               "frames is established by cyclic-rotation alignment of\n"
               "resampled polylines.");

    // Tool text: visible version/author label at the bottom of the panel.
    Divider(f);
    Text_knob(f, "AlphaToRoto v 1.0.0 by Peter Mercell");

    // Hidden: accumulated serialized curves (can be large, don't save to .nk).
    // Each _execute() appends "F <frame>\n<curves...>\n" to this knob, and
    // Python parses the frame-keyed blob after nuke.execute() returns.
    String_knob(f, &_curve_data_buf, "curve_data", "");
    SetFlags(f, Knob::INVISIBLE | Knob::NO_ANIMATION | Knob::DO_NOT_WRITE);

    // Hidden: counter that Python bumps to reset the accumulator before each
    // new nuke.execute() run.
    Int_knob(f, &_reset_trigger, "_reset_trigger", "");
    SetFlags(f, Knob::INVISIBLE | Knob::NO_ANIMATION | Knob::DO_NOT_WRITE);
}

int AlphaToRoto::knob_changed(Knob* k) {
    if (k && std::strcmp(k->name().c_str(), "_reset_trigger") == 0) {
        _accum_buf.clear();
        if (Knob* cd = knob("curve_data")) cd->set_text("");
        return 1;
    }
    return Iop::knob_changed(k);
}


// ---------------------------------------------------------------------------
// Bitmap construction from input0's alpha
// ---------------------------------------------------------------------------
bool AlphaToRoto::build_bitmap(potrace_bitmap_t& bm,
                               int& out_origin_x, int& out_origin_y)
{
    // By the time we're called (from _execute()), Nuke has set our
    // outputContext to the current frame and called validate() on us, which
    // propagates upstream. input0() therefore evaluates at the correct frame
    // automatically -- no OutputContext juggling needed.
    Iop& in = input0();
    in.validate(true);

    const ChannelSet& ch = in.channels();
    if (!ch.contains(Chan_Alpha)) {
        error("AlphaToRoto: input has no alpha channel.");
        return false;
    }

    const DD::Image::Box& bb = in.info().box();
    const int w = bb.w();
    const int h = bb.h();
    if (w <= 0 || h <= 0) {
        error("AlphaToRoto: empty bounding box.");
        return false;
    }
    out_origin_x = bb.x();
    out_origin_y = bb.y();

    bm.w  = w;
    bm.h  = h;
    bm.dy = (w + BM_WORDBITS - 1) / BM_WORDBITS;
    bm.map = (potrace_word*)std::calloc(
        (size_t)bm.dy * (size_t)h, sizeof(potrace_word));
    if (!bm.map) {
        error("AlphaToRoto: failed to allocate bitmap (%d x %d).", w, h);
        return false;
    }

    ChannelSet req(Chan_Alpha);
    in.request(bb.x(), bb.y(), bb.r(), bb.t(), req, 1);

    const float thr = _threshold;

    Row row(bb.x(), bb.r());
    for (int y = bb.y(); y < bb.t(); ++y) {
        in.get(y, bb.x(), bb.r(), req, row);
        const float* a = row[Chan_Alpha];
        // Nuke: y=bb.y() is bottom row. potrace: y=0 is bottom row.
        // So local potrace y = y - bb.y(). No flip.
        const int by = y - bb.y();
        for (int ax = bb.x(); ax < bb.r(); ++ax) {
            if (a[ax] > thr) {
                const int bx = ax - bb.x();
                BM_PUT(&bm, bx, by, 1);
            }
        }
    }
    return true;
}


// ---------------------------------------------------------------------------
// Serialize potrace's path list to a simple line-based text blob:
//
//   S <idx> <sign> <n_segments>
//   M <x> <y>                                      (moveto -- start point)
//   C <c1x> <c1y> <c2x> <c2y> <ex> <ey>            (cubic bezier segment)
//   L <x> <y>                                      (line segment -- corner)
//   E                                              (end shape)
//
// All coordinates are Nuke absolute pixel coords (bitmap origin + offset).
// ---------------------------------------------------------------------------
std::string AlphaToRoto::serialize_paths(potrace_state_t* st,
                                         int origin_x, int origin_y)
{
    std::ostringstream oss;
    oss.precision(6);
    oss << std::fixed;

    int idx = 0;
    for (potrace_path_t* p = st->plist; p != nullptr; p = p->next, ++idx) {
        const potrace_curve_t& c = p->curve;
        if (c.n <= 0) continue;

        oss << "S " << idx << ' '
            << (p->sign == '+' ? '+' : '-') << ' '
            << c.n << '\n';

        // potrace convention: the start point of a closed path is the
        // endpoint of the LAST segment (c.c[n-1][2]), not the first.
        const potrace_dpoint_t& start = c.c[c.n - 1][2];
        oss << "M "
            << (start.x + origin_x) << ' '
            << (start.y + origin_y) << '\n';

        for (int i = 0; i < c.n; ++i) {
            const potrace_dpoint_t* seg = c.c[i];
            if (c.tag[i] == POTRACE_CURVETO) {
                oss << "C "
                    << (seg[0].x + origin_x) << ' ' << (seg[0].y + origin_y) << ' '
                    << (seg[1].x + origin_x) << ' ' << (seg[1].y + origin_y) << ' '
                    << (seg[2].x + origin_x) << ' ' << (seg[2].y + origin_y) << '\n';
            } else {
                // POTRACE_CORNER: polyline via seg[1] (corner vertex) to seg[2].
                oss << "L "
                    << (seg[1].x + origin_x) << ' '
                    << (seg[1].y + origin_y) << '\n';
                oss << "L "
                    << (seg[2].x + origin_x) << ' '
                    << (seg[2].y + origin_y) << '\n';
            }
        }
        oss << "E\n";
    }
    return oss.str();
}


// ---------------------------------------------------------------------------
// Per-frame trace entry point.
//
// Executable::execute() is called by Nuke's render pipeline when Python
// invokes nuke.execute(thisNode, first, last). Nuke loops frames for us and
// sets outputContext()/validate() per frame, so we just need to trace "now"
// and append the result to our accumulator.
// ---------------------------------------------------------------------------
void AlphaToRoto::execute() {
    validate(true);
    trace_current_frame_and_append();
}

void AlphaToRoto::trace_current_frame_and_append() {
    const int frame = static_cast<int>(outputContext().frame());

    potrace_bitmap_t bm = {};
    int origin_x = 0, origin_y = 0;
    if (!build_bitmap(bm, origin_x, origin_y)) {
        // error() already set
        if (bm.map) std::free(bm.map);
        return;
    }

    potrace_param_t* par = potrace_param_default();
    if (!par) {
        error("AlphaToRoto: potrace_param_default() failed.");
        std::free(bm.map);
        return;
    }
    par->turdsize = _turdsize;
    par->alphamax = _alphamax;
    // Force curve optimization off: in some Nuke versions the rotopaint API
    // does not persist Bezier tangents correctly, so merged segments render
    // as straight polylines. potrace_param_default() sets opticurve=1, so
    // we explicitly clear it here.
    par->opticurve = 0;

    potrace_state_t* st = potrace_trace(par, &bm);
    if (!st || st->status != POTRACE_STATUS_OK) {
        const int s = st ? st->status : -1;
        error("AlphaToRoto: potrace_trace failed at frame %d (status=%d).",
              frame, s);
        if (st) potrace_state_free(st);
        potrace_param_free(par);
        std::free(bm.map);
        return;
    }

    // Build this frame's block with F-marker prefix and append to the
    // accumulator (which persists across _execute() calls within a single
    // nuke.execute() run -- Python clears it via _reset_trigger beforehand).
    std::ostringstream block;
    block << "F " << frame << '\n';
    block << serialize_paths(st, origin_x, origin_y);
    _accum_buf += block.str();

    if (Knob* cd = knob("curve_data")) {
        cd->set_text(_accum_buf.c_str());
    }

    potrace_state_free(st);
    potrace_param_free(par);
    std::free(bm.map);
}
