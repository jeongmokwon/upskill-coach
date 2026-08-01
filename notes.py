"""
User-notes rendering — exploration P3 (brief §7 "User notes").

Turns the stored notes into the planner's prompt block B: the
compressed, falsifiable knowledge about THIS user. Rendering is
deliberately plain — one line per note, condition → move → expected
reaction — because the reader is an LLM choosing the next step, not
a human browsing.

Block B also carries the user profile brief (brief §7 "User profile
brief") — who they are, what kind of learning they are doing, and
what they asked for in their own words. Notes say what MOVES work on
this user; the brief says who the user IS. Both go into every send,
so both stay short.
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


def render_profile_block(user_id):
    """→ compact prompt block describing WHO this user is ('' if no
    brief has been generated yet). Deliberately terse: it is prefixed
    to block B on every single send."""
    brief = db.get_user_profile_brief(user_id)
    if not brief:
        return ""
    types = json.loads(brief["learning_types_json"] or "[]")
    materials = json.loads(brief["materials_json"] or "[]")
    wants = json.loads(brief["wants_json"] or "[]")
    path = db.get_current_path(user_id) or {}
    lines = ["## Who this user is (profile brief v%d)" % brief["version"]]
    if brief["job"]:
        lines.append(f"- Work/field: {brief['job']}")
    if types:
        lines.append(f"- Learning types: {', '.join(types)}"
                     + (f" (path framing: {path.get('path_kind')})"
                        if path.get("path_kind") else ""))
    if materials:
        lines.append(f"- Learns from: {', '.join(materials)}")
    if wants:
        lines.append("- What they asked for, THEIR words (quote them "
                     "back; never rephrase their goal into yours):")
        for w in wants:
            quote = (w.get("quote") or "").strip()
            meaning = (w.get("meaning") or "").strip()
            lines.append(f"  · \"{quote}\"" + (f" → {meaning}" if meaning
                                               else ""))
    if brief["personality"]:
        lines.append(f"- How they engage: {brief['personality']}")
    return "\n".join(lines)


def render_notes_block(user_id, profile=True, moves=True):
    """→ prompt block B for the planner: the profile brief followed by
    the scored notes ('' if the user has neither — an empty block
    degrades the planner gracefully to prior+trace).

    profile=False returns the scored notes alone, for callers that
    render the brief themselves (the SMS assembler puts the user's
    facts between the two).

    moves=False drops the `(if state · move X@n → expect Y)` line from
    each note, leaving the claim. Used while onboarding is incomplete.
    A move prescription is an instruction about what to DO, written in
    the user's own vocabulary and marked with an expected payoff — the
    most executable thing in the prompt. Observed: the checklist asked
    for the offer, and the coach played a note's `micro_ask@2` almost
    verbatim instead. During onboarding the claims still shape HOW to
    say things; the moves come back once there is a plan to run."""
    profile = render_profile_block(user_id) if profile else ""
    notes = db.get_user_notes(user_id)
    if not notes:
        return profile
    lines = ([profile, ""] if profile else [])
    lines += ["## What is KNOWN about this user (scored notes — trust "
              "confirmed > hypothesis; never contradict a confirmed note "
              "without new evidence)",
              ""]
    lines += (["Move chains below are multi-TURN protocols: play the "
               "first move, wait for the user's response, then advance. "
               "Never collapse a chain into one message.", ""]
              if moves else
              ["These describe how this person tends to respond. They "
               "shape HOW you say what you say — never WHAT this "
               "message has to settle. That is the onboarding focus "
               "block, and nothing here outranks it.", ""])
    for n in notes:
        given = _fmt_given(json.loads(n["given_json"]))
        when = ", ".join(json.loads(n["when_json"])) or "?"
        tag = n["confidence"]
        line = (f"- [{tag}] {n['claim']}"
                + (f"\n  (if {given} · move {when} → "
                   f"expect {n['expect'] or '?'})" if moves else ""))
        lines.append(line)
    return "\n".join(lines)
