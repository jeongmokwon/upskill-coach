"""
Reproduce the prod symptom locally by routing through extract.py's
monkey-patched Scene exactly the way prod does.

Run with the Manim-equipped venv:
    /Users/jeongmokwon/Desktop/manim-venv/bin/python test_repro_via_extract.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent

# Helper prelude (must mirror coach.py's _HELPER_PRELUDE)
HELPERS = '''
import sys as _sys
def _uc_make(text, fs, weight=None, **kw):
    kw['font'] = 'Inter'
    kw['font_size'] = fs
    if weight is not None:
        kw.setdefault('weight', weight)
    t = Text(text, **kw)
    try:
        t._uc_font_size = fs
    except Exception:
        pass
    print(f"DEBUG_UC: helper made Text(font_size={fs}) for {text[:40]!r}", file=_sys.stderr, flush=True)
    return t

def Title(s, **kw):    return _uc_make(s, 28, weight=BOLD, **kw)
def Subtitle(s, **kw): return _uc_make(s, 22, **kw)
def Caption(s, **kw):  return _uc_make(s, 18, **kw)
def AxisLabel(s, **kw):return _uc_make(s, 16, **kw)
def CellDigit(s, **kw):return _uc_make(s, 18, **kw)
def CodeText(s, **kw): return _uc_make(s, 16, **kw)
'''

# Mimic LLM's LinearHeadTransform — same operations the user reported
SCENE = HELPERS + '''
class TestScene(Scene):
    def construct(self):
        title = Title("Linear head: (B,T,C) -> (B,T,vocab_size)")
        self.play(Write(title), run_time=0.1)

        input_label = Subtitle("Token embeddings: (4, 8, 256)")
        input_label.move_to([-4, 1.8, 0])
        self.play(Write(input_label), run_time=0.1)

        weight_label = Subtitle("Linear layer\\nW: (256, 10000)")
        weight_label.move_to([0, 1.8, 0])
        self.play(Write(weight_label), run_time=0.1)

        weight_dims = AxisLabel("(C, vocab_size)")
        sq = Square()
        weight_dims.next_to(sq, DOWN, buff=0.15)
        self.play(Write(weight_dims), run_time=0.1)

        vocab_indicator = AxisLabel("vocab_size = 10k")
        sq2 = Square().shift(RIGHT * 4)
        vocab_indicator.next_to(sq2, DOWN, buff=0.2)
        self.play(Write(vocab_indicator), run_time=0.1)

        insight = Caption("Every position predicts next token from full vocabulary!")
        insight.move_to([0, -2.2, 0])
        self.play(Write(insight), run_time=0.1)
'''

def main():
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as f:
        f.write("from manim import *\n" + SCENE)
        scene_path = f.name

    print(f"Scene file: {scene_path}\n")

    # Run extract.py exactly as coach.py does in prod
    extract = PROJECT / "animation_extractor" / "extract.py"
    result = subprocess.run(
        [sys.executable, str(extract), scene_path, "TestScene"],
        capture_output=True, text=True, cwd=str(PROJECT),
    )
    print("─── stderr ───")
    print(result.stderr[:3000])
    print()
    print("─── stdout (parsed JSON, font_size per Text) ───")
    try:
        data = json.loads(result.stdout)
    except Exception as e:
        print(f"JSON parse failed: {e}")
        print("First 500 chars of stdout:")
        print(result.stdout[:500])
        return

    text_mobjects = [(mid, m) for mid, m in data.get("mobjects", {}).items()
                     if m.get("type") in ("Text", "MarkupText")]
    print(f"Total Text mobjects: {len(text_mobjects)}")
    for mid, m in text_mobjects:
        text_preview = m.get("text", "")[:50].replace("\n", " ")
        print(f"  {mid}  font_size={m.get('font_size'):>8.3f}  height={m.get('height', '-')!s:>10s}  text={text_preview!r}")

    os.unlink(scene_path)

if __name__ == "__main__":
    main()
