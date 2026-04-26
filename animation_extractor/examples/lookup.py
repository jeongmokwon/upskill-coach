from manim import *
import numpy as np


class Lookup(Scene):
    """
    Animation 3: What is a lookup?

    ONE concept only: given an integer (token id), go to that row of the
    embedding table. That row IS the embedding vector for that token.

    ~8 seconds. No LaTeX required.
    """

    def construct(self):
        VOCAB = 10
        C = 16
        TARGET_ID = 6
        np.random.seed(42)

        row_colors = [
            interpolate_color(BLUE, ORANGE, v / (VOCAB - 1)) for v in range(VOCAB)
        ]
        row_opacities = [
            [float(np.random.uniform(0.35, 0.95)) for _ in range(C)]
            for _ in range(VOCAB)
        ]

        # ═══ Title ═══
        title = Text("What is a lookup?", font_size=32, font="Inter").to_edge(UP, buff=0.5)
        self.play(Write(title), run_time=0.7)

        # ═══ Stage 1: a single token id on the left ═══
        token_cell = Square(
            side_length=1.0, stroke_color=BLUE_C, stroke_width=2.5,
        ).shift(LEFT * 4.8 + UP * 0.3)
        token_text = Text(
            str(TARGET_ID), font_size=48, color=WHITE, font="Inter").move_to(token_cell.get_center())
        token_caption = Text(
            "token id", font_size=16, color=BLUE_B, font="Inter").next_to(token_cell, DOWN, buff=0.25)

        self.play(FadeIn(token_cell), Write(token_text), run_time=0.8)
        self.play(Write(token_caption), run_time=0.3)
        self.wait(0.3)

        # ═══ Stage 2: embedding table on the right ═══
        vocab_cell_h = 0.32
        vec_cell_w = 0.20
        vocab_cells = VGroup()
        embedding_rows = []
        base_x = 0.5
        base_y = 1.9

        for v in range(VOCAB):
            idc = Square(
                side_length=vocab_cell_h, stroke_color=BLUE_C, stroke_width=1,
            )
            idc.move_to([base_x, base_y - v * vocab_cell_h, 0])
            num = Text(str(v), font_size=13, color=WHITE, font="Inter").move_to(idc.get_center())
            vocab_cells.add(VGroup(idc, num))

            row = VGroup(*[
                Rectangle(
                    width=vec_cell_w, height=vocab_cell_h - 0.03,
                    stroke_width=0.2, stroke_color=GREY_D,
                    fill_opacity=row_opacities[v][c],
                    fill_color=row_colors[v],
                )
                for c in range(C)
            ]).arrange(RIGHT, buff=0)
            row.next_to(idc, RIGHT, buff=0.08)
            embedding_rows.append(row)

        rows_group = VGroup(*embedding_rows)
        table_label = Text(
            "embedding table", font_size=16, color=YELLOW_B, font="Inter").next_to(rows_group, UP, buff=0.2)

        self.play(
            FadeIn(vocab_cells, lag_ratio=0.04),
            FadeIn(rows_group, lag_ratio=0.04),
            FadeIn(table_label),
            run_time=1.0,
        )
        self.wait(0.3)

        # ═══ Stage 3: arrow from token id → row TARGET_ID ═══
        source_box = SurroundingRectangle(
            token_cell, color=GREEN, stroke_width=3, buff=0.05,
        )
        target_id_cell = vocab_cells[TARGET_ID]

        arrow = Arrow(
            source_box.get_right() + RIGHT * 0.05,
            target_id_cell.get_left() + LEFT * 0.05,
            color=GREEN, stroke_width=3, buff=0.02,
            max_tip_length_to_length_ratio=0.08,
        )

        self.play(Create(source_box), run_time=0.4)
        self.play(GrowArrow(arrow), run_time=0.7)

        # Highlight row 6, dim the others so the target is visually isolated
        target_row = embedding_rows[TARGET_ID]
        row_box = SurroundingRectangle(
            target_row, color=GREEN, stroke_width=2.5, buff=0.03,
        )

        dim_anims = []
        for v in range(VOCAB):
            if v != TARGET_ID:
                dim_anims.append(embedding_rows[v].animate.set_opacity(0.2))
                dim_anims.append(vocab_cells[v].animate.set_opacity(0.35))

        self.play(Create(row_box), *dim_anims, run_time=0.7)
        self.wait(0.3)

        # ═══ Stage 4: extract the row — move it to the bottom as a standalone vector ═══
        extracted = target_row.copy()
        self.play(
            extracted.animate.move_to(DOWN * 2.2).scale(1.4),
            run_time=0.9,
        )

        vector_label = Text(
            f"embedding vector for token {TARGET_ID}   —   shape (C,) = (16,)",
            font_size=18, color=ORANGE,font="Inter").next_to(extracted, DOWN, buff=0.3)
        self.play(Write(vector_label), run_time=0.7)
        self.wait(1.0)
