#!/usr/bin/env python3
"""
Full pipeline test: greeting → eval (diagnostic) → generator (teaching)

No web UI, no panel rendering — just terminal.
Panels print as placeholders.

Usage:
  export ANTHROPIC_API_KEY='sk-ant-...'
  python test_pipeline.py
"""

import json
import os
import sys

import anthropic

ONTOLOGY_PATH = os.path.join(os.path.dirname(__file__), "ontology.json")
MODEL = "claude-sonnet-4-20250514"
NUM_DIAGNOSTIC_QUESTIONS = 3

client = anthropic.Anthropic()

def load_ontology():
    with open(ONTOLOGY_PATH) as f:
        return json.load(f)

# ────────────────────────────────────────────────────────────────────
# EVAL AGENT — diagnostic phase
# ────────────────────────────────────────────────────────────────────

def build_eval_question_gen_prompt(ontology, topic, history):
    """System prompt for eval to generate diagnostic short-answer questions."""
    return f"""You are a diagnostic evaluator for a learning coach. The user wants to learn: {topic}

Your job in this turn: generate ONE short-answer diagnostic question that will help you assess the learner's current level.

These questions follow the "short-answer diagnostic" principle:
- The question should be answerable in 1-20 words
- Wrong answers are informative (see error_taxonomy below)
- The question should surface signals about: pattern recognition, working memory, transfer ability, attention to detail
- Start with very basic, foundational questions — you can escalate difficulty only if the learner handles the basic ones well

You have asked {len(history)} diagnostic question(s) so far out of {NUM_DIAGNOSTIC_QUESTIONS} total.

Previous Q&A:
{json.dumps(history, indent=2, ensure_ascii=False) if history else "(none yet)"}

Reference — error_taxonomy (what wrong answers reveal):
{json.dumps(ontology["error_taxonomy"], indent=2, ensure_ascii=False)}

Reference — diagnostic_cues (what to look for in responses):
{json.dumps(ontology["diagnostic_cues"], indent=2, ensure_ascii=False)}

Output ONLY a JSON object in this format:
{{
  "question": "the diagnostic question in the user's language",
  "example_shown": "optional example to show before the question (like 'var num = 1'), or null",
  "ideal_answer": "what a correct answer would look like",
  "tests_for": "what cognitive ability this question is probing"
}}"""


def build_eval_observation_prompt(ontology, topic, question_obj, user_answer):
    """System prompt for eval to analyze one answer."""
    return f"""You are a diagnostic evaluator analyzing a single user answer.

Topic: {topic}

Question asked:
{json.dumps(question_obj, indent=2, ensure_ascii=False)}

User's answer: "{user_answer}"

Reference — error_taxonomy:
{json.dumps(ontology["error_taxonomy"], indent=2, ensure_ascii=False)}

Reference — diagnostic_cues:
{json.dumps(ontology["diagnostic_cues"], indent=2, ensure_ascii=False)}

Reference — user_states (emotional/motivational states):
{json.dumps(ontology["user_states"], indent=2, ensure_ascii=False)}

Output ONLY a JSON object:
{{
  "error_type": "E001-E007 or null if correct",
  "error_reasoning": "why you chose this error_type",
  "cue_ratings": {{
    "D001_relevance": "high | mid | low",
    "D002_orthographic": "high | mid | low",
    "D003_completeness": "high | mid | low"
  }},
  "observed_states": [
    {{ "state": "B0XX", "intensity": "low | mid | high" }}
  ],
  "notes": "brief observation about this answer"
}}"""


def build_eval_conclusion_prompt(ontology, topic, diagnostic_log):
    """System prompt for eval to synthesize final user_state."""
    return f"""You are a diagnostic evaluator. The diagnostic phase is complete.

Topic: {topic}

Diagnostic log (all questions + answers + observations):
{json.dumps(diagnostic_log, indent=2, ensure_ascii=False)}

Based on the full diagnostic log, synthesize the learner's profile.

Tier definitions:
- upper: pattern recognition + transfer + attention to detail all strong
- upper-mid: mostly correct with minor syntactic or completeness issues
- mid: can reproduce but struggles to modify; some orthographic slips
- lower-mid: incomplete answers, fragments, low attention to detail
- lower: single elements, no structure, minimal engagement with the question
- lowest: avoidance, irrelevant answers, or complete disconnect

Output ONLY a JSON object:
{{
  "tier": "upper | upper-mid | mid | lower-mid | lower | lowest",
  "tier_reasoning": "1-2 sentences on why this tier",
  "dominant_error_patterns": ["E0XX", ...],
  "current_emotional_states": [
    {{ "state": "B0XX", "intensity": "low | mid | high" }}
  ],
  "summary_for_generator": "3-5 sentence narrative the generator can use to adapt its teaching. Write in the voice of a tutor briefing another tutor about this student."
}}"""


def call_llm(system_prompt, user_message="proceed"):
    """Single-shot LLM call, returns parsed JSON."""
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    text = response.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract JSON from the text
        import re
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            return json.loads(m.group())
        raise


# ────────────────────────────────────────────────────────────────────
# GENERATOR AGENT — teaching phase
# ────────────────────────────────────────────────────────────────────

def build_generator_system_prompt(ontology, topic, user_state, lesson_plan):
    plan_section = (
        json.dumps(lesson_plan, indent=2, ensure_ascii=False)
        if lesson_plan
        else "(no lesson plan yet — create one in your next turn if T002 applies)"
    )

    return f"""You are an expert learning coach. Your job is to teach the user about: {topic}

═══════════════════════════════════════════════════════════
LEARNER PROFILE (from diagnostic phase — critical context)
═══════════════════════════════════════════════════════════

{json.dumps(user_state, indent=2, ensure_ascii=False)}

Adapt your teaching to this learner:
- If tier is lower/lower-mid: maximize scaffolding, one concept at a time (strict P018), errorless learning (P007), heavy use of inline completion prompts (P019), P022 barrier reduction. For T002 practice: give only the single most important substep.
- If tier is mid: standard scaffolding, still err on the side of errorless, frequent check-ins. For T002 practice: 1-2 key substeps.
- If tier is upper-mid: standard approach, can use socratic questioning. For T002 practice: most substeps.
- If tier is upper: socratic questioning (P020), desirable difficulty (P016), less scaffolding, faster pace. For T002 practice: all substeps, possibly extended.

═══════════════════════════════════════════════════════════
CURRENT LESSON PLAN (fixed once created this session)
═══════════════════════════════════════════════════════════

{plan_section}

═══════════════════════════════════════════════════════════
OUTPUT FORMAT (always this exact JSON)
═══════════════════════════════════════════════════════════

{{
  "message": "your chat message (markdown ok)",
  "panels": [
    {{
      "type": "panel_id",
      "action": "open | close | update",
      "content": {{ ... per content_schema ... }}
    }}
  ],
  "lesson_plan": {{
    "topic": "string",
    "substeps": [
      {{ "substep_id": "s1", "label": "short label", "key_idea": "what this substep accomplishes" }}
    ],
    "practice_substep_ids": ["s1", "s2"]
  }},
  "meta": {{
    "principle_used": "P0XX",
    "pattern": "T0XX or null",
    "pattern_step": "step name or null"
  }}
}}

Include "lesson_plan" ONLY when you are in T002 step=plan. In all other turns, omit that field.
Once a lesson_plan exists in the context above, do NOT modify it — it is fixed for the session.

═══════════════════════════════════════════════════════════
TEACHING PRINCIPLES
═══════════════════════════════════════════════════════════

{json.dumps(ontology["pedagogical_principles"], indent=2, ensure_ascii=False)}

═══════════════════════════════════════════════════════════
AVAILABLE PANELS
═══════════════════════════════════════════════════════════

{json.dumps(ontology["panels"], indent=2, ensure_ascii=False)}

═══════════════════════════════════════════════════════════
TEACHING PATTERNS
═══════════════════════════════════════════════════════════

{json.dumps(ontology["teaching_patterns"], indent=2, ensure_ascii=False)}

═══════════════════════════════════════════════════════════
RULES
═══════════════════════════════════════════════════════════

- Respond in the same language the user writes in
- Apply ONE concept per turn (P018)
- Weave in natural fill-in-the-blank prompts (P019)
- Start concrete, move to abstract (P011)
- For code topics, use T002 flow: plan → model_concept (animation) → model_code (apprentice_demo, substep by substep) → practice (apprentice_practice, tier-selected substeps) → reflect (chat)
- NEVER open panel_apprentice_demo before completing model_concept with panel_animation
- NEVER dump all code at once in panel_apprentice_demo — add substep cells progressively across turns
- ALWAYS output valid JSON — no plain text
"""


# ────────────────────────────────────────────────────────────────────
# RENDER helpers
# ────────────────────────────────────────────────────────────────────

def print_divider(label):
    print(f"\n{'═'*60}\n  {label}\n{'═'*60}\n")


def render_panel_placeholder(panel):
    action = panel.get("action", "?").upper()
    ptype = panel.get("type", "?")
    content = panel.get("content", {})

    print(f"  ┌─ [{action}] {ptype}")
    for k, v in content.items():
        if isinstance(v, str):
            # Multi-line string values — indent each line
            for line in v.split("\n"):
                print(f"  │  {k + ': ' if line == v.split(chr(10))[0] else ' ' * (len(k) + 2)}{line}")
        elif isinstance(v, (list, dict)):
            # Pretty-print JSON values
            v_str = json.dumps(v, indent=2, ensure_ascii=False)
            lines = v_str.split("\n")
            print(f"  │  {k}:")
            for line in lines:
                print(f"  │    {line}")
        else:
            print(f"  │  {k}: {v}")
    print(f"  └─")


# ────────────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────────────

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY first")
        sys.exit(1)

    ontology = load_ontology()

    # ── Phase 1: Greeting ──────────────────────────────────────────
    print_divider("PHASE 1: GREETING")
    print("Coach: 안녕! 오늘 뭘 배우고 싶어요? (What do you want to learn today?)\n")
    topic = input("You: ").strip()
    if not topic:
        topic = "javascript closures"

    # ── Phase 2: Diagnostic ────────────────────────────────────────
    print_divider("PHASE 2: DIAGNOSTIC")
    print(f"(Eval agent will ask {NUM_DIAGNOSTIC_QUESTIONS} short-answer questions)\n")

    diagnostic_log = []

    for i in range(NUM_DIAGNOSTIC_QUESTIONS):
        # Eval generates question
        q_prompt = build_eval_question_gen_prompt(ontology, topic, diagnostic_log)
        question_obj = call_llm(q_prompt)

        # Display question
        print(f"[Q{i+1}/{NUM_DIAGNOSTIC_QUESTIONS}] tests_for: {question_obj.get('tests_for', '?')}")
        if question_obj.get("example_shown"):
            print(f"\nExample shown: {question_obj['example_shown']}")
        print(f"\nCoach: {question_obj['question']}\n")

        # Get answer
        answer = input("You: ").strip()

        # Eval observes
        obs_prompt = build_eval_observation_prompt(ontology, topic, question_obj, answer)
        observation = call_llm(obs_prompt)

        print(f"\n  [observed] error_type: {observation.get('error_type')} | "
              f"cues: R={observation['cue_ratings']['D001_relevance']}, "
              f"O={observation['cue_ratings']['D002_orthographic']}, "
              f"C={observation['cue_ratings']['D003_completeness']}")
        if observation.get("notes"):
            print(f"  [notes] {observation['notes']}")
        print()

        diagnostic_log.append({
            "question": question_obj,
            "answer": answer,
            "observation": observation,
        })

    # ── Eval synthesizes user_state ──────────────────────────────
    print_divider("DIAGNOSTIC CONCLUSION")
    conclusion_prompt = build_eval_conclusion_prompt(ontology, topic, diagnostic_log)
    user_state = call_llm(conclusion_prompt)

    print(f"Tier: {user_state['tier']}")
    print(f"Reasoning: {user_state['tier_reasoning']}")
    print(f"Dominant errors: {user_state['dominant_error_patterns']}")
    print(f"Emotional states: {user_state['current_emotional_states']}")
    print(f"\nSummary for generator:")
    print(f"  {user_state['summary_for_generator']}")

    # ── Phase 3: Teaching ─────────────────────────────────────────
    print_divider("PHASE 3: TEACHING")
    print("(Generator now has the full learner profile. Type 'quit' to exit, 'plan' to show lesson plan)\n")

    lesson_plan = None
    messages = []

    def render_turn(parsed):
        print(f"\nCoach: {parsed.get('message', '(no message)')}\n")
        for p in parsed.get("panels", []):
            render_panel_placeholder(p)
        meta = parsed.get("meta", {})
        if meta:
            print(f"\n  meta: principle={meta.get('principle_used')} "
                  f"pattern={meta.get('pattern')} step={meta.get('pattern_step')}")
        if parsed.get("lesson_plan"):
            print(f"\n  [LESSON PLAN CREATED]")
        print()

    def run_turn(user_message):
        nonlocal lesson_plan
        messages.append({"role": "user", "content": user_message})
        gen_system = build_generator_system_prompt(ontology, topic, user_state, lesson_plan)
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=gen_system,
            messages=messages,
        )
        raw_text = response.content[0].text
        messages.append({"role": "assistant", "content": raw_text})

        try:
            parsed = json.loads(raw_text)
            # Capture lesson_plan on first creation only (fixed after)
            if parsed.get("lesson_plan") and lesson_plan is None:
                lesson_plan = parsed["lesson_plan"]
            render_turn(parsed)
        except json.JSONDecodeError:
            print(f"\nCoach (raw, not JSON): {raw_text[:1000]}\n")

    # Prime the conversation — let coach start
    run_turn(f"나는 {topic}을(를) 배우고 싶어. 시작해줘.")

    while True:
        try:
            user_input = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue
        if user_input == "quit":
            break
        if user_input == "plan":
            if lesson_plan:
                print(f"\nLesson Plan:\n{json.dumps(lesson_plan, indent=2, ensure_ascii=False)}\n")
            else:
                print("\n  (no lesson plan yet)\n")
            continue

        run_turn(user_input)


if __name__ == "__main__":
    main()
