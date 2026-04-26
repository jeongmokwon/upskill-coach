from manim import *
import numpy as np


class LookupRow(Scene):
    """
    Animation 4a: Look up every token in ONE batch row.

    ONE concept only: the first row [6,3,7,...] becomes 8 row-vectors
    via 8 independent lookups in the embedding table.
    No (T,C) framing, no batch stacking — just "8 lookups yield 8 vectors".

    ~10 seconds. No LaTeX required.
    """

    def construct(self):
        B, T, C = 4, 8, 16
        VOCAB = 10
        np.random.seed(42)
        token_ids_all = np.random.randint(0, VOCAB, (B, T))
        tokens = token_ids_all[0]   # first batch row

        row_colors = [
            interpolate_color(BLUE, ORANGE, v / (VOCAB - 1)) for v in range(VOCAB)
        ]
        row_opacities = [
            [float(np.random.uniform(0.35, 0.95)) for _ in range(C)]
            for _ in range(VOCAB)
        ]

        # ═══ Title ═══
        title = Text(
            "Look up every token in batch 0", font_size=28, font="Inter").to_edge(UP, buff=0.35)
        self.play(Write(title), run_time=0.6)

        # ═══ Stage 1: input row (top) ═══
        input_cell_w = 0.48
        input_cells = []
        for t in range(T):
            sq = Square(side_length=input_cell_w, stroke_color=BLUE_C, stroke_width=1.5)
            sq.move_to([-1.7 + t * input_cell_w, 2.3, 0])
            num = Text(str(int(tokens[t])), font_size=16, color=WHITE, font="Inter").move_to(sq.get_center())
            input_cells.append(VGroup(sq, num))
        input_group = VGroup(*input_cells)
        input_caption = Text(
            "batch 0 tokens", font_size=13, color=BLUE_B, font="Inter").next_to(input_group, LEFT, buff=0.2)
        self.play(FadeIn(input_group, lag_ratio=0.03), Write(input_caption), run_time=0.8)
        self.wait(0.2)

        # ═══ Stage 2: embedding table on the left ═══
        tbl_row_h = 0.22
        tbl_cell_w = 0.10
        emb_rows = []
        row_labels = VGroup()
        table_group = VGroup()
        for v in range(VOCAB):
            row = VGroup(*[
                Rectangle(
                    width=tbl_cell_w, height=tbl_row_h,
                    stroke_width=0.3, stroke_color=GREY_D,
                    fill_opacity=row_opacities[v][c],
                    fill_color=row_colors[v],
                )
                for c in range(C)
            ]).arrange(RIGHT, buff=0)
            emb_rows.append(row)
            table_group.add(row)
        table_group.arrange(DOWN, buff=0.02)
        table_group.move_to([-3.8, 0.1, 0])
        for v, row in enumerate(emb_rows):
            lbl = Text(str(v), font_size=10, color=GREY_B, font="Inter").next_to(row, LEFT, buff=0.08)
            row_labels.add(lbl)
        table_label = Text(
            "embedding table", font_size=13, color=YELLOW_B, font="Inter").next_to(table_group, UP, buff=0.15)

        self.play(
            FadeIn(table_group, lag_ratio=0.03),
            FadeIn(row_labels, lag_ratio=0.03),
            FadeIn(table_label),
            run_time=0.8,
        )

        # ═══ Stage 3: output slot placeholders on the right ═══
        out_row_h = 0.22
        out_cell_w = 0.10
        out_slots = []
        out_group = VGroup()
        for t in range(T):
            slot = VGroup(*[
                Rectangle(
                    width=out_cell_w, height=out_row_h,
                    stroke_width=0.2, stroke_color=GREY_D,
                    fill_opacity=0.06, fill_color=GREY_D,
                )
                for _ in range(C)
            ]).arrange(RIGHT, buff=0)
            out_slots.append(slot)
            out_group.add(slot)
        out_group.arrange(DOWN, buff=0.06)
        out_group.move_to([3.0, 0.1, 0])
        out_label = Text("8 vectors", font_size=13, color=ORANGE, font="Inter").next_to(out_group, UP, buff=0.15)

        self.play(FadeIn(out_group, lag_ratio=0.03), FadeIn(out_label), run_time=0.6)
        self.wait(0.2)

        # ═══ Stage 4: detailed lookup for t=0 ═══
        t = 0
        tid = int(tokens[t])
        src_cell = input_cells[t]
        src_hl = SurroundingRectangle(src_cell, color=GREEN, stroke_width=2.5, buff=0.04)
        self.play(Create(src_hl), run_time=0.3)

        arrow_in = Arrow(
            src_hl.get_bottom() + DOWN * 0.02,
            row_labels[tid].get_top() + UP * 0.02,
            color=GREEN, stroke_width=2.5, buff=0.02,
            max_tip_length_to_length_ratio=0.06,
        )
        self.play(GrowArrow(arrow_in), run_time=0.4)

        target_row = emb_rows[tid]
        row_box = SurroundingRectangle(target_row, color=GREEN, stroke_width=2, buff=0.02)
        self.play(Create(row_box), run_time=0.3)

        row_copy = target_row.copy()
        self.play(row_copy.animate.move_to(out_slots[t].get_center()), run_time=0.55)
        self.play(
            FadeOut(src_hl), FadeOut(arrow_in), FadeOut(row_box),
            run_time=0.22,
        )

        # ═══ Stage 5: detailed lookup for t=1 (faster) ═══
        t = 1
        tid = int(tokens[t])
        src_cell = input_cells[t]
        src_hl = SurroundingRectangle(src_cell, color=GREEN, stroke_width=2.5, buff=0.04)
        target_row = emb_rows[tid]
        row_box = SurroundingRectangle(target_row, color=GREEN, stroke_width=2, buff=0.02)
        arrow_in = Arrow(
            src_hl.get_bottom(), row_labels[tid].get_top(),
            color=GREEN, stroke_width=2.5, buff=0.02,
            max_tip_length_to_length_ratio=0.06,
        )
        row_copy = target_row.copy()

        self.play(Create(src_hl), GrowArrow(arrow_in), Create(row_box), run_time=0.5)
        self.play(row_copy.animate.move_to(out_slots[t].get_center()), run_time=0.45)
        self.play(FadeOut(src_hl), FadeOut(arrow_in), FadeOut(row_box), run_time=0.22)

        # ═══ Stage 6: fast-fill t=2..7 in parallel ═══
        fast_anims = []
        for t in range(2, T):
            tid = int(tokens[t])
            target_row = emb_rows[tid]
            row_copy = target_row.copy()
            fast_anims.append(
                row_copy.animate.move_to(out_slots[t].get_center())
            )
        self.play(LaggedStart(*fast_anims, lag_ratio=0.11), run_time=1.8)
        self.wait(0.4)

        # ═══ Stage 7: final caption ═══
        caption = Text(
            "→  8 row-vectors",
            font_size=18, color=ORANGE,font="Inter").next_to(out_group, DOWN, buff=0.3)
        self.play(Write(caption), run_time=0.55)
        self.wait(0.8)
