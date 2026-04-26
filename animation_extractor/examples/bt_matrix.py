from manim import *


class BTMatrix(Scene):
    """
    Animation 1: What is a (B, T) matrix?

    ONE concept only — no embedding table, no lookup, no (B,T,C).
    Just: 4 sentences, each with 8 tokens, arranged as a (4, 8) matrix.

    ~9 seconds. No LaTeX required.
    """

    def construct(self):
        B, T = 4, 8
        # Use 4 strings of exactly 8 characters so each char = one token
        sentences = ["Hello!!!", "Goodbye!", "Tokenize", "PyTorch!"]

        # Stable char → int id (for visual consistency when morphing)
        unique_chars = sorted(set("".join(sentences)))
        char_to_id = {c: i for i, c in enumerate(unique_chars)}

        # ═══ Title ═══
        title = Text(
            "What is a (B, T) matrix?", font_size=32, font="Inter").to_edge(UP, buff=0.5)
        self.play(Write(title), run_time=0.7)

        # ═══ Stage 1: 4 sentences appear as rows of character cells ═══
        cell_w = 0.6
        squares = {}
        chars = {}
        sentence_labels = VGroup()
        row_groups = []

        for b in range(B):
            row = VGroup()
            for t in range(T):
                sq = Square(
                    side_length=cell_w,
                    stroke_color=BLUE_C,
                    stroke_width=1.5,
                )
                sq.move_to([-2.1 + t * cell_w, 0.6 - b * cell_w, 0])
                ch = Text(sentences[b][t], font_size=22, color=WHITE, font="Inter")
                ch.move_to(sq.get_center())
                squares[(b, t)] = sq
                chars[(b, t)] = ch
                row.add(VGroup(sq, ch))
            row_groups.append(row)

            # "Sentence 0/1/2/3" label to the left of each row
            lbl = Text(
                f'"{sentences[b]}"',
                font_size=15,
                color=GREY_B,
                slant=ITALIC,font="Inter")
            lbl.next_to(row[0], LEFT, buff=0.35)
            sentence_labels.add(lbl)

        for b in range(B):
            self.play(
                FadeIn(row_groups[b], lag_ratio=0.08),
                FadeIn(sentence_labels[b]),
                run_time=0.5,
            )
        self.wait(0.4)

        # ═══ Stage 2: characters morph into integer token IDs ═══
        id_transforms = []
        for b in range(B):
            for t in range(T):
                tid = char_to_id[sentences[b][t]]
                new_num = Text(str(tid), font_size=20, color=YELLOW_B, font="Inter")
                new_num.move_to(chars[(b, t)].get_center())
                id_transforms.append(Transform(chars[(b, t)], new_num))

        self.play(
            LaggedStart(*id_transforms, lag_ratio=0.012),
            FadeOut(sentence_labels),
            run_time=1.6,
        )
        self.wait(0.3)

        # ═══ Stage 3: axis labels via braces ═══
        matrix_group = VGroup(
            *[squares[(b, t)] for b in range(B) for t in range(T)]
        )

        left_brace = Brace(matrix_group, LEFT, color=GREEN)
        b_label = Text(
            "B = 4  (batch)", font_size=18, color=GREEN, font="Inter").next_to(left_brace, LEFT, buff=0.15)

        top_brace = Brace(matrix_group, UP, color=ORANGE)
        t_label = Text(
            "T = 8  (sequence length)", font_size=18, color=ORANGE, font="Inter").next_to(top_brace, UP, buff=0.15)

        self.play(GrowFromCenter(left_brace), Write(b_label), run_time=0.85)
        self.play(GrowFromCenter(top_brace), Write(t_label), run_time=0.85)

        # ═══ Stage 4: final shape caption ═══
        caption = Text(
            "(B, T) = (4, 8)   —   32 tokens total",
            font_size=22,
            color=BLUE_B,font="Inter").next_to(matrix_group, DOWN, buff=0.8)
        self.play(Write(caption), run_time=0.7)
        self.wait(1.1)
