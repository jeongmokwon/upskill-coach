from manim import *
import numpy as np


class TCMatrix(Scene):
    """
    Animation 4b: 8 row-vectors → (T, C) matrix.

    ONE concept only: those 8 row-vectors TOGETHER form a (T, C) = (8, 16)
    matrix. No lookup, no batches — just "this collection is a matrix".

    ~7 seconds. No LaTeX required.
    """

    def construct(self):
        B, T, C = 4, 8, 16
        VOCAB = 10
        np.random.seed(42)
        token_ids_all = np.random.randint(0, VOCAB, (B, T))
        tokens = token_ids_all[0]

        row_colors = [
            interpolate_color(BLUE, ORANGE, v / (VOCAB - 1)) for v in range(VOCAB)
        ]
        row_opacities = [
            [float(np.random.uniform(0.35, 0.95)) for _ in range(C)]
            for _ in range(VOCAB)
        ]

        # ═══ Title ═══
        title = Text(
            "8 vectors make a (T, C) matrix", font_size=28, font="Inter").to_edge(UP, buff=0.4)
        self.play(Write(title), run_time=0.6)

        # ═══ Stage 1: 8 row-vectors stacked ═══
        slab_row_h = 0.32
        slab_cell_w = 0.22
        slab_rows = []
        slab = VGroup()
        for t in range(T):
            tid = int(tokens[t])
            row = VGroup(*[
                Rectangle(
                    width=slab_cell_w, height=slab_row_h,
                    stroke_width=0.3, stroke_color=GREY_D,
                    fill_opacity=row_opacities[tid][c],
                    fill_color=row_colors[tid],
                )
                for c in range(C)
            ]).arrange(RIGHT, buff=0)
            slab_rows.append(row)
            slab.add(row)
        slab.arrange(DOWN, buff=0.04)
        slab.move_to([0, -0.2, 0])

        self.play(
            LaggedStart(*[FadeIn(r) for r in slab_rows], lag_ratio=0.08),
            run_time=1.5,
        )
        self.wait(0.4)

        # ═══ Stage 2: frame the whole thing as ONE matrix ═══
        matrix_frame = SurroundingRectangle(
            slab, color=ORANGE, stroke_width=2.5, buff=0.08,
        )
        self.play(Create(matrix_frame), run_time=0.6)
        self.wait(0.3)

        # ═══ Stage 3: axis labels via braces ═══
        left_brace = Brace(matrix_frame, LEFT, color=GREEN, buff=0.1)
        t_label = Text(
            "T = 8  (sequence length)", font_size=17, color=GREEN,font="Inter").next_to(left_brace, LEFT, buff=0.15)

        top_brace = Brace(matrix_frame, UP, color=ORANGE, buff=0.1)
        c_label = Text(
            "C = 16  (embedding dim)", font_size=17, color=ORANGE,font="Inter").next_to(top_brace, UP, buff=0.15)

        self.play(GrowFromCenter(left_brace), Write(t_label), run_time=0.85)
        self.play(GrowFromCenter(top_brace), Write(c_label), run_time=0.85)

        # ═══ Stage 4: final caption ═══
        caption = Text(
            "(T, C) = (8, 16)",
            font_size=24, color=YELLOW_B,font="Inter").next_to(matrix_frame, DOWN, buff=0.7)
        self.play(Write(caption), run_time=0.6)
        self.wait(1.0)
