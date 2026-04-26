from manim import *
import numpy as np


class EmbeddingTable(Scene):
    """
    Animation 2: What is an embedding table?

    ONE concept only: each token id gets its own C-dimensional vector.
    Collect them all → embedding table, shape (vocab_size, C).

    ~9 seconds. No LaTeX required.
    """

    def construct(self):
        VOCAB = 10
        C = 16
        np.random.seed(42)

        # Stable per-row color + opacity — so each row has a visual identity
        row_colors = [
            interpolate_color(BLUE, ORANGE, v / (VOCAB - 1)) for v in range(VOCAB)
        ]
        row_opacities = [
            [float(np.random.uniform(0.35, 0.95)) for _ in range(C)]
            for _ in range(VOCAB)
        ]

        # ═══ Title ═══
        title = Text(
            "What is an embedding table?", font_size=32
        ).to_edge(UP, buff=0.5)
        self.play(Write(title), run_time=0.7)

        # ═══ Stage 1: vocabulary column (token ids 0..9) ═══
        vocab_cell_h = 0.34
        vocab_cells = VGroup()
        for v in range(VOCAB):
            cell = Square(
                side_length=vocab_cell_h,
                stroke_color=BLUE_C, stroke_width=1.5,
            )
            cell.move_to([-3.8, 2.0 - v * vocab_cell_h, 0])
            num = Text(str(v), font_size=14, color=WHITE).move_to(cell.get_center())
            vocab_cells.add(VGroup(cell, num))

        vocab_header = Text(
            "token id", font_size=15, color=BLUE_B
        ).next_to(vocab_cells, UP, buff=0.2)

        self.play(Write(vocab_header), run_time=0.3)
        self.play(FadeIn(vocab_cells, lag_ratio=0.05), run_time=1.0)
        self.wait(0.4)

        # ═══ Stage 2: each token sprouts a C-dim vector ═══
        vec_cell_w = 0.22
        embedding_rows = []
        for v in range(VOCAB):
            row = VGroup(*[
                Rectangle(
                    width=vec_cell_w, height=vocab_cell_h - 0.03,
                    stroke_width=0.3, stroke_color=GREY_D,
                    fill_opacity=row_opacities[v][c],
                    fill_color=row_colors[v],
                )
                for c in range(C)
            ]).arrange(RIGHT, buff=0)
            row.next_to(vocab_cells[v], RIGHT, buff=0.3)
            embedding_rows.append(row)

        grow_anims = [GrowFromEdge(row, LEFT) for row in embedding_rows]
        self.play(LaggedStart(*grow_anims, lag_ratio=0.08), run_time=2.2)
        self.wait(0.3)

        # ═══ Stage 3: axis labels ═══
        all_rows = VGroup(*embedding_rows)

        # Left brace on vocab column → vocab_size
        left_brace = Brace(vocab_cells, LEFT, color=GREEN, buff=0.1)
        b_label = Text(
            "vocab_size = 10", font_size=17, color=GREEN
        ).next_to(left_brace, LEFT, buff=0.15)

        # Top brace on embedding rows → C
        top_brace = Brace(all_rows, UP, color=ORANGE, buff=0.1)
        c_label = Text(
            "C = 16  (embedding dim)", font_size=17, color=ORANGE
        ).next_to(top_brace, UP, buff=0.15)

        self.play(GrowFromCenter(left_brace), Write(b_label), run_time=0.85)
        self.play(GrowFromCenter(top_brace), Write(c_label), run_time=0.85)

        # ═══ Stage 4: final caption ═══
        caption = Text(
            "embedding table:   (vocab_size, C) = (10, 16)",
            font_size=22, color=YELLOW_B,
        ).next_to(all_rows, DOWN, buff=0.8)
        self.play(Write(caption), run_time=0.7)
        self.wait(1.0)
