"""
User-notes rendering — exploration P3 (brief §7 "User notes").

Turns the stored notes into the planner's prompt block B: the
compressed, falsifiable knowledge about THIS user. Rendering is
deliberately plain — one line per note, condition → move → expected
reaction — because the reader is an LLM choosing the next step, not
a human browsing.
"""

import json

import db


def _fmt_given(given):
    if not given:
        return "any state"
    parts = []
    for k, v in given.items():
        parts.append(f"{k}={v}")
    return ", ".join(parts)


def render_notes_block(user_id):
    """→ prompt block for the planner ('' if the user has no notes —
    an empty block degrades the planner gracefully to prior+trace)."""
    notes = db.get_user_notes(user_id)
    if not notes:
        return ""
    lines = ["## What is KNOWN about this user (scored notes — trust "
             "confirmed > hypothesis; never contradict a confirmed note "
             "without new evidence)",
             "",
             "Move chains below are multi-TURN protocols: play the "
             "first move, wait for the user's response, then advance. "
             "Never collapse a chain into one message.",
             ""]
    for n in notes:
        given = _fmt_given(json.loads(n["given_json"]))
        when = ", ".join(json.loads(n["when_json"])) or "?"
        tag = n["confidence"]
        line = (f"- [{tag}] {n['claim']}\n"
                f"  (if {given} · move {when} → expect {n['expect'] or '?'})")
        lines.append(line)
    return "\n".join(lines)
