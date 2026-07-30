"""
Initial notes + sequence plan from onboarding — P0-B (brief §7
"Initial policy generation", exploration build stage formerly
called P7).

When a user's onboarding completes, one dedicated LLM call reads
the whole onboarding conversation through the prior's behavioral-
science lens and produces three artifacts:

  1. initial user notes — hypothesis-confidence conditional
     statements, each quoting the onboarding conversation as
     evidence
  2. a sequence plan (2-5 steps) whose rationale cites those notes
  3. a user profile brief (brief §7) — job, learning types from the
     fixed taxonomy, learning materials, what they want from Theo as
     VERBATIM quotes, a personality read, and which path_kind their
     learning types prescribe

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
import re
import threading

import anthropic

import db
import sms

MODEL = os.environ.get("GENPLAN_MODEL", "claude-sonnet-4-5")
MAX_ATTEMPTS = 3   # 1 try + 2 retries with validation feedback

# Plan steps must be playable moves — the drain tag and the
# server-side silence tag are not plannable intents.
_PLANNABLE_TAGS = frozenset(sms.STEP_VOCABULARY) - {"none", "hold"}

# Learning types (brief §7). A FIXED multi-label taxonomy, not free
# text: the offer and the step selection branch on these, so a novel
# value is a silent behavior hole rather than extra nuance. New types
# are added here deliberately, from pilot evidence.
LEARNING_TYPES = (
    "memorization",              # retention of specific content
    "retrieval_fluency",         # instant recall under questioning
    "skill_mastery",             # practice toward doing it well
    "conceptual_understanding",  # why it works, not just that it does
    "project_completion",        # ship a concrete thing
    "exam_prep",                 # a dated external bar
    "language_acquisition",
    "habit_formation",           # the doing itself is the goal
    "exploration",               # deciding whether to pursue at all
)

# Which framing the learning path's middle layer takes for this user
# (learning_paths.path_kind). project-shaped is one option, not the
# assumption.
PATH_KINDS = (
    "deliverable",   # a project WITH a done-condition
    "coverage",      # a body of material to reach a level over
    "duration",      # sustained practice, measured in kept days
)

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
            "profile": {
                "type": "object",
                "description": ("Who this learner is, read off the "
                                "conversation. Omit or leave empty any "
                                "field the conversation does not support "
                                "— never infer."),
                "properties": {
                    "job": {"type": "string",
                            "description": "Their work/field, if stated."},
                    "learning_types": {
                        "type": "array",
                        "items": {"type": "string",
                                  "enum": list(LEARNING_TYPES)},
                        "minItems": 1,
                        "description": "Multi-label; from the fixed "
                                       "taxonomy only.",
                    },
                    "materials": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "What they learn FROM (their own "
                                       "notes, a specific book, a "
                                       "course...).",
                    },
                    "wants": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "quote": {"type": "string",
                                          "description": "VERBATIM words "
                                                         "of the user."},
                                "meaning": {"type": "string",
                                            "description": "One line on "
                                                           "what it asks "
                                                           "of Theo."},
                            },
                            "required": ["quote", "meaning"],
                        },
                    },
                    "personality": {
                        "type": "string",
                        "description": "Free-form read of how they engage.",
                    },
                    "path_kind": {
                        "type": "string",
                        "enum": list(PATH_KINDS),
                        "description": "Which framing their learning path's "
                                       "middle layer takes, given the "
                                       "learning types.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Which utterances drove this "
                                       "reading — so a wrong read can be "
                                       "told apart from a wrong rule.",
                    },
                },
                "required": ["learning_types", "wants", "personality",
                             "path_kind", "rationale"],
            },
        },
        "required": ["notes", "plan", "profile"],
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


def _norm(s):
    """Whitespace-normalized text for verbatim matching (line breaks
    and double spaces are not paraphrase)."""
    return re.sub(r"\s+", " ", s or "").strip()


def _validate(payload, user_text=None, require_profile=False):
    """Semantic checks the JSON schema can't express. → list of
    error strings (empty = valid). user_text: the user's own turns,
    whitespace-normalized, against which `wants` quotes are checked
    for being verbatim."""
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

    profile = payload.get("profile")
    if profile is None:
        if require_profile:
            errors.append("profile: missing (required)")
        return errors
    types = profile.get("learning_types") or []
    if not types:
        errors.append("profile.learning_types: at least one required")
    for t in types:
        if t not in LEARNING_TYPES:
            errors.append(f"profile.learning_types: unknown type {t!r} "
                          f"— must be one of {list(LEARNING_TYPES)}")
    # Never a reading without its grounds — the rationale is what
    # tells a wrong rule apart from a wrong read of the user.
    if not (profile.get("rationale") or "").strip():
        errors.append("profile.rationale: required — say which "
                      "utterances drove this reading")
    if profile.get("path_kind") not in PATH_KINDS:
        errors.append(f"profile.path_kind: {profile.get('path_kind')!r} "
                      f"not in {list(PATH_KINDS)}")
    for i, w in enumerate(profile.get("wants") or []):
        quote = _norm(w.get("quote"))
        if not quote:
            errors.append(f"profile.wants[{i}].quote: empty")
            continue
        # Verbatim or it is a bug: raw is sacred applies to
        # self-description. Wrapping punctuation is forgiven, wording
        # is not.
        if user_text is not None \
                and quote.strip(" \"'“”‘’.,!?~…。") not in user_text:
            errors.append(
                f"profile.wants[{i}].quote: {w.get('quote')!r} does not "
                f"appear in what the user actually said — quote their "
                f"exact words, never a paraphrase or a translation")
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
3. **A profile brief**: who this learner is, for every future
   message the coach writes.
   - `job`: their work or field, only if they said it.
   - `learning_types`: multi-label, ONLY from this fixed taxonomy —
     {", ".join(LEARNING_TYPES)}. It decides what Theo offers them
     (e.g. memorization + retrieval_fluency prescribes active
     recall and spaced questioning; exploration prescribes low-
     commitment sampling). Pick every one that applies.
   - `materials`: what they learn FROM — their own notes, a named
     book, a course, a codebase.
   - `wants`: what they want from Theo, as **VERBATIM QUOTES of
     their own words** (the server checks them against the
     transcript and rejects paraphrase — copy the characters they
     typed, do not clean up, translate, or summarize), each with a
     one-line `meaning`.
   - `personality`: a free-form read of how they engage — pace,
     defensiveness, humor, what they respond to.
   - `path_kind`: which framing their learning path's middle layer
     takes, given the learning types — `deliverable` (a project
     with a done-condition), `coverage` (a body of material to
     reach a level over, e.g. "instant recall on the first 3
     chapters"), `duration` (sustained practice).
   - `rationale`: which utterances drove this reading, especially
     the learning types and path_kind. When the coaching later
     underperforms, this is what separates "wrong rule" from "wrong
     read of the user".

   A field the conversation does not support is left out. Do NOT
   infer a job from a topic, a personality from politeness, or a
   want they never expressed — an empty field is information; an
   invented one is damage.

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
    # Only the user's own turns count as a source of verbatim quotes —
    # quoting the coach back at itself is not self-description.
    user_text = _norm(" ".join(m["content"] for m in convo
                               if m["role"] == "user"))
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
                  else _validate(tool_input, user_text=user_text,
                                 require_profile=True))
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

    # The brief goes live immediately (operator decision): unlike the
    # nightly proposals, a first read of the user has nothing to
    # revise and every send until the operator looks is otherwise
    # written blind.
    profile = payload["profile"]
    brief_version = db.save_user_profile_brief(
        user_id, job=profile.get("job", ""),
        learning_types=profile.get("learning_types") or [],
        materials=profile.get("materials") or [],
        wants=profile.get("wants") or [],
        personality=profile.get("personality", ""),
        rationale=profile.get("rationale", ""), source="p7",
        llm_call_id=llm_call_id)

    # path_kind is a property of the learning types, so it lands on
    # the path as a new version (append-only) carrying the existing
    # three layers forward. No path yet = nothing to stamp; the
    # brief still records the learning types.
    path_kind = profile.get("path_kind", "")
    path = db.get_current_path(user_id)
    if path and path_kind and path.get("path_kind") != path_kind:
        db.save_learning_path(
            user_id, path["direction"], project=path["project"],
            done_condition=path["project_done_condition"],
            source="p7", path_kind=path_kind)

    db.log_event(user_id, "plan_generated",
                 {"plan_version": plan_version,
                  "notes_count": len(payload["notes"]),
                  "brief_version": brief_version,
                  "path_kind": path_kind,
                  "llm_call_id": llm_call_id,
                  "onboarding_started_at": started},
                 source="genplan")
    print(f"[GENPLAN] {user_id}: plan v{plan_version} + "
          f"{len(payload['notes'])} notes + brief v{brief_version} "
          f"generated — review via /plan and /notes before the first "
          f"sequence send", flush=True)
    return {"plan_version": plan_version,
            "notes": len(payload["notes"]),
            "brief_version": brief_version,
            "path_kind": path_kind,
            "llm_call_id": llm_call_id}


_REPLAN_TOOL = {
    "name": "submit_plan_proposal",
    "description": "Propose a revised sequence plan for this user.",
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": _TOOL["input_schema"]["properties"]["plan"]
                     ["properties"]["steps"],
            "rationale": {"type": "string"},
        },
        "required": ["steps", "rationale"],
    },
}


def propose_replan(user_id, reasons, client=None):
    """Generate a plan-v2 PROPOSAL after [REPLAN:] requests (P0-D
    part 2). Deliberately does NOT save a sequence_plans row — the
    latest saved version is the ACTIVE plan, so activation is the
    operator's move: review the plan_proposed event, then POST the
    steps to /plan. Inputs: prior + vocabulary + the user's notes +
    the recent trajectory + the current plan and the replan reasons.
    Returns the proposal dict or None."""
    import notes as notes_mod
    import trace as trace_mod

    plan = db.get_current_plan(user_id)
    prior, h_prior = sms._read_prompt_versioned("prior")
    plan_text = "(no active plan)"
    if plan:
        plan_text = f"v{plan['version']}, cursor at step {plan['cursor'] + 1}:\n" + \
            "\n".join(f"  {i+1}. {s['tag']}@{s.get('intensity', 2)} — "
                      f"{s.get('intent', '')}"
                      for i, s in enumerate(plan["steps"]))
    system = f"""You are the planning layer of Theo, an AI learning coach. The
coach flagged that this user's current sequence plan no longer fits
the signals. Propose a REVISED plan (2-5 steps, {{tag, intensity
1-3, intent}}) via the submit_plan_proposal tool (you MUST call
it). Ground every step in the user's notes or the prior; the
rationale must say what the old plan got wrong, citing the replan
reasons and the trajectory.

---

{prior}

---

{_vocab_section()}

---

{notes_mod.render_notes_block(user_id) or "(no notes yet)"}

---

## Current plan (being replaced)

{plan_text}

## Why the coach requested a replan

{chr(10).join('- ' + r for r in reasons)}

## Recent trajectory

{trace_mod.render_trace(user_id, days=7)}"""
    messages = [{"role": "user", "content": "Propose the revised plan."}]
    if client is None:
        client = anthropic.Anthropic()
    payload, errors, llm_call_id = None, [], None
    for attempt in range(MAX_ATTEMPTS):
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=2000, system=system,
                messages=messages, tools=[_REPLAN_TOOL],
                tool_choice={"type": "tool",
                             "name": "submit_plan_proposal"})
            tool_input = next((b.input for b in resp.content
                               if getattr(b, "type", "") == "tool_use"),
                              None)
        except Exception as e:
            print(f"[GENPLAN] ❌ replan call failed: {e}", flush=True)
            db.log_event(user_id, "llm_error",
                         {"where": "propose_replan",
                          "error": str(e)[:300]}, source="genplan")
            return None
        raw = json.dumps(tool_input, ensure_ascii=False) if tool_input else ""
        llm_call_id = db.save_llm_call(
            user_id, f"replan_attempt{attempt + 1}", MODEL, system,
            messages, prompt_versions={"prior": h_prior},
            response_text=raw)
        errors = (["no tool_use block"] if tool_input is None
                  else _validate({"notes": [], "plan": tool_input}))
        if not errors:
            payload = tool_input
            break
        messages = messages + [
            {"role": "assistant", "content": raw or "(no tool call)"},
            {"role": "user", "content": "Validation failed:\n- "
             + "\n- ".join(errors) + "\nResubmit corrected."}]
    if payload is None:
        db.log_event(user_id, "replan_proposal_failed",
                     {"errors": errors[:10], "llm_call_id": llm_call_id},
                     source="genplan")
        return None
    db.log_event(user_id, "plan_proposed",
                 {"steps": payload["steps"],
                  "rationale": payload["rationale"][:600],
                  "replan_reasons": reasons[:5],
                  "llm_call_id": llm_call_id}, source="genplan")
    print(f"[GENPLAN] {user_id}: plan proposal logged — review the "
          f"plan_proposed event, then POST /plan to activate",
          flush=True)
    return payload


def generate_async(user_id):
    """Fire-and-forget wrapper for the completion hook — generation
    takes tens of seconds and must not delay the reply that
    completed onboarding."""
    threading.Thread(target=generate, args=(user_id,),
                     daemon=True).start()
