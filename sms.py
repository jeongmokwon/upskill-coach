"""
SMS tutor — Twilio + Claude glue.

Lives alongside the web tutor in coach.py. Same DB, same Claude, just
a different channel. Single-user MVP: one phone number maps to one
user_id via env vars.

Public entry points (called from coach.py route handlers):

    handle_inbound(from_number, body) -> reply text or None
        Called by /sms/inbound webhook. Returns the text to reply with
        (already sent — return value is for logging/debugging).

    handle_cron_tick(slot) -> reply text or None
        Called by /sms/cron-tick at scheduled times. Builds prompt for
        the slot, calls Claude, sends via Twilio.

Slot prompts live in prompts/sms_*.md and are re-read on every call —
edit + push to deploy a new prompt, no restart needed.

Env vars expected (all set in Render dashboard):

    ANTHROPIC_API_KEY        — already used by coach.py
    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_FROM_NUMBER       — the number Twilio gave us, E.164
    TUTOR_USER_PHONE         — the user's phone, E.164
    TUTOR_USER_ID            — user_id in our DB to map SMS thread to
    CRON_SECRET              — shared secret for /sms/cron-tick auth
"""

import os
import re
import time
import json
from datetime import datetime, timedelta, timezone

import anthropic

import db
import policy

# ─── Config ──────────────────────────────────────────────────────────

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

SLOTS = ("morning", "lunch", "afternoon", "evening")

# User-local time is approximated with a fixed offset from server
# time (same convention as features.py/trace.py/annotate.py — no
# zoneinfo; DST drift accepted for the pilot).
TZ_OFFSET_H = int(os.environ.get("TZ_OFFSET_HOURS", "-8"))

# Max one Anthropic Sonnet call per slot. Cheap enough we just always
# use the same model as the web tutor for now — consistency beats
# pennies of savings.
MODEL = "claude-sonnet-4-5"

# How much SMS history to feed back into Claude as conversation.
# Was 20, sized for the original "4 nudges a day" design — but real
# evening study sessions run dozens of messages in one sitting, and
# the early part of the night (agreements, good explanations) was
# scrolling out of the window mid-conversation. 50 covers a long
# evening plus carryover. Messages are short; token cost is minor.
# If sessions outgrow this too, the real fix is summary-compression
# (compact synthesis of older turns + raw recent N), not a bigger N.
HISTORY_LIMIT = 50

# A reply of exactly one of these (case-insensitive, strip
# punctuation) is treated as a meta-command, not conversation.
SKIP_TOKENS = {"skip", "stop", "pause", "mute"}
LATER_TOKENS = {"later", "tonight", "9pm", "evening"}


# ─── Twilio (lazy import to keep coach.py boot working without it) ──

_twilio_client = None


def _twilio():
    """Lazy Twilio REST client. None if env vars missing."""
    global _twilio_client
    if _twilio_client is not None:
        return _twilio_client
    sid = os.environ.get("TWILIO_ACCOUNT_SID")
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (sid and token):
        return None
    try:
        from twilio.rest import Client
    except ImportError:
        print("[SMS] twilio package not installed — pip install twilio", flush=True)
        return None
    _twilio_client = Client(sid, token)
    return _twilio_client


def _channel_prefix():
    """Return Twilio API prefix for the active messaging channel.

    Twilio uses the same Messages API for SMS and WhatsApp, but
    WhatsApp endpoints are addressed as 'whatsapp:+15551234567'.
    SMS endpoints are bare 'E.164'. Set MESSAGING_CHANNEL=whatsapp
    in env to flip the whole pipeline to WhatsApp without touching
    code — useful while A2P 10DLC / Toll-Free verification is
    pending and we want to keep iterating via WhatsApp Sandbox.
    """
    return "whatsapp:" if os.environ.get("MESSAGING_CHANNEL", "sms").lower() == "whatsapp" else ""


def _addr(phone):
    """Render `phone` (raw E.164) as the right Twilio address for the
    active channel. Idempotent: leaves already-prefixed addresses
    alone, so it's safe to call twice or to pass values that already
    have the prefix in env."""
    if not phone:
        return phone
    if phone.startswith("whatsapp:") or phone.startswith("sms:"):
        return phone
    return f"{_channel_prefix()}{phone}"


def send_sms(to_number, body, user_id=None):
    """Send `body` to `to_number` (E.164). Returns Twilio SID or None.
    `user_id` is used only for event attribution on failures.

    Splits on lines containing only '---' so the LLM can emit two
    "SMS bubbles" by separating them, and we send each as a real
    distinct SMS with a small gap. If body has no separator it sends
    as one message.

    Despite the name, this also handles WhatsApp when
    MESSAGING_CHANNEL=whatsapp — the Twilio Messages API is the same
    for both, only the address format differs.
    """
    client = _twilio()
    from_number = os.environ.get("TWILIO_FROM_NUMBER")
    if not (client and from_number):
        print(f"[SMS] skipping send — Twilio not configured. Would have sent: {body[:80]}...", flush=True)
        return None

    # Split on a line that is exactly '---' (with optional surrounding
    # whitespace). Leaves '---' inside code/text alone.
    parts = re.split(r"\n\s*---\s*\n", body.strip())
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        return None

    from_addr = _addr(from_number)
    to_addr = _addr(to_number)

    last_sid = None
    for i, part in enumerate(parts):
        try:
            msg = client.messages.create(
                from_=from_addr,
                to=to_addr,
                body=part,
            )
            last_sid = msg.sid
            print(f"[SMS] sent ({len(part)} chars) sid={msg.sid}", flush=True)
        except Exception as e:
            print(f"[SMS] ❌ send failed: {e}", flush=True)
            # Infra failures are data (brief §4.1) — the Twilio outage
            # was a pivotal natural experiment that survived only by
            # memory. Never let a send failure go unrecorded.
            db.log_event(user_id, "sms_send_failed",
                         {"error": str(e)[:300], "part_index": i,
                          "to": to_addr}, source="sms")
            break
        # Gap between messages so they arrive in order on the user's
        # device. Twilio doesn't guarantee order across back-to-back
        # API calls. WhatsApp Sandbox additionally rate-limits to one
        # message every 3 seconds — slower gap there avoids throttling
        # on the second bubble.
        if i < len(parts) - 1:
            time.sleep(3.5 if _channel_prefix() else 1.0)
    return last_sid


def verify_twilio_signature(url, params, signature):
    """Verify a Twilio webhook signature.

    Returns True if valid, False otherwise. If the auth token is
    missing we fail closed (return False) — better to reject than to
    silently accept unsigned traffic.
    """
    token = os.environ.get("TWILIO_AUTH_TOKEN")
    if not (token and signature):
        return False
    try:
        from twilio.request_validator import RequestValidator
    except ImportError:
        return False
    validator = RequestValidator(token)
    return validator.validate(url, params, signature)


# ─── Prompt loading (re-read on every call, no cache) ───────────────

def _read_prompt(name):
    """Read prompts/{name}.md — fresh from disk every call.

    No caching is intentional: the user edits prompts in their editor,
    git pushes, Render redeploys, and the NEXT slot picks up the new
    prompt. If they want to A/B mid-day they can ship a small change
    and the next slot fires the new version.
    """
    path = os.path.join(PROMPTS_DIR, f"{name}.md")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _read_prompt_versioned(name):
    """Read a prompt template AND register its version (T2).
    Returns (content, hash). The hash identifies the TEMPLATE text
    (pre-placeholder-rendering) — that's the stable identity; the
    rendered prompt differs on every call because context differs."""
    content = _read_prompt(name)
    return content, db.register_prompt_version(name, content)


# ─── Context builder ────────────────────────────────────────────────

def _format_recent_insights(user_id):
    """Pull last N insights, format as a short bulleted block for the
    prompt. Empty string if none — the prompt template handles that
    gracefully ("if recent_insights is sparse").
    """
    rows = []
    # get_recent_insights() uses thread-local user_id; for SMS we run
    # off-request and need to set it explicitly.
    db.set_thread_user(user_id)
    try:
        rows = db.get_recent_insights(limit=3)
    except Exception as e:
        print(f"[SMS] failed to load insights: {e}", flush=True)
    if not rows:
        return "(no recent insights — first few SMS sessions, or fresh user)"

    lines = []
    for r in rows:
        analysis = r.get("analysis")
        if not analysis:
            continue
        # `analysis` is a JSON string (or already-parsed dict
        # depending on DB driver). Normalize.
        if isinstance(analysis, str):
            try:
                analysis = json.loads(analysis)
            except Exception:
                lines.append(f"- {analysis[:200]}")
                continue
        # Best-effort pull of the human-readable bits the analyzer
        # writes. Schema can drift — be defensive.
        summary = (
            analysis.get("summary")
            or analysis.get("pedagogy_notes")
            or analysis.get("weak_concepts")
            or analysis
        )
        if isinstance(summary, (dict, list)):
            summary = json.dumps(summary)[:200]
        lines.append(f"- {str(summary)[:200]}")
    return "\n".join(lines) if lines else "(insights present but unreadable)"


def _request_fresh_screen(user_id, wait_s=8.0):
    """If a screen-observe session is live, ask the local agent for
    an immediate capture and wait briefly so the reply is built
    against the user's CURRENT screen instead of one up to 60s stale.

    No-ops instantly when no agent session is open (zero latency
    added to normal conversations), and skips the wait when the
    latest observation is already fresh (<15s).
    Runs in the inbound executor thread, so blocking sleep is fine.
    """
    try:
        if not db.get_open_observe_session(user_id):
            return
        rows = db.get_recent_observations(user_id, minutes=2, limit=1)
        last_ts = rows[-1]["ts"] if rows else ""
        if last_ts:
            from datetime import datetime as _dt
            try:
                age = (_dt.now() - _dt.fromisoformat(last_ts)).total_seconds()
                if age < 15:
                    return  # already fresh enough
            except Exception:
                pass

        import observe as observe_mod
        # Decision point (T3): whether to block the reply on a fresh
        # capture. Deterministic today; later variants could sample
        # wait length or skip probability.
        choice, decision_id = policy.decide(
            "fresh_screen_wait", user_id,
            options=["wait_for_fresh", "proceed_stale"],
            context={"last_capture_ts": last_ts})
        if choice != "wait_for_fresh":
            return
        observe_mod.request_capture(user_id)
        db.log_event(user_id, "fresh_capture_requested",
                     {"decision_id": decision_id}, source="sms")
        t0 = time.time()
        while time.time() - t0 < wait_s:
            time.sleep(0.5)
            new = db.get_recent_observations(user_id, minutes=2, limit=1)
            if new and new[-1]["ts"] > last_ts:
                print(f"[SMS] fresh screen capture landed in {time.time()-t0:.1f}s", flush=True)
                db.log_event(user_id, "fresh_capture_landed",
                             {"elapsed_s": round(time.time() - t0, 1)}, source="sms")
                return
        print(f"[SMS] fresh capture didn't land within {wait_s}s — replying with what we have", flush=True)
        db.log_event(user_id, "fresh_capture_timeout",
                     {"waited_s": wait_s}, source="sms")
    except Exception as e:
        print(f"[SMS] fresh-screen request failed (non-fatal): {e}", flush=True)


def _format_recent_screen(user_id):
    """Recent screen observations (last 30 min) from the local
    observer agent, formatted for the prompt. Empty-state string
    when no agent is running — the prompt tells the LLM to simply
    not reference the screen in that case."""
    try:
        rows = db.get_recent_observations(user_id, minutes=30, limit=5)
    except Exception as e:
        print(f"[SMS] failed to load observations: {e}", flush=True)
        rows = []
    if not rows:
        return "(no live screen session right now)"
    lines = []
    for r in rows:
        hhmm = (r.get("ts") or "")[11:16]
        lines.append(f"- [{hhmm}] {r['summary']}")
    return "\n".join(lines)


def _format_today_sessions(user_id):
    try:
        rows = db.get_today_sessions_for_user(user_id)
    except Exception as e:
        print(f"[SMS] failed to load today's sessions: {e}", flush=True)
        rows = []
    if not rows:
        return "(no web sessions today)"
    lines = []
    for r in rows:
        topic = r.get("study_topic") or "(no topic recorded)"
        start = r.get("start_time", "")[:16]  # YYYY-MM-DD HH:MM
        end = r.get("end_time")
        duration = ""
        if end:
            try:
                t0 = datetime.fromisoformat(r["start_time"])
                t1 = datetime.fromisoformat(end)
                mins = round((t1 - t0).total_seconds() / 60)
                duration = f", ~{mins}min"
            except Exception:
                pass
        lines.append(f"- {start}: {topic}{duration}")
    return "\n".join(lines)


def _prompt_name_for_slot(slot, phase):
    """Which prompt file to load for a given (slot, phase) combo.

    Only two slots produce messages under the redesign:
      morning → always sms_morning (thread-keeping only)
      evening → sms_discovery in Phase 0, sms_first_bite in Phase 1
    lunch/afternoon are skipped upstream (see handle_cron_tick).
    """
    if slot == "morning":
        return "sms_morning"
    if slot == "evening":
        return "sms_first_bite" if phase == "first_bite" else "sms_discovery"
    # Unreachable in normal flow — handle_cron_tick skips lunch/afternoon.
    return None


def _build_placeholders(user_id):
    """Assemble the placeholder dict used by shared + slot prompts.

    Includes both the legacy fields (user_name/goal/studying/
    insights/today_sessions) and the phase-flow fields
    (phase/agreed_first_bite/discovery_day) that the redesigned
    prompts reference.
    """
    profile = db.get_user_profile_by_id(user_id) or {}
    phase_state = db.get_user_phase(user_id)
    return {
        "user_name": profile.get("user_name") or "you",
        "goal": profile.get("goal") or "(not set)",
        "studying": profile.get("studying") or "(not set)",
        "recent_insights": _format_recent_insights(user_id),
        "today_sessions": _format_today_sessions(user_id),
        "phase": phase_state["phase"],
        "agreed_first_bite": phase_state["agreed_first_bite"] or "(not yet agreed)",
        "agreed_goal": phase_state["agreed_goal"] or "(not yet agreed)",
        "ignition_marker": phase_state["ignition_marker"] or "(not yet defined)",
        # 1-indexed day count for the LLM's "Day X of 3" awareness.
        "discovery_day": db.days_in_discovery(user_id) + 1,
        "recent_screen": _format_recent_screen(user_id),
    }


class _SafeDict(dict):
    """format_map helper: unknown {brace} keys pass through unchanged
    so LLM prompt bodies with JSON examples don't blow up rendering."""
    def __missing__(self, k):
        return "{" + k + "}"


def _build_context_blocks(user_id):
    """The exploration prediction call's three blocks (brief §7):
    A = policy prior, B = user notes, C = recent trajectory +
    computed features. Returns (text, versions). Never raises —
    a failed block renders as absent and the planner degrades
    gracefully (empty notes ≈ global-prompt behavior)."""
    import features as features_mod
    import notes as notes_mod
    import trace as trace_mod

    versions = {}
    parts = []
    try:
        prior, h_prior = _read_prompt_versioned("prior")
        versions["prior"] = h_prior
        parts.append(prior)
    except Exception as e:
        print(f"[SMS] ⚠️ prior block failed: {e}", flush=True)
    try:
        notes_block = notes_mod.render_notes_block(user_id)
        if notes_block:
            parts.append(notes_block)
    except Exception as e:
        print(f"[SMS] ⚠️ notes block failed: {e}", flush=True)
    try:
        feats = features_mod.render_features(
            features_mod.compute_features(user_id))
        trace_block = trace_mod.render_trace(user_id, days=3)
        parts.append("## Recent trajectory (step-language; you are "
                     "choosing the NEXT token)\n\n"
                     f"Current state: {feats}\n\n{trace_block}")
    except Exception as e:
        print(f"[SMS] ⚠️ trace block failed: {e}", flush=True)
    return "\n\n---\n\n".join(parts), versions


def _build_system_prompt(slot, user_id):
    """Assemble the full planner prompt for a scheduled slot:
    prior (A) + shared persona/rules + notes (B) + trajectory (C) +
    slot-specific mode prompt.

    Returns (system_prompt, prompt_versions) — versions is a dict
    {template_name: hash} identifying the exact template texts used
    (T2). Returns (None, {}) if the slot has no message to send
    under current state (used to no-op lunch/afternoon).
    """
    prompt_name = _prompt_name_for_slot(slot, db.get_user_phase(user_id)["phase"])
    if prompt_name is None:
        return None, {}
    shared, h_shared = _read_prompt_versioned("sms_shared")
    slot_prompt, h_slot = _read_prompt_versioned(prompt_name)
    fields = _build_placeholders(user_id)
    rendered_shared = shared.format_map(_SafeDict(**fields))
    rendered_slot = slot_prompt.format_map(_SafeDict(**fields))
    context, ctx_versions = _build_context_blocks(user_id)
    versions = {"sms_shared": h_shared, prompt_name: h_slot,
                **ctx_versions}
    parts = [context, rendered_shared, rendered_slot]
    # Last block wins on recency. Precedence: dormancy gate >
    # onboarding checklist > sequence assignment. Dormancy overrides
    # everything; an incomplete onboarding shows the checklist (there
    # is no plan yet during onboarding); a completed user gets the
    # plan assignment.
    if _is_dormant(user_id):
        parts.append(_build_dormant_block(user_id))
    else:
        ob_block = _build_onboarding_block(user_id)
        if ob_block:
            parts.append(ob_block)
        else:
            plan_block = _build_plan_block(user_id)
            if plan_block:
                parts.append(plan_block)
    return "\n\n---\n\n".join(parts), versions


# ─── Commit-marker protocol (Phase 0 → Phase 1) ──────────────────────
#
# The LLM signals a phase transition by embedding [COMMIT: "..."]
# anywhere in its response. We parse it, save the bite, transition
# state, and strip the marker before sending to the user.

_COMMIT_MARKER_RE = re.compile(
    r'\[COMMIT:\s*"([^"]{3,400})"\s*\]',
    re.DOTALL,
)

# [GOAL: "..."] — persists the agreed goal chain. Unlike COMMIT it
# does not transition phase and may fire in any phase, any number of
# times (later agreements refine earlier ones).
_GOAL_MARKER_RE = re.compile(
    r'\[GOAL:\s*"([^"]{3,600})"\s*\]',
    re.DOTALL,
)

# [IGNITION_DEF: "..."] — the user's OWN observable definition of
# "it started", agreed during discovery ("랩탑 앞에 앉아 IDE/Colab에
# 코드 타이핑"). Per-user ground truth for ignition judgments.
# [IGNITION: n] — a 1-5 real-time judgment, emitted when replying to
# a user message: does this reply (plus screen context) indicate the
# user's ignition marker is being met right now? Cheap early signal;
# the authoritative daily call stays with the nightly annotation
# (T5), which also sees the silences — ignition usually happens
# AFTER the last reply, when the user goes quiet because they are
# working.
_IGNITION_DEF_RE = re.compile(
    r'\[IGNITION_DEF:\s*"([^"]{3,400})"\s*\]',
    re.DOTALL,
)
_IGNITION_SCORE_RE = re.compile(r'\[IGNITION:\s*([1-5])\s*\]')


def _process_ignition_markers(user_id, text, trigger):
    """Parse & act on [IGNITION_DEF:] and [IGNITION: n]; return text
    with both stripped."""
    def_match = _IGNITION_DEF_RE.search(text)
    if def_match:
        db.set_ignition_marker(user_id, def_match.group(1).strip())
        text = _IGNITION_DEF_RE.sub("", text)

    score_match = _IGNITION_SCORE_RE.search(text)
    if score_match:
        score = int(score_match.group(1))
        marker = db.get_user_phase(user_id)["ignition_marker"]
        if marker:
            db.log_event(user_id, "ignition_judgment",
                         {"score": score, "trigger": trigger,
                          "marker": marker},
                         source="sms")
        else:
            # Prompt forbids scoring before a marker is defined, but
            # LLM compliance isn't a guarantee (observed in prod:
            # score 1 emitted on a cron send with marker ""). A score
            # with no definition is unjudgeable noise — strip the tag,
            # record nothing.
            print(f"[SMS] stray [IGNITION: {score}] with no marker "
                  f"defined — dropped", flush=True)
        text = _IGNITION_SCORE_RE.sub("", text)

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


# Onboarding markers (P0-A). Two more fill-a-field markers in the
# GOAL/COMMIT family:
#   [PATH: "direction | project | project done-condition"] — the
#     three-layer route (T8). Exactly three '|'-separated parts.
#   [SCHEDULE: "20:00-22:00, 08:00-08:30"] — agreed send windows,
#     comma-separated HH:MM-HH:MM in the user's local day. Parsed
#     mechanically (the hourly tick consumes it); an unparseable
#     value is NOT saved — logged, and the field stays missing on
#     the checklist so the conversation returns to it.
_PATH_MARKER_RE = re.compile(r'\[PATH:\s*"([^"]{3,600})"\s*\]', re.DOTALL)
_SCHEDULE_MARKER_RE = re.compile(r'\[SCHEDULE:\s*"([^"]{3,200})"\s*\]')
_WINDOW_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)$')


def _process_onboarding_markers(user_id, text):
    """Parse & act on [PATH:] and [SCHEDULE:]; strip both; then let
    the server recompute the checklist (completion may flip here)."""
    m = _PATH_MARKER_RE.search(text)
    if m:
        parts = [p.strip() for p in m.group(1).split("|")]
        if len(parts) == 3 and all(parts):
            db.save_learning_path(user_id, direction=parts[0],
                                  project=parts[1],
                                  done_condition=parts[2])
        else:
            print(f"[SMS] ⚠️ malformed [PATH:] ({len(parts)} parts) — "
                  f"not saved", flush=True)
        text = _PATH_MARKER_RE.sub("", text)

    m = _SCHEDULE_MARKER_RE.search(text)
    if m:
        raw = m.group(1).strip()
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        windows = []
        ok = bool(tokens)
        for t in tokens:
            wm = _WINDOW_RE.match(t)
            if not wm:
                ok = False
                break
            windows.append({"start": f"{int(wm.group(1)):02d}:{wm.group(2)}",
                            "end": f"{int(wm.group(3)):02d}:{wm.group(4)}"})
        if ok:
            db.save_user_schedule(user_id, windows, raw_text=raw)
        else:
            print(f"[SMS] ⚠️ malformed [SCHEDULE:] {raw!r} — not saved",
                  flush=True)
        text = _SCHEDULE_MARKER_RE.sub("", text)

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _build_onboarding_block(user_id):
    """The checklist block (P0-A): injected into every call while
    onboarding is incomplete, so the conversation keeps steering
    toward the missing fields. Empty string once completed."""
    state = db.get_onboarding_state(user_id)
    if state["completed_at"]:
        return ""
    label = {"goal": "goal (their own words — [GOAL:])",
             "path": "big-steps path ([PATH: \"direction | project | "
                     "done-condition\"])",
             "bite": "first concrete task for the next day-or-two "
                     "([COMMIT:])",
             "ignition_marker": "their observable definition of \"it "
                                "started\" ([IGNITION_DEF:])",
             "schedule": "agreed messaging windows ([SCHEDULE: "
                         "\"20:00-22:00\"], their local time)"}
    lines = [
        "## Onboarding checklist (server-computed — fill fields via "
        "markers, never announce completion yourself)",
        "",
        "Filled: " + (", ".join(state["filled"]) or "(none yet)"),
        "Missing:",
    ]
    for f in state["missing"]:
        lines.append(f"- {label[f]}")
    lines += [
        "",
        "Steer the conversation toward the missing fields naturally — "
        "no interrogation; a couple of fields per evening is fine "
        "(discovery runs up to 3 days). Fill a field ONLY when the "
        "user has actually said/agreed to it — never speculatively. "
        "The server flips onboarding to complete when the last field "
        "fills; until then this checklist reappears every call.",
    ]
    return "\n".join(lines)


# Sequence-plan markers (exploration v2). The plan lives server-side
# as state; the LLM's judgments about it travel as markers:
#   [ADVANCE]          — the user's latest reply completed the current
#                        step's purpose; move the cursor forward.
#   [REPLAN: "reason"] — the plan no longer fits the signals; recorded
#                        for the operator/nightly to re-plan. Cursor
#                        stays; the planner may act on prior+notes in
#                        the meantime.
# ([STAY] is accepted and stripped as an explicit no-op.)
_ADVANCE_RE = re.compile(r'\[ADVANCE\]')
_STAY_RE = re.compile(r'\[STAY\]')
_REPLAN_RE = re.compile(r'\[REPLAN:\s*"([^"]{3,300})"\s*\]', re.DOTALL)


def _process_plan_markers(user_id, text, trigger):
    """Parse & act on [ADVANCE]/[STAY]/[REPLAN:]; return stripped text."""
    if _ADVANCE_RE.search(text):
        plan = db.get_current_plan(user_id)
        if plan:
            new_idx = min(plan["cursor"] + 1, len(plan["steps"]))
            db.move_plan_cursor(user_id, new_idx,
                                reason=f"advance via {trigger}")
            if new_idx >= len(plan["steps"]):
                db.log_event(user_id, "plan_completed",
                             {"version": plan["version"]}, source="sms")
                print(f"[PLAN] {user_id}: plan v{plan['version']} complete",
                      flush=True)
        else:
            print(f"[PLAN] stray [ADVANCE] with no plan — ignored", flush=True)
        text = _ADVANCE_RE.sub("", text)
    replan = _REPLAN_RE.search(text)
    if replan:
        db.log_event(user_id, "plan_replan_requested",
                     {"reason": replan.group(1).strip(), "trigger": trigger},
                     source="sms")
        text = _REPLAN_RE.sub("", text)
    text = _STAY_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


# Dormancy gate (operator decision 2026-07-28): the "no tasks while
# dormant / zero-demand re-opening" rules were being left to the
# LLM's judgment and were observably not followed. They are now
# MECHANICAL: the server detects dormancy before the call and swaps
# the sequence assignment for a re-opening directive. A fresh user
# who has never messaged is new, not dormant.
DORMANT_AFTER_H = int(os.environ.get("DORMANT_AFTER_H", "48"))

_DORMANT_FORBIDDEN_TAGS = frozenset({
    "micro_ask", "choice_offer", "implementation_cue", "handoff",
    "secure_commit", "map",
})


def _dormancy_hours(user_id):
    """Hours since the user's last inbound message, or None if they
    have never messaged."""
    last_in = db.get_last_event(user_id, "sms_in")
    if not last_in:
        return None
    return (datetime.now()
            - datetime.fromisoformat(last_in["ts"])).total_seconds() / 3600


def _is_dormant(user_id):
    hours = _dormancy_hours(user_id)
    return hours is not None and hours >= DORMANT_AFTER_H


def _build_dormant_block(user_id):
    hours = _dormancy_hours(user_id)
    return f"""## Mode: re-opening after dormancy (server-detected — OVERRIDES the sequence plan)

The user has been silent for ~{int(hours)}h. Tonight's message is a
ZERO-DEMAND re-opening. This is mechanical, not your judgment call:

- No tasks, no bites, no asks, no laptop, no "오늘 저녁?" scheduling
  pokes. Nothing the user could fail to do.
- Allowed move families: connect, validate, elicit_why (@1 at
  most), reframe_state, release — or hold (send nothing) if even a
  warm touch would read as pressure today.
- The sequence plan is PAUSED — do not perform its current step.
  It resumes once the user speaks again.
- Success tonight = the message is safe to read and not answer. An
  unanswered zero-demand message costs nothing; an unanswered ask
  poisons the channel."""


def _build_plan_block(user_id):
    """The assignment block (exploration v2): the LLM receives ONLY
    the current step as its job — later steps exist server-side, so
    collapsing a sequence into one send is structurally impossible,
    not merely forbidden. Empty string when the user has no plan
    (planner falls back to free choice per prior+notes)."""
    plan = db.get_current_plan(user_id)
    if not plan or not plan["steps"]:
        return ""
    cur, steps = plan["cursor"], plan["steps"]
    if cur >= len(steps):
        return ("## Sequence assignment\n\n"
                f"Plan v{plan['version']} is COMPLETE. Choose freely per "
                "the prior and this user's notes; a new plan will be set "
                "at the next review. Emit [REPLAN: \"...\"] with a "
                "suggestion if you see the natural next sequence.")
    step = steps[cur]
    intent = step.get("intent", "")
    lines = [
        "## Sequence assignment (this message's job — nothing more)",
        "",
        f"Plan v{plan['version']}, step {cur + 1} of {len(steps)}: "
        f"**{step['tag']}@{step.get('intensity', 2)}** — {intent}",
        "",
        "Execute THIS step only. Later steps of the plan are not yours "
        "to perform in this message, and are shown by name only so you "
        "can angle the conversation toward them without starting them.",
    ]
    if cur + 1 < len(steps):
        lines.append(f"Next (name only, do NOT perform): "
                     f"{steps[cur + 1]['tag']}")
    lines += [
        "",
        "Plan judgments (markers, stripped by the server):",
        "- When REPLYING to the user: if their latest message completed "
        "the current step's purpose, emit [ADVANCE] — your message then "
        "works the NEXT step. If not yet, emit [STAY] and keep working "
        "this step.",
        "- If the plan visibly no longer fits the signals, emit "
        "[REPLAN: \"reason\"] and act per the prior and notes instead.",
    ]
    return "\n".join(lines)


# [STEP: tag@2, tag@1] — the LLM self-tags which behavioral levers
# this outbound message pulls, with a 1-3 intensity per tag. This is
# instrumentation, not constraint: the coach improvises freely and
# REPORTS what it did, so [state + (step, intensity) + outcome]
# triples accumulate from day one. The vocabulary below is the
# canonical step lexicon (ignition-only scope); the same lexicon later
# becomes the planning language for per-user sequence plans, so the
# stored shape ({tag, intensity}) is shared between "what happened"
# and future "what was planned".
_STEP_MARKER_RE = re.compile(r'\[STEP:\s*([a-z_0-9@,\s]+?)\s*\]', re.IGNORECASE)

# [EXPECT: reply] — the planner's prediction of the user's next
# reaction (exploration P5). One token from a closed vocabulary;
# stored on the sms_out event and scored against reality by the
# nightly job. Every send is a falsifiable bet.
_EXPECT_VOCAB = frozenset({"no_reply", "reply", "advance", "withdraw",
                           "ignition"})
_EXPECT_MARKER_RE = re.compile(r'\[EXPECT:\s*([a-z_]+)\s*\]', re.IGNORECASE)


def _process_expect_marker(text):
    """Extract [EXPECT: token] → (expect_or_None, stripped_text).
    Unknown tokens stored verbatim (vocabulary feedback), flagged."""
    m = _EXPECT_MARKER_RE.search(text)
    if not m:
        return None, text
    token = m.group(1).strip().lower()
    if token not in _EXPECT_VOCAB:
        print(f"[SMS] ⚠️ unknown EXPECT token {token!r} — stored verbatim",
              flush=True)
    text = _EXPECT_MARKER_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return token, text

STEP_VOCABULARY = frozenset({
    # 접촉 — demand-free contact
    "connect", "validate",
    # 동기 — the user's own reasons
    "elicit_why", "identity_frame", "spark_curiosity",
    # 구조 — ambiguity removal & commitment
    "map", "secure_commit",
    # 효능감 — Bandura's four sources
    "evoke_mastery", "vicarious_model", "affirm_ability", "reframe_state",
    # 점화 — activation
    "micro_ask", "choice_offer", "implementation_cue", "handoff",
    # 페이싱 — withdrawal is also an action
    "release", "hold",
    # drain: none of the levers (reached by exclusion only)
    "none",
})

# Tags with no language realization carry no intensity.
_NO_INTENSITY_TAGS = frozenset({"hold", "none"})


def _process_step_marker(user_id, text):
    """Extract the [STEP: ...] self-tag → (steps, stripped_text).

    steps is a list of {"tag": str, "intensity": int|None} in
    utterance order. Missing intensity defaults to 2; values clamp to
    1-3. Unknown tags are STORED verbatim (raw is sacred — a tag the
    LLM invented is itself signal about the vocabulary's coverage)
    but flagged in logs. No marker → ([], text) — absence is visible
    in the event payload as an empty list.
    """
    m = _STEP_MARKER_RE.search(text)
    if not m:
        return [], text
    steps = []
    for part in m.group(1).split(","):
        part = part.strip().lower()
        if not part:
            continue
        tag, _, level = part.partition("@")
        tag = tag.strip()
        if tag in _NO_INTENSITY_TAGS:
            intensity = None
        else:
            try:
                intensity = max(1, min(3, int(level.strip())))
            except ValueError:
                intensity = 2
        if tag not in STEP_VOCABULARY:
            print(f"[SMS] ⚠️ unknown step tag {tag!r} from LLM — "
                  f"stored verbatim", flush=True)
        steps.append({"tag": tag, "intensity": intensity})
    text = _STEP_MARKER_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return steps, text


def _process_commit_marker(user_id, text):
    """Parse and act on control markers the LLM may embed in its
    response, and return the text with all markers stripped.

    [GOAL: "..."]   — save/refine the agreed goal (any phase).
    [COMMIT: "..."] — save first bite + transition discovery→first_bite.
    """
    goal_match = _GOAL_MARKER_RE.search(text)
    if goal_match:
        db.set_agreed_goal(user_id, goal_match.group(1).strip())
        text = _GOAL_MARKER_RE.sub("", text)

    match = _COMMIT_MARKER_RE.search(text)
    if match:
        bite = match.group(1).strip()
        phase = db.get_user_phase(user_id)["phase"]
        if phase == "discovery":
            # Decision point (T3): accept the LLM's commit marker as a
            # real phase transition. Deterministic accept today; the
            # hook exists so acceptance policy (e.g. require explicit
            # user confirmation) can be varied and joined to outcomes.
            choice, decision_id = policy.decide(
                "commit_marker_accept", user_id,
                options=["accept", "hold"],
                context={"bite": bite[:200]})
            if choice == "accept":
                # P0-A: the bite is ONE checklist field, not the whole
                # graduation — the phase flips only when the full
                # onboarding predicate completes (see
                # check_and_complete_onboarding, called after marker
                # processing on both paths).
                db.set_agreed_bite(user_id, bite, decision_id=decision_id)
        else:
            # LLM emitted a commit while already in Phase 1 — ignore, log.
            print(f"[SMS] stray COMMIT marker while phase={phase!r}, ignoring", flush=True)
            db.log_event(user_id, "commit_marker_ignored",
                         {"phase": phase, "bite": bite}, source="sms")
        text = _COMMIT_MARKER_RE.sub("", text)

    # Collapse the double-blank that stripping mid-paragraph can leave.
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


# ─── Inbound message handling ───────────────────────────────────────

def _is_command(body, token_set):
    """Match body as a single-word command, case-insensitive,
    ignoring surrounding whitespace and trailing punctuation."""
    cleaned = body.strip().lower().rstrip(".!?")
    return cleaned in token_set


# Marker file for "skip the rest of today's slots". Written when user
# texts "skip"; cron-tick checks it before sending. File path keyed by
# user_id and YYYYMMDD so it auto-expires at midnight UTC (close
# enough — DST drift here is harmless).
_SKIP_DIR = "/tmp/upskill_sms_skip"


def _skip_marker_path(user_id):
    os.makedirs(_SKIP_DIR, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    return os.path.join(_SKIP_DIR, f"{user_id}_{day}")


def _mark_skip_today(user_id):
    path = _skip_marker_path(user_id)
    with open(path, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())


def _is_skipped_today(user_id):
    return os.path.exists(_skip_marker_path(user_id))


# "later" defers a slot to evening. We keep it simple: when user texts
# "later", we just acknowledge — the evening 9pm cron will fire
# regardless. (The deferred-message-replay path is a v2 nicety; for
# now the user gets the evening slot's normal content.)


def _strip_channel(addr):
    """Strip 'whatsapp:' / 'sms:' prefix from a Twilio address so we
    compare raw E.164 numbers on inbound. TUTOR_USER_PHONE in env is
    stored as bare E.164; Twilio webhooks deliver inbound `From` with
    the channel prefix on WhatsApp."""
    for p in ("whatsapp:", "sms:"):
        if addr.startswith(p):
            return addr[len(p):]
    return addr


def _resolve_user_from_phone(from_number):
    """Map an inbound phone number to a user_id. Single-user MVP: env
    var TUTOR_USER_PHONE must match exactly (after stripping any
    channel prefix Twilio added).
    """
    expected = os.environ.get("TUTOR_USER_PHONE", "").strip()
    user_id = os.environ.get("TUTOR_USER_ID", "").strip()
    if not (expected and user_id):
        return None
    incoming = _strip_channel(from_number.strip())
    if incoming != _strip_channel(expected):
        print(f"[SMS] inbound from unknown number {from_number} (normalized {incoming}), ignoring", flush=True)
        return None
    return user_id


def handle_inbound(from_number, body):
    """Process an inbound SMS. Returns the text we replied with (or
    None if we chose not to reply)."""
    user_id = _resolve_user_from_phone(from_number)
    if not user_id:
        # Unknown sender is still an event (brief: nothing unrecorded).
        db.log_event(None, "sms_in_unknown_sender",
                     {"from": _strip_channel(from_number), "text": body[:200]},
                     source="sms")
        return None

    # Log the user's message FIRST so it's part of history before we
    # build context for our reply.
    db.save_sms_message(user_id, "user", body, "in")
    db.log_event(user_id, "sms_in", {"text": body}, source="sms")

    # Meta-commands short-circuit the LLM.
    if _is_command(body, SKIP_TOKENS):
        _mark_skip_today(user_id)
        db.log_event(user_id, "skip_today", {}, source="sms")
        reply = "ok, no more pings today. talk tomorrow."
        send_sms(from_number, reply, user_id=user_id)
        db.save_sms_message(user_id, "assistant", reply, "out")
        db.log_event(user_id, "sms_out", {"text": reply, "trigger": "skip_ack"}, source="sms")
        return reply
    if _is_command(body, LATER_TOKENS):
        db.log_event(user_id, "defer_to_evening", {}, source="sms")
        reply = "got it — picking this back up at 9."
        send_sms(from_number, reply, user_id=user_id)
        db.save_sms_message(user_id, "assistant", reply, "out")
        db.log_event(user_id, "sms_out", {"text": reply, "trigger": "later_ack"}, source="sms")
        return reply

    # If the user is mid-study with the observer running, grab a
    # fresh screen capture before building the reply context.
    _request_fresh_screen(user_id)

    # Scope history to the current phase so old conversations from
    # before a phase transition don't bleed in.
    phase_state = db.get_user_phase(user_id)
    history = db.get_recent_sms_messages(
        user_id, limit=HISTORY_LIMIT, since=phase_state["phase_started_at"]
    )
    # `history` ends with the user message we just inserted, which is
    # what the Anthropic API expects (last message = user turn).

    # Use the phase-specific evening prompt for inbound replies too —
    # the LLM should be in the same mode whether the user is replying
    # to a scheduled ping or texting spontaneously.
    system_prompt, prompt_versions = _build_system_prompt_for_reply(user_id)

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=system_prompt,
            messages=history,
        )
        reply_text = resp.content[0].text.strip()
    except Exception as e:
        print(f"[SMS] ❌ Claude call failed on inbound: {e}", flush=True)
        db.log_event(user_id, "llm_error",
                     {"where": "inbound_reply", "error": str(e)[:300]},
                     source="sms")
        return None

    # Flight-recorder snapshot of the call (T2b): the exact input the
    # API received + the raw response, BEFORE marker-stripping so the
    # record shows what the model actually produced.
    llm_call_id = db.save_llm_call(
        user_id, "inbound_reply", MODEL, system_prompt, history,
        prompt_versions, reply_text)

    # Parse & handle [COMMIT: "..."] marker, strip it from user-visible text.
    reply_text = _process_commit_marker(user_id, reply_text)
    reply_text = _process_ignition_markers(user_id, reply_text,
                                           trigger="inbound_reply")
    reply_text = _process_onboarding_markers(user_id, reply_text)
    reply_text = _process_plan_markers(user_id, reply_text,
                                       trigger="inbound_reply")
    expect, reply_text = _process_expect_marker(reply_text)
    steps, reply_text = _process_step_marker(user_id, reply_text)
    # Field fills above may have completed the checklist — code, not
    # the LLM, makes that call. Completion fires the initial plan
    # generation in the background (P0-B) so this reply isn't
    # delayed; the operator reviews /plan + /notes before the first
    # sequence-mode send.
    if db.check_and_complete_onboarding(user_id):
        import genplan
        genplan.generate_async(user_id)
    db.mark_onboarding_started(user_id)
    send_sms(from_number, reply_text, user_id=user_id)
    db.save_sms_message(user_id, "assistant", reply_text, "out")
    db.log_event(user_id, "sms_out",
                 {"text": reply_text, "trigger": "inbound_reply",
                  "prompt_versions": prompt_versions,
                  "llm_call_id": llm_call_id,
                  "steps": steps, "expect": expect,
                  "phase": db.get_user_phase(user_id)["phase"]},
                 source="sms")
    return reply_text


def _build_system_prompt_for_reply(user_id):
    """Shared persona + phase-specific mode prompt, with placeholders
    filled — used for inbound conversational replies.

    The mode prompt matters here: without it, the LLM only has the
    generic flow-companion persona, and it may drift into tutoring
    behavior when replying to the user's messages. Including the
    discovery / first_bite prompt keeps the LLM anchored to the
    same job it has during the scheduled evening ping.
    """
    shared, h_shared = _read_prompt_versioned("sms_shared")
    phase = db.get_user_phase(user_id)["phase"]
    mode_name = "sms_first_bite" if phase == "first_bite" else "sms_discovery"
    mode_prompt, h_mode = _read_prompt_versioned(mode_name)
    fields = _build_placeholders(user_id)
    rendered_shared = shared.format_map(_SafeDict(**fields))
    rendered_mode = mode_prompt.format_map(_SafeDict(**fields))
    context, ctx_versions = _build_context_blocks(user_id)
    versions = {"sms_shared": h_shared, mode_name: h_mode,
                **ctx_versions}
    parts = [context, rendered_shared, rendered_mode]
    # Same precedence as the scheduled path, minus dormancy (the
    # user just messaged us — by definition not dormant).
    ob_block = _build_onboarding_block(user_id)
    if ob_block:
        parts.append(ob_block)
    else:
        plan_block = _build_plan_block(user_id)
        if plan_block:
            parts.append(plan_block)
    return "\n\n---\n\n".join(parts), versions


# ─── Scheduled slot handling ────────────────────────────────────────

def handle_cron_tick(slot, window=None):
    """Run a scheduled slot: decide whether to send, and if so,
    load prompt, call Claude, send WhatsApp.

    Returns the sent text, or None if we declined to send.

    Under the Phase 0/1 redesign, the four scheduled slots have very
    different jobs:
      morning  — thread-keeping (only if there's prior conversation)
      lunch    — always skip (user is in startup+kid time)
      afternoon — always skip (same reason)
      evening  — the anchor slot; discovery or first_bite prompt
                 depending on user's current phase.

    `window` ("HH:MM-HH:MM") is set when the call comes from the
    per-user schedule tick (P0-C) rather than a fixed cron. It rides
    along into the cron_tick and sms_out event payloads so the trace
    shows which agreed window fired — and so the schedule tick can
    dedupe against the event log.
    """
    if slot not in SLOTS:
        print(f"[SMS] unknown slot {slot!r}", flush=True)
        return None

    # Extra payload fields for window-driven calls; empty for the
    # legacy fixed crons so their event shape is unchanged.
    win_extra = {"window": window} if window else {}

    user_id = os.environ.get("TUTOR_USER_ID", "").strip()
    to_number = os.environ.get("TUTOR_USER_PHONE", "").strip()
    if not (user_id and to_number):
        print(f"[SMS] {slot}: TUTOR_USER_ID/PHONE not set — skipping", flush=True)
        db.log_event(None, "cron_tick",
                     {"slot": slot, "action": "skipped", "reason": "env_unset"},
                     source="cron")
        return None

    def _skip(reason):
        print(f"[SMS] {slot}: skipping — {reason}", flush=True)
        # A deliberately-unsent slot is itself a coaching action:
        # the server tags it `hold` (the LLM never ran, so it can't
        # self-tag). Silence enters the same step-labeled dataset.
        db.log_event(user_id, "cron_tick",
                     {"slot": slot, "action": "skipped", "reason": reason,
                      "steps": [{"tag": "hold", "intensity": None}],
                      **win_extra},
                     source="cron")
        return None

    if _is_skipped_today(user_id):
        return _skip("user_skip_today")

    # Fixed-slot suppression (P0-C): once a user has an agreed
    # schedule, the hourly schedule tick owns their sends — the
    # legacy fixed crons stand down, so a window coinciding with a
    # fixed slot can never double-send. Calls carrying `window` ARE
    # the schedule tick and pass through.
    if window is None and slot in ("morning", "evening") \
            and db.get_user_schedule(user_id):
        return _skip("user_schedule_active")

    # Slot-specific gating.
    if slot in ("lunch", "afternoon"):
        return _skip("daytime_slot_disabled")

    if slot == "morning":
        # Skip if there's no prior conversation IN THE CURRENT PHASE.
        # We scope to phase-timer to prevent a "morning!" ping that
        # references stale pre-phase context.
        phase_state = db.get_user_phase(user_id)
        recent = db.get_recent_sms_messages(
            user_id, limit=1, since=phase_state["phase_started_at"]
        )
        if not recent:
            return _skip("no_thread_this_phase")

    if slot == "evening":
        # Start the Phase 0 timer on the first evening tick (idempotent).
        db.ensure_phase_timer_started(user_id)

    # Decision point (T3): all hard gates passed — does policy fire
    # this slot? Deterministic "fire" today; this is where send-vs-
    # hold experiments (timing, frequency backoff) will sample later.
    fire_choice, fire_decision_id = policy.decide(
        f"{slot}_fire", user_id,
        options=["fire", "hold"],
        context={"phase": db.get_user_phase(user_id)["phase"]})
    if fire_choice != "fire":
        return _skip(f"policy_hold:{fire_decision_id}")

    system_prompt, prompt_versions = _build_system_prompt(slot, user_id)
    if system_prompt is None:
        return _skip("no_prompt_for_state")

    # Scope history to current phase — see get_recent_sms_messages docstring.
    phase_state = db.get_user_phase(user_id)
    history = db.get_recent_sms_messages(
        user_id, limit=HISTORY_LIMIT, since=phase_state["phase_started_at"]
    )

    # If there's no recent SMS history, prime with a single user-turn
    # placeholder. Anthropic requires the messages array to start with
    # a user role and to be non-empty.
    if not history:
        history = [{"role": "user", "content": f"(scheduled {slot} slot — no prior thread)"}]
    elif history[-1]["role"] == "assistant":
        # Last turn was us. Add a synthetic user-turn so Claude has
        # something to respond to. The slot prompt itself is in the
        # system message; this is just a "go" signal.
        history.append({"role": "user", "content": f"(scheduled {slot} slot fired)"})

    try:
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=500,
            system=system_prompt,
            messages=history,
        )
        text = resp.content[0].text.strip()
    except Exception as e:
        print(f"[SMS] ❌ Claude call failed on {slot}: {e}", flush=True)
        db.log_event(user_id, "llm_error",
                     {"where": f"cron_{slot}", "error": str(e)[:300]},
                     source="cron")
        return None

    # Flight-recorder snapshot of the call (T2b) — raw response,
    # pre-marker-stripping.
    llm_call_id = db.save_llm_call(
        user_id, f"cron_{slot}", MODEL, system_prompt, history,
        prompt_versions, text)

    # Parse & handle [COMMIT: "..."] marker (Phase 0→1), strip it out.
    text = _process_commit_marker(user_id, text)
    text = _process_ignition_markers(user_id, text, trigger=f"cron_{slot}")
    text = _process_onboarding_markers(user_id, text)
    text = _process_plan_markers(user_id, text, trigger=f"cron_{slot}")
    expect, text = _process_expect_marker(text)
    steps, text = _process_step_marker(user_id, text)
    if db.check_and_complete_onboarding(user_id):
        import genplan
        genplan.generate_async(user_id)
    db.mark_onboarding_started(user_id)

    # Dormant-mode enforcement (detection tier): the step tags confess
    # if the planner played an ask into a dormant channel.
    if _is_dormant(user_id):
        bad = [s["tag"] for s in steps
               if s.get("tag") in _DORMANT_FORBIDDEN_TAGS]
        if bad:
            print(f"[SMS] ⚠️ dormant-mode violation: {bad}", flush=True)
            db.log_event(user_id, "planner_violation",
                         {"mode": "dormant_reopen",
                          "forbidden_steps": bad,
                          "llm_call_id": llm_call_id}, source="cron")

    # The planner may CHOOSE silence on a scheduled send ([STEP: hold]
    # with no message body) — deliberate non-action is an action.
    # Record it like a skip so the trace shows a hold token; send
    # nothing.
    if not text.strip() and any(s.get("tag") == "hold" for s in steps):
        print(f"[SMS] {slot}: planner chose hold — nothing sent", flush=True)
        db.log_event(user_id, "cron_tick",
                     {"slot": slot, "action": "held_by_planner",
                      "decision_id": fire_decision_id,
                      "llm_call_id": llm_call_id,
                      "steps": steps, "expect": expect,
                      **win_extra},
                     source="cron")
        return None

    send_sms(to_number, text, user_id=user_id)
    db.save_sms_message(user_id, "assistant", text, "out")
    db.log_event(user_id, "cron_tick",
                 {"slot": slot, "action": "fired",
                  "decision_id": fire_decision_id, **win_extra},
                 source="cron")
    db.log_event(user_id, "sms_out",
                 {"text": text, "trigger": f"cron_{slot}",
                  "prompt_versions": prompt_versions,
                  "llm_call_id": llm_call_id,
                  "steps": steps, "expect": expect,
                  "phase": db.get_user_phase(user_id)["phase"],
                  **win_extra},
                 source="cron")
    return text


# ─── Per-user schedule tick (P0-C) ──────────────────────────────────
#
# A single hourly Render cron POSTs /sms/schedule-tick. The handler
# checks the active user's latest agreed schedule ([SCHEDULE:] rows,
# versioned in user_schedule) and fires any window whose start hour
# equals the user's current local hour. Slot semantics are mapped
# mechanically — a window starting before 12:00 local runs the
# morning-slot behavior, 12:00 or later runs the evening-slot
# behavior — by delegating to handle_cron_tick(slot, window=...).
# Windows fire once per day, deduped against the event log: every
# window-driven cron_tick payload carries the window token, and a
# match within WINDOW_DEDUP_H hours means "already served today".

# 20h (not 24h) so normal cron jitter or a DST-shifted local hour
# can't make a daily window miss its next-day firing.
WINDOW_DEDUP_H = 20


def _window_token(w):
    return f"{w['start']}-{w['end']}"


def _window_fired_recently(user_id, token, now):
    """ts of the last cron_tick that served this window within the
    dedup horizon, else None. Substring match on the JSON payload —
    same dialect-neutral trick as the infra sweep (T6)."""
    ev = db.get_last_event(user_id, "cron_tick",
                           payload_contains=f'"window": "{token}"')
    if not ev:
        return None
    age_h = (now - datetime.fromisoformat(ev["ts"])).total_seconds() / 3600
    return ev["ts"] if age_h < WINDOW_DEDUP_H else None


def handle_schedule_tick(now=None):
    """Run the hourly per-user send-window check (P0-C).

    Single-user shim like the rest of the pipeline: serves
    TUTOR_USER_ID only. No schedule rows → no-op (the founder's
    fixed crons keep serving him). `now` is injectable for tests
    (naive server-local, matching event timestamps).

    Returns the sent text, or None if nothing fired.
    """
    user_id = os.environ.get("TUTOR_USER_ID", "").strip()
    if not user_id:
        print("[SMS] schedule-tick: TUTOR_USER_ID not set — no-op",
              flush=True)
        return None

    sched = db.get_user_schedule(user_id)
    if not sched:
        print(f"[SMS] schedule-tick: no schedule for {user_id} — no-op",
              flush=True)
        return None
    try:
        windows = json.loads(sched["windows_json"])
    except Exception as e:
        print(f"[SMS] ⚠️ schedule-tick: unreadable windows_json "
              f"v{sched['version']}: {e}", flush=True)
        return None

    now = now or datetime.now()
    local_hour = (now + timedelta(hours=TZ_OFFSET_H)).hour

    for w in windows:
        try:
            start_hour = int(w["start"].split(":")[0])
        except (KeyError, ValueError, AttributeError):
            print(f"[SMS] ⚠️ schedule-tick: malformed window {w!r} — "
                  f"skipped", flush=True)
            continue
        if local_hour != start_hour:
            continue
        token = _window_token(w)
        fired_ts = _window_fired_recently(user_id, token, now)
        if fired_ts:
            print(f"[SMS] schedule-tick: window {token} already served "
                  f"at {fired_ts} — deduped", flush=True)
            continue
        slot = "morning" if start_hour < 12 else "evening"
        print(f"[SMS] schedule-tick: window {token} → {slot} semantics",
              flush=True)
        return handle_cron_tick(slot, window=token)
    return None


def schedule_status(user_id, now=None):
    """Debug snapshot for GET /schedule: the latest schedule version
    plus per-window served-today state (same dedup rule the tick
    uses, so the view answers "will the next tick fire?")."""
    sched = db.get_user_schedule(user_id)
    if not sched:
        return {"schedule": None, "windows": []}
    now = now or datetime.now()
    try:
        windows = json.loads(sched["windows_json"])
    except Exception:
        windows = []
    out = []
    for w in windows:
        token = _window_token(w)
        try:
            start_hour = int(w["start"].split(":")[0])
        except (KeyError, ValueError, AttributeError):
            continue
        fired_ts = _window_fired_recently(user_id, token, now)
        out.append({"window": token,
                    "slot": "morning" if start_hour < 12 else "evening",
                    "fired_today": bool(fired_ts),
                    "last_fired": fired_ts})
    return {"schedule": {"version": sched["version"], "ts": sched["ts"],
                         "raw_text": sched["raw_text"],
                         "source": sched["source"]},
            "windows": out}
