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

# `nudge` is the operator's poke: identical to the evening slot in
# prompt and behavior, but time-agnostic and honestly labeled — a
# manual send recorded as cron_evening would corrupt every
# slot-conditioned pattern in the exploration data. Never fired by a
# schedule; the endpoint requires an explicit user_id for it.
SLOTS = ("morning", "lunch", "afternoon", "evening", "nudge")

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

# Below this the analysis pass's smalltalk_aversion read stays
# advisory (no prompt block); at or above it the no-small-talk
# block is enforced. Some pilot users visibly bounced off
# chit-chat openers.
SMALLTALK_AVERSION_THRESHOLD = 0.6

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

    # A separator at the very edge is a model tic (a '---' with no
    # first bubble): the splitter requires \n before it, so a leading
    # one would ride into the user's first SMS line verbatim —
    # observed in the PR-A smoke run. Shave the edges first.
    body = body.strip()
    body = re.sub(r"^(?:\s*---\s*\n)+", "", body)
    body = re.sub(r"(?:\n\s*---\s*)+$", "", body)

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
        # Ordering gate between bubbles. A fixed 1s gap was not
        # enough: Korean text is UCS-2 (67 chars/segment), so a long
        # first bubble rides 4-6 SMS segments and can take several
        # seconds longer through the carrier than the short second
        # bubble — field report (2026-08-11): the husband's weekend
        # two-bubble sends arrived reversed, repeatedly. Wait until
        # Twilio reports the first bubble handed off (sent/delivered)
        # before releasing the next, then a settle gap; on timeout
        # send anyway — a late bubble beats a dropped one.
        if i < len(parts) - 1:
            _await_bubble_handoff(client, msg.sid)
            time.sleep(3.5 if _channel_prefix() else 1.5)
    return last_sid


def _await_bubble_handoff(client, sid, timeout=10.0):
    """Poll one message's status until Twilio reports it handed off
    to the carrier (or terminally failed), bounded by `timeout`."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status = client.messages(sid).fetch().status
        except Exception as e:
            print(f"[SMS] bubble status poll failed: {e}", flush=True)
            return
        if status in ("sent", "delivered", "read", "failed",
                      "undelivered"):
            return
        time.sleep(0.7)
    print(f"[SMS] ⚠️ bubble {sid} not handed off after {timeout}s — "
          f"sending next anyway", flush=True)


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
        return _strip_authoring_comments(f.read())


_HTML_COMMENT_RE = re.compile(r"<!--.*?-->\s*", re.DOTALL)


def _strip_authoring_comments(text):
    """Drop <!-- ... --> blocks. They are notes to whoever edits the
    file — why a section was moved, when it is loaded — and the model
    has no use for them. Observed: the note explaining why the user's
    facts moved next to the profile brief was being shipped to the
    coach, directly above the block it described."""
    return _HTML_COMMENT_RE.sub("", text).strip() + "\n"


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
    try:
        rows = db.get_recent_insights(limit=3, user_id=user_id)
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
    if slot in ("evening", "nudge"):
        return "sms_first_bite" if phase == "first_bite" else "sms_discovery"
    # Unreachable in normal flow — handle_cron_tick skips lunch/afternoon.
    return None


def _language_rule(user_id):
    """The outbound-language instruction. An explicit user
    preference (any preference key that looks language-shaped)
    wins; the default is English."""
    try:
        for k, v in (db.get_user_preferences(user_id) or {}).items():
            if "lang" in k.lower() or "언어" in k:
                return (f"**Write every outbound message in the "
                        f"language this user explicitly asked for: "
                        f"{v['value']}.**")
    except Exception as e:
        print(f"[SMS] ⚠️ language rule failed: {e}", flush=True)
    return "**Write every outbound message in English.**"


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
        # The language rule: an explicit user preference wins;
        # otherwise English (pilots are effectively all English —
        # the blanket Korean mandate this replaces was what the
        # standing preference kept losing to).
        "language_rule": _language_rule(user_id),
        # The offer-stock discipline matters while the standing
        # offer is being negotiated; once onboarding is complete
        # the promise is set and the paragraph is dead weight.
        "offer_discipline": (
            "" if _onboarding_done(user_id) else
            "Every offer you make must be executable with ONLY the "
            "stock above. If the thing that would truly help is not "
            "in it, offer the nearest real thing and name the edge "
            "honestly — \"I can't work from the file directly, but "
            "with what you've shared I can get this far\" beats a "
            "promise you cannot keep. An unkept promise is worse "
            "than none: this list is why the user can trust a "
            "\"will do\" from you at all."),
        # Goal discipline splits on whether a goal EXISTS, not on
        # onboarding completion — a goal agreed on day 2 of
        # discovery is already settled fact while other checklist
        # fields are still open. (The profile goal/studying columns
        # are NOT surfaced here: nothing in the SMS product writes
        # them — they hold legacy-app boilerplate at best.)
        "goal_discipline": (
            "The AGREED GOAL is settled fact. Never re-open it from "
            "scratch — reference it in passing; re-asking reads as "
            "amnesia."
            if (phase_state["agreed_goal"] or "").strip() else
            "No goal has been agreed yet — never assert one; "
            "discovering it is what these conversations are for."),
        # 1-indexed day count for the LLM's "Day X of 3" awareness.
        "discovery_day": db.days_in_discovery(user_id) + 1,
        "recent_screen": _format_recent_screen(user_id),
        # The standing promise. Write-only until now: analyze_turn
        # extracted it, db stored it, the completion gate read it —
        # and the coach never saw it again, so an agreed offer was
        # made once and forgotten forever.
        "agreed_offer": (profile.get("agreed_offer") or "").strip()
                        or "(not yet agreed)",
    }


class _SafeDict(dict):
    """format_map helper: unknown {brace} keys pass through unchanged
    so LLM prompt bodies with JSON examples don't blow up rendering."""
    def __missing__(self, k):
        return "{" + k + "}"


def _clock_block():
    """A human-readable clock line. Prepended to the context because
    a machine-shaped `local_time=08:18` inside the features line was
    observably skimmed past — the coach asked how the user's day had
    gone at 8:18 in the morning.

    It states the hour and stops. An earlier version told the coach to
    "match the hour", which read as a standing order to open with an
    hour-appropriate greeting — small talk aimed at a user whose brief
    says no social warm-up is needed."""
    now_local = datetime.now() + timedelta(hours=TZ_OFFSET_H)
    hour = now_local.hour
    part = ("the middle of the night" if hour < 5
            else "early morning" if hour < 8
            else "morning" if hour < 11 else "midday" if hour < 14
            else "afternoon" if hour < 17
            else "early evening" if hour < 20
            else "evening" if hour < 23 else "late night")
    weekday = ["Monday", "Tuesday", "Wednesday", "Thursday",
               "Friday", "Saturday", "Sunday"][now_local.weekday()]
    return (f"## Right now, for this user\n\n"
            f"It is **{now_local.strftime('%H:%M')} on {weekday} "
            f"({part})** where they are.")


def _server_turn(text):
    """A turn the SERVER puts in the messages array — the scheduled-send
    signal, or feedback on a draft that broke a rule.

    The Anthropic messages array has only `user` and `assistant` roles,
    so anything we inject wears the user's role. Left bare, it is
    indistinguishable from something the user actually typed: the coach
    was reading "(scheduled evening slot fired)" and "Your message broke
    a hard rule" as conversation, and writing its next message in that
    context. The envelope + the contract block below make the boundary
    explicit.
    """
    return {"role": "user",
            "content": f"<server_instruction>\n{text}\n</server_instruction>"}


def _conversation_contract_block():
    """The LAST block of the system prompt — it sits immediately before
    the messages array, so "everything above is instruction, everything
    below is what happened" is true physically, not just as a claim."""
    return (
        "## Everything below this line is the conversation itself\n\n"
        "The turns that follow are the real exchange with this user, "
        "verbatim, oldest first. The `[Wed 22:48, 2d ago]` prefixes "
        "are the server's annotation of when each turn happened — the "
        "user did not type those.\n\n"
        "Turns wrapped in `<server_instruction>` are NOT from the user. "
        "That is this system talking to you: why you are being asked to "
        "write right now, or what your previous draft got wrong. The "
        "user never saw them and never wrote them. Never quote one, "
        "never reply to one as though the user had spoken, and never "
        "let its wording leak into what you send.\n\n"
        "Everything ABOVE this line is instruction. Everything BELOW is "
        "what actually happened between you and this person.")


def _onboarding_done(user_id):
    """True once the server has stamped onboarding complete. Several
    prompt blocks are gated on this: during onboarding the coach is
    having a conversation, not running a plan, and doctrine about
    material that does not exist yet is noise between it and the one
    thing this message is for."""
    try:
        return bool(db.get_onboarding_state(user_id)["completed_at"])
    except Exception as e:
        print(f"[SMS] ⚠️ onboarding state unreadable: {e}", flush=True)
        return True   # fail toward the fuller prompt, never the emptier


def _step_compact_block():
    """The step vocabulary during onboarding: the tags and nothing
    else. Tagging must not stop while onboarding runs — those turns
    are the freshest exploration data we get, and the trajectory block
    is built from them — but the anchors, the intensity calibration and
    the sequence discipline are about executing a plan that does not
    exist yet. Full version returns as prompts/sms_step_vocabulary.md
    once onboarding completes."""
    return (
        "## Tag your move (required on EVERY response)\n\n"
        "Decide which 1-3 coaching moves this moment calls for, at what "
        "intensity, THEN write a message that executes exactly those. "
        "Append at the very end (the server strips both; the user never "
        "sees them):\n\n"
        "    [STEP: validate@2, elicit_why@1]\n"
        "    [EXPECT: reply]\n\n"
        "Intensity: 1 = light touch, 3 = direct/deep; when unsure, 2. "
        "[EXPECT:] is your honest prediction of their next reaction — "
        "one of `no_reply` | `reply` | `advance` | `withdraw` | "
        "`ignition`.\n\n"
        "접촉: `connect` (demand-free contact) · `validate` (name and "
        "accept their state)\n"
        "동기: `elicit_why` (they articulate why — must be an OPEN "
        "question, and the LAST move of the message) · `identity_frame` "
        "· `spark_curiosity`\n"
        "구조: `map` (lay out the path) · `secure_commit` (lock explicit "
        "agreement)\n"
        "효능감: `evoke_mastery` · `vicarious_model` · `affirm_ability` "
        "(cite real evidence, never empty praise) · `reframe_state`\n"
        "점화: `micro_ask` · `choice_offer` · `implementation_cue` · "
        "`handoff`\n"
        "페이싱: `release` (end warmly, no extraction) · `hold` "
        "(server-only — you will not use it)\n"
        "drain: `none` (nothing above fits; no intensity)\n\n"
        "Do not invent tags. If the same move already went unanswered "
        "in your last 2 sends, play a different family instead — a move "
        "that keeps failing with this user is wrong for them, not "
        "insufficiently repeated.")


def _build_context_blocks(user_id, focus_block=None):
    """The exploration prediction call's three blocks (brief §7):
    A = policy prior, B = user notes, C = recent trajectory +
    computed features. Returns (text, versions). Never raises —
    a failed block renders as absent and the planner degrades
    gracefully (empty notes ≈ global-prompt behavior).

    focus_block (the onboarding checklist, the plan assignment, or
    the dormancy gate — whichever applies) rides in second, right
    under the clock. It used to be appended last, behind ~25 other
    sections; the coach read it as trivia and opened with small talk
    while its focus was the offer. What time it is and what this
    message is for are the two things that must not be buried.
    """
    import features as features_mod
    import notes as notes_mod
    import trace as trace_mod

    versions = {}
    parts = []
    # Policy prior REMOVED from live assembly (operator decision,
    # 2026-08-11): it was rendering only post-onboarding, and the
    # line-by-line prompt review judged it dead weight for the drill
    # era. prompts/prior.md is kept, marked deprecated — restore by
    # reverting this block if the ignition principles are needed
    # again.
    try:
        profile = notes_mod.render_profile_block(user_id)
        if profile:
            parts.append(profile)
    except Exception as e:
        print(f"[SMS] ⚠️ profile block failed: {e}", flush=True)
    try:
        facts, h_facts = _read_prompt_versioned("sms_user_facts")
        versions["sms_user_facts"] = h_facts
        parts.append(facts.format_map(_SafeDict(**_build_placeholders(user_id))))
    except Exception as e:
        print(f"[SMS] ⚠️ user facts block failed: {e}", flush=True)
    # Enforced only past the threshold — a weak or unjudged read
    # renders nothing and the persona's default warmth applies. The
    # value is a living judgment (analyze_turn re-reports it as
    # conversation accumulates), so the block tracks the user, not
    # a first impression.
    try:
        _aversion = (db.get_user_profile_by_id(user_id) or {}).get(
            "smalltalk_aversion")
        if _aversion is not None \
                and _aversion >= SMALLTALK_AVERSION_THRESHOLD:
            parts.append(
                "## No small talk with this user\n\n"
                "The accumulated conversation shows this user does "
                "not want chit-chat (confidence "
                f"{_aversion:.1f}). No weather, no how-was-your-day, "
                "no filler openers. Warmth is fine, but it rides ON "
                "the substance — open with the point. This is a "
                "standing read, re-judged as conversation "
                "accumulates.")
    except Exception as e:
        print(f"[SMS] ⚠️ smalltalk block failed: {e}", flush=True)
    try:
        prefs = db.get_user_preferences(user_id)
        if prefs:
            plines = ["## Standing preferences (user-stated, binding)",
                      "",
                      "Rules this user has explicitly set for how you "
                      "talk to them:"]
            for k, v in prefs.items():
                ev = f' — "{v["evidence"]}"' if v.get("evidence") else ""
                plines.append(f"- {k}: {v['value']}{ev}")
            parts.append("\n".join(plines))
    except Exception as e:
        print(f"[SMS] ⚠️ preferences block failed: {e}", flush=True)
    try:
        mat_block = _build_materials_block(user_id)
        if mat_block:
            parts.append(mat_block)
    except Exception as e:
        print(f"[SMS] ⚠️ materials block failed: {e}", flush=True)
    try:
        notes_block = notes_mod.render_notes_block(
            user_id, profile=False, moves=False)
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
    # The clock in words, then what this message is for. Both go at
    # the very top: a key=value clock buried in a feature line got
    # ignored (at 08:18 the coach asked "오늘 하루 어땠어?"), and a
    # focus block appended last got ignored the same way. Inserted
    # outside the try above so a failing trace cannot take them down
    # with it.
    if focus_block:
        parts.insert(0, focus_block)
    parts.insert(0, _clock_block())
    return "\n\n---\n\n".join(parts), versions


def _build_drill_prompt(user_id, drill_ctx):
    """Planner prompt for a scheduled drill-question send: the usual
    context blocks (clock, profile, standing preferences — the
    English rule rides here) + shared persona + the server-selected
    item + the drill mode prompt. The item block precedes the mode
    prompt so 'ask THIS' is established before 'here's how to ask'."""
    import drill
    shared, h_shared = _read_prompt_versioned("sms_shared")
    mode_prompt, h_mode = _read_prompt_versioned("sms_drill")
    fields = _build_placeholders(user_id)
    context, ctx_versions = _build_context_blocks(user_id)
    versions = {"sms_shared": h_shared, "sms_drill": h_mode,
                **ctx_versions}
    parts = [context,
             shared.format_map(_SafeDict(**fields)),
             drill.question_block(drill_ctx)]
    # ④ the person ledger shapes HOW the question is asked —
    # placed right beside the item so both are in hand when the
    # question gets written.
    try:
        pblock = drill.person_block(user_id)
        if pblock:
            parts.append(pblock)
    except Exception as e:
        print(f"[SMS] ⚠️ person block failed: {e}", flush=True)
    parts += [mode_prompt.format_map(_SafeDict(**fields)),
              _conversation_contract_block()]
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
    ensure_my_link_delivered(user_id)
    shared, h_shared = _read_prompt_versioned("sms_shared")
    slot_prompt, h_slot = _read_prompt_versioned(prompt_name)
    fields = _build_placeholders(user_id)
    rendered_shared = shared.format_map(_SafeDict(**fields))
    rendered_slot = slot_prompt.format_map(_SafeDict(**fields))
    context, ctx_versions = _build_context_blocks(
        user_id, focus_block=_build_focus_block(user_id))
    versions = {"sms_shared": h_shared, prompt_name: h_slot,
                **ctx_versions}
    parts = [context, rendered_shared]
    parts += _phase_gated_blocks(user_id, versions)
    parts.append(rendered_slot)
    cap_block = _hold_cap_block(user_id)
    if cap_block:
        parts.append(cap_block)
    parts.append(_conversation_contract_block())
    return "\n\n---\n\n".join(parts), versions


def _phase_gated_blocks(user_id, versions):
    """The blocks that only make sense once there is material and a
    plan: hard rules 2-8 and the full step vocabulary. During
    onboarding they are replaced by rule 1 (already in sms_shared)
    and the compact tag list."""
    # Step vocabulary and the compact tag list removed from assembly
    # (exp/step-surface-removal): the 17-move lexicon was biasing
    # every message toward coaching-move shapes.
    if not _onboarding_done(user_id):
        return []
    out = []
    for name in ("sms_hard_rules_full",):
        try:
            text, h = _read_prompt_versioned(name)
            versions[name] = h
            out.append(text)
        except Exception as e:
            print(f"[SMS] ⚠️ {name} block failed: {e}", flush=True)
    return out


def _build_materials_block(user_id):
    """## Their materials — Theo's read AND the user's own account,
    side by side. '' when nothing is registered. The user's words
    always outrank the digest: the digest knows what the file
    contains, only the user knows why it exists."""
    mats = db.get_user_materials(user_id)
    if not mats:
        # Absence is not an assertion. With no block at all, the
        # model filled the silence from the conversation's narrative
        # — observed live: the user said "올려놓을게", a day passed,
        # and the evening cron opened with "워드파일 올려놓은 거
        # 읽어봤어" about a file that was never uploaded. State the
        # emptiness explicitly.
        block = (
            "## Their materials — NONE\n\n"
            "Their /my page is empty right now. Nothing has been "
            "shared — no file, no link — whatever the conversation "
            "says or promises. A promise to upload is not an upload. "
            "You have read NOTHING of theirs — never speak as if "
            "you have. (Never open with 'I read your file' or "
            "'파일 읽어봤어' — nothing exists to read.)")
        # An empty page reads two ways, and only the stored alignment
        # tells them apart. Unsettled ('') means the question is still
        # open. Settled no_material means the emptiness IS the answer
        # — observed live: coaches kept treating it as a to-do and
        # re-asking for an upload the user had already said cannot
        # exist. This rider must stand in EVERY conversation, not just
        # the alignment turn, so it lives here rather than in a focus.
        if (db.get_onboarding_state(user_id)["material_status"]
                == "no_material"):
            block += (
                "\n\n"
                "And for THIS user, the empty page is not a gap: they "
                "are SETTLED as studying without a material — a stored "
                "decision, not something still missing. Never ask them "
                "to upload, share, link, or register anything, in any "
                "conversation, ever; the ask-to-upload move does not "
                "exist for this user. Your offer works without a "
                "material. If THEY bring up a material on their own, "
                "engage with it naturally — the settled answer can "
                "change, but only they change it.")
        return block
    lines = ["## Their materials — what they study from",
             "",
             "Two readings live here: your one-time digest (what the "
             "material contains) and their own walkthrough account "
             "(what it is FOR). Where they differ, THEIR words win — "
             "and the gap itself is worth a conversation.",
             ""]
    label = {"none": "not walked through yet",
             "in_progress": "walkthrough in progress",
             "validated": "walked through — sample confirmed by them"}
    for m in mats:
        head = m.get("title") or m.get("source_url") or "(unnamed)"
        lines.append(f"### {head} ({m['kind']}) — "
                     f"{label.get(m['walkthrough_status'], '?')}")
        if m.get("source_url") and m["kind"] == "link":
            lines.append(f"- Link: {m['source_url']} (you have not "
                         "fetched it; rely on what you know of it and "
                         "on their account)")
        if m.get("digest"):
            lines.append(f"- Your read: {m['digest']}")
        if m.get("user_description"):
            lines.append(f"- Their description: {m['user_description']}")
        for w in (m.get("wants") or []):
            q = (w.get("quote") or "").strip()
            mn = (w.get("meaning") or "").strip()
            if q:
                lines.append(f"- They said: \"{q}\""
                             + (f" → {mn}" if mn else ""))
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_focus_block(user_id, dormancy=True):
    """What THIS message is for — the one block that changes turn to
    turn. Precedence: dormancy gate > onboarding checklist > sequence
    assignment. Dormancy overrides everything; an incomplete
    onboarding shows the checklist (there is no plan yet during
    onboarding); a completed user gets the plan assignment.

    dormancy=False on the reply path: the user just messaged us, so
    by definition they are not dormant.
    """
    if dormancy and _is_dormant(user_id):
        return _build_dormant_block(user_id)
    # Sequence-plan assignments retired (2026-08-12, PR-A): the
    # machinery was idling (no active plan on any live user) and the
    # step-sequence frame is shelved. Post-onboarding, the mode
    # prompt and context blocks carry the message's job.
    return _build_onboarding_block(user_id)


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
    """ARCHIVED (2026-08-12, PR-A): ignition judgments retired with
    the ignition→flow frame. Kept as a pure stripper so a model
    that still emits [IGNITION: n] from habit never leaks it to the
    user; nothing is recorded."""
    if _IGNITION_SCORE_RE.search(text):
        print(f"[SMS] stray [IGNITION] marker ({trigger}) — ignition "
              f"is retired; stripped", flush=True)
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


_DAY_TOKENS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4,
               "sat": 5, "sun": 6}
_DAY_SETS = {"daily": None, "weekdays": [0, 1, 2, 3, 4],
             "weekends": [5, 6]}


def parse_schedule_windows(raw):
    """'20:00-22:00, 08:00-08:30' → [{'start','end'[,'days']}] or []
    if any token is malformed (all-or-nothing: a half-parsed schedule
    would silently mis-time sends). Shared with the analysis call.

    A token may carry a day scope: '20:00-20:15@weekdays',
    '@weekends', or '@mon,wed,fri'. No scope = every day. Added for
    the operator's own live request ('앞으로 주말은 보내지 마') —
    a schedule is a weekly table, not a daily alarm."""
    tokens = [t.strip() for t in (raw or "").split(",") if t.strip()]
    # day lists are comma-free in raw ('@mon wed fri' or '@mon+wed');
    # accept +, space, or / as separators inside the day part
    windows = []
    for t in tokens:
        part, _, dayspec = t.partition("@")
        m = _WINDOW_RE.match(part.strip())
        if not m:
            return []
        w = {"start": f"{int(m.group(1)):02d}:{m.group(2)}",
             "end": f"{int(m.group(3)):02d}:{m.group(4)}"}
        dayspec = dayspec.strip().lower()
        if dayspec:
            if dayspec in _DAY_SETS:
                if _DAY_SETS[dayspec] is not None:
                    w["days"] = _DAY_SETS[dayspec]
            else:
                days = []
                for d in re.split(r"[+/\s]+", dayspec):
                    if d not in _DAY_TOKENS:
                        return []
                    days.append(_DAY_TOKENS[d])
                if not days:
                    return []
                w["days"] = sorted(set(days))
        windows.append(w)
    return windows


# Extraction markers are NO LONGER acted on here (brief §7 "Markers
# vs. the analysis call"): those fields are recovered from the
# transcript by analyze_turn, which is single-task, sees the whole
# conversation, and is re-runnable. The generation model may still
# emit them out of habit (they sit in its own history), so we strip
# them so nothing leaks to the user — and log it, since a rising
# count means the prompt still teaches them.
_EXTRACTION_MARKER_RES = (
    _GOAL_MARKER_RE, _COMMIT_MARKER_RE, _PATH_MARKER_RE,
    _SCHEDULE_MARKER_RE, _IGNITION_DEF_RE,
)

# The model sees server turns wrapped in <server_instruction> tags in
# its history and occasionally roleplays one into its own reply —
# observed live 2026-08-09: a full instruction block, tags and all,
# reached a user's phone. Same imitation family as time prefixes.
_SERVER_INSTRUCTION_RE = re.compile(
    r"<server_instruction>.*?(?:</server_instruction>|\Z)\s*", re.S)


def _strip_extraction_markers(user_id, text):
    stripped = []
    if _SERVER_INSTRUCTION_RE.search(text):
        stripped.append("server_instruction")
        text = _SERVER_INSTRUCTION_RE.sub("", text)
    for rx in _EXTRACTION_MARKER_RES:
        if rx.search(text):
            stripped.append(rx.pattern.split(":")[0].strip("[\\"))
            text = rx.sub("", text)
    if stripped:
        print(f"[SMS] stripped stale extraction markers {stripped} "
              f"(analysis call owns these now)", flush=True)
        db.log_event(user_id, "stale_marker_stripped",
                     {"markers": stripped}, source="sms")
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


def _expectation_due(user_id):
    """True when the fixed expectation-setting message (checklist v2
    item 1) must go out before anything else does."""
    state = db.get_onboarding_state(user_id)
    return (not state["completed_at"]
            and "expectation_setting" in state["missing"])


def send_expectation_message(user_id, to_number, trigger):
    """Deliver the fixed expectation-setting text (identical for
    every user, from prompts/expectation_setting.md), stamp the
    checklist item, record everything. SERVER-SENT by design: the one
    onboarding item with zero LLM variance. Returns the text, or None
    if sending failed."""
    text = _read_prompt("expectation_setting").strip()
    if not text:
        print("[SMS] ⚠️ expectation_setting.md is empty — not sending",
              flush=True)
        return None
    send_sms(to_number, text, user_id=user_id)
    db.save_sms_message(user_id, "assistant", text, "out")
    db.set_expectation_sent(user_id, source="sms")
    db.log_event(user_id, "sms_out",
                 {"text": text, "trigger": trigger,
                  "server_sent": True}, source="sms")
    return text


def ensure_my_link_delivered(user_id):
    """When the onboarding focus reaches the material items with no
    materials, no link email on record, and an email address on file
    — send the /my link email NOW, so the coach can honestly say
    '메일함 봐봐'. Since activation started sending the welcome email
    (which carries the link and logs my_link_emailed), this is the
    safety net for users activated without an email address whose
    address arrived later. Without it, the coach improvises a
    tokenless URL (observed live: 'learningtheo.com/my에 올려줄래?' —
    a 404 for the user). Idempotent via the my_link_emailed event.
    Never raises."""
    try:
        state = db.get_onboarding_state(user_id)
        if state["completed_at"] or not state["missing"] \
                or state["missing"][0] not in ("material_alignment",
                                               "material_understanding"):
            return
        if db.get_user_materials(user_id):
            return
        if any(e["kind"] == "my_link_emailed"
               for e in db.get_events(user_id, limit=300)):
            return
        email = ((db.get_user_profile_by_id(user_id) or {})
                 .get("email") or "").strip()
        if "@" not in email:
            return
        import emailer
        emailer.send_my_link(user_id, email)
    except Exception as e:
        print(f"[SMS] ⚠️ my-link auto-delivery failed: {e}", flush=True)


def _link_pointer(user_id):
    """How the coach may refer to the /my upload link, by whether an
    email carrying it has actually gone out."""
    emailed = any(e["kind"] == "my_link_emailed"
                  for e in db.get_events(user_id, limit=300))
    return ("you already emailed them their private page link — "
            "point them at their INBOX ('메일함 봐봐, 내가 링크 "
            "하나 보내놨어'), never paste the link itself into a "
            "text"
            if emailed else
            "their /my page takes a file or a link, but do NOT "
            "send that link over SMS — it reaches them by email")


def _alignment_label(user_id):
    """The material_alignment focus: settle WHAT they study from —
    or that nothing exists yet. Both answers are fine; the point is
    that it becomes stored fact instead of per-turn guessing."""
    return (
        "settle what they actually study from — a file, notes, a "
        "video, a course, a book — or that nothing exists yet. Both "
        "answers are equally good outcomes; this is a question about "
        "their reality, not a push to produce something. If they "
        "HAVE something: " + _link_pointer(user_id) + ". If it is "
        "unsharable (a book, a course, an app), have them NAME it — "
        "the name becomes the anchor. If there IS no material yet — "
        "they said so; believe them. Do NOT recite an ask-to-see "
        "line at them (observed live: the coach said '네가 얘기한 그 "
        "자료, 직접 보면—' to a user who had JUST said they have "
        "nothing — a template parrot, and it reads as not "
        "listening). No material simply means your offer will be "
        "built without one — perhaps agreeing together what the "
        "first material should be, perhaps something YOU build for "
        "them piece by piece (drafting a concept guide is within "
        "your stock: you know things)")


def _walkthrough_label(user_id):
    """The material_understanding focus (only reached once alignment
    settled on has_material), phased by what exists. Material not yet
    shown: get them to show or name the thing. After: lead the
    walkthrough to a validated sample."""
    mats = db.get_user_materials(user_id)
    if not mats:
        return (
            "they told you a material exists but nothing is "
            "registered yet. FIRST read the conversation for which "
            "situation this is — never assume:\n"
            "  (a) They HAVE a material they haven't shown (they "
            "mentioned notes, a file, a video they study from) → ask "
            "to SEE it, with the honest reason that seeing it makes "
            "you precise; " + _link_pointer(user_id) + ".\n"
            "  (b) They study from something unsharable (a book, a "
            "course, an app) → have them NAME it and tell you what "
            "it covers — the name becomes the anchor")
    m = mats[0]
    named = m.get("title") or "their material"
    if db.get_active_screen_session(user_id):
        return (
            f"{named} is on their screen — you are looking at it "
            "TOGETHER, live. The goal: understand what they want from "
            "this material well enough to actually perform your offer "
            "on it. How this conversation works:\n"
            "  - Ask them to show you with EXAMPLES, not abstractions "
            "— '어떤 게 되면 좋겠는지 예를 하나 들어줄래? 실제로 "
            "있었던 상황이면 더 좋고.' Their examples are the gold; "
            "abstract wants are lead.\n"
            "  - When YOU ask about the material, pin the exact spot "
            "— name the section/page/heading from the observations "
            "('3.2 정산 기준 표에서—'), never a vague '이 부분'. "
            "Questions about what the material contains are wasted "
            "turns (you can see it); ask about what only they know — "
            "where this material meets their life: what moment it is "
            "FOR, what keeps not happening, what 'done' or 'good' "
            "would look like in their world (for some users that is a "
            "person who asks them things; for others a deadline, a "
            "build, a test — take whichever shape THEIR examples "
            "reveal, never assume one).\n"
            "  - One question a turn. Their words outrank your digest "
            "and the screen both.\n"
            "  - Test your understanding with a SAMPLE of your offer "
            "(an insider-plausible question, a concrete next piece) "
            "and let them judge it.\n"
            "  - Closing: when their examples stop surprising you, "
            "that is your cue to TEST — not to close. The ONLY "
            "license to close is the sample earning their '맞아, 딱 "
            "그런 거'. Your own sense that you could perform the "
            "offer licenses nothing (you will always feel able — "
            "that is exactly why the user, not you, judges the "
            "sample). Once the sample has passed, close cleanly and "
            "declare it: '이 정도면 다 파악했어. 여기까지 하자 — "
            "이제 내가 뭘 할지 알겠어.' Do not let the walkthrough "
            "trail on past its purpose")
    return (
        f"lead a walkthrough of {named} — in THEIR words, not yours. "
        "One goal: their own account of what they want from it, "
        "precise enough that you could build your standing offer from "
        "it. What that is varies completely by person — a moment they "
        "must perform in, a piece they keep avoiding with a deadline, "
        "a situation they want to survive. Do NOT arrive with a "
        "question template; follow what they show you, one question a "
        "turn, and remember your digest of the material is YOUR "
        "reading — theirs outranks it. You are done only when you can "
        "produce a SAMPLE of what you would do for them (an "
        "insider-plausible question, a concrete next-piece cut — "
        "something an expert in their world would nod at, never trivia "
        "an outsider would ask) and they confirm it rings true. Name "
        "the trade honestly: '이렇게 물을 거라고 생각했는데, 맞는지 "
        "봐줘. 네가 검증해주면 내가 빨리 네가 원하는 역할을 해줄 수 "
        "있어.' If they say that is not how it works, that is the "
        "walkthrough WORKING — keep walking. When nothing they say "
        "surprises you anymore, that is your cue to test a sample — "
        "and only its '맞아, 딱 그런 거' licenses you to close: "
        "'다 파악했어, 여기까지 하자.' Your own sense of readiness "
        "licenses nothing")


def _offer_label(user_id):
    """The offer focus: what Theo will do for them, ongoing. Branches
    on where the raw material for the proposal comes from — a settled
    no_material user has no walkthrough and no confirmed sample, so
    pointing the coach at those left it with nothing to build from."""
    if db.get_onboarding_state(user_id)["material_status"] != "no_material":
        return ("what YOU will do for them, ongoing. PROPOSE it "
                "— never ask them what you could do. Build it "
                "directly from the walkthrough: their own words "
                "about what they want, and the sample they "
                "already confirmed. Usually the honest shape is "
                "'that thing I just did that you said rings true "
                "— I will keep doing it, like this, at your "
                "times.' '내가 뭘 도와줄까?' is the failure mode: "
                "it hands your job back to them and costs a long "
                "answer they have no reason to write")
    return ("what YOU will do for them, ongoing — knowing there is "
            "no material walkthrough to build from here. Build the "
            "proposal directly from their goal, their ignition "
            "marker, and everything the conversation has shown you. "
            "PROPOSE it proactively, within your capability stock: "
            "showing up at their times, remembering, your own "
            "knowledge, sitting with them in sessions they start. "
            "If you are not yet confident what the right offer is, "
            "do NOT ask an open '내가 뭘 도와줄까?' — that is the "
            "failure mode here exactly as it is for material users; "
            "it hands your job back to them. Instead ask ONE "
            "targeted discovery question about where this learning "
            "keeps failing in their actual days, and build the "
            "proposal from the answer. And never ask them to "
            "produce or upload a material — that rule stands here "
            "too")


def _build_onboarding_block(user_id):
    """The checklist block (P0-A): injected into every call while
    onboarding is incomplete, so the conversation keeps steering
    toward the missing fields. Empty string once completed."""
    state = db.get_onboarding_state(user_id)
    if state["completed_at"]:
        return ""
    label = {"expectation_setting": "(server-delivered — the fixed "
                                    "expectation message goes out "
                                    "automatically; never try to "
                                    "deliver or restate it yourself)",
             "goal": "their goal, in their own words",
             "schedule": "the times of day they want to hear from you",
             "material_alignment": _alignment_label(user_id),
             "material_understanding": _walkthrough_label(user_id),
             "offer": _offer_label(user_id)}
    # expectation_setting is the server's job (a fixed message, sent
    # mechanically) — it is never the LLM's focus. Normally it is
    # already sent before any LLM call runs; the web-chat path is the
    # exception, where the next SMS touchpoint will deliver it.
    missing = [f for f in state["missing"] if f != "expectation_setting"]
    lines = [
        "## Onboarding — what is still unsettled",
        "",
        "Settled so far: " + (", ".join(state["filled"]) or "(nothing yet)"),
    ]
    if missing:
        lines += ["",
                  "This block outranks everything below it. While "
                  "onboarding is incomplete, the focus item is what "
                  "this message must be spent on — the notes, the "
                  "trajectory and the persona all describe HOW to say "
                  "it, and none of them replaces WHAT it is for. If "
                  "something further down suggests a different move, "
                  "that move is for a later turn.",
                  "",
                  f"**This message's focus: {label[missing[0]]}.**",
                  "Work only that one. The rest are for later turns:"]
        for f in missing[1:]:
            lines.append(f"- {label[f]}")
    lines += [
        "",
        "Get REAL agreement on the focus item, one at a time, at "
        "conversational pace. Take as long as it takes: a long, "
        "wandering conversation is a good outcome here, not a delay.",
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
    """Parse & act on plan markers; return stripped text.

    P0-D: cursor movement is now the dedicated judge's job
    (_judge_step_completion, run BEFORE reply generation). A stray
    [ADVANCE]/[STAY] from the generation model is stripped and
    ignored — one judgment, one authority. [REPLAN:] remains the
    generation model's channel for flagging plan misfit."""
    if _ADVANCE_RE.search(text):
        print(f"[PLAN] stray [ADVANCE] from generation ({trigger}) — "
              f"ignored; the judge owns the cursor", flush=True)
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


# WhatsApp's 24-hour customer-service window: outside it, a
# business-initiated free-form message is refused by WhatsApp — but
# Twilio ACCEPTS the API call, so our code sees success and logs a
# send that nobody received. Observed with pilot user #1: a send 33h
# after his last message was recorded as delivered on our side and
# never reached him. Ghost sends poison the data twice (a phantom
# outbound, and a "no reply" that was never possible), so we refuse
# them at the source. This whole class of problem disappears on SMS.
WHATSAPP_WINDOW_H = 24


def whatsapp_window_closed(user_id):
    """True when the channel is WhatsApp and the user has been silent
    longer than the free-form window allows. A user who has never
    written is also outside it (their first inbound opens it).

    An operator-recorded reopening counts as a user message, because
    it stands for one we cannot see: Twilio's sandbox consumes the
    `join <code>` text itself and never forwards it to our webhook,
    so a user can legitimately reopen their window while our record
    still shows silence. See mark_whatsapp_window_open."""
    if os.environ.get("MESSAGING_CHANNEL", "sms").lower() != "whatsapp":
        return False
    hours = _dormancy_hours(user_id)
    override = db.get_last_event(user_id, "whatsapp_window_opened")
    if override:
        try:
            override_h = (datetime.now()
                          - datetime.fromisoformat(override["ts"])
                          ).total_seconds() / 3600
            hours = override_h if hours is None else min(hours, override_h)
        except Exception:
            pass
    return hours is None or hours >= WHATSAPP_WINDOW_H


# Planner-chosen silence is SUSPENDED (operator decision 2026-08-01).
# The rationale it kept giving — "their last question is still
# unanswered" — is true after essentially every conversation: people
# answer what matters and go to bed without replying to the final
# turn. Treated as a signal, that makes holding the permanent state
# Planner-chosen silence (hold) RETIRED (2026-08-12, operator
# decision): it was already suspended by default after two
# consecutive holds cost a WhatsApp window; the suspension is now
# permanent. Silence belongs to the SERVER (pause, schedule,
# dormancy gate, closed WhatsApp window) — the planner always
# writes.


def _hold_cap_block(user_id):
    """The standing must-send block for scheduled sends."""
    return ("## This send must produce a message\n\n"
            "Choosing silence is not available to you. If the "
            "moment feels wrong — they are at work, their last "
            "question is still hanging — that shapes WHAT you "
            "write (something small, warm, easy to leave "
            "unanswered), not WHETHER you write. Genuine silence "
            "is decided by the server before you are called.")


def mark_whatsapp_window_open(user_id, note=""):
    """Operator override: record that the user has re-joined / messaged
    the sandbox, reopening their 24h free-form window. Stamped now, so
    it expires on its own like a real inbound would — this cannot
    permanently disable the gate."""
    db.log_event(user_id, "whatsapp_window_opened",
                 {"note": note[:300] or "operator confirmed the user "
                                        "re-joined the sandbox"},
                 source="admin")
    print(f"[SMS] WhatsApp window manually reopened for {user_id}",
          flush=True)


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


# Step-completion judge (P0-D). Side-duty judgments were observably
# dropped by the generation call, so completion is a DEDICATED
# single-task LLM call that runs BEFORE the reply prompt is built —
# the generation then receives the already-updated assignment. Code
# moves the cursor; the judge only answers one question.
# (Sequence-plan machinery — _check_plan_deviation and
# _build_plan_block — ARCHIVED 2026-08-12, PR-A. The step-sequence
# frame is shelved: no live user had an active plan, so removal
# changes no behavior. sequence_plans data and accessors remain in
# db.py; restore from git history if a sequence-shaped user appears.)


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


# ─── Send guards (mechanical, no LLM) ───────────────────────────────
#
# Pilot user #1 stopped replying at exactly the message that asked two
# open-ended things at once. Question burden is the one conversational
# rule we can enforce objectively, so we do: count question marks
# across the whole outbound (bubbles included). One regeneration
# attempt with the violation named; if it still violates we SEND
# ANYWAY and log it — a coach that goes silent is worse than a coach
# that asks two questions — and the violation rate becomes data.

MAX_QUESTIONS = 1
_QUESTION_RE = re.compile(r"[?？]")

# The role-swap phrasing family: "~해놓은 거" speaks as the party who
# DID the action ("문자 보내놓은 거 확인했어?" = I sent, you check).
# Observed (eval C1, ~1/5 generations): "자료 올려놓은 거 확인했어?"
# — the coach speaking as if IT had uploaded something, to a user
# whose upload never arrived. Only the user can upload; with zero
# materials registered this phrasing cannot be honest from anyone,
# so it is mechanically checkable — the narrow-guard exception to
# "hallucinations are judge work".
_UPLOADED_THING_RE = re.compile(
    r"올려\s*(?:놓은|둔|준)\s*(?:거|파일|자료)"
    # The same receipt claim in English — the prompt went English-
    # native (2026-08-12) and the original incident promptly
    # reappeared as "Hey — I just read through your file." Only
    # first-person past-tense READ claims match; "I'll read your
    # file (once you upload it)" is a legitimate promise.
    r"|\bI(?:'ve|\s+have)?(?:\s+just|\s+already)?\s+"
    r"(?:read|reviewed|looked\s+through|went\s+through|"
    r"finished(?:\s+reading)?)\s+(?:through\s+)?"
    r"(?:your|the)\s+(?:files?|docs?|documents?|notes?|materials?)",
    re.IGNORECASE)
_UPLOAD_GUARD_MSG = (
    "Only the USER can upload, to their /my page — you never upload "
    "anything, and nothing has been uploaded by anyone. You have "
    "read NOTHING of theirs ('I read your file' is a fabrication). "
    "If you mention the upload, ask whether THEY have done it yet "
    "('자료 올렸어?'). Remove every reference to an already-uploaded "
    "or already-read thing.")

# [HOLD: "reason"] — deliberate silence needs a recorded WHY. Sending
# nothing is a real intervention; without a reason in the log the
# operator cannot tell a considered hold from a broken pipeline.
_HOLD_REASON_RE = re.compile(r'\[HOLD:\s*"([^"]{3,300})"\s*\]', re.DOTALL)


def _process_hold_reason(text):
    """→ (reason_or_None, text_without_the_marker)."""
    m = _HOLD_REASON_RE.search(text)
    if not m:
        return None, text
    return m.group(1).strip(), _HOLD_REASON_RE.sub("", text).strip()


def check_send_guards(text, steps, user_id=None):
    """→ list of violation strings ([] = clean). `user_id` enables
    the state-conditioned guards (they read the DB)."""
    violations = []
    n = len(_QUESTION_RE.findall(text or ""))
    if n > MAX_QUESTIONS:
        violations.append(
            f"{n} questions in one message — ask exactly one. Keep the "
            f"single most important question and drop the rest (they "
            f"can come in later turns).")
    # (missing-[STEP:] violation removed — step tagging retired with
    # the step surfaces, exp/step-surface-removal.)
    if (user_id and _UPLOADED_THING_RE.search(text or "")
            and not db.get_user_materials(user_id)):
        violations.append(_UPLOAD_GUARD_MSG)
    return violations


def generate_message(user_id, system_prompt, history, trigger,
                     max_tokens=500, prompt_versions=None):
    """Call the model, process its markers, and enforce the send
    guards with ONE regeneration attempt.

    Returns (text, steps, expect, llm_call_id) or (None, ...) if the
    call itself failed. A message that still violates after the retry
    is returned anyway — silence is worse — with a
    send_guard_violation event recording what slipped through.
    """
    client = anthropic.Anthropic()
    attempt_history = list(history)
    text = steps = expect = llm_call_id = None
    violations = []

    for attempt in (1, 2, 3):
        try:
            resp = client.messages.create(
                model=MODEL, max_tokens=max_tokens,
                system=system_prompt, messages=attempt_history)
            raw = resp.content[0].text.strip()
        except Exception as e:
            print(f"[SMS] ❌ Claude call failed on {trigger}: {e}",
                  flush=True)
            db.log_event(user_id, "llm_error",
                         {"where": trigger, "error": str(e)[:300]},
                         source="sms")
            return None, [], None, None, None

        # Flight-record BEFORE any stripping (T2b): the record shows
        # exactly what the model produced, markers included.
        llm_call_id = db.save_llm_call(
            user_id, trigger if attempt == 1 else f"{trigger}_retry",
            MODEL, system_prompt, attempt_history,
            prompt_versions or {}, raw)

        text = _strip_extraction_markers(user_id, raw)
        text = _process_ignition_markers(user_id, text, trigger=trigger)
        text = _process_plan_markers(user_id, text, trigger=trigger)
        expect, text = _process_expect_marker(text)
        hold_reason, text = _process_hold_reason(text)
        steps, text = _process_step_marker(user_id, text)

        # Planner-chosen silence is RETIRED (2026-08-12): an empty
        # body is never a valid answer. One regeneration; a model
        # that refuses to write twice yields nothing to send — the
        # send skips loudly instead of fabricating.
        if not text.strip():
            if attempt >= 2:
                print("[SMS] ⚠️ empty body twice — nothing to send",
                      flush=True)
                db.log_event(user_id, "hold_while_suspended",
                             {"reason": hold_reason or "(none)",
                              "llm_call_id": llm_call_id},
                             source="sms")
                return None, steps, expect, llm_call_id, hold_reason
            print("[SMS] empty body — regenerating", flush=True)
            attempt_history = attempt_history + [
                {"role": "assistant", "content": raw},
                _server_turn("You may not choose silence — this send "
                             "must produce a message. Send something "
                             "small and easy to leave unanswered.")]
            continue

        violations = check_send_guards(text, steps, user_id=user_id)
        # Two rewrite attempts for guard violations (was one): the
        # one-question rule failed through a single retry twice in
        # one live evening — both the draft AND its rewrite carried
        # two questions.
        if not violations or attempt == 3:
            break
        print(f"[SMS] guard violation, regenerating: {violations}",
              flush=True)
        tail = "\nRewrite the SAME message."
        if any("questions in one message" in v for v in violations):
            tail += (" Keep the one most important question exactly; "
                     "every other question must become a statement "
                     "or disappear — do not merge them into a "
                     "bigger question.")
        attempt_history = attempt_history + [
            {"role": "assistant", "content": raw},
            _server_turn("Your draft broke a hard rule:\n- "
                         + "\n- ".join(violations) + tail)]

    if violations:
        print(f"[SMS] ⚠️ sending despite violations: {violations}",
              flush=True)
        db.log_event(user_id, "send_guard_violation",
                     {"violations": violations, "trigger": trigger,
                      "llm_call_id": llm_call_id}, source="sms")
    return text, steps, expect, llm_call_id, hold_reason


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
    text = re.sub(r"(\n?---\s*)+$", "", text).strip()
    # History turns carry server annotations like "[수요일 14:51,
    # 지금]"; the model imitates the format and prefixes its OWN
    # output (observed live: users received texts starting
    # "[수요일 14:51] ..."). Same failure family as marker
    # self-imitation — strip any leading bracket annotation.
    text = re.sub(r"^\[[^\]\n]{1,40}\]\s*", "", text)
    return steps, text


# _process_commit_marker is gone: [GOAL:] and [COMMIT:] were
# extraction markers and the analysis call owns those fields now.
# The operator rescue endpoints (/sms/set-goal, /sms/set-bite) still
# write them directly, and db.commit_first_bite still exists for the
# forced-transition rescue path.


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
    """Map an inbound phone number to a user_id.

    The DB (user_profiles.phone) is the source of truth; the
    TUTOR_USER_* env pair stays as a fallback so the pilot user's
    routing never breaks mid-migration. DB wins on conflict — a
    number bound in the DB routes there even while the env still
    points somewhere.
    """
    incoming = _strip_channel(from_number.strip())
    bound = db.get_user_by_phone(incoming)
    if bound:
        return bound
    expected = os.environ.get("TUTOR_USER_PHONE", "").strip()
    user_id = os.environ.get("TUTOR_USER_ID", "").strip()
    if not (expected and user_id):
        return None
    if incoming != _strip_channel(expected):
        print(f"[SMS] inbound from unknown number {from_number} (normalized {incoming}), ignoring", flush=True)
        return None
    return user_id


# Per-user reply locks: a user who sends three texts in a burst gets
# ONE answer that has read all three, not three interleaved replies
# (observed live: two coach replies 19 seconds apart to a two-text
# burst). In-process locks are sufficient — the service runs as a
# single instance.
_inbound_locks = {}
_inbound_locks_guard = __import__("threading").Lock()


def _inbound_lock(user_id):
    with _inbound_locks_guard:
        if user_id not in _inbound_locks:
            _inbound_locks[user_id] = __import__("threading").Lock()
        return _inbound_locks[user_id]


def handle_inbound(from_number, body):
    """Process an inbound SMS. Returns the text we replied with (or
    None if we chose not to reply).

    Burst folding: the message is saved immediately, then the reply
    runs under a per-user lock with two freshness checks — on entry
    (a newer message already exists → this handler folds; the newer
    one answers everything) and after generation (a newer message
    arrived WHILE generating → the drafted reply is discarded rather
    than sent stale). Every burst thus gets exactly one reply,
    written with the full burst in view."""
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
    my_msg_id = db.get_last_user_message_id(user_id)

    with _inbound_lock(user_id):
        return _reply_to_inbound(user_id, from_number, body, my_msg_id)


def _reply_to_inbound(user_id, from_number, body, my_msg_id):
    latest = db.get_last_user_message_id(user_id)
    if latest != my_msg_id:
        # A newer message arrived while we waited for the lock — its
        # handler will answer the whole burst.
        db.log_event(user_id, "inbound_folded",
                     {"text": body[:120]}, source="sms")
        return None

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

    # Checklist v2 gate: a user who texts in before the first cron
    # touch still gets the fixed expectation message first, as its
    # own bubble; the LLM reply follows with it already in history.
    if _expectation_due(user_id):
        send_expectation_message(user_id, from_number,
                                 "inbound_expectation")

    # If the user is mid-study with the observer running, grab a
    # fresh screen capture before building the reply context.
    _request_fresh_screen(user_id)

    # Scope history to the current phase so old conversations from
    # before a phase transition don't bleed in.
    phase_state = db.get_user_phase(user_id)
    history = db.get_recent_sms_messages(
        user_id, limit=HISTORY_LIMIT, since=phase_state["phase_started_at"],
        with_time=True
    )
    # `history` ends with the user message we just inserted, which is
    # what the Anthropic API expects (last message = user turn).

    # P0-D: judge the current plan step against this reply BEFORE
    # building the prompt — the generation call receives the
    # already-advanced assignment (judge is a no-op for users without
    # a plan or mid-onboarding).
    # One analysis pass: extracts onboarding fields from the whole
    # transcript AND judges the current plan step (brief §7). Runs
    # before the prompt is built so generation sees updated state.
    import analyze_turn
    analyze_turn.analyze(user_id, trigger="inbound")

    # v3 four-signals pass: if an open drill question is outstanding
    # and this message answers it, grade BEFORE the reply is written —
    # the attempt, the prediction score, and the item status land in
    # the ledgers, and the reply prompt gets the verdict plus the
    # anchor quote so any correction comes from the user's own
    # material, never model memory.
    import drill
    drill_graded = drill.grade_if_answering(user_id)

    # A graded answer chains the NEXT question through the bank —
    # the husband's own protocol ("You can ask me more questions to
    # the extent I answer your prior questions"). Without this, the
    # reply path free-generates follow-up quizzes with no anchor,
    # no prediction, no grading (observed 2026-08-13: a fabricated
    # 'broker confirmation' premise attributed to his file). The
    # prediction is recorded BEFORE the reply is written, same as a
    # scheduled send.
    drill_next = None
    if drill_graded:
        try:
            drill_next = drill.prepare_scheduled_question(user_id)
        except Exception as e:
            print(f"[SMS] ⚠️ follow-up prepare failed: {e}",
                  flush=True)

    # Use the phase-specific evening prompt for inbound replies too —
    # the LLM should be in the same mode whether the user is replying
    # to a scheduled ping or texting spontaneously.
    ensure_my_link_delivered(user_id)
    system_prompt, prompt_versions = _build_system_prompt_for_reply(
        user_id, drill_graded=drill_graded, drill_next=drill_next)

    reply_text, steps, expect, llm_call_id, _hold = generate_message(
        user_id, system_prompt, history, "inbound_reply",
        max_tokens=400, prompt_versions=prompt_versions)
    if reply_text is None:
        return None
    if db.get_last_user_message_id(user_id) != my_msg_id:
        # They typed again while we were generating: this draft never
        # saw that message. Sending it would answer the past —
        # discard, and let the newer handler (waiting on the lock)
        # answer everything.
        db.log_event(user_id, "reply_discarded_stale",
                     {"draft": reply_text[:160],
                      "llm_call_id": llm_call_id}, source="sms")
        return None
    # Field fills above may have completed the checklist — code, not
    # the LLM, makes that call. Completion fires the initial plan
    # generation in the background (P0-B) so this reply isn't
    # delayed; the operator reviews /plan + /notes before the first
    # sequence-mode send.
    if db.check_and_complete_onboarding(user_id):
        import genplan
        genplan.generate_async(user_id)
    db.mark_onboarding_started(user_id)

    # Answer-leak guard on a bank-served follow-up, same rule as the
    # scheduled path: one rewrite, and if it still leaks, send the
    # reply WITHOUT the question part being trusted — log loudly.
    if drill_next and reply_text.strip() \
            and drill.leaks_answer(reply_text, drill_next["item"]):
        db.log_event(user_id, "drill_answer_leak",
                     {"draft": reply_text[:300],
                      "llm_call_id": llm_call_id,
                      "item_id": drill_next["item"]["id"],
                      "followup": True, "attempt": 1}, source="sms")
        history2 = history + [
            {"role": "assistant", "content": reply_text},
            _server_turn("Your draft contains the answer key of the "
                         "next drill item (anchor/rubric contents). "
                         "Rewrite: feedback on their answer, then "
                         "ONLY the question.")]
        retry_text, steps, expect, llm_call_id, _h = generate_message(
            user_id, system_prompt, history2, "inbound_reply_leak_retry",
            max_tokens=400, prompt_versions=prompt_versions)
        if retry_text:
            reply_text = retry_text

    send_sms(from_number, reply_text, user_id=user_id)
    db.save_sms_message(user_id, "assistant", reply_text, "out")
    db.log_event(user_id, "sms_out",
                 {"text": reply_text, "trigger": "inbound_reply",
                  "prompt_versions": prompt_versions,
                  "llm_call_id": llm_call_id,
                  "steps": steps, "expect": expect,
                  "phase": db.get_user_phase(user_id)["phase"]},
                 source="sms")
    if drill_next:
        db.log_event(user_id, "drill_question_sent",
                     {"item_id": drill_next["item"]["id"],
                      "prediction_id": drill_next["prediction_id"],
                      "reask": drill_next["reask"],
                      "why": drill_next["why"], "followup": True,
                      "llm_call_id": llm_call_id}, source="sms")
    # Life-track ops hop: AFTER the reply (so it sees the coach's
    # confirmation in history), in the background (extraction must
    # not delay the conversation), and only for lane-open users —
    # tracks_ops.run() itself re-checks the gate, so this costs the
    # husband's path nothing but a boolean.
    if db.tracks_lane_open(user_id):
        import threading

        import tracks_ops
        threading.Thread(target=tracks_ops.run, args=(user_id,),
                         kwargs={"trigger": "inbound"},
                         daemon=True).start()
    return reply_text


def _build_system_prompt_for_reply(user_id, drill_graded=None,
                                   drill_next=None):
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
    context, ctx_versions = _build_context_blocks(
        user_id, focus_block=_build_focus_block(user_id, dormancy=False))
    versions = {"sms_shared": h_shared, mode_name: h_mode,
                **ctx_versions}
    parts = [context, rendered_shared]
    import drill
    if drill_graded:
        parts.append(drill.graded_reply_block(drill_graded))
        if drill_next:
            parts.append(drill.followup_block(drill_next))
    try:
        if not drill_next and drill.active_drill_track(user_id):
            # The Wednesday hole: with no served item in hand, the
            # reply path used to freelance quiz questions (no bank,
            # no anchor, no prediction) — one embedded a fabricated
            # premise and attributed it to the user's file. Served
            # item or no question.
            parts.append(
                "## No freelance drill questions\n\n"
                "Drill questions for this user come ONLY from "
                "server-selected bank items. No item block in this "
                "prompt = no new drill question in this reply — "
                "respond conversationally instead (clarifying "
                "questions about what THEY said are fine). If they "
                "want another question, tell them one is coming; "
                "the server will serve it on their next answer or "
                "tomorrow morning.")
    except Exception as e:
        print(f"[SMS] ⚠️ freelance-guard block failed: {e}", flush=True)
    try:
        tb = _tracks_block(user_id)
        if tb:
            parts.append(tb)
    except Exception as e:
        print(f"[SMS] ⚠️ tracks block failed: {e}", flush=True)
    parts += _phase_gated_blocks(user_id, versions)
    parts.append(rendered_mode)
    # (The per-reply ignition judgment block was retired 2026-08-12,
    # PR-A — prompts/sms_ignition_judgment.md is kept, deprecated.)
    parts.append(_conversation_contract_block())
    return "\n\n---\n\n".join(parts), versions


def _tracks_block(user_id):
    """Life-track lane prompt block — ONLY for tracks-enabled users
    (empty string otherwise, so the husband's prompt is
    byte-identical to before). Renders current tracks + the rules of
    the track conversation. The actual WRITES happen in the
    tracks_ops hop after the reply; this block is what lets the
    coach hold the design conversation honestly."""
    if not db.tracks_lane_open(user_id):
        return ""
    import tracks_ops
    rows = db.get_life_tracks(user_id, statuses=("active", "held"))
    lines = ["## Life tracks (the concerns you carry for them)"]
    if rows:
        for t in rows:
            lines.append(f"- [{t['status']}] {t['name']} — {t['role']} "
                         f"(part: {t['part_type']}, "
                         f"surfacing: {t['surfacing']})")
            items = db.get_track_items(t["id"], status="open")
            if items:
                lines.append(f"  open items: " + "; ".join(
                    (it["payload"] or "{}")[:80] for it in items[:8]))
    else:
        lines.append("(none yet — the track conversation builds them)")
    parts_list = "\n".join(f"- {k}: {v}"
                           for k, v in tracks_ops.PART_TYPES.items())
    lines.append(f"""
You can design tracks WITH the user in this conversation: they name a
recurring concern, you propose a concrete track (name, what you'd do,
when you'd bring it up, what you'd never do), they adjust or confirm.
Everything is adjustable later in plain conversation — cadences,
wording, items, retiring a track.

Machines you actually have (propose ONLY these):
{parts_list}

Rules:
- A track exists when the user AGREES to a concrete proposal — restate
  the final shape briefly when they do. The server records agreed
  changes after this reply; never claim a track is already running
  before the user agreed.
- If a concern needs a machine NOT in the list above (reading their
  email, watching prices, anything beyond the parts), say honestly
  that this one has to be BUILT and brought back — "이건 우리가
  만들어서 가져와야 한다" — and that you'll flag it. Do not fake it.
- Deferring is a valid outcome ("let's not track this yet") — say so
  plainly.
- Ask for missing profile facts a track needs (a child's age, which
  day the nanny comes) as part of the design conversation, one at a
  time, never as a form.""")
    return "\n".join(lines)


TRACK_CONVO_OPENER = (
    "Different kind of question today — what are the things you're "
    "juggling in your head every day? Not big projects, the "
    "recurring stuff: lists, restocks, calls you owe, things you "
    "check on. Dump them here and let's see which ones I can carry "
    "for you.")


def send_track_convo_opener(user_id):
    """Operator/cron-triggered opener for the track conversation.
    Enables the lane if it isn't already, then sends a fixed opener
    (deterministic on purpose — the conversation after it is the
    experiment). Returns sent text or None."""
    db.enable_tracks(user_id, source="admin")
    phone = _phone_for(user_id)
    if not phone:
        print(f"[TRACKS] no phone for {user_id}", flush=True)
        return None
    text = TRACK_CONVO_OPENER
    send_sms(phone, text, user_id=user_id)
    db.save_sms_message(user_id, "assistant", text, "out")
    db.log_event(user_id, "sms_out",
                 {"text": text, "trigger": "track_convo_opener"},
                 source="sms")
    return text


# ─── Scheduled slot handling ────────────────────────────────────────

def _session_journey_block(user_id, session_id):
    """The session's observations, oldest first, for the web reply."""
    obs = db.get_session_observations(session_id, limit=12)
    ssn = db.get_screen_session(session_id) or {}
    lines = ["## This session's journey (screen observations, oldest first)"]
    if ssn.get("declared_source"):
        lines.append(f"Declared source: {ssn['declared_source']}")
    if not obs:
        lines.append("(no observations yet — the session just started; "
                     "do not guess at their screen)")
    for o in obs:
        t = (o.get("ts") or "")[11:16]
        lines.append(f"\n[{t}] {o['summary']}")
    return "\n".join(lines)


def build_web_turn(user_id, session_id, text, jpeg_bytes=None):
    """Everything a web-chat turn needs BEFORE the model call:
    stores the inbound message, kicks the background frame
    observation, and returns (system_prompt, history,
    prompt_versions) ready for a (streaming or not) create call."""
    db.save_sms_message(user_id, "user", text, "in", channel="web")

    # Latency budget: chat dies past ~10s, and the first live test
    # measured 25-30s because analysis, the frame read and the reply
    # ran in series (three user messages then piled into three
    # interleaved pipelines). The reply is now ONE model call:
    # - the raw frame goes INTO the reply call itself (the coach
    #   sees the pixels — even fresher than a pre-read),
    # - the journey observation for the same frame is logged in the
    #   BACKGROUND (the record must not gate the conversation),
    # - analyze_turn runs AFTER the reply, also in the background.
    if jpeg_bytes:
        import threading

        import eyes
        ssn = db.get_screen_session(session_id) or {}
        threading.Thread(
            target=eyes.read_frame,
            args=(user_id, session_id, jpeg_bytes, "chat"),
            kwargs={"declared_source": ssn.get("declared_source") or ""},
            daemon=True).start()

    system_prompt, prompt_versions = _build_system_prompt_for_reply(user_id)
    web_block, h_web = _read_prompt_versioned("sms_web_session")
    prompt_versions["sms_web_session"] = h_web
    system_prompt = (system_prompt + "\n\n---\n\n" + web_block
                     + "\n\n---\n\n"
                     + _session_journey_block(user_id, session_id))

    history = db.get_recent_sms_messages(user_id, limit=HISTORY_LIMIT,
                                         with_time=True)
    if jpeg_bytes and history and history[-1]["role"] == "user":
        import base64 as b64mod
        system_prompt += (
            "\n\nThe user's newest message carries a live capture of "
            "their screen AT THIS MOMENT — this image outranks every "
            "older observation in the journey above.")
        history = history[:-1] + [{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64",
                            "media_type": "image/jpeg",
                            "data": b64mod.b64encode(jpeg_bytes).decode()}},
                {"type": "text", "text": history[-1]["content"]}]}]
    return system_prompt, history, prompt_versions


def finish_web_turn(user_id, session_id, raw_text, system_prompt,
                    history, prompt_versions):
    """Everything AFTER the model produced the full turn text (the
    streaming path accumulates it chunk by chunk first): markers
    parsed and stripped, guards checked (log-only — a streamed turn
    cannot be retried, the user already read it), records written,
    analysis kicked. Returns the clean display text."""
    llm_call_id = db.save_llm_call(
        user_id, "web_session_reply", MODEL, system_prompt,
        [m if isinstance(m.get("content"), str)
         else {"role": m["role"], "content": "[frame+text]"}
         for m in history],
        prompt_versions=prompt_versions, response_text=raw_text)
    expect, text = _process_expect_marker(raw_text)
    steps, text = _process_step_marker(user_id, text)
    text = _process_ignition_markers(user_id, text, "web_session_reply")
    text = _strip_extraction_markers(user_id, text)
    if not text.strip():
        return ""
    violations = check_send_guards(text, steps, user_id=user_id)
    if violations:
        db.log_event(user_id, "send_guard_violation",
                     {"violations": violations,
                      "trigger": "web_session_reply",
                      "llm_call_id": llm_call_id}, source="web")
    db.save_sms_message(user_id, "assistant", text, "out",
                        channel="web")
    db.log_event(user_id, "web_out",
                 {"text": text, "session_id": session_id,
                  "steps": steps, "expect": expect,
                  "llm_call_id": llm_call_id}, source="web")
    import threading

    import analyze_turn
    threading.Thread(target=analyze_turn.analyze, args=(user_id,),
                     kwargs={"trigger": "web_reply"}, daemon=True).start()
    return text


def generate_web_reply(user_id, session_id, text, jpeg_bytes=None):
    """Non-streaming web turn (kept for tests and as fallback)."""
    system_prompt, history, versions = build_web_turn(
        user_id, session_id, text, jpeg_bytes)
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL, max_tokens=600, system=system_prompt,
        messages=history)
    raw = "".join(b.text for b in resp.content
                  if getattr(b, "type", "") == "text")
    return finish_web_turn(user_id, session_id, raw, system_prompt,
                           history, versions)


def _phone_for(user_id):
    """The user's send-to number: profile first, env fallback (only
    when the env pair names this exact user)."""
    prof = db.get_user_profile_by_id(user_id) or {}
    phone = (prof.get("phone") or "").strip()
    if phone:
        return phone
    if os.environ.get("TUTOR_USER_ID", "").strip() == user_id:
        return os.environ.get("TUTOR_USER_PHONE", "").strip()
    return ""


def handle_material_ready(user_id, material_id):
    """The user just uploaded a material and the digest is done —
    the freshest window there is: they opened the link, shared the
    thing, and are probably still holding the phone. Waiting for
    tomorrow's cron would burn exactly the moment the whole arc was
    built to reach, so the coach follows up NOW.

    Fires only when this upload is the awaited step: onboarding
    incomplete AND the current focus is material_understanding (the
    upload itself just settled alignment, so that is where the focus
    lands). The WhatsApp window gate still applies. Returns sent
    text or None."""
    try:
        state = db.get_onboarding_state(user_id)
        if state["completed_at"] or not state["missing"] \
                or state["missing"][0] != "material_understanding":
            return None
        if whatsapp_window_closed(user_id):
            db.log_event(user_id, "material_ready_not_sent",
                         {"material_id": material_id,
                          "reason": "whatsapp_window_closed"},
                         source="sms")
            return None
        to_number = _phone_for(user_id)
        if not to_number:
            db.log_event(user_id, "material_ready_not_sent",
                         {"material_id": material_id,
                          "reason": "no_phone_bound"}, source="sms")
            return None
        m = db.get_material(material_id) or {}
        system_prompt, prompt_versions = \
            _build_system_prompt_for_reply(user_id)
        history = db.get_recent_sms_messages(
            user_id, limit=HISTORY_LIMIT, with_time=True)
        history.append(_server_turn(
            f"The user just shared '{m.get('title') or 'their material'}' "
            f"on their /my page moments ago, and you have finished "
            f"reading it (your digest is in the materials block "
            f"above). They are probably still at the screen. Write "
            f"the next message: let them know you have actually read "
            f"it — one specific, true thing you noticed shows that "
            f"better than any adjective — and open the walkthrough."))
        text, steps, expect, llm_call_id, _hold = generate_message(
            user_id, system_prompt, history, "material_ready",
            max_tokens=400, prompt_versions=prompt_versions)
        if not text or not text.strip():
            return None
        text = _strip_extraction_markers(user_id, text)
        send_sms(to_number, text, user_id=user_id)
        db.save_sms_message(user_id, "assistant", text, "out")
        db.log_event(user_id, "sms_out",
                     {"text": text, "trigger": "material_ready",
                      "material_id": material_id, "steps": steps,
                      "expect": expect, "llm_call_id": llm_call_id},
                     source="sms")
        return text
    except Exception as e:
        print(f"[SMS] ⚠️ material_ready follow-up failed: {e}",
              flush=True)
        return None


def handle_cron_tick(slot, window=None):
    """Run a scheduled slot for EVERY active user (M2 fan-out).

    Iterates the DB roster (bound phone + status active), with the
    TUTOR_USER_* env pair appended as a fallback entry when it names
    a user not already on the roster. One user's failure never
    touches the next: each runs in its own try/except and failures
    become cron_user_failed events.

    Returns the last sent text (back-compat with single-user
    callers/tests), or None."""
    roster = list(db.get_active_users())
    env_uid = os.environ.get("TUTOR_USER_ID", "").strip()
    env_phone = os.environ.get("TUTOR_USER_PHONE", "").strip()
    if env_uid and env_phone \
            and env_uid not in {u["user_id"] for u in roster}:
        roster.append({"user_id": env_uid, "phone": env_phone})
    if not roster:
        print(f"[SMS] {slot}: no active users — skipping", flush=True)
        db.log_event(None, "cron_tick",
                     {"slot": slot, "action": "skipped",
                      "reason": "no_active_users"}, source="cron")
        return None
    last = None
    for u in roster:
        try:
            sent = _cron_tick_for_user(u["user_id"], u["phone"], slot,
                                       window=window)
            if sent:
                last = sent
        except Exception as e:
            print(f"[SMS] ⚠️ {slot}: {u['user_id']} failed: {e}",
                  flush=True)
            db.log_event(u["user_id"], "cron_user_failed",
                         {"slot": slot, "error": str(e)[:300]},
                         source="cron")
    return last


def _cron_tick_for_user(user_id, to_number, slot, window=None):
    """The original single-user slot body: decide whether to send,
    and if so, load prompt, call Claude, send.

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

    # A user-requested silence window ("주말 동안 보내지 마") blocks
    # every proactive send until it expires. Inbound replies are not
    # gated — answering someone who wrote to you is not a ping.
    paused = db.get_pause(user_id)
    if paused:
        return _skip(f"paused_until:{paused}")

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
        # references stale pre-phase context. A drill user is exempt:
        # their morning send is a bank question, which needs no
        # thread to stand on.
        import drill
        phase_state = db.get_user_phase(user_id)
        recent = db.get_recent_sms_messages(
            user_id, limit=1, since=phase_state["phase_started_at"]
        )
        if not recent and not drill.active_drill_track(user_id):
            return _skip("no_thread_this_phase")

    if whatsapp_window_closed(user_id):
        hours = _dormancy_hours(user_id)
        print(f"[SMS] {slot}: WhatsApp 24h window closed "
              f"({'never wrote' if hours is None else f'{hours:.0f}h silent'})"
              f" — not sending", flush=True)
        db.log_event(user_id, "whatsapp_window_closed",
                     {"slot": slot,
                      "silent_hours": None if hours is None else round(hours, 1),
                      "note": "free-form send refused; the user must "
                              "message first to reopen the window"},
                     source="cron")
        # The tick DID happen — record it, or the cron watchdog reads
        # a healthy-but-gated slot as a dead cron. Observed: two
        # window-refused evenings produced two false cron_missed
        # alarms while Render showed the job succeeding.
        db.log_event(user_id, "cron_tick",
                     {"slot": slot, "action": "refused_window_closed"},
                     source="cron")
        return None

    if slot in ("evening", "nudge"):
        # Start the Phase 0 timer on the first contact (idempotent).
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

    # Checklist v2 gate: the fixed expectation-setting message is
    # Theo's first onboarding message, sent by the server — this slot
    # delivers it INSTEAD of an LLM turn. The next slot (or the
    # user's reply) opens the conversation proper.
    if _expectation_due(user_id):
        text = send_expectation_message(user_id, to_number,
                                        f"cron_{slot}_expectation")
        if text is None:
            return _skip("expectation_prompt_empty")
        db.mark_onboarding_started(user_id)
        db.log_event(user_id, "cron_tick",
                     {"slot": slot, "action": "fired_expectation",
                      "decision_id": fire_decision_id, **win_extra},
                     source="cron")
        return text

    # v3 drill engine (출제 전환): a user with an active drill track
    # gets today's bank question instead of the generic slot prompt.
    # The old morning prompt says "this is not a study slot" — for a
    # drill user, the morning question IS the product, so the drill
    # branch owns the send. Selection is code, the prediction is
    # recorded before the question exists, and the planner only
    # phrases the served item.
    drill_ctx = None
    if slot in ("morning", "evening", "nudge"):
        try:
            import drill
            drill_ctx = drill.prepare_scheduled_question(user_id)
        except Exception as e:
            print(f"[SMS] ⚠️ drill prepare failed for {user_id}: {e}",
                  flush=True)
            db.log_event(user_id, "drill_error",
                         {"where": "prepare", "error": str(e)[:300]},
                         source="cron")
    if drill_ctx:
        trigger = f"cron_{slot}_drill"
        system_prompt, prompt_versions = _build_drill_prompt(
            user_id, drill_ctx)
    else:
        trigger = f"cron_{slot}"
        system_prompt, prompt_versions = _build_system_prompt(slot, user_id)
    if system_prompt is None:
        return _skip("no_prompt_for_state")

    # Scope history to current phase — see get_recent_sms_messages docstring.
    phase_state = db.get_user_phase(user_id)
    history = db.get_recent_sms_messages(
        user_id, limit=HISTORY_LIMIT, since=phase_state["phase_started_at"],
        with_time=True
    )

    # If there's no recent SMS history, prime with a single user-turn
    # placeholder. Anthropic requires the messages array to start with
    # a user role and to be non-empty.
    if not history:
        history = [_server_turn(
            f"The scheduled {slot} send is firing and there is no prior "
            f"thread with this user — this is your first message to "
            f"them. Write it.")]
    elif history[-1]["role"] == "assistant":
        # Anthropic needs a trailing user turn to answer. Ours says, in
        # the open, that it is the clock and not the person.
        history.append(_server_turn(
            f"The scheduled {slot} send is firing. The user has not "
            f"written since the last turn above. Write the next "
            f"message."))

    text, steps, expect, llm_call_id, hold_reason = generate_message(
        user_id, system_prompt, history, trigger,
        max_tokens=500, prompt_versions=prompt_versions)
    if text is None:
        return None

    # Answer-leak guard (drill sends only): a question that contains
    # its own answer key grades as a fake 'complete' and poisons the
    # ledgers. One rewrite attempt; if the rewrite still leaks, hold
    # the send — no question beats an answer-key question.
    if drill_ctx and text.strip():
        import drill
        if drill.leaks_answer(text, drill_ctx["item"]):
            db.log_event(user_id, "drill_answer_leak",
                         {"draft": text[:300], "llm_call_id": llm_call_id,
                          "item_id": drill_ctx["item"]["id"],
                          "attempt": 1}, source="cron")
            history = history + [
                {"role": "assistant", "content": text},
                _server_turn(
                    "Your draft contains the answer key (anchor "
                    "contents / rubric elements). Rewrite: ONLY the "
                    "question, nothing the user is supposed to "
                    "retrieve themselves.")]
            text, steps, expect, llm_call_id, hold_reason = \
                generate_message(user_id, system_prompt, history,
                                 f"{trigger}_leak_retry",
                                 max_tokens=500,
                                 prompt_versions=prompt_versions)
            if text is None:
                return None
            if drill.leaks_answer(text, drill_ctx["item"]):
                db.log_event(user_id, "drill_answer_leak",
                             {"draft": text[:300],
                              "llm_call_id": llm_call_id,
                              "item_id": drill_ctx["item"]["id"],
                              "attempt": 2, "held": True},
                             source="cron")
                return _skip("drill_answer_leak")
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

    # (Planner-chosen hold RETIRED 2026-08-12 — generate_message now
    # returns None on a twice-empty body, handled above; deliberate
    # silence belongs to the server gates alone.)

    send_sms(to_number, text, user_id=user_id)
    db.save_sms_message(user_id, "assistant", text, "out")
    db.log_event(user_id, "cron_tick",
                 {"slot": slot, "action": "fired",
                  "decision_id": fire_decision_id, **win_extra},
                 source="cron")
    db.log_event(user_id, "sms_out",
                 {"text": text, "trigger": trigger,
                  "prompt_versions": prompt_versions,
                  "llm_call_id": llm_call_id,
                  "steps": steps, "expect": expect,
                  "phase": db.get_user_phase(user_id)["phase"],
                  **win_extra},
                 source="cron")
    if drill_ctx:
        db.log_event(user_id, "drill_question_sent",
                     {"item_id": drill_ctx["item"]["id"],
                      "prediction_id": drill_ctx["prediction_id"],
                      "reask": drill_ctx["reask"],
                      "why": drill_ctx["why"],
                      "llm_call_id": llm_call_id, **win_extra},
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
    """Run the hourly per-user send-window check (P0-C), fanned out
    over every active user (M2). Same isolation contract as the
    fixed-cron fan-out. Returns the last sent text or None."""
    roster = list(db.get_active_users())
    env_uid = os.environ.get("TUTOR_USER_ID", "").strip()
    if env_uid and env_uid not in {u["user_id"] for u in roster}:
        roster.append({"user_id": env_uid,
                       "phone": os.environ.get("TUTOR_USER_PHONE",
                                               "").strip()})
    last = None
    for u in roster:
        try:
            sent = _schedule_tick_for_user(u["user_id"], now=now)
            if sent:
                last = sent
        except Exception as e:
            print(f"[SMS] ⚠️ schedule-tick: {u['user_id']} failed: {e}",
                  flush=True)
            db.log_event(u["user_id"], "cron_user_failed",
                         {"slot": "schedule_tick",
                          "error": str(e)[:300]}, source="cron")
    return last


def _schedule_tick_for_user(user_id, now=None):
    """One user's window check — the original P0-C body."""
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
    local_dt = now + timedelta(hours=TZ_OFFSET_H)
    local_hour = local_dt.hour

    for w in windows:
        try:
            start_hour = int(w["start"].split(":")[0])
        except (KeyError, ValueError, AttributeError):
            print(f"[SMS] ⚠️ schedule-tick: malformed window {w!r} — "
                  f"skipped", flush=True)
            continue
        if local_hour != start_hour:
            continue
        if w.get("days") is not None \
                and local_dt.weekday() not in w["days"]:
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
        # THIS user's window fires THIS user only. It used to call
        # handle_cron_tick (the M2 all-users fan-out) — a single-user-
        # era leftover, observed live 2026-08-07: one user's 09:00
        # window texted every user on the roster, and the night
        # before, another user's 20:00 window double-sent the first
        # (11 minutes after his own fixed evening cron).
        phone = _phone_for(user_id)
        if not phone:
            print(f"[SMS] schedule-tick: no phone bound for {user_id} "
                  f"— window {token} skipped", flush=True)
            db.log_event(user_id, "cron_tick",
                         {"slot": slot, "action": "skipped",
                          "reason": "no_phone_bound", "window": token},
                         source="cron")
            return None
        return _cron_tick_for_user(user_id, phone, slot, window=token)
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
