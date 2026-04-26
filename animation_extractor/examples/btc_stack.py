from manim import *
import numpy as np


class BTCStack(Scene):
    """
    Animation 4c: 4 batches → (B, T, C).

    ONE concept only: you have 4 (T, C) matrices — one per batch.
    Stack them = (B, T, C). No lookup, no per-token detail.

    ~8 seconds. No LaTeX required.
    """

    def construct(self):
        B, T, C = 4, 8, 16
        VOCAB = 10
        np.random.seed(42)
        token_ids = np.random.randint(0, VOCAB, (B, T))

        row_colors = [
            interpolate_color(BLUE, ORANGE, v / (VOCAB - 1)) for v in range(VOCAB)
        ]
        row_opacities = [
            [float(np.random.uniform(0.35, 0.95)) for _ in range(C)]
            for _ in range(VOCAB)
        ]

        def make_slab(batch_idx, alpha_mult=1.0):
            slab = VGroup()
            slab_row_h = 0.26
            slab_cell_w = 0.18
            for t in range(T):
                tid = int(token_ids[batch_idx][t])
                row = VGroup(*[
                    Rectangle(
                        width=slab_cell_w, height=slab_row_h,
                        stroke_width=0.3, stroke_color=GREY_D,
                        fill_opacity=row_opacities[tid][c] * alpha_mult,
                        fill_color=row_colors[tid],
                    )
                    for c in range(C)
                ]).arrange(RIGHT, buff=0)
                slab.add(row)
            slab.arrange(DOWN, buff=0.03)
            return slab

        # ═══ Title ═══
        title = Text(
            "4 batches  →  (B, T, C)", font_size=28, font="Inter").to_edge(UP, buff=0.4)
        self.play(Write(title), run_time=0.6)

        # ═══ Stage 1: ONE (T, C) matrix front and center ═══
        slab_b0 = make_slab(0, alpha_mult=1.0)
        slab_b0.move_to([-1.5, -0.2, 0])

        b0_label = Text(
            "one batch:  (T, C)",
            font_size=16, color=GREEN,font="Inter").next_to(slab_b0, DOWN, buff=0.25)

        self.play(FadeIn(slab_b0), Write(b0_label), run_time=0.9)
        self.wait(0.4)

        # ═══ Stage 2: 3 more (T,C) matrices cascade in behind ═══
        slab_b1 = make_slab(1, alpha_mult=0.85)
        slab_b2 = make_slab(2, alpha_mult=0.7)
        slab_b3 = make_slab(3, alpha_mult=0.55)

        dx = 0.45
        dy = 0.3
        slab_b1.move_to(slab_b0.get_center() + np.array([dx, dy, 0]))
        slab_b2.move_to(slab_b0.get_center() + np.array([2 * dx, 2 * dy, 0]))
        slab_b3.move_to(slab_b0.get_center() + np.array([3 * dx, 3 * dy, 0]))

        # Reveal back → front so the depth ordering reads correctly
        self.play(FadeIn(slab_b3, shift=LEFT * 0.4), run_time=0.45)
        self.play(FadeIn(slab_b2, shift=LEFT * 0.4), run_time=0.45)
        self.play(FadeIn(slab_b1, shift=LEFT * 0.4), run_time=0.45)
        self.wait(0.3)

        # ═══ Stage 3: indicate B dimension ═══
        # Arrow along the "depth" diagonal
        depth_start = slab_b0.get_corner(DOWN + RIGHT) + np.array([0.1, -0.1, 0])
        depth_end = slab_b3.get_corner(DOWN + RIGHT) + np.array([0.1, -0.1, 0])
        b_arrow = Arrow(
            depth_start, depth_end,
            color=ORANGE, stroke_width=3, buff=0.05,
            max_tip_length_to_length_ratio=0.08,
        )
        b_label = Text(
            "B = 4  (batch)",
            font_size=17, color=ORANGE,font="Inter").next_to(b_arrow.get_center(), RIGHT, buff=0.25).shift(UP * 0.1)

        self.play(
            GrowArrow(b_arrow), Write(b_label),
            FadeOut(b0_label),
            run_time=0.8,
        )
        self.wait(0.3)

        # ═══ Stage 4: final caption ═══
        final_caption = Text(
            "(B, T, C) = (4, 8, 16)",
            font_size=26, color=YELLOW_B,font="Inter").shift(DOWN * 3.0)
        self.play(Write(final_caption), run_time=0.7)
        self.wait(1.0)
