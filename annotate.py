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
    return results
