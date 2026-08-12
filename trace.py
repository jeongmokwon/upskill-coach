"""
Trace serialization — exploration P1 (brief §7 "Trace notation").

Renders any user-period from the event log as the step-language
token sequence: an alternating two-author trace of coach tokens
(chosen steps) and world tokens (user replies with mechanical
metadata, silences, screen sessions). One rendering serves four
readers: the planner's context block C, the founder's morning
review, the nightly scorer's input, and — unchanged — the exact
serialization a future sequence model would train on.

Layering rule: this module renders FACTS only (raw minutes, raw
word counts). Bucketing/interpretation (fast/slow, tired/engaged)
belongs to the feature computer (P2), so no judgment leaks into
the trace.

Notation:
    19:50  coach: map@1, micro_ask@2   (evening)
    [no reply]
    07:03  user: reply +11h 8w  "오늘 저녁에 할게"   (verbose: text snippet)
    19:50  coach: hold   (evening: user_skip_today)
    [screen x4]
    ~ judge: ignition 4/5
    ~ note: phase → first_bite
    [silence 1d — trailing]
"""

import json
import os
from datetime import datetime, timedelta

import db

# PT-day grouping follows the codebase's fixed-offset convention
# (see annotate.py / get_today_sessions_for_user).
TZ_OFFSET_H = int(os.environ.get("TZ_OFFSET_HOURS", "-8"))

# A coach send with no user reply for this long counts as [no reply]
# even before the next send arrives (an evening ping unanswered
# overnight is a fact worth a token of its own).
NO_REPLY_AFTER_H = 12


def _fmt_delta(seconds):
    m = seconds / 60.0
    if m < 60:
        return f"+{max(1, round(m))}m"
    h = m / 60.0
    if h < 48:
        return f"+{round(h)}h"
    return f"+{round(h / 24)}d"


def _pt(ts_iso):
    """Server-naive ts → PT-ish datetime (fixed offset convention)."""
    return datetime.fromisoformat(ts_iso) + timedelta(hours=TZ_OFFSET_H)


def _payload(e):
    try:
        return json.loads(e["payload"])
    except Exception:
        return {}


def _steps_str(steps):
    parts = []
    for s in steps or []:
        tag = s.get("tag", "?")
        parts.append(tag if s.get("intensity") is None
                     else f"{tag}@{s['intensity']}")
    return ", ".join(parts)


def render_trace(user_id, days=3, verbose=False, now=None):
    """Render the last `days` PT-days of a user's events as a
    step-language trace. Returns a plain-text block, oldest-first.
    `now` injectable for tests."""
    now = now or datetime.now()
    start = (now + timedelta(hours=TZ_OFFSET_H) - timedelta(days=days)) \
        .replace(hour=0, minute=0, second=0, microsecond=0) \
        - timedelta(hours=TZ_OFFSET_H)
    events = db.get_events_with_ids(user_id, start.isoformat(),
                                    now.isoformat(), limit=2000)

    lines = []
    current_day = None
    pending_out_ts = None      # last coach send awaiting a user reply
    last_in_ts = None
    screen_run = 0

    def flush_screen_run():
        nonlocal screen_run
        if screen_run and not verbose:
            lines.append(f"  [screen x{screen_run}]")
        screen_run = 0

    # [no reply] semantics: a pending coach send is CONFIRMED
    # unanswered the moment another coach send supersedes it (however
    # short the gap — the sequence fact is "two sends, no user token
    # between"). Only at the end of the window, where no superseding
    # send exists yet, do we require NO_REPLY_AFTER_H before calling
    # it. Day boundaries deliberately do NOT clear pending: a reply
    # at 07:00 to a 22:00 send must still render its true +9h latency.

    for e in events:
        kind = e["kind"]
        p = _payload(e)
        pt = _pt(e["ts"])
        hhmm = pt.strftime("%H:%M")

        if kind not in ("observation",):
            flush_screen_run()

        day = pt.date().isoformat()
        if day != current_day:
            lines.append(f"== {day} ({pt.strftime('%a')}) ==")
            current_day = day

        if kind == "sms_out":
            if pending_out_ts:
                lines.append("  [no reply]")
                pending_out_ts = None
            # Step tags retired from rendering (exp/step-surface-
            # removal): the trajectory keeps its rhythm function
            # (what fired when, silences, replies) without the move
            # lexicon conditioning every send.
            trig = p.get("trigger", "")
            lines.append(f"  {hhmm}  coach: sent   ({trig})")
            if verbose and p.get("text"):
                snippet = p["text"].replace("\n", " / ")[:120]
                lines.append(f'         "{snippet}"')
            pending_out_ts = e["ts"]

        elif kind == "sms_in":
            latency = ""
            if pending_out_ts:
                secs = (datetime.fromisoformat(e["ts"])
                        - datetime.fromisoformat(pending_out_ts)).total_seconds()
                latency = f" {_fmt_delta(secs)}"
                pending_out_ts = None
            text = p.get("text", "")
            words = len(text.split())
            line = f"  {hhmm}  user: reply{latency} {words}w"
            if verbose:
                line += f'  "{text[:80]}"'
            lines.append(line)
            last_in_ts = e["ts"]

        elif kind == "cron_tick" and p.get("action") in ("skipped",
                                                        "held_by_planner"):
            # hold tokens: deliberate silence is an action
            if any(s.get("tag") == "hold" for s in p.get("steps") or []):
                why = p.get("reason") or p.get("action")
                lines.append(f"  {hhmm}  coach: hold   "
                             f"({p.get('slot')}: {why})")

        elif kind == "observation":
            if verbose:
                lines.append(f"  {hhmm}  screen: "
                             f"{p.get('summary', '')[:80]}")
            else:
                screen_run += 1

        elif kind == "ignition_judgment":
            lines.append(f"  ~ judge: ignition {p.get('score')}/5")

        elif kind == "ignition_def_set":
            lines.append(f'  ~ note: ignition marker = "{p.get("marker", "")}"')

        elif kind == "phase_transition":
            lines.append(f"  ~ note: phase → {p.get('to', p.get('phase', '?'))}")

        elif kind == "goal_set":
            lines.append("  ~ note: goal set/refined")

        elif kind == "capture_gap" and verbose:
            lines.append(f"  ~ infra: capture gap "
                         f"{p.get('gap_minutes')}min")

    flush_screen_run()
    if pending_out_ts:
        gap_h = (now - datetime.fromisoformat(pending_out_ts)) \
            .total_seconds() / 3600
        if gap_h >= NO_REPLY_AFTER_H:
            lines.append("  [no reply]")

    # Trailing silence: how long since the user last spoke.
    if last_in_ts:
        secs = (now - datetime.fromisoformat(last_in_ts)).total_seconds()
        if secs >= 24 * 3600:
            lines.append(f"  [silence {_fmt_delta(secs)[1:]} — trailing]")
    elif events:
        lines.append("  [no user reply in window]")

    if not events:
        return "(no events in window)"
    return "\n".join(lines)
