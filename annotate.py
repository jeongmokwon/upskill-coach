"""
LearnerState v1 + nightly annotation job — WEEK1_ORDER T5, brief §4.2.

Reads one learner's events for one day, asks the model to distill
them into a small structured state snapshot, and appends the result
to learner_state_snapshots. Everything is versioned three ways —
schema_version (the shape of the state object), prompt_version (the
T2 content-hash of the annotation template), model — so historical
days can be RE-annotated later under better definitions and the old
rows survive untouched: append-only, a new row per run, never an
update. The founder reads snapshots each morning; later they become
training signal.

Outcome definitions inside the prompt follow the D4 *draft* of
outcome_v1 — expected to be wrong, revised against raw data later;
that's exactly why prompt_version is stamped on every row.
"""

import json
import os
import re
from datetime import datetime, timedelta

import db

SCHEMA_VERSION = 1
MODEL = os.environ.get("ANNOTATE_MODEL", "claude-sonnet-4-5")

# Day boundaries follow the codebase's PT convention (see
# get_today_sessions_for_user): event ts are naive server-local
# (UTC on Render), and PT is approximated with a fixed offset.
TZ_OFFSET_H = int(os.environ.get("TZ_OFFSET_HOURS", "-8"))

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "prompts")

_STATE_KEYS = ("phase", "momentum", "last_ignition_at",
               "friction_signals", "ego_friction_events",
               "channel_state", "outcome_v1_events")


class _SafeDict(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def _day_range(day):
    """'YYYY-MM-DD' (a PT day) → (start_iso, end_iso) in the naive
    server-local timeline events are stored in."""
    start_pt = datetime.fromisoformat(day)
    start_server = start_pt - timedelta(hours=TZ_OFFSET_H)
    return start_server.isoformat(), (start_server + timedelta(days=1)).isoformat()


def _default_day():
    """Yesterday in PT — what 'nightly' means when no day is given."""
    now_pt = datetime.now() + timedelta(hours=TZ_OFFSET_H)
    return (now_pt - timedelta(days=1)).date().isoformat()


def _digest(events):
    """Render events as compact one-per-line evidence with ids."""
    lines = []
    for e in events:
        payload = e["payload"]
        if isinstance(payload, str) and len(payload) > 300:
            payload = payload[:300] + "…"
        lines.append(f"[{e['id']}] {e['ts']} {e['kind']} "
                     f"({e['source']}) {payload}")
    return "\n".join(lines)


def _parse_state(raw):
    """Model output → validated state dict. Tolerates code fences and
    leading prose; raises ValueError when no usable JSON exists."""
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in model output")
    state = json.loads(m.group(0))
    missing = [k for k in _STATE_KEYS if k not in state]
    if missing:
        raise ValueError(f"state missing keys: {missing}")
    return state


def _evidence_ids(state):
    ids = set()
    for key in ("friction_signals", "ego_friction_events",
                "outcome_v1_events"):
        for item in state.get(key) or []:
            for eid in (item.get("event_ids") or []):
                ids.add(eid)
    return sorted(ids)


def annotate_day(user_id, day=None, client=None):
    """Annotate one user's PT day. Returns the stored state dict, or
    None if the user had no events that day. `client` injectable for
    tests; defaults to a real Anthropic client."""
    day = day or _default_day()
    start, end = _day_range(day)
    events = db.get_events_with_ids(user_id, start, end)
    if not events:
        print(f"[T5] {user_id} {day}: no events — skipping", flush=True)
        return None

    with open(os.path.join(_PROMPTS_DIR, "learner_state.md"),
              encoding="utf-8") as f:
        template = f.read()
    prompt_version = db.register_prompt_version("learner_state", template)

    phase_state = db.get_user_phase(user_id)
    prompt = template.format_map(_SafeDict(
        user_id=user_id,
        day=day,
        phase=phase_state.get("phase") or "(unknown)",
        agreed_goal=phase_state.get("agreed_goal") or "(not yet agreed)",
        agreed_first_bite=phase_state.get("agreed_first_bite") or "(none)",
        ignition_marker=phase_state.get("ignition_marker")
                        or "(not yet defined — use the generic fallback)",
        events_digest=_digest(events),
    ))

    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    messages = [{"role": "user", "content": "Annotate this day."}]
    resp = client.messages.create(
        model=MODEL, max_tokens=2000,
        system=prompt, messages=messages,
    )
    raw = resp.content[0].text

    # Flight-record the call (T2b) BEFORE parsing — a malformed
    # response is precisely the case where the raw record matters.
    llm_call_id = db.save_llm_call(
        user_id, f"annotate_{day}", MODEL, prompt, messages,
        prompt_versions={"learner_state": prompt_version},
        response_text=raw)

    state = _parse_state(raw)
    evidence = _evidence_ids(state)
    db.save_learner_state_snapshot(
        user_id, day, SCHEMA_VERSION, prompt_version, MODEL,
        state, evidence, llm_call_id)
    db.log_event(user_id, "learner_state_annotated", {
        "day": day,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": prompt_version,
        "evidence_event_count": len(evidence),
    }, source="annotate")
    return state


# ─── P6 nightly loop (P0-D part 2) ───────────────────────────────

REPLY_WINDOW_H = 12   # a reply within this long counts as "reply"


def score_expectations(user_id, day=None):
    """Mechanically score yesterday's [EXPECT:] bets against what the
    log shows. v1 honesty: the mechanical layer can only distinguish
    reply vs no_reply — an expect of reply/advance/withdraw/ignition
    scores as hit iff ANY reply arrived in the window; semantic
    grading of which KIND of reply stays with the human/LLM layers.
    Emits one expect_scored event per bet, dedup-safe on re-runs."""
    day = day or _default_day()
    start, end = _day_range(day)
    end_ext = (datetime.fromisoformat(end)
               + timedelta(hours=REPLY_WINDOW_H)).isoformat()
    events = db.get_events_with_ids(user_id, start, end_ext)
    ins = [e["ts"] for e in events if e["kind"] == "sms_in"]
    scored = 0
    for e in events:
        if e["kind"] != "sms_out" or e["ts"] >= end:
            continue
        try:
            p = json.loads(e["payload"])
        except Exception:
            continue
        expect = p.get("expect")
        if not expect:
            continue
        if db.get_last_event(user_id, "expect_scored",
                             payload_contains=f'"sms_out_event_id": {e["id"]}'):
            continue   # already scored on a previous run
        out_ts = datetime.fromisoformat(e["ts"])
        deadline = (out_ts + timedelta(hours=REPLY_WINDOW_H)).isoformat()
        replied = any(e["ts"] < t <= deadline for t in ins)
        actual = "reply" if replied else "no_reply"
        hit = (expect == "no_reply") == (actual == "no_reply")
        db.log_event(user_id, "expect_scored",
                     {"sms_out_event_id": e["id"], "expect": expect,
                      "actual_mechanical": actual, "hit": hit},
                     source="annotate")
        scored += 1
    if scored:
        print(f"[P6] {user_id} {day}: scored {scored} expect bets",
              flush=True)
    return scored


_PROPOSAL_TOOL = {
    "name": "propose_note_updates",
    "description": "Propose user-note updates based on the day's "
                   "evidence. Empty list is a fine answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "proposals": {
                "type": "array",
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string",
                                   "enum": ["new", "confirm", "retire"]},
                        "note_id": {"type": "string"},
                        "claim": {"type": "string"},
                        "when": {"type": "array",
                                 "items": {"type": "string"}},
                        "expect": {"type": "string"},
                        "evidence_event_ids": {"type": "array",
                                               "items": {"type": "integer"}},
                        "reason": {"type": "string"},
                    },
                    "required": ["action", "reason"],
                },
            },
        },
        "required": ["proposals"],
    },
}


def propose_notes(user_id, day=None, client=None):
    """Nightly note-update proposals (P0-D part 2). The LLM reads the
    day's trace against the user's current notes and proposes
    new/confirm/retire actions — PROPOSALS ONLY, logged as a
    note_proposals event for the operator to apply via POST /notes.
    Nothing is written to user_notes here (manual policy engine)."""
    import sms
    import trace as trace_mod

    day = day or _default_day()
    notes = db.get_user_notes(user_id)
    notes_view = "\n".join(
        f"- [{n['note_id']}] ({n['confidence']}) {n['claim']}\n"
        f"    when={n['when_json']} expect={n['expect']}"
        for n in notes) or "(no notes yet)"
    system = (
        "You maintain the falsifiable user notes of an AI learning "
        "coach. Read today's trajectory against the current notes and "
        "propose updates via the propose_note_updates tool (you MUST "
        "call it): 'confirm' a hypothesis the day's evidence supports, "
        "'retire' one it contradicts, 'new' for a pattern with clear "
        "evidence (cite event ids from the trace). An empty proposals "
        "list is the RIGHT answer for an uneventful day — never invent. "
        "These are proposals; a human applies them.\n\n"
        f"## Current notes\n\n{notes_view}")
    messages = [{"role": "user", "content":
                 f"## Today's trajectory ({day})\n\n"
                 + trace_mod.render_trace(user_id, days=1, verbose=True)}]
    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=1500, system=system,
            messages=messages, tools=[_PROPOSAL_TOOL],
            tool_choice={"type": "tool", "name": "propose_note_updates"})
        tool_input = next((b.input for b in resp.content
                           if getattr(b, "type", "") == "tool_use"), None)
    except Exception as e:
        print(f"[P6] ⚠️ note proposals failed for {user_id}: {e}",
              flush=True)
        return None
    llm_call_id = db.save_llm_call(
        user_id, f"note_proposals_{day}", MODEL, system, messages,
        prompt_versions={},
        response_text=json.dumps(tool_input, ensure_ascii=False)
        if tool_input else "")
    proposals = (tool_input or {}).get("proposals", [])
    known_ids = {n["note_id"] for n in notes}
    valid = []
    for p in proposals:
        if p["action"] in ("confirm", "retire") \
                and p.get("note_id") not in known_ids:
            print(f"[P6] dropping proposal for unknown note "
                  f"{p.get('note_id')!r}", flush=True)
            continue
        bad_tag = any(t.split("@")[0] not in sms.STEP_VOCABULARY
                      for t in (p.get("when") or []))
        if p["action"] == "new" and bad_tag:
            print(f"[P6] dropping 'new' proposal with unknown tag",
                  flush=True)
            continue
        valid.append(p)
    if valid:
        db.log_event(user_id, "note_proposals",
                     {"day": day, "proposals": valid,
                      "llm_call_id": llm_call_id}, source="annotate")
        print(f"[P6] {user_id} {day}: {len(valid)} note proposals — "
              f"apply via POST /notes", flush=True)
    return valid


def _pending_replan_reasons(user_id):
    """Replan requests newer than both the active plan and the last
    proposal → the reasons list to re-plan against ([] = none)."""
    last_req = db.get_last_event(user_id, "plan_replan_requested")
    if not last_req:
        return []
    plan = db.get_current_plan(user_id)
    last_prop = db.get_last_event(user_id, "plan_proposed")
    for blocker_ts in [(plan or {}).get("ts"),
                       (last_prop or {}).get("ts")]:
        if blocker_ts and last_req["ts"] <= blocker_ts:
            return []
    try:
        return [json.loads(last_req["payload"]).get("reason", "")]
    except Exception:
        return ["(unparseable reason)"]


def annotate_all(day=None, client=None):
    """Annotate every user active on `day` (default: yesterday PT).
    Per-user failures are recorded and don't stop the sweep.
    Returns {user_id: "ok" | "skipped" | "error: ..."}."""
    day = day or _default_day()
    start, end = _day_range(day)
    results = {}
    for user_id in db.get_active_user_ids(start, end):
        try:
            state = annotate_day(user_id, day, client=client)
            results[user_id] = "ok" if state else "skipped"
        except Exception as e:
            print(f"[T5] ⚠️ annotate {user_id} {day} failed: {e}",
                  flush=True)
            db.log_event(user_id, "annotation_failed",
                         {"day": day, "error": str(e)[:300]},
                         source="annotate")
            results[user_id] = f"error: {str(e)[:120]}"
        # P6 nightly loop — each stage independent; a failure in one
        # never blocks the others (watchdog discipline).
        try:
            score_expectations(user_id, day)
        except Exception as e:
            print(f"[P6] ⚠️ expect scoring failed {user_id}: {e}",
                  flush=True)
        try:
            if db.get_onboarding_state(user_id)["completed_at"]:
                propose_notes(user_id, day, client=client)
        except Exception as e:
            print(f"[P6] ⚠️ note proposals failed {user_id}: {e}",
                  flush=True)
        try:
            reasons = _pending_replan_reasons(user_id)
            if reasons:
                import genplan
                genplan.propose_replan(user_id, reasons, client=client)
        except Exception as e:
            print(f"[P6] ⚠️ replan proposal failed {user_id}: {e}",
                  flush=True)
    return results
