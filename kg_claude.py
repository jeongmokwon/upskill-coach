"""
Claude Integration for Knowledge Graph
- Domain classification from user input
- Diagnostic question generation
- Concept extraction from conversations
- Exercise generation + grading
"""

import json
import anthropic

_client = None
MODEL = "claude-sonnet-4-20250514"

def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client

USER_BACKGROUND = ""


def _parse_json(text):
    """Robust JSON parsing - handles backticks, markdown, etc."""
    text = text.strip()
    # Remove markdown code blocks
    if "```" in text:
        parts = text.split("```")
        for part in parts:
            part = part.strip()
            if part.startswith("json"):
                part = part[4:].strip()
            if part.startswith("[") or part.startswith("{"):
                try:
                    return json.loads(part)
                except json.JSONDecodeError:
                    continue
    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in text
        for start_char, end_char in [("[", "]"), ("{", "}")]:
            start = text.find(start_char)
            end = text.rfind(end_char)
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start:end+1])
                except json.JSONDecodeError:
                    continue
        return None


def identify_domains(user_response):
    """Extract knowledge domain from user's study topic.
    Returns: [{"id": "deep_learning", "name": "Deep Learning"}]
    """
    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=200,
        system="""The user described what they want to study. Classify it into ONE academic/technical field.
Reply with JSON array only. No backticks. Exactly 1 item.
domain id is snake_case English, name is English.

Good: deep_learning, natural_language_processing, contract_law, organic_chemistry, frontend_development
Bad: building_gpt_from_scratch, transformer_and_gpt, studying_math

Examples:
User: "Karpathy let's build GPT review"
[{"id": "deep_learning", "name": "Deep Learning"}]

User: "Contract review study"
[{"id": "contract_law", "name": "Contract Law"}]

User: "Learning React"
[{"id": "frontend_development", "name": "Frontend Development"}]""",
        messages=[{"role": "user", "content": user_response}],
    )

    text = response.content[0].text.strip()
    print(f"  [debug] Claude response: {text}")

    result = _parse_json(text)
    if result and isinstance(result, list):
        return result
    return [{"id": "general", "name": user_response[:50]}]


def generate_diagnostic_questions(domain_name, n=5):
    """Generate CAT-style diagnostic questions.
    Returns: [{"question": "...", "concept": "concept_id", "concept_name": "...", "difficulty": "easy/medium/hard", "answer_hint": "..."}]
    """
    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=600,
        system=f"""Create {n} diagnostic questions for "{domain_name}".
Difficulty: 2 easy, 2 medium, 1 hard.
Each question must test a DIFFERENT core concept.
Keep questions short and clear. Max 2 lines of code if needed.
No markdown. No backticks. Plain text only.

User background: {USER_BACKGROUND}

Reply with JSON array ONLY. Nothing else:
[{{"question": "short question", "concept": "concept_id_snake_case", "concept_name": "Concept Name", "difficulty": "easy", "answer_hint": "answer keyword"}}]""",
        messages=[{"role": "user", "content": f"{domain_name} diagnostic {n} questions"}],
    )

    text = response.content[0].text.strip()
    print(f"  [debug] Diagnostic response: {text[:200]}...")

    result = _parse_json(text)
    if result and isinstance(result, list):
        return result
    print(f"  [debug] Failed to parse diagnostic questions")
    return []


def grade_answer(question, user_answer, concept_name):
    """Grade user's answer.
    Returns: {"correct": bool, "feedback": "...", "explanation": "..."}
    """
    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=150,
        system="""Grade this answer. Reply with JSON only. No backticks.
{"correct": true/false, "feedback": "one line feedback in English", "explanation": "short explanation if wrong, empty string if correct"}
Be generous - if the core idea is right, mark correct even if wording is imperfect.""",
        messages=[{
            "role": "user",
            "content": f"Concept: {concept_name}\nQuestion: {question}\nStudent answer: {user_answer}",
        }],
    )

    result = _parse_json(response.content[0].text)
    if result:
        return result
    return {"correct": False, "feedback": "Grading failed", "explanation": ""}


def extract_concepts_from_conversation(domain_name, conversation_text, existing_concepts):
    """Extract new concepts from conversation.
    Returns: [{"id": "...", "name": "...", "prerequisites": ["..."]}]
    """
    existing_list = ", ".join(existing_concepts) if existing_concepts else "none"

    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=300,
        system=f"""Extract new "{domain_name}" concepts from this conversation.
Existing concepts: {existing_list}
Only return NEW concepts not in the existing list.

STRICT RULES:
- Only extract technical concepts directly related to {domain_name}
- Do NOT extract meta-learning concepts (like "project-based learning", "study method")
- Do NOT extract vague concepts (like "foundation", "readiness")
- Good examples: "attention mechanism", "batch normalization", "cross entropy loss"
- Bad examples: "learning method", "study plan", "code review"
- Max 2 new concepts per conversation
- If nothing technical was discussed, return empty array []

Reply with JSON array only. No backticks.
[{{"id": "snake_case", "name": "English Name", "prerequisites": ["existing_concept_id"]}}]""",
        messages=[{"role": "user", "content": conversation_text}],
    )

    result = _parse_json(response.content[0].text)
    if result and isinstance(result, list):
        return result
    return []


def generate_exercise(domain_name, concept_name, mastery, user_background=None):
    """Generate exercise based on knowledge gap."""
    if mastery < 0.3:
        difficulty = "very easy"
    elif mastery < 0.6:
        difficulty = "medium"
    else:
        difficulty = "challenging"

    bg = user_background or USER_BACKGROUND

    response = _get_client().messages.create(
        model=MODEL,
        max_tokens=100,
        system=f"""Create ONE {difficulty} exercise about "{concept_name}".
User background: {bg}
One line question. Max 2 lines of code. No markdown. Plain text. English.""",
        messages=[{"role": "user", "content": f"{concept_name} exercise"}],
    )

    return response.content[0].text.strip()


def determine_level(diagnostic_results):
    """Determine level from diagnostic results."""
    easy_correct = sum(1 for r in diagnostic_results if r["difficulty"] == "easy" and r["correct"])
    medium_correct = sum(1 for r in diagnostic_results if r["difficulty"] == "medium" and r["correct"])
    hard_correct = sum(1 for r in diagnostic_results if r["difficulty"] == "hard" and r["correct"])

    if hard_correct >= 1 and medium_correct >= 1:
        return "advanced"
    elif medium_correct >= 1 or (easy_correct >= 2 and medium_correct >= 0):
        return "intermediate"
    elif easy_correct >= 1:
        return "beginner"
    else:
        return "novice"
