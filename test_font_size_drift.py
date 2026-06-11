"""
Pin down where Manim's Text.font_size drifts — does .move_to() / .next_to() /
VGroup membership / animation playback secretly mutate `height`?

Run with the Manim-equipped venv:
    /Users/jeongmokwon/Desktop/manim-venv/bin/python test_font_size_drift.py

Prints font_size + height + initial_height before/after each operation.
"""
from manim import *


def report(label, t):
    print(f"  {label:35s}  font_size={t.font_size:7.3f}  "
          f"height={t.height:.4f}  initial_height={t.initial_height:.4f}  "
          f"_font_size={t._font_size:.2f}")


def main():
    print("─── Test 1: bare Text(font_size=22) ───")
    t = Text("Linear layer\nW: (256, 10000)", font_size=22, font="Inter")
    report("after construction", t)

    t.move_to([0, 1.8, 0])
    report("after .move_to([0, 1.8, 0])", t)

    print()
    print("─── Test 2: bare Text(font_size=16) + .next_to() ───")
    t2 = Text("vocab_size = 10k", font_size=16, font="Inter")
    report("after construction", t2)

    other = Square(side_length=1.0)
    t2.next_to(other, DOWN, buff=0.2)
    report("after .next_to(square, DOWN)", t2)

    print()
    print("─── Test 3: bare Text(font_size=18) + .move_to() ───")
    t3 = Text("Every position predicts next token from full vocabulary!",
              font_size=18, font="Inter")
    report("after construction", t3)

    t3.move_to([0, -2.2, 0])
    report("after .move_to([0, -2.2, 0])", t3)

    print()
    print("─── Test 4: same as Test 1 but inside a Scene ───")

    class _Probe(Scene):
        def construct(self):
            t = Text("Linear layer\nW: (256, 10000)", font_size=22, font="Inter")
            report("inside Scene, after construction", t)
            t.move_to([0, 1.8, 0])
            report("inside Scene, after .move_to", t)
            self.add(t)
            report("inside Scene, after self.add(t)", t)
            self.play(Write(t), run_time=0.1)
            report("inside Scene, after self.play(Write)", t)

    _Probe().render()


if __name__ == "__main__":
    main()
