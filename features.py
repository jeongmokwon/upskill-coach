"""
Given-feature computation — exploration P2 (brief §7 "User notes").

Computes the closed feature set that user-note conditions (`given`)
and the planner's context are allowed to reference. Everything here
is MECHANICAL — computed from the event log by explicit rules, no
LLM interpretation. The judgment rules (what counts as tired, what
counts as fast) live in the constants below, versioned with the
code, so any note scored against these features is reproducible.

The trace (P1) renders raw facts; this module turns facts into the
small vocabulary notes are written in.
"""

import json
import os
from datetime import datetime, timedelta

import db

# ── Bucketing rules (explicit, code-versioned) ──────────────────────
FAST_REPLY_MIN = 5          # reply within 5 min → fast
SLOW_REPLY_MIN = 30         # reply after 30+ min → slow
OPENER_GAP_H = 4            # no exchange for 4h → next send opens a conversation
TIRED_MAX_WORDS = 3         # a reply this short...
TIRED_AFTER_HOUR = 22       # ...this late (PT) reads as tired
ENGAGED_MIN_WORDS = 8       # a fast reply this long reads as engaged

TZ_OFFSET_H = int(os.environ.get("TZ_OFFSET_HOURS", "-8"))


def _age_minutes(ts_iso, now):
    return (now - datetime.fromisoformat(ts_iso)).total_seconds() / 60.0


def compute_features(user_id, now=None):
    """→ dict with the six given-features. Never raises; missing data
    degrades to neutral values (a fresh user is an opener with no
    signal, not an error)."""
    now = now or datetime.now()
    feats = {
        # Wall-clock on the USER's clock. Without this the planner
        # inferred the hour from the slot name and asserted it —
        # observed: a noon send opening with "저녁 됐네".
        "local_time": (now + timedelta(hours=TZ_OFFSET_H)).strftime("%H:%M"),
        "position": "opener",
        "last_steps": [],
        "silence_streak_days": None,
        "reply_latency": None,        # fast | mid | slow (last reply)
        "user_signal": "neutral",     # tired | engaged | resistant | neutral
        "phase": "discovery",
    }
    try:
        feats["phase"] = db.get_user_phase(user_id)["phase"]

        last_out = db.get_last_event(user_id, "sms_out")
        last_in = db.get_last_event(user_id, "sms_in")

        # position: if any exchange happened within OPENER_GAP_H, the
        # next send continues a conversation; otherwise it opens one.
        recent = [e for e in (last_out, last_in) if e]
        if recent and min(_age_minutes(e["ts"], now) for e in recent) \
                < OPENER_GAP_H * 60:
            feats["position"] = "mid"

        if last_out:
            try:
                steps = json.loads(last_out["payload"]).get("steps") or []
                feats["last_steps"] = [s.get("tag") for s in steps]
            except Exception:
                pass

        if last_in:
            silence_min = _age_minutes(last_in["ts"], now)
            feats["silence_streak_days"] = int(silence_min // (24 * 60))

            # latency of that last reply, vs the send preceding it
            in_ts = last_in["ts"]
            prev_outs = [e for e in db.get_events_with_ids(
                user_id,
                (datetime.fromisoformat(in_ts) - timedelta(days=2)).isoformat(),
                in_ts, limit=200) if e["kind"] == "sms_out"]
            if prev_outs:
                lat_min = (datetime.fromisoformat(in_ts)
                           - datetime.fromisoformat(prev_outs[-1]["ts"])
                           ).total_seconds() / 60.0
                feats["reply_latency"] = ("fast" if lat_min < FAST_REPLY_MIN
                                          else "slow" if lat_min >= SLOW_REPLY_MIN
                                          else "mid")

            # user_signal from the last reply's mechanics
            try:
                text = json.loads(last_in["payload"]).get("text", "")
            except Exception:
                text = ""
            words = len(text.split())
            reply_hour = (datetime.fromisoformat(in_ts)
                          + timedelta(hours=TZ_OFFSET_H)).hour
            if words <= TIRED_MAX_WORDS and reply_hour >= TIRED_AFTER_HOUR:
                feats["user_signal"] = "tired"
            elif feats["reply_latency"] == "fast" and words >= ENGAGED_MIN_WORDS:
                feats["user_signal"] = "engaged"

        # resistant: the user invoked skip today
        skip = db.get_last_event(user_id, "skip_today")
        if skip and _age_minutes(skip["ts"], now) < 24 * 60:
            feats["user_signal"] = "resistant"
    except Exception as e:
        print(f"[FEATURES] ⚠️ compute failed for {user_id}: {e}", flush=True)
    return feats


def render_features(feats):
    """One-line natural rendering for the planner prompt."""
    parts = []
    if feats.get("local_time"):
        parts.append(f"local_time={feats['local_time']}")
    parts.append(f"position={feats['position']}")
    if feats["last_steps"]:
        parts.append("last_steps=" + ",".join(feats["last_steps"]))
    if feats["silence_streak_days"] is not None:
        parts.append(f"silence={feats['silence_streak_days']}d")
    if feats["reply_latency"]:
        parts.append(f"last_reply={feats['reply_latency']}")
    # signal (tired/engaged/resistant) and phase are deliberately NOT
    # rendered: signal is a crude mechanics heuristic from the
    # wellness-aware era, never a real analysis of the user, and
    # phase already renders (in words) in the user-facts block.
    return " · ".join(parts)
