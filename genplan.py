"""
Initial notes + sequence plan from onboarding — P0-B (brief §7
"Initial policy generation", exploration build stage formerly
called P7).

When a user's onboarding completes, one dedicated LLM call reads
the whole onboarding conversation through the prior's behavioral-
science lens and produces two artifacts:

  1. initial user notes — hypothesis-confidence conditional
     statements, each quoting the onboarding conversation as
     evidence
  2. a sequence plan (2-5 steps) whose rationale cites those notes

The chain plan → notes → conversation quotes is the point: the
rehearsal evaluates whether the reasoning was grounded, not whether
the first guess was right.

Output discipline (operator-designed): a FORCED tool call with a
JSON schema — the model cannot answer in prose — plus server-side
semantic validation (tags in vocabulary, 2-5 steps, intensities
1-3, expect vocabulary) with bounded retries. On persistent
failure NOTHING is partially saved; the raw attempts live in
llm_calls and a p7_generation_failed event flags the operator.
The founder reviews /notes and /plan before the first
sequence-mode send.
"""

import json
import os
import threading

import anthropic

import db
import sms

MODEL = os.environ.get("GENPLAN_MODEL", "claude-sonnet-4-5")
MAX_ATTEMPTS = 3   # 1 try + 2 retries with validation feedback

# Plan steps must be playable moves — the drain tag and the
# server-side silence tag are not plannable intents.
_PLANNABLE_TAGS = frozenset(sms.STEP_VOCABULARY) - {"none", "hold"}

_TOOL = {
    "name": "submit_initial_plan",
    "description": ("Submit the initial user notes and sequence plan "
                    "derived from the onboarding conversation."),
    "input_schema": {
        "type": "object",
        "properties": {
            "notes": {
                "type": "array",
                "minItems": 2,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "claim": {"type": "string"},
                        "given": {"type": "object"},
                        "when": {"type": "array",
                                 "items": {"type": "string"}},
                        "expect": {"type": "string"},
                        "evidence_quotes": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1},
                    },
                    "required": ["claim", "when", "expect",
                                 "evidence_quotes"],
                },
            },
            "plan": {
                "type": "object",
                "properties": {
                    "steps": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 5,
                        "items": {
                            "type": "object",
                            "properties": {
                                "tag": {"type": "string"},
                                "intensity": {"type": "integer"},
                                "intent": {"type": "string"},
                            },
                            "required": ["tag", "intensity", "intent"],
                        },
                    },
                    "rationale": {"type": "string"},
                },
                "required": ["steps", "rationale"],
            },
        },
        "required": ["notes", "plan"],
    },
}


def _vocab_section():
    """The step-vocabulary definitions + intensity anchors from
    sms_shared (single source of truth). Falls back to the bare tag
    list if the section can't be located."""
    try:
        shared = sms._read_prompt("sms_shared")
        start = shared.index("### Vocabulary")
        rest = shared[start:]
        end = rest.find("\n## ")
        return rest[:end] if end > 0 else rest
    except Exception:
        return "Step tags: " + ", ".join(sorted(sms.STEP_VOCABULARY))


def _validate(payload):
    """Semantic checks the JSON schema can't express. → list of
    error strings (empty = valid)."""
    errors = []
    for i, n in enumerate(payload.get("notes", [])):
        for t in n.get("when", []):
            tag = t.split("@")[0].strip().lower()
            if tag not in sms.STEP_VOCABULARY:
                errors.append(f"notes[{i}].when: unknown tag {t!r}")
        if n.get("expect") not in sms._EXPECT_VOCAB:
            errors.append(f"notes[{i}].expect: {n.get('expect')!r} not in "
                          f"{sorted(sms._EXPECT_VOCAB)}")
    steps = payload.get("plan", {}).get("steps", [])
    if not 2 <= len(steps) <= 5:
        errors.append(f"plan.steps: {len(steps)} steps (must be 2-5)")
    for i, s in enumerate(steps):
        if s.get("tag") not in _PLANNABLE_TAGS:
            errors.append(f"plan.steps[{i}].tag: {s.get('tag')!r} not a "
                          f"plannable vocabulary tag")
        if not isinstance(s.get("intensity"), int) \
                or not 1 <= s["intensity"] <= 3:
            errors.append(f"plan.steps[{i}].intensity: must be 1-3")
    return errors


def _build_system(user_id):
    prior, h_prior = sms._read_prompt_versioned("prior")
    phase = db.get_user_phase(user_id)
    path = db.get_current_path(user_id) or {}
    sched = db.get_user_schedule(user_id) or {}
    deliverables = (
        f"- Agreed goal (their own words): {phase['agreed_goal']}\n"
        f"- Path: {path.get('direction', '?')} | {path.get('project', '?')}"
        f" | done when: {path.get('project_done_condition', '?')}\n"
        f"- First bite: {phase['agreed_first_bite']}\n"
        f"- Their ignition marker (the sequence's terminal condition): "
        f"{phase['ignition_marker']}\n"
        f"- Agreed messaging windows: {sched.get('raw_text', '?')}"
    )
    system = f"""You are the planning layer of Theo, an AI learning coach. A new
user has just completed onboarding. Read their whole onboarding
conversation through the behavioral-science lens below and produce,
via the submit_initial_plan tool (you MUST call it):

1. **Initial user notes** (2-10): falsifiable conditional statements
   about THIS user — what levers look promising or dangerous, from
   what they actually said. Every note quotes the conversation
   verbatim as evidence (their words, not your paraphrase). Write
   `when` as step tags (optionally tag@intensity) and `expect` as
   one of: no_reply, reply, advance, withdraw, ignition. These are
   hypotheses from words, not behavior — be honest about weak
   signals by simply not writing a note.
2. **A sequence plan** (2-5 steps) for reaching THEIR ignition
   marker: each step {{tag, intensity 1-3, intent}} where intent is
   one line the coach will receive as its per-message assignment.
   The rationale must cite your notes — a step with no grounding in
   a note should default to the prior's theory, and say so.

The plan will be played ONE STEP PER TURN across real conversations
(the server enforces this), so design steps as conversational
moments, not a monologue outline.

---

{prior}

---

{_vocab_section()}

---

## What onboarding agreed (the five fields)

{deliverables}"""
    return system, {"prior": h_prior}


def generate(user_id):
    """Run the generation call for a user. Returns a summary dict on
    success, None on failure. Never raises — failures are evented."""
    state = db.get_onboarding_state(user_id)
    started = state.get("started_at")
    convo = db.get_recent_sms_messages(user_id, limit=500, since=None)
    if not convo:
        db.log_event(user_id, "p7_generation_failed",
                     {"errors": ["no onboarding conversation found"]},
                     source="genplan")
        return None

    transcript = "\n".join(
        f"{'USER' if m['role'] == 'user' else 'COACH'}: {m['content']}"
        for m in convo)
    system, versions = _build_system(user_id)
    messages = [{"role": "user",
                 "content": "## Onboarding conversation (oldest first)\n\n"
                            + transcript}]

    client = anthropic.Anthropic()
    llm_call_id = None
    payload = None
    errors = []
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=4000,
                system=system, messages=messages,
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "submit_initial_plan"},
            )
            tool_input = next((b.input for b in resp.content
                               if getattr(b, "type", "") == "tool_use"), None)
        except Exception as e:
            print(f"[GENPLAN] ❌ call failed: {e}", flush=True)
            db.log_event(user_id, "llm_error",
                         {"where": "genplan", "error": str(e)[:300]},
                         source="genplan")
            return None

        # Flight-record BEFORE validation (T2b): failed attempts are
        # exactly the ones whose raw output matters.
        raw = json.dumps(tool_input, ensure_ascii=False) if tool_input else ""
        llm_call_id = db.save_llm_call(
            user_id, f"genplan_attempt{attempt + 1}", MODEL, system,
            messages, prompt_versions=versions, response_text=raw)

        errors = (["no tool_use block in response"] if tool_input is None
                  else _validate(tool_input))
        if not errors:
            payload = tool_input
            break
        print(f"[GENPLAN] attempt {attempt + 1} invalid: {errors}",
              flush=True)
        messages = messages + [
            {"role": "assistant", "content": raw or "(no tool call)"},
            {"role": "user",
             "content": "Your submission failed validation:\n- "
                        + "\n- ".join(errors)
                        + "\nCall submit_initial_plan again, corrected."},
        ]

    if payload is None:
        # Nothing is partially saved — raw attempts live in llm_calls.
        db.log_event(user_id, "p7_generation_failed",
                     {"errors": errors[:10], "llm_call_id": llm_call_id},
                     source="genplan")
        return None

    for n in payload["notes"]:
        db.save_user_note(
            user_id, n["claim"], given=n.get("given") or {},
            when=n["when"], expect=n["expect"],
            evidence=[{"quote": q, "source": "onboarding"}
                      for q in n["evidence_quotes"]],
            confidence="hypothesis", source="p7")
    plan_version = db.save_sequence_plan(
        user_id, payload["plan"]["steps"],
        rationale=payload["plan"]["rationale"], source="p7")
    db.log_event(user_id, "plan_generated",
                 {"plan_version": plan_version,
                  "notes_count": len(payload["notes"]),
                  "llm_call_id": llm_call_id,
                  "onboarding_started_at": started},
                 source="genplan")
    print(f"[GENPLAN] {user_id}: plan v{plan_version} + "
          f"{len(payload['notes'])} notes generated — review via "
          f"/plan and /notes before the first sequence send", flush=True)
    return {"plan_version": plan_version,
            "notes": len(payload["notes"]),
            "llm_call_id": llm_call_id}


def generate_async(user_id):
    """Fire-and-forget wrapper for the completion hook — generation
    takes tens of seconds and must not delay the reply that
    completed onboarding."""
    threading.Thread(target=generate, args=(user_id,),
                     daemon=True).start()
