#!/usr/bin/env python3
"""
Manim → JSON timeline extractor.

Runs a Manim Scene's construct() method without rasterizing. Instead it
monkey-patches Scene.play / wait / add / remove to record a timeline of
animations + serialized Mobject states. The output JSON is designed to be
replayed in the browser (see renderer.html / renderer.js).

Usage:
    python extract.py <path_to_py> <SceneClassName> [<output.json>]

Example:
    python extract.py ~/Desktop/bt_matrix.py BTMatrix bt_matrix.json

Requires manim (Community) installed in the active venv.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path


# ────────────────────────────────────────────────────────────────────
# Guard: we must patch Manim BEFORE the user's module is imported.
# ────────────────────────────────────────────────────────────────────

import manim
from manim import (
    Scene,
    Mobject,
    VMobject,
    VGroup,
)


# ────────────────────────────────────────────────────────────────────
# Color helpers
# ────────────────────────────────────────────────────────────────────

def _to_hex(color) -> str:
    """Convert a Manim color to '#rrggbb'. Tolerant to many input shapes."""
    if color is None:
        return "#808080"
    # ManimColor or similar
    try:
        r, g, b = color.to_rgb()
        return "#{:02x}{:02x}{:02x}".format(
            int(round(r * 255)), int(round(g * 255)), int(round(b * 255))
        )
    except Exception:
        pass
    # Already a string
    if isinstance(color, str):
        s = color.lstrip("#")
        if len(s) in (3, 6, 8):
            return "#" + s
    # Tuple / list of floats
    try:
        r, g, b = list(color)[:3]
        return "#{:02x}{:02x}{:02x}".format(
            int(round(float(r) * 255)),
            int(round(float(g) * 255)),
            int(round(float(b) * 255)),
        )
    except Exception:
        pass
    return "#808080"


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# ────────────────────────────────────────────────────────────────────
# Registry: Mobject → stable string id
# ────────────────────────────────────────────────────────────────────

class _Registry:
    def __init__(self):
        self._counter = 0
        self._by_pyid: dict[int, str] = {}
        self.mobjects: dict[str, dict] = {}
        # Strong refs to registered mobjects. Without these, Python can
        # garbage-collect a fading-out mobject and then reuse its id() for a
        # brand-new mobject — causing ID collisions in _by_pyid that resolve
        # to the wrong registry entry (bug observed with LookupRow's per-turn
        # helpers: SurroundingRectangle / Arrow / etc.).
        self._strong_refs: list = []

    def id_of(self, m) -> str | None:
        """Get (or assign) a stable id for a Mobject. Returns None for non-Mobjects."""
        if m is None or not isinstance(m, Mobject):
            return None
        py = id(m)
        if py in self._by_pyid:
            return self._by_pyid[py]
        self._counter += 1
        our_id = f"m_{self._counter}"
        self._by_pyid[py] = our_id
        self._strong_refs.append(m)
        self.mobjects[our_id] = _serialize_mobject(m, our_id, self)
        return our_id

    def refresh(self, m):
        """Re-serialize a mobject that already has an id (to update its state)."""
        py = id(m)
        our_id = self._by_pyid.get(py)
        if our_id is None:
            return self.id_of(m)
        self.mobjects[our_id] = _serialize_mobject(m, our_id, self)
        return our_id


# ────────────────────────────────────────────────────────────────────
# Mobject serialization
# ────────────────────────────────────────────────────────────────────

SUPPORTED_TYPES = {
    "Square", "Rectangle", "Circle",
    "Text", "MarkupText",
    "Arrow", "Line", "DoubleArrow",
    "Brace", "BraceText",
    "SurroundingRectangle",
    "VGroup", "Group",
}


def _serialize_mobject(m: Mobject, mid: str, registry: _Registry) -> dict:
    """Take a lightweight snapshot of a Mobject's visible state."""
    type_name = type(m).__name__
    data: dict = {"id": mid, "type": type_name}

    # Position (center)
    center = _safe(lambda: m.get_center(), [0.0, 0.0, 0.0])
    data["x"] = float(center[0])
    data["y"] = float(center[1])

    # Bounding box (for most shapes)
    w = _safe(lambda: float(m.get_width()))
    h = _safe(lambda: float(m.get_height()))
    if w is not None:
        data["width"] = w
    if h is not None:
        data["height"] = h

    # Stroke / fill (safe-guarded)
    s_color = _safe(lambda: m.get_stroke_color())
    if s_color is not None:
        data["stroke_color"] = _to_hex(s_color)
    s_width = _safe(lambda: float(m.get_stroke_width()))
    if s_width is not None:
        data["stroke_width"] = s_width
    s_opacity = _safe(lambda: float(m.get_stroke_opacity()))
    if s_opacity is not None:
        data["stroke_opacity"] = s_opacity

    f_color = _safe(lambda: m.get_fill_color())
    if f_color is not None:
        data["fill_color"] = _to_hex(f_color)
    f_opacity = _safe(lambda: float(m.get_fill_opacity()))
    if f_opacity is not None:
        data["fill_opacity"] = f_opacity

    # Text-specific
    if type_name in ("Text", "MarkupText"):
        # Prefer original_text (preserves whitespace) over .text (which Manim
        # strips of spaces for internal rendering).
        data["text"] = getattr(m, "original_text", None) or getattr(m, "text", "")
        # font_size — Manim Text stores as attr on some versions
        fs = getattr(m, "font_size", None)
        if fs is None:
            fs = _safe(lambda: max(8.0, float(m.get_height()) * 50.0), 24.0)
        data["font_size"] = float(fs)
        data["slant"] = getattr(m, "slant", "NORMAL")
        data["weight"] = getattr(m, "weight", "NORMAL")
        # Color: Manim Text is a VGroup of letter submobjects — the real color
        # lives on the submobjects, NOT on the top-level mobject. Use the
        # first submobject's fill color, falling back to stroke color.
        color_hex = None
        subs = getattr(m, "submobjects", None) or []
        for sub in subs:
            sc = _safe(lambda s=sub: s.get_fill_color())
            if sc is not None:
                color_hex = _to_hex(sc)
                break
        if color_hex is None:
            for sub in subs:
                sc = _safe(lambda s=sub: s.get_stroke_color())
                if sc is not None:
                    color_hex = _to_hex(sc)
                    break
        if color_hex is None:
            color_hex = "#e6edf3"  # safe default (near-white) for dark background
        data["color"] = color_hex
        # For Text we override stroke/fill color with the actual letter color
        data["fill_color"] = color_hex
        # Don't draw stroke on text (Manim text renders as filled glyph)
        data["stroke_width"] = 0
        data["stroke_opacity"] = 0

    # Arrow / Line-specific
    if type_name in ("Arrow", "Line", "DoubleArrow"):
        start = _safe(lambda: list(m.get_start()))
        end = _safe(lambda: list(m.get_end()))
        if start is not None and end is not None:
            data["start"] = [float(start[0]), float(start[1])]
            data["end"] = [float(end[0]), float(end[1])]

    # Brace-specific: approximate as a horizontal/vertical bracket spanning its bounding box
    if type_name in ("Brace",):
        # Manim Brace exposes get_direction() returning a unit vector pointing
        # FROM the brace's tip AWAY from the subject it braces. LEFT brace →
        # [-1, 0, 0]; UP brace → [0, 1, 0]; etc.
        direction = _safe(lambda: list(m.get_direction()), [0.0, 1.0, 0.0])
        data["direction"] = [float(direction[0]), float(direction[1])]
        tip = _safe(lambda: list(m.get_tip()))
        if tip is not None:
            data["tip"] = [float(tip[0]), float(tip[1])]

    # SurroundingRectangle-specific: already has stroke + bbox, nothing extra

    # VGroup / Group-specific: list children ids
    if type_name in ("VGroup", "Group"):
        child_ids = []
        for sub in m.submobjects:
            # Only register supported children to avoid cascading unsupported types
            cid = registry.id_of(sub)
            if cid is not None:
                child_ids.append(cid)
        data["children"] = child_ids

    return data


# ────────────────────────────────────────────────────────────────────
# `.animate` handling
#
# We do NOT override Manim's own `.animate` property — it returns an
# `_AnimationBuilder` that's deeply integrated with Manim's composition
# system (LaggedStart, AnimationGroup, etc. call `prepare_animation()`
# on each inner animation, which rejects foreign types).
#
# Instead we let Manim build the `_AnimationBuilder`/`_MethodAnimation`
# normally, then intercept in `_record_animation` by reading the
# builder's `.target` attribute (a mobject in its FINAL state after the
# chained methods were applied). That gives us the end-state snapshot
# without any patching.
# ────────────────────────────────────────────────────────────────────

def _install_animate_property():
    """No-op — kept for API symmetry. We rely on Manim's own .animate."""
    pass


# ────────────────────────────────────────────────────────────────────
# Scene patching
# ────────────────────────────────────────────────────────────────────

_timeline: list = []
_registry: _Registry = _Registry()
_current_time_ms = [0]  # mutable holder


def _reset_state():
    global _timeline, _registry
    _timeline = []
    _registry = _Registry()
    _current_time_ms[0] = 0


def _record_animation(anim, start_ms: int, total_ms: int) -> None:
    """Convert a Manim Animation into one or more timeline entries."""
    anim_type = type(anim).__name__

    # Manim's .animate syntax: obj.animate.shift(RIGHT) returns an
    # _AnimationBuilder. LaggedStart etc. call .build() on it at prepare time;
    # but when we intercept play() directly, we may receive either an unbuilt
    # _AnimationBuilder OR an already-built _MethodAnimation.
    if anim_type == "_AnimationBuilder":
        # Force-build to get the underlying animation
        try:
            built = anim.build()
        except Exception as e:
            print(f"  [extract] _AnimationBuilder.build() failed: {e}", file=sys.stderr)
            return
        _record_method_animation(built, anim, start_ms, total_ms)
        return

    if anim_type in ("_MethodAnimation", "ApplyMethod"):
        _record_method_animation(anim, None, start_ms, total_ms)
        return

    # LaggedStart → expand into staggered entries
    if anim_type in ("LaggedStart", "LaggedStartMap"):
        inner = getattr(anim, "animations", None) or []
        lag = float(getattr(anim, "lag_ratio", 0.05) or 0.05)
        n = len(inner)
        if n == 0:
            return
        # Each sub-animation runs for a slice of total_ms. We stagger starts.
        stagger = int(total_ms * lag)
        per_dur = max(1, total_ms - (n - 1) * stagger)
        for i, sub in enumerate(inner):
            sub_start = start_ms + i * stagger
            _record_animation(sub, sub_start, per_dur)
        return

    # AnimationGroup / Succession → run in parallel (group) or sequence (succession)
    if anim_type == "AnimationGroup":
        inner = getattr(anim, "animations", None) or []
        for sub in inner:
            _record_animation(sub, start_ms, total_ms)
        return
    if anim_type == "Succession":
        inner = getattr(anim, "animations", None) or []
        if not inner:
            return
        per = max(1, total_ms // len(inner))
        for i, sub in enumerate(inner):
            _record_animation(sub, start_ms + i * per, per)
        return

    # Regular animation: target is its mobject(s)
    target_mobj = getattr(anim, "mobject", None)
    if target_mobj is None and hasattr(anim, "target_mobject"):
        target_mobj = anim.target_mobject

    # Transform / TransformFromCopy: special — has source & target
    if anim_type == "Transform":
        source = anim.mobject
        target = getattr(anim, "target_mobject", None) or getattr(anim, "target_copy", None)
        src_id = _registry.id_of(source)
        # End state = serialize target as a candidate new state for source
        end_state = None
        if target is not None:
            # Don't register target in the global map (it's ephemeral), just dump its props
            end_state = _serialize_mobject(target, "tmp", _registry)
            end_state.pop("id", None)
        _timeline.append({
            "at": start_ms,
            "action": "transform",
            "target": src_id,
            "end": end_state,
            "duration": total_ms,
        })
        return

    if anim_type == "TransformFromCopy":
        # source stays; a copy moves from source's position to target's
        source = anim.mobject
        target = getattr(anim, "target_mobject", None) or getattr(anim, "target_copy", None)
        src_id = _registry.id_of(source)
        # register target so it's visible in the mobject map too
        tgt_id = _registry.id_of(target) if target is not None else None
        start_state = _serialize_mobject(source, "tmp", _registry)
        start_state.pop("id", None)
        end_state = _serialize_mobject(target, "tmp", _registry) if target is not None else None
        if end_state is not None:
            end_state.pop("id", None)
        _timeline.append({
            "at": start_ms,
            "action": "transform_from_copy",
            "source": src_id,
            "target": tgt_id,
            "start": start_state,
            "end": end_state,
            "duration": total_ms,
        })
        return

    # Default: single-target animation (FadeIn, FadeOut, Create, Write, Grow*, etc.)
    target_id = _registry.id_of(target_mobj)

    # Map animation class → action name used by the renderer
    action_map = {
        "FadeIn": "fade_in",
        "FadeOut": "fade_out",
        "Write": "write",
        "Unwrite": "unwrite",
        "Create": "create",
        "Uncreate": "uncreate",
        "DrawBorderThenFill": "create",
        "ShowCreation": "create",
        "GrowFromCenter": "grow_from_center",
        "GrowFromEdge": "grow_from_edge",
        "GrowArrow": "grow_arrow",
        "ApplyMethod": "apply_method",
    }
    action = action_map.get(anim_type, anim_type.lower())

    entry = {
        "at": start_ms,
        "action": action,
        "target": target_id,
        "duration": total_ms,
    }
    # FadeIn(shift=...) captures the shift vector
    shift = getattr(anim, "shift_vector", None)
    if shift is not None:
        entry["shift"] = [float(shift[0]), float(shift[1])]
    # GrowFromEdge: has edge attribute
    edge = getattr(anim, "edge", None)
    if edge is not None:
        try:
            entry["edge"] = [float(edge[0]), float(edge[1])]
        except Exception:
            pass

    _timeline.append(entry)


def _record_method_animation(anim, builder, start_ms: int, total_ms: int) -> None:
    """Handle a Manim _MethodAnimation / ApplyMethod produced by .animate syntax.

    Manim's `.animate.foo(...)` creates an _AnimationBuilder that:
      1. Clones the source mobject via generate_target() → mobject.target
      2. Applies the chained methods to the TARGET (not the original)
      3. build() wraps that into a _MethodAnimation

    So the "end state" we want is whatever `mobject.target` looks like.
    """
    source = anim.mobject
    src_id = _registry.id_of(source)

    # The target mobject carries the end-state geometry/color/etc.
    target = getattr(source, "target", None)
    if target is None and builder is not None:
        target = getattr(builder.mobject, "target", None)

    start_state = _serialize_mobject(source, "tmp", _registry)
    start_state.pop("id", None)

    end_state = None
    if target is not None:
        end_state = _serialize_mobject(target, "tmp", _registry)
        end_state.pop("id", None)

    _timeline.append({
        "at": start_ms,
        "action": "apply_method",
        "target": src_id,
        "start": start_state,
        "end": end_state,
        "duration": total_ms,
    })

    # Sync source's in-memory state to the end state so subsequent animations
    # start from the correct position. Do NOT refresh the registry — the
    # registry stores INITIAL state, which the renderer uses to place the DOM
    # element. Refreshing would make the element appear at its end state
    # from the start, breaking fly-in animations.
    if target is not None:
        try:
            source.become(target)
        except Exception:
            pass


def _patched_scene_init(self, *args, **kwargs):
    # Avoid calling the real Scene.__init__ (which initializes a renderer).
    # Set up just the attributes our patched methods depend on. We write into
    # __dict__ directly to avoid triggering Scene's properties (time, camera, etc.)
    # which don't have setters.
    self.__dict__["mobjects"] = []
    self.__dict__["renderer"] = _DummyRenderer()
    self.__dict__["animations"] = None
    self.__dict__["random_seed"] = 0


class _DummyRenderer:
    """Minimal stand-in for manim's renderer. All operations are no-ops."""
    def __init__(self):
        self.time = 0.0
        self.num_plays = 0
        self.camera = None
        self.skip_animations = False

    def init_scene(self, scene):
        pass

    def play(self, *args, **kwargs):
        pass

    def update_frame(self, *args, **kwargs):
        pass

    def render(self, *args, **kwargs):
        pass

    def scene_finished(self, *args, **kwargs):
        pass

    def add_frame(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        # Anything else: return a no-op callable
        def noop(*args, **kwargs):
            return None
        return noop


def _patched_play(self, *animations, run_time=None, **kwargs):
    rt_sec = run_time if run_time is not None else 1.0
    total_ms = int(round(float(rt_sec) * 1000))
    start_ms = _current_time_ms[0]
    for anim in animations:
        _record_animation(anim, start_ms, total_ms)
    _current_time_ms[0] += total_ms


def _patched_wait(self, duration=1.0, *args, **kwargs):
    _current_time_ms[0] += int(round(float(duration) * 1000))


def _patched_add(self, *mobjects):
    for m in mobjects:
        _registry.id_of(m)


def _patched_remove(self, *mobjects):
    # no-op for our purposes
    pass


def _patch_scene():
    Scene.__init__ = _patched_scene_init  # type: ignore
    Scene.play = _patched_play  # type: ignore
    Scene.wait = _patched_wait  # type: ignore
    Scene.add = _patched_add  # type: ignore
    Scene.remove = _patched_remove  # type: ignore


# ────────────────────────────────────────────────────────────────────
# Entrypoint
# ────────────────────────────────────────────────────────────────────

def extract(file_path: str, class_name: str) -> dict:
    _reset_state()
    _install_animate_property()
    _patch_scene()

    file_path = os.path.abspath(file_path)
    if not os.path.exists(file_path):
        raise FileNotFoundError(file_path)

    spec = importlib.util.spec_from_file_location("_user_scene", file_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore
    sys.modules["_user_scene"] = mod
    spec.loader.exec_module(mod)  # type: ignore

    cls = getattr(mod, class_name, None)
    if cls is None:
        raise AttributeError(f"{class_name} not found in {file_path}")

    scene = cls()
    scene.construct()

    return {
        "source_file": str(file_path),
        "scene_class": class_name,
        "coord_system": "manim",  # x in ~[-7.11, 7.11], y in [-4, 4]
        "total_duration_ms": _current_time_ms[0],
        "mobjects": _registry.mobjects,
        "timeline": _timeline,
    }


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        sys.exit(2)
    file_path = sys.argv[1]
    class_name = sys.argv[2]
    output_path = sys.argv[3] if len(sys.argv) >= 4 else None

    result = extract(file_path, class_name)

    text = json.dumps(result, indent=2, ensure_ascii=False)
    if output_path:
        Path(output_path).write_text(text)
        print(
            f"Extracted {len(result['mobjects'])} mobjects, "
            f"{len(result['timeline'])} timeline entries, "
            f"{result['total_duration_ms']}ms → {output_path}"
        )
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
