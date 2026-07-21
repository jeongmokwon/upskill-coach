"""
Infra events + capture-gap detection — WEEK1_ORDER T6, brief §4.1.

The brief's "pivotal natural experiments" (a dead cron, a crashed
observer, a silently-expired WhatsApp sandbox) must self-record:
an outage that survives only in the founder's memory is data lost.
This module is a periodic server-side sweep that turns absences —
things that did NOT happen — into events on the same append-only
log everything else uses.

Three checks, all dedup-guarded so a sweep every 2 minutes emits
each finding once, not 30 times an hour:

1. capture_gap — an open observe session with no capture for
   CAPTURE_GAP_MIN minutes. Includes whether the observer agent is
   still long-polling (alive but not capturing ≠ process dead).
2. cron_missed — an expected daily slot with no cron_tick event
   (fired OR skipped — both log ticks) for CRON_STALE_H hours.
   Staleness beats calendar math: no timezone/DST bookkeeping, and
   the November UTC-pin shift (see memory/brief) can't false-alarm
   a rolling window.
3. whatsapp_expiry_suspected — we sent recently, but the user has
   been silent longer than the sandbox's 3-day rejoin window, so
   sends may be silently undelivered (Twilio accepts them either
   way). Only meaningful while MESSAGING_CHANNEL=whatsapp.

Twilio hard send failures are NOT here — send_sms() already logs
`sms_send_failed` at the failure site, where the exception is.
"""

import os
from datetime import datetime

import db

CAPTURE_GAP_MIN = 5
CRON_STALE_H = 25
EXPECTED_SLOTS = ("morning", "evening")
WHATSAPP_SILENCE_H = 72     # sandbox rejoin window
SUSPICION_REPEAT_H = 24     # re-flag a persisting suspicion daily


def _age_minutes(iso_ts):
    """Minutes elapsed since a stored ISO timestamp; None if unparsable.
    Timestamps are naive server-local (db.py convention), so compare
    against naive now()."""
    try:
        return (datetime.now() - datetime.fromisoformat(iso_ts)).total_seconds() / 60.0
    except Exception:
        return None


def sweep():
    """Run all checks once. Never raises — the watchdog must not be
    able to hurt the thing it watches."""
    try:
        _check_capture_gaps()
    except Exception as e:
        print(f"[INFRA] ⚠️ capture-gap check failed: {e}", flush=True)

    user_id = os.environ.get("TUTOR_USER_ID", "").strip()
    if user_id:
        try:
            _check_cron_staleness(user_id)
        except Exception as e:
            print(f"[INFRA] ⚠️ cron-staleness check failed: {e}", flush=True)
        try:
            _check_whatsapp_expiry(user_id)
        except Exception as e:
            print(f"[INFRA] ⚠️ whatsapp-expiry check failed: {e}", flush=True)


def _check_capture_gaps():
    # Deferred import: observe pulls in the anthropic SDK at module
    # load, which the sweep itself doesn't need (only the in-memory
    # poll-liveness registry lives there).
    import observe
    for sess in db.get_open_observe_sessions():
        session_id, user_id = sess["session_id"], sess["user_id"]
        last_capture = (db.get_last_observation_ts(session_id)
                        or sess["started_at"])
        gap_min = _age_minutes(last_capture)
        if gap_min is None or gap_min < CAPTURE_GAP_MIN:
            continue
        # One gap event per silence stretch: if we already reported
        # since the last capture, this is the same ongoing gap.
        prev = db.get_last_event(user_id, "capture_gap",
                                 payload_contains=session_id)
        if prev and prev["ts"] > last_capture:
            continue
        db.log_event(user_id, "capture_gap", {
            "session_id": session_id,
            "last_capture_at": last_capture,
            "gap_minutes": round(gap_min, 1),
            "observer_polling": observe.observer_alive(user_id),
        }, source="infra")
        print(f"[INFRA] capture gap: session {session_id} silent "
              f"{gap_min:.0f} min", flush=True)


def _check_cron_staleness(user_id):
    stale_min = CRON_STALE_H * 60
    for slot in EXPECTED_SLOTS:
        needle = f'"slot": "{slot}"'
        last_tick = db.get_last_event(user_id, "cron_tick",
                                      payload_contains=needle)
        # Some ticks land before TUTOR_USER_ID resolution (env_unset)
        # under '_unknown'; those still prove the cron ran.
        if not last_tick:
            last_tick = db.get_last_event("_unknown", "cron_tick",
                                          payload_contains=needle)
        tick_age = _age_minutes(last_tick["ts"]) if last_tick else None
        if tick_age is not None and tick_age < stale_min:
            continue
        # No tick ever: stay quiet. On a fresh deploy the log is
        # legitimately empty; alarming on absence-of-history would
        # fire forever on every new environment.
        if last_tick is None:
            continue
        prev = db.get_last_event(user_id, "cron_missed",
                                 payload_contains=needle)
        prev_age = _age_minutes(prev["ts"]) if prev else None
        if prev_age is not None and prev_age < stale_min:
            continue
        db.log_event(user_id, "cron_missed", {
            "slot": slot,
            "last_tick_at": last_tick["ts"],
            "stale_hours": round(tick_age / 60.0, 1),
        }, source="infra")
        print(f"[INFRA] cron missed: {slot} last ticked "
              f"{tick_age/60.0:.1f}h ago", flush=True)


def _check_whatsapp_expiry(user_id):
    if os.environ.get("MESSAGING_CHANNEL", "").strip().lower() != "whatsapp":
        return
    last_out = db.get_last_event(user_id, "sms_out")
    out_age = _age_minutes(last_out["ts"]) if last_out else None
    if out_age is None or out_age > 24 * 60:
        return  # not actively sending — nothing to suspect
    last_in = db.get_last_event(user_id, "sms_in")
    in_age = _age_minutes(last_in["ts"]) if last_in else None
    if in_age is not None and in_age < WHATSAPP_SILENCE_H * 60:
        return  # user spoke recently — window is open
    prev = db.get_last_event(user_id, "whatsapp_expiry_suspected")
    prev_age = _age_minutes(prev["ts"]) if prev else None
    if prev_age is not None and prev_age < SUSPICION_REPEAT_H * 60:
        return
    db.log_event(user_id, "whatsapp_expiry_suspected", {
        "last_outbound_at": last_out["ts"],
        "last_inbound_at": last_in["ts"] if last_in else None,
        "user_silent_hours": round(in_age / 60.0, 1) if in_age else None,
    }, source="infra")
    print(f"[INFRA] whatsapp sandbox expiry suspected — sends may be "
          f"silently undelivered", flush=True)
