"""
Per-turn analysis call — brief §7 "Markers vs. the analysis call".

One dedicated single-task LLM call that runs on each inbound reply,
BEFORE the coach's generation call, so generation receives already-
updated state and only has to talk well.

Why this exists: three times a generation call silently dropped a
side duty (ignition scoring, plan [ADVANCE], then the whole marker
set collapsing mid-conversation). A model that is talking will not
reliably also do bookkeeping. So everything that can be READ FROM
THE TRANSCRIPT is extracted here instead of being emitted as a
marker by the speaker:

  - onboarding fields (goal, path, bite, ignition marker, schedule,
    offer) — filled ONLY when the user actually said or agreed to
    it, never inferred
  - step-completion judgment against the active sequence plan
    (the former _judge_step_completion, absorbed)

Decision markers ([STEP:], [EXPECT:], [REPLAN:]) stay with the
generation call — they exist only in the speaker's head and cannot
be recovered from the transcript.

Two properties that matter more than the call itself:
  - it sees the WHOLE conversation, so a fact stated three turns
    ago is still catchable — a missed turn is no longer permanent
    data loss
  - it is RE-RUNNABLE over history (analyze_history), so past
    conversations can be back-extracted
"""

import json
import os

import anthropic

import db

MODEL = os.environ.get("ANALYZE_MODEL", "claude-sonnet-4-5")

_TOOL = {
    "name": "submit_analysis",
    "description": ("Report what the conversation now establishes. "
                    "Omit any field the user has not actually said or "
                    "agreed to — omission is always the safe answer."),
    "input_schema": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": "The learning goal in the USER'S own "
                               "words/framing, one line.",
            },
            "path_direction": {
                "type": "string",
                "description": "Long-horizon direction (months-years).",
            },
            "path_project": {
                "type": "string",
                "description": "The mid-horizon target. Its shape "
                               "depends on how they learn: a "
                               "deliverable, a coverage target, or a "
                               "duration of practice.",
            },
            "path_done_condition": {
                "type": "string",
                "description": "How they will know the mid-horizon "
                               "target is reached.",
            },
            "first_bite": {
                "type": "string",
                "description": "The concrete 3-5 minute action agreed "
                               "for the next day or two — a first "
                               "physical motion, not a study session.",
            },
            "ignition_marker": {
                "type": "string",
                "description": "The observable first motion of one "
                               "ordinary session for THIS user — "
                               "something a screenshot or a message "
                               "could verify ('opens the Word file and "
                               "starts working through it'). NOT their "
                               "goal's success criterion ('answers "
                               "instantly when asked' is success months "
                               "out, not the start of tonight). Unlike "
                               "the agreement fields, you SHOULD DERIVE "
                               "this from what they told you about "
                               "their material and how they work — it "
                               "is a measuring instrument, not a "
                               "promise they made.",
            },
            "ignition_marker_basis": {
                "type": "string",
                "enum": ["stated", "inferred"],
                "description": "stated = they said or confirmed it in "
                               "so many words. inferred = you derived "
                               "it from their material/workflow.",
            },
            "ignition_marker_confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "For an inferred marker: how sure are "
                               "you? low = omit the marker instead.",
            },
            "schedule": {
                "type": "string",
                "description": "Agreed messaging windows as "
                               "HH:MM-HH:MM, comma-separated, in the "
                               "user's local time.",
            },
            "offer": {
                "type": "string",
                "description": "What the coach committed to doing for "
                               "them, that they confirmed.",
            },
            "material_description": {
                "type": "string",
                "description": "The user's OWN account of the newest "
                               "material — what it is, in their words "
                               "(compressed is fine; invented is not). "
                               "Omit if they have not described it.",
            },
            "material_wants": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "quote": {"type": "string"},
                        "meaning": {"type": "string"},
                    },
                    "required": ["quote"],
                },
                "description": "What the user wants from this "
                               "material, as VERBATIM quotes of the "
                               "USER'S OWN messages plus your "
                               "one-line reading. NEVER quote a "
                               "coach line — a coach question is not "
                               "a user want, however suggestive. The "
                               "server verifies every quote against "
                               "the user's actual messages and "
                               "silently drops any that do not "
                               "match. Report the full cumulative "
                               "list each time (it replaces the "
                               "stored one).",
            },
            "walkthrough_sample_validated": {
                "type": "boolean",
                "description": "True ONLY if BOTH happened in the "
                               "transcript: the coach produced a "
                               "concrete sample of its offer (a "
                               "question it would ask, a piece it "
                               "would cut) AND the user affirmed it "
                               "rings true. A proposal without the "
                               "user's yes is false. When true, quote "
                               "both sides in "
                               "walkthrough_sample_evidence.",
            },
            "walkthrough_sample_evidence": {
                "type": "string",
                "description": "The coach's sample and the user's "
                               "affirmation, quoted.",
            },
            "material_status": {
                "type": "string",
                "enum": ["has_material", "no_material"],
                "description": "Only when the conversation SETTLED "
                               "whether they study from a material. "
                               "has_material = they named or "
                               "described one (file, notes, video, "
                               "course, book — shared or not). "
                               "no_material = they said nothing "
                               "exists yet. Omit while it is still "
                               "unasked or ambiguous.",
            },
            "material_named": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "description": "When the conversation settled WHICH "
                               "specific thing they study from but "
                               "nothing is registered yet (an "
                               "unsharable book/course/app, or a "
                               "first material they agreed to) — its "
                               "name as the user would recognize it, "
                               "plus their own account of what it "
                               "covers. Omit if a material is "
                               "already registered.",
            },
            "step_completed": {
                "type": "string",
                "enum": ["yes", "no", "uncertain", "not_applicable"],
                "description": "Did the user's latest reply accomplish "
                               "the current plan step's purpose? "
                               "not_applicable when no step is shown.",
            },
            "step_reason": {"type": "string"},
            "notes_for_operator": {
                "type": "string",
                "description": "Anything notable a human should see "
                               "(optional, one line).",
            },
        },
        "required": ["step_completed", "step_reason"],
    },
}

_FIELD_TO_KEY = {
    "goal": "goal",
    "bite": "first_bite",
    "ignition_marker": "ignition_marker",
    "schedule": "schedule",
    "offer": "offer",
    "path": "path_direction",
}


def _norm(t):
    """Whitespace-normalized text for verbatim matching (line breaks
    and double spaces are not paraphrase; anything else is)."""
    import re as _re
    return _re.sub(r"\s+", " ", t or "").strip()


def _user_said(user_id, quote, limit=100):
    """True if `quote` appears verbatim (whitespace-normalized) in
    one of the USER'S own messages. The attribution guard: observed
    in prod, the extraction quoted the COACH'S questions as user
    wants — plausible text, wrong mouth. Prompt rules lower the
    rate; only matching against the actual transcript makes
    mis-attribution structurally impossible."""
    q = _norm(quote)
    if not q:
        return False
    for m in db.get_recent_sms_messages(user_id, limit=limit):
        if m["role"] == "user" and q in _norm(m["content"]):
            return True
    return False


def _transcript(user_id, limit=100):
    msgs = db.get_recent_sms_messages(user_id, limit=limit)
    return "\n".join(
        f"{'USER' if m['role'] == 'user' else 'COACH'}: {m['content']}"
        for m in msgs)


def _build_system(user_id):
    """Assemble the analysis prompt: what is already known, what is
    still missing, and (post-onboarding) the current plan step."""
    state = db.get_onboarding_state(user_id)
    phase = db.get_user_phase(user_id)
    path = db.get_current_path(user_id) or {}
    sched = db.get_user_schedule(user_id) or {}
    prof = db.get_user_profile_by_id(user_id) or {}

    known = [
        f"- goal: {phase['agreed_goal'] or '(unknown)'}",
        f"- path: {path.get('direction', '(unknown)')} | "
        f"{path.get('project', '')} | {path.get('project_done_condition', '')}",
        f"- first bite: {phase['agreed_first_bite'] or '(unknown)'}",
        f"- ignition marker: {phase['ignition_marker'] or '(unknown)'}",
        f"- schedule: {sched.get('raw_text', '(unknown)')}",
        f"- offer (what the coach committed to): "
        f"{prof.get('agreed_offer') or '(unknown)'}",
        f"- material alignment: "
        f"{state.get('material_status') or '(not settled)'}",
    ]
    mats = db.get_user_materials(user_id)
    if mats:
        m = mats[0]
        known.append(
            f"- newest material: {m.get('title') or m.get('source_url')} "
            f"({m['kind']}, walkthrough: {m['walkthrough_status']}) | "
            f"their description so far: "
            f"{m.get('user_description') or '(none)'} | "
            f"wants recorded: {len(m.get('wants') or [])}")

    step_block = "No active plan step — report step_completed as not_applicable."
    plan = db.get_current_plan(user_id)
    if state["completed_at"] and plan and plan["cursor"] < len(plan["steps"]):
        s = plan["steps"][plan["cursor"]]
        step_block = (
            f"Current plan step: {s['tag']}@{s.get('intensity', 2)} — "
            f"{s.get('intent', '')}\n"
            "yes = the reply itself accomplishes what this step was for "
            "(an elicit step: they actually articulated it; an ask step: "
            "they did it or committed to it). no = not yet. uncertain = "
            "genuinely ambiguous (treated as no). Judge substance, not "
            "politeness.")

    return f"""You are the analysis layer of Theo, an AI learning coach. You do
not talk to the user. You read the conversation and report what it
now establishes, via the submit_analysis tool (you MUST call it).

## Hard rule: agreements come only from the user's own words

goal, path, first_bite, schedule, offer are AGREEMENTS. Fill them
only when the user actually said or explicitly agreed to them.
Never infer one, never complete a half-agreement. If it is not
settled, OMIT the field — omission costs one more turn of
conversation; a wrong fill corrupts their record. Re-report a field
only if the conversation REFINED it beyond what is known below
(identical restatements: omit).

**ignition_marker is the exception, on purpose.** It is not a
promise the user makes — it is the instrument the system judges
their sessions with, so it should be DERIVED from what they have
told you about their material and how they work. Once you know
someone studies from a Word file of their own notes, "opens that
file and starts working through it" is a sound inference, not an
invention. Report it with basis=inferred and your confidence; use
low only when you would be guessing, and then omit the marker
entirely. An inferred marker is provisional — the coach will
confirm it in passing later — so a reasonable inference beats an
empty field.

## Already known (do not re-report unchanged)

{chr(10).join(known)}

Still missing: {', '.join(state['missing']) or '(nothing)'}

## Step judgment

{step_block}

## Field notes

- schedule must be HH:MM-HH:MM (24h), comma-separated, the user's
  local time — convert if they said "저녁 8시쯤".
- ignition_marker must be observable ("에디터 열고 타이핑 시작"),
  not a feeling ("집중되면").
- path_project's shape follows how they learn: a deliverable with a
  done-condition, a coverage target ("자료 3장까지 즉답"), or a
  duration of practice. Report whichever they agreed to.
- offer only counts once the user has confirmed it, not when the
  coach merely proposed it.
- material_description / material_wants belong to the newest
  material shown above and follow the agreements rule: the user's
  words only. material_wants REPLACES the stored list — report the
  full cumulative set every time.
- walkthrough_sample_validated is the arc's gate: it flips the
  material to walked-through and unlocks the offer, so hold it to
  the letter — the coach demonstrated a sample AND the user
  affirmed it. "그런 건 안 물어봐" is a false; it is also exactly
  the walkthrough working, so record what they DID say in
  material_wants.
- material_status is the settled answer to "do they study from
  something?" — report it the turn the conversation settles it,
  either way. no_material is a good answer, not a failure; it means
  the offer gets built without one. Do not infer no_material from
  silence — only from them saying so.
- material_named: when they name the specific thing (an unsharable
  book/course, or a first material just agreed), report it so the
  server can register the name as the anchor."""


def analyze(user_id, trigger="inbound", client=None):
    """Run one analysis pass. Applies validated extractions, judges
    the plan step, returns a summary dict. Never raises — analysis
    must not be able to break the reply path."""
    try:
        transcript = _transcript(user_id)
        if not transcript.strip():
            return None
        system = _build_system(user_id)
        messages = [{"role": "user",
                     "content": "## Conversation (oldest first)\n\n"
                                + transcript}]
        if client is None:
            client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=1200, system=system,
            messages=messages, tools=[_TOOL],
            tool_choice={"type": "tool", "name": "submit_analysis"})
        payload = next((b.input for b in resp.content
                        if getattr(b, "type", "") == "tool_use"), None)
        llm_call_id = db.save_llm_call(
            user_id, f"analysis_{trigger}", MODEL, system, messages,
            prompt_versions={},
            response_text=json.dumps(payload, ensure_ascii=False)
            if payload else "")
        if not payload:
            print(f"[ANALYZE] no tool call for {user_id}", flush=True)
            return None

        applied = _apply(user_id, payload, llm_call_id)
        judged = _judge(user_id, payload, llm_call_id)
        db.log_event(user_id, "turn_analyzed",
                     {"trigger": trigger, "applied": applied,
                      "step_completed": payload.get("step_completed"),
                      "operator_note": (payload.get("notes_for_operator")
                                        or "")[:200],
                      "llm_call_id": llm_call_id}, source="analyze")
        if applied:
            print(f"[ANALYZE] {user_id}: filled {applied}", flush=True)
        return {"applied": applied, "judged": judged,
                "llm_call_id": llm_call_id}
    except Exception as e:
        print(f"[ANALYZE] ⚠️ failed for {user_id}: {e}", flush=True)
        return None


def _apply(user_id, p, llm_call_id):
    """Write validated extractions. Returns the list of fields
    actually written (unchanged values are skipped, so re-runs over
    history are idempotent)."""
    import sms   # marker-format validators live with the send path

    applied = []
    phase = db.get_user_phase(user_id)
    prof = db.get_user_profile_by_id(user_id) or {}

    goal = (p.get("goal") or "").strip()
    if goal and goal != (phase["agreed_goal"] or "").strip():
        db.set_agreed_goal(user_id, goal, source="analyze")
        applied.append("goal")

    bite = (p.get("first_bite") or "").strip()
    if bite and bite != (phase["agreed_first_bite"] or "").strip():
        db.set_agreed_bite(user_id, bite, source="analyze")
        applied.append("bite")

    marker = (p.get("ignition_marker") or "").strip()
    if marker and marker != (phase["ignition_marker"] or "").strip():
        basis = (p.get("ignition_marker_basis") or "inferred").strip()
        conf = (p.get("ignition_marker_confidence") or "medium").strip()
        # A low-confidence inference is a guess; leave the field empty
        # so the conversation keeps steering toward it. A STATED
        # marker always lands regardless of the confidence field.
        if basis == "inferred" and conf == "low":
            print(f"[ANALYZE] low-confidence ignition inference — "
                  f"not saved: {marker!r}", flush=True)
        else:
            status = "confirmed" if basis == "stated" else "provisional"
            db.set_ignition_marker(user_id, marker, source="analyze",
                                   status=status, basis=basis,
                                   confidence=conf)
            applied.append(f"ignition_marker({status})")

    status = (p.get("material_status") or "").strip()
    if status:
        prof_status = (prof.get("material_status") or "").strip()
        # A registered material outranks a conversational
        # "no_material" reading (uploads are facts; readings drift).
        if status == "no_material" and db.get_user_materials(user_id):
            print("[ANALYZE] no_material reading ignored — a material "
                  "is registered", flush=True)
        elif status != prof_status:
            db.set_material_status(user_id, status, source="analyze")
            applied.append(f"material_status({status})")

    named = p.get("material_named") or {}
    if (named.get("title") or "").strip() \
            and not db.get_user_materials(user_id):
        mid = db.add_user_material(
            user_id, "named",
            title=named["title"].strip()[:200], source="analyze")
        if (named.get("description") or "").strip():
            db.update_material_walkthrough(
                mid, user_description=named["description"].strip(),
                status="in_progress", source="analyze")
        applied.append("material_named")

    mats = db.get_user_materials(user_id)
    if mats:
        m = mats[0]
        desc = (p.get("material_description") or "").strip()
        wants = p.get("material_wants") or None
        if wants is not None:
            wants = [{"quote": (w.get("quote") or "").strip(),
                      "meaning": (w.get("meaning") or "").strip()}
                     for w in wants if (w.get("quote") or "").strip()]
            verified, rejected = [], []
            for w in wants:
                (verified if _user_said(user_id, w["quote"])
                 else rejected).append(w)
            if rejected:
                print(f"[ANALYZE] ⚠️ dropped {len(rejected)} want "
                      f"quote(s) not found in user messages",
                      flush=True)
                db.log_event(user_id, "want_quote_rejected",
                             {"material_id": m["id"],
                              "quotes": [w["quote"][:120]
                                         for w in rejected],
                              "llm_call_id": llm_call_id},
                             source="analyze")
            wants = verified
        validated = bool(p.get("walkthrough_sample_validated"))
        status = None
        if validated and m["walkthrough_status"] != "validated":
            status = "validated"
        elif (desc or wants) and m["walkthrough_status"] == "none":
            status = "in_progress"
        changed = ((desc and desc != (m.get("user_description") or ""))
                   or (wants is not None and wants != (m.get("wants") or []))
                   or status is not None)
        if changed:
            db.update_material_walkthrough(
                m["id"],
                user_description=desc or None,
                wants=wants,
                status=status,
                source="analyze")
            applied.append("walkthrough"
                           + (f"({status})" if status else ""))
            if validated and (p.get("walkthrough_sample_evidence")
                              or "").strip():
                db.log_event(user_id, "walkthrough_validated",
                             {"material_id": m["id"],
                              "evidence": p["walkthrough_sample_evidence"],
                              "llm_call_id": llm_call_id},
                             source="analyze")

    offer = (p.get("offer") or "").strip()
    if offer and offer != (prof.get("agreed_offer") or "").strip():
        db.set_agreed_offer(user_id, offer, source="analyze")
        applied.append("offer")

    direction = (p.get("path_direction") or "").strip()
    if direction:
        cur = db.get_current_path(user_id) or {}
        project = (p.get("path_project") or cur.get("project") or "").strip()
        done = (p.get("path_done_condition")
                or cur.get("project_done_condition") or "").strip()
        if (direction, project, done) != (cur.get("direction"),
                                          cur.get("project"),
                                          cur.get("project_done_condition")):
            db.save_learning_path(user_id, direction, project, done,
                                  source="analyze")
            applied.append("path")

    raw_sched = (p.get("schedule") or "").strip()
    if raw_sched:
        cur_sched = db.get_user_schedule(user_id) or {}
        if raw_sched != (cur_sched.get("raw_text") or "").strip():
            windows = sms.parse_schedule_windows(raw_sched)
            if windows:
                db.save_user_schedule(user_id, windows,
                                      raw_text=raw_sched, source="analyze")
                applied.append("schedule")
            else:
                print(f"[ANALYZE] ⚠️ unparseable schedule {raw_sched!r} — "
                      f"not saved", flush=True)

    if applied and db.check_and_complete_onboarding(user_id):
        import genplan
        genplan.generate_async(user_id)
        applied.append("(onboarding completed → plan generation started)")
    return applied


def _judge(user_id, p, llm_call_id):
    """Move the plan cursor on a confident completion verdict."""
    verdict = p.get("step_completed")
    if verdict not in ("yes", "no", "uncertain"):
        return None
    plan = db.get_current_plan(user_id)
    if not plan or plan["cursor"] >= len(plan["steps"]):
        return None
    step = plan["steps"][plan["cursor"]]
    db.log_event(user_id, "step_judged",
                 {"step_index": plan["cursor"], "tag": step["tag"],
                  "completed": verdict,
                  "reason": (p.get("step_reason") or "")[:300],
                  "llm_call_id": llm_call_id}, source="analyze")
    if verdict != "yes":
        return verdict
    new_idx = plan["cursor"] + 1
    db.move_plan_cursor(user_id, new_idx,
                        reason=f"analysis: {(p.get('step_reason') or '')[:120]}",
                        source="analyze")
    if new_idx >= len(plan["steps"]):
        db.log_event(user_id, "plan_completed",
                     {"version": plan["version"]}, source="analyze")
        print(f"[ANALYZE] {user_id}: plan v{plan['version']} complete",
              flush=True)
    return verdict


def analyze_history(user_id, client=None):
    """Back-extract from a conversation that predates the analysis
    call (or that lost fields to dropped markers). Same pass, run on
    demand — extraction is idempotent, so this is safe to repeat."""
    return analyze(user_id, trigger="history", client=client)
