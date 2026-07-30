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
                "description": "The concrete small task agreed for "
                               "the next day or two.",
            },
            "ignition_marker": {
                "type": "string",
                "description": "THEIR observable definition of 'it "
                               "started' — something a screenshot or "
                               "a message could verify.",
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
    ]

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

## Hard rule: report only what is IN the conversation

Fill a field only when the user actually said it or explicitly
agreed to it. Never infer, never complete a half-agreement, never
carry over your own assumptions. If the user has not settled
something, OMIT that field entirely — omission costs one more turn
of conversation; a wrong fill corrupts their record. Re-report a
field only if the conversation has REFINED it beyond what is
already known below (identical restatements: omit).

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
  coach merely proposed it."""


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
        db.set_ignition_marker(user_id, marker, source="analyze")
        applied.append("ignition_marker")

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
