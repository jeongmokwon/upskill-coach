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

# ─── Config ──────────────────────────────────────────────────────────

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

SLOTS = ("morning", "lunch", "afternoon", "evening")

# Max one Anthropic Sonnet call per slot. Cheap enough we just always
# use the same model as the web tutor for now — consistency beats
# pennies of savings.
MODEL = "claude-sonnet-4-5"

# How much SMS history to feed back into Claude as conversation. 20
# messages ≈ 10 round-trips, plenty for one-user context.
HISTORY_LIMIT = 20

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


def send_sms(to_number, body):
    """Send `body` to `to_number` (E.164). Returns Twilio SID or None.

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
        # 1-indexed day count for the LLM's "Day X of 3" awareness.
        "discovery_day": db.days_in_discovery(user_id) + 1,
    }


class _SafeDict(dict):
    """format_map helper: unknown {brace} keys pass through unchanged
    so LLM prompt bodies with JSON examples don't blow up rendering."""
    def __missing__(self, k):
        return "{" + k + "}"


def _build_system_prompt(slot, user_id):
    """Assemble shared + slot prompt with user context interpolated.

    Returns None if the slot has no message to send under current
    state (used to no-op lunch/afternoon under the redesign).
    """
    prompt_name = _prompt_name_for_slot(slot, db.get_user_phase(user_id)["phase"])
    if prompt_name is None:
        return None
    shared = _read_prompt("sms_shared")
    slot_prompt = _read_prompt(prompt_name)
    fields = _build_placeholders(user_id)
    rendered_shared = shared.format_map(_SafeDict(**fields))
    rendered_slot = slot_prompt.format_map(_SafeDict(**fields))
    return rendered_shared + "\n\n---\n\n" + rendered_slot


# ─── Commit-marker protocol (Phase 0 → Phase 1) ──────────────────────
#
# The LLM signals a phase transition by embedding [COMMIT: "..."]
# anywhere in its response. We parse it, save the bite, transition
# state, and strip the marker before sending to the user.

_COMMIT_MARKER_RE = re.compile(
    r'\[COMMIT:\s*"([^"]{3,400})"\s*\]',
    re.DOTALL,
)


def _process_commit_marker(user_id, text):
    """If the LLM emitted [COMMIT: "..."] and the user is still in
    discovery, save the bite and transition phase. Return the text
    with the marker stripped either way.
    """
    match = _COMMIT_MARKER_RE.search(text)
    if not match:
        return text
    bite = match.group(1).strip()
    stripped = _COMMIT_MARKER_RE.sub("", text).strip()
    # Collapse the double-blank that stripping mid-paragraph can leave.
    stripped = re.sub(r"\n{3,}", "\n\n", stripped)

    phase = db.get_user_phase(user_id)["phase"]
    if phase == "discovery":
        db.commit_first_bite(user_id, bite)
        print(f"[SMS] Phase 0→1 for {user_id} on user agreement: {bite!r}", flush=True)
    else:
        # LLM emitted a commit while already in Phase 1 — ignore, log.
        print(f"[SMS] stray COMMIT marker while phase={phase!r}, ignoring", flush=True)
    return stripped


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
        return None

    # Log the user's message FIRST so it's part of history before we
    # build context for our reply.
    db.save_sms_message(user_id, "user", body, "in")

    # Meta-commands short-circuit the LLM.
    if _is_command(body, SKIP_TOKENS):
        _mark_skip_today(user_id)
        reply = "ok, no more pings today. talk tomorrow."
        send_sms(from_number, reply)
        db.save_sms_message(user_id, "assistant", reply, "out")
        return reply
    if _is_command(body, LATER_TOKENS):
        reply = "got it — picking this back up at 9."
        send_sms(from_number, reply)
        db.save_sms_message(user_id, "assistant", reply, "out")
        return reply

    # Regular conversation: feed full history + shared persona to
    # Claude, get a reply, send it.
    history = db.get_recent_sms_messages(user_id, limit=HISTORY_LIMIT)
    # `history` ends with the user message we just inserted, which is
    # what the Anthropic API expects (last message = user turn).

    # For inbound replies (not a scheduled slot), only the shared
    # persona applies — no slot-specific instruction.
    system_prompt = _build_system_prompt_for_reply(user_id)

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
        return None

    # Parse & handle [COMMIT: "..."] marker, strip it from user-visible text.
    reply_text = _process_commit_marker(user_id, reply_text)
    send_sms(from_number, reply_text)
    db.save_sms_message(user_id, "assistant", reply_text, "out")
    return reply_text


def _build_system_prompt_for_reply(user_id):
    """Shared persona only, with placeholders filled — used for
    inbound conversational replies (not scheduled slots).

    Includes the phase-flow placeholders so the LLM knows if it's
    in discovery vs first_bite mode during a back-and-forth exchange.
    """
    shared = _read_prompt("sms_shared")
    fields = _build_placeholders(user_id)
    return shared.format_map(_SafeDict(**fields))


# ─── Scheduled slot handling ────────────────────────────────────────

def handle_cron_tick(slot):
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
    """
    if slot not in SLOTS:
        print(f"[SMS] unknown slot {slot!r}", flush=True)
        return None

    user_id = os.environ.get("TUTOR_USER_ID", "").strip()
    to_number = os.environ.get("TUTOR_USER_PHONE", "").strip()
    if not (user_id and to_number):
        print(f"[SMS] {slot}: TUTOR_USER_ID/PHONE not set — skipping", flush=True)
        return None

    if _is_skipped_today(user_id):
        print(f"[SMS] {slot}: user skipped today — not sending", flush=True)
        return None

    # Slot-specific gating.
    if slot in ("lunch", "afternoon"):
        print(f"[SMS] {slot}: daytime slot disabled under redesign — skipping", flush=True)
        return None

    if slot == "morning":
        # Skip if there's no prior conversation — a cold "morning!"
        # with nothing to reference is noise, not signal.
        if not db.get_recent_sms_messages(user_id, limit=1):
            print(f"[SMS] morning: no prior thread — skipping (waiting for evening)", flush=True)
            return None

    if slot == "evening":
        # Start the Phase 0 timer on the first evening tick (idempotent).
        db.ensure_phase_timer_started(user_id)

    system_prompt = _build_system_prompt(slot, user_id)
    if system_prompt is None:
        print(f"[SMS] {slot}: no prompt for current state — skipping", flush=True)
        return None

    history = db.get_recent_sms_messages(user_id, limit=HISTORY_LIMIT)

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
        return None

    # Parse & handle [COMMIT: "..."] marker (Phase 0→1), strip it out.
    text = _process_commit_marker(user_id, text)
    send_sms(to_number, text)
    db.save_sms_message(user_id, "assistant", text, "out")
    return text
