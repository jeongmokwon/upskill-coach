#!/usr/bin/env python3
"""
Minimal generator test — no eval agent, no web UI, no panels rendered.
Just: ontology in system prompt → Claude API → see what comes out.

Usage:
  export ANTHROPIC_API_KEY='...'
  python test_generator.py
"""

import json
import os
import sys

import anthropic

ONTOLOGY_PATH = os.path.join(os.path.dirname(__file__), "ontology.json")

def load_generator_ontology():
    with open(ONTOLOGY_PATH) as f:
        full = json.load(f)
    return {
        "pedagogical_principles": full["pedagogical_principles"],
        "panels": full["panels"],
        "teaching_patterns": full["teaching_patterns"],
    }

def build_system_prompt(ontology, topic):
    return f"""You are an expert learning coach. Your job is to teach the user about: {topic}

You have two output channels:
1. Chat message — what you say to the learner
2. Panel commands — which visual panels to open/close with what content

ALWAYS respond in this exact JSON format:
{{
  "message": "your chat message to the learner (markdown ok)",
  "panels": [
    {{
      "type": "panel_id from the panels catalog",
      "action": "open | close | update",
      "content": {{ ... panel-specific content per content_schema ... }}
    }}
  ],
  "meta": {{
    "principle_used": "P0XX — which principle guided this turn",
    "pattern": "T0XX or null",
    "pattern_step": "step name or null"
  }}
}}

If no panel action is needed, return "panels": [].

---

TEACHING PRINCIPLES (your core teaching DNA — internalize these):

{json.dumps(ontology["pedagogical_principles"], indent=2, ensure_ascii=False)}

---

AVAILABLE PANELS (your visual tools — use them when they serve learning):

{json.dumps(ontology["panels"], indent=2, ensure_ascii=False)}

---

TEACHING PATTERNS (common multi-step sequences — follow these when appropriate):

{json.dumps(ontology["teaching_patterns"], indent=2, ensure_ascii=False)}

---

IMPORTANT RULES:
- Respond in the same language the user writes in (Korean or English)
- Apply ONE concept per turn (P018)
- Weave in natural fill-in-the-blank prompts (P019)
- Start concrete, move to abstract (P011)
- Always respond in the JSON format above — no plain text responses
"""

def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY first")
        sys.exit(1)

    ontology = load_generator_ontology()
    print(f"Loaded: {len(ontology['pedagogical_principles'])} principles, "
          f"{len(ontology['panels'])} panels, "
          f"{len(ontology['teaching_patterns'])} patterns")

    topic = input("\nTopic to learn: ").strip()
    if not topic:
        topic = "javascript closures"

    system = build_system_prompt(ontology, topic)
    messages = []
    client = anthropic.Anthropic()

    print(f"\n{'='*50}")
    print(f"Topic: {topic}")
    print(f"System prompt: ~{len(system)//4} tokens")
    print(f"Type 'quit' to exit, 'raw' to toggle raw JSON")
    print(f"{'='*50}\n")

    show_raw = False

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
        if user_input == "raw":
            show_raw = not show_raw
            print(f"  [raw JSON: {'ON' if show_raw else 'OFF'}]")
            continue

        messages.append({"role": "user", "content": user_input})

        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=system,
            messages=messages,
        )

        raw_text = response.content[0].text
        messages.append({"role": "assistant", "content": raw_text})

        # Try to parse as JSON
        try:
            parsed = json.loads(raw_text)

            # Print chat message
            print(f"\nCoach: {parsed.get('message', '(no message)')}\n")

            # Print panel commands
            panels = parsed.get("panels", [])
            if panels:
                for p in panels:
                    action = p.get("action", "?")
                    ptype = p.get("type", "?")
                    print(f"  [{action.upper()}] {ptype}")
                    if show_raw:
                        content = p.get("content", {})
                        print(f"    {json.dumps(content, indent=4, ensure_ascii=False)[:500]}")
                print()

            # Print meta
            meta = parsed.get("meta", {})
            if meta:
                principle = meta.get("principle_used", "?")
                pattern = meta.get("pattern", None)
                step = meta.get("pattern_step", None)
                meta_str = f"  principle: {principle}"
                if pattern:
                    meta_str += f" | pattern: {pattern}"
                if step:
                    meta_str += f" > {step}"
                print(meta_str)
                print()

        except json.JSONDecodeError:
            # LLM didn't return valid JSON — print raw
            print(f"\nCoach (raw): {raw_text[:1000]}\n")
            print("  [WARNING: response was not valid JSON]\n")

if __name__ == "__main__":
    main()
