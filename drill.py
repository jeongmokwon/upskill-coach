"""The v3 drill engine: selection → prediction → question → grading.

The loop the ledgers exist for. A scheduled send for a user with an
active drill track no longer plays the thread-keeping morning prompt
(which forbids quizzes — the root cause of the missing morning
questions): the server selects an item from the bank, records a
prediction BEFORE the question goes out, and the planner writes the
question grounded in the item's anchor quote. When the answer comes
back, one grading pass turns it into all four signals — element
verdicts, self_confidence, style observation, prediction outcome —
and writes the ledgers.

Selection is deliberately plain code, not a model call: the KPI is
"does Theo know what he'll get wrong", and a scoring function you
can read is a scoring function you can debug. FSRS plugs in here
later, once attempts accumulate; until then the priority is
  P(miss)   — evidence of past misses (item-level, then kind-level)
  soon      — untested-but-hard first (exam pressure lever, later)
  decay     — recently asked items stand down
with every ~5th send a likely-hit to verify predictions instead of
only confirming misses.
"""

import json
import os
import re
from datetime import datetime, timedelta

import anthropic

import db

MODEL = os.environ.get("DRILL_MODEL", "claude-sonnet-4-5")

# An unanswered question stays "open" this long: inside it, scheduled
# sends re-ask instead of stacking a second question, and an inbound
# reply is checked against it for grading. Past it, the prediction is
# simply never scored (accuracy counts only scored rows — an expired
# question is missing data, not a miss).
OPEN_QUESTION_H = 48

# Every Nth drill send picks a likely-hit instead of a likely-miss.
# A predictor that only ever fires on expected misses can't be told
# apart from a pessimist; the KPI needs both directions sampled.
LIKELY_HIT_EVERY = 5

_ASKABLE = ("untested", "learning")

# How much each signal moves an item up the queue. Readable > clever.
_W_MISSED = 30       # the user missed this exact item before
_W_PARTIAL = 20      # partially — second branches, list tails
_W_UNTESTED = 4      # × est_difficulty: hard unseen material first
_W_KIND_MISS = 8     # the user misses this KIND of structure
# Asked within this window → stand down. 7 days, not 3 (operator
# call, 2026-08-11): at 0-3 questions/day a 3-day gap re-serves
# material while the first exposure is still fresh; the intended
# retry rhythm is a 7-14 day window (proper spacing comes with
# FSRS later — this constant is the crude stand-in).
_RECENT_DAYS = 7


def active_drill_track(user_id):
    tracks = db.get_tracks(user_id, mode="drill")
    return tracks[0] if tracks else None


def _age_hours(ts):
    try:
        return (datetime.now()
                - datetime.fromisoformat(ts)).total_seconds() / 3600
    except Exception:
        return None


def _recent_item_ids(user_id, attempts):
    """Items touched within the stand-down window (감쇠)."""
    recent = set()
    for p in db.get_predictions(user_id, limit=200):
        h = _age_hours(p["ts"])
        if h is not None and h < _RECENT_DAYS * 24:
            recent.add(p["item_id"])
    for a in attempts:
        h = _age_hours(a["ts"])
        if h is not None and h < _RECENT_DAYS * 24:
            recent.add(a.get("item_id"))
    return recent


def select_item(user_id, track_id):
    """Deterministic FALLBACK selection (weight arithmetic). The
    live path is rank_select — this runs when the ranking call
    fails, and stays rerunnable on a laptop. Returns (item, why) or
    (None, reason)."""
    items = [i for i in db.get_knowledge_items(track_id)
             if i["status"] in _ASKABLE]
    if not items:
        return None, "no askable items"

    attempts = db.get_attempts(track_id, limit=500)
    last_by_item = {}
    kind_misses = {}
    for a in reversed(attempts):          # oldest → newest; last wins
        if a.get("item_id"):
            last_by_item[a["item_id"]] = a
    by_kind_items = {i["id"]: i["kind"] for i in items}
    for a in attempts:
        kind = by_kind_items.get(a.get("item_id"))
        if kind and a["verdict"] in ("missed", "partial"):
            kind_misses[kind] = kind_misses.get(kind, 0) + 1

    asked_recently = set()
    for p in db.get_predictions(user_id, limit=200):
        h = _age_hours(p["ts"])
        if h is not None and h < _RECENT_DAYS * 24:
            asked_recently.add(p["item_id"])
    for a in attempts:
        h = _age_hours(a["ts"])
        if h is not None and h < _RECENT_DAYS * 24:
            asked_recently.add(a.get("item_id"))

    def miss_score(it):
        s, why = 0, []
        last = last_by_item.get(it["id"])
        if last and last["verdict"] == "missed":
            s += _W_MISSED; why.append("missed before")
        elif last and last["verdict"] == "partial":
            s += _W_PARTIAL; why.append("partial before")
        if it["status"] == "untested":
            s += _W_UNTESTED * (it["est_difficulty"] or 2)
            why.append(f"untested, difficulty {it['est_difficulty']}")
        if kind_misses.get(it["kind"]):
            s += _W_KIND_MISS
            why.append(f"weak kind: {it['kind']}")
        return s, why

    fresh = [i for i in items if i["id"] not in asked_recently] or items

    # The verification send: pick what he's most likely to GET RIGHT.
    n_preds = len(db.get_predictions(user_id, limit=500))
    if n_preds and n_preds % LIKELY_HIT_EVERY == LIKELY_HIT_EVERY - 1:
        pick = min(fresh, key=lambda i: (miss_score(i)[0], i["id"]))
        return pick, "likely-hit verification pick"

    pick = max(fresh, key=lambda i: (miss_score(i)[0], -i["id"]))
    _, why = miss_score(pick)
    return pick, ", ".join(why) or "rotation"


_RANK_TOOL = {
    "name": "submit_selection",
    "description": "Choose the one item to drill next.",
    "input_schema": {
        "type": "object",
        "properties": {
            "item_id": {"type": "integer"},
            "p_miss": {"type": "string",
                       "enum": ["high", "medium", "low"]},
            "why": {"type": "string",
                    "description": "One line tying the pick to THIS "
                                   "user's record — which past miss "
                                   "or stated preference makes this "
                                   "the item worth drilling now."},
        },
        "required": ["item_id", "p_miss", "why"],
    },
}

_RANK_SYSTEM = """You are the selection brain of a learning coach. \
From the candidate bank items, choose the ONE to drill next for \
THIS user, using their attempt record, their style notes, and their \
standing content preferences (the preferences govern scope and \
style — obey them).

mode=likely_miss (the default): pick the item this user is MOST \
LIKELY TO GET WRONG that is worth knowing — their record shows \
which structures slip (derive it from the record, e.g. numbers, \
attributions, list tails). A detail they'd miss beats a concept \
they hold.

mode=likely_hit_probe: this turn verifies the prediction system in \
the other direction — pick the item they are most likely to get \
RIGHT."""


def rank_select(user_id, track, client=None):
    """② the live selection: code narrows candidates, the model
    ranks them against the user's record, notes, and content
    preferences. Falls back to the deterministic scorer on any
    failure — a selection call must never cost the user their
    question."""
    track_id = track["id"]
    items = [i for i in db.get_knowledge_items(track_id)
             if i["status"] in _ASKABLE]
    if not items:
        return None, "no askable items"
    attempts = db.get_attempts(track_id, limit=500)
    fresh = [i for i in items
             if i["id"] not in _recent_item_ids(user_id, attempts)] \
        or items
    if len(fresh) == 1:
        return fresh[0], "only fresh candidate"
    n_preds = len(db.get_predictions(user_id, limit=500))
    probe = bool(n_preds) and n_preds % LIKELY_HIT_EVERY \
        == LIKELY_HIT_EVERY - 1
    last_by_item = {}
    for a in reversed(attempts):
        if a.get("item_id"):
            last_by_item[a["item_id"]] = a["verdict"]
    payload = {
        "mode": "likely_hit_probe" if probe else "likely_miss",
        "candidates": [
            {"item_id": i["id"], "stem": i["stem"], "kind": i["kind"],
             "est_difficulty": i["est_difficulty"],
             "status": i["status"],
             "last_verdict": last_by_item.get(i["id"], "never asked")}
            for i in fresh[:15]],
        "attempt_record": _record_summary(track_id),
        "style_notes": [n["observation"]
                        for n in db.get_person_notes(user_id)][-12:],
        "content_preferences": _prefs_block(user_id),
    }
    try:
        client = client or anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=400, system=_RANK_SYSTEM,
            messages=[{"role": "user", "content":
                       json.dumps(payload, ensure_ascii=False)}],
            tools=[_RANK_TOOL],
            tool_choice={"type": "tool", "name": "submit_selection"})
        p = next((b.input for b in resp.content
                  if getattr(b, "type", "") == "tool_use"), None)
        db.save_llm_call(user_id, "drill_select", MODEL, _RANK_SYSTEM,
                         [{"role": "user", "content": "(candidates)"}],
                         {}, json.dumps(p, ensure_ascii=False)
                         if p else "")
        pick = next((i for i in fresh
                     if i["id"] == (p or {}).get("item_id")), None)
        if pick is None:
            raise ValueError(
                f"invalid item_id {(p or {}).get('item_id')}")
        mode = "probe: " if probe else ""
        return pick, (f"{mode}p_miss={p.get('p_miss')} — "
                      f"{p.get('why', '')}")
    except Exception as e:
        print(f"[DRILL] ⚠️ rank_select failed ({e}) — falling back "
              f"to scorer", flush=True)
        db.log_event(user_id, "drill_error",
                     {"where": "rank_select", "error": str(e)[:200]},
                     source="server")
        item, why = select_item(user_id, track_id)
        return item, f"(fallback scoring) {why}"


def person_block(user_id, limit=10):
    """④ the person ledger, rendered for the question-writing
    prompt: how this user answers, what phrasing works — shaping
    input, never content to recite at them."""
    notes = db.get_person_notes(user_id)
    if not notes:
        return ""
    lines = ["## How this user answers (person ledger — use it to "
             "shape the question; never recite it at them)"]
    lines += [f"- {n['observation']}" for n in notes[-limit:]]
    return "\n".join(lines)


_PREDICT_TOOL = {
    "name": "submit_prediction",
    "description": "Record the performance prediction for this item.",
    "input_schema": {
        "type": "object",
        "properties": {
            "predicted_verdict": {
                "type": "string",
                "enum": ["complete", "partial", "missed"]},
            "predicted_difficulty": {
                "type": "integer", "minimum": 1, "maximum": 4},
            "reason": {
                "type": "string",
                "description": "One line: which element will break, "
                               "or why it will hold."},
        },
        "required": ["predicted_verdict", "predicted_difficulty",
                     "reason"],
    },
}

_PREDICT_SYSTEM = """You are the prediction module of a learning \
coach. Before a drill question is sent, you predict how THIS user \
will perform on it, from their ledgers. The prediction is recorded \
before the question goes out and scored against the real answer — \
it is the coach's accountability metric, so predict what you \
actually believe, not what would look supportive.

Everything you know about this user is in the data provided — \
their attempt history (what they missed, element by element) and \
the style notes. Derive their weak structures from that record; \
you have no other knowledge of them."""


def _predict(user_id, item, track_id, client=None):
    """Record the prediction for an item — BEFORE the question exists.
    Never raises: if the model call fails, a fallback prediction is
    recorded with the failure in its reason. The loop stays intact
    and the record stays honest."""
    attempts = db.get_attempts(track_id, limit=60)
    notes = db.get_person_notes(user_id)
    compact = {
        "item": {"stem": item["stem"], "elements": item["elements"],
                 "kind": item["kind"],
                 "est_difficulty": item["est_difficulty"],
                 "status": item["status"]},
        "recent_attempts": [
            {"question": a["question"][:120], "verdict": a["verdict"],
             "self_confidence": a["self_confidence"],
             "missed_elements": [e["name"] for e in a.get("elements", [])
                                 if e.get("verdict") == "miss"]}
            for a in attempts[:15]],
        "style_notes": [n["observation"] for n in notes][:12],
    }
    try:
        client = client or anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=300, system=_PREDICT_SYSTEM,
            messages=[{"role": "user", "content":
                       json.dumps(compact, ensure_ascii=False)}],
            tools=[_PREDICT_TOOL],
            tool_choice={"type": "tool", "name": "submit_prediction"})
        p = next((b.input for b in resp.content
                  if getattr(b, "type", "") == "tool_use"), None)
        db.save_llm_call(user_id, "drill_predict", MODEL,
                         _PREDICT_SYSTEM,
                         [{"role": "user", "content": "(ledgers)"}],
                         {}, json.dumps(p, ensure_ascii=False)
                         if p else "")
        if not p:
            raise ValueError("no tool call")
        return db.record_prediction(
            item["id"], user_id, p["predicted_verdict"],
            p.get("predicted_difficulty"), p.get("reason", ""))
    except Exception as e:
        print(f"[DRILL] ⚠️ prediction call failed: {e}", flush=True)
        return db.record_prediction(
            item["id"], user_id, "partial",
            item.get("est_difficulty"),
            f"(fallback: prediction call failed: {str(e)[:120]})")


def _last_graded_attempt(user_id, track_id, within_days=7):
    """The most recent item-linked attempt, with ITS item's anchor —
    so the next question's send can close the loop on it without
    inventing facts. (Observed in the smoke test: the second send
    'corrected' the user's answer from model memory and fabricated
    a citation the anchor contradicts.)"""
    for a in db.get_attempts(track_id, limit=10):
        if not a.get("item_id"):
            continue
        h = _age_hours(a["ts"])
        if h is None or h > within_days * 24:
            return None
        item = next((i for i in db.get_knowledge_items(track_id)
                     if i["id"] == a["item_id"]), None)
        if not item:
            return None
        return {"stem": item["stem"], "verdict": a["verdict"],
                "anchor_quote": item["anchor_quote"],
                "missed": [e["name"] for e in a.get("elements", [])
                           if e.get("verdict") in ("miss", "partial")]}
    return None


def prepare_scheduled_question(user_id, client=None, record=True):
    """Everything the send path needs to fire a drill question, or
    None if this user isn't a drill user / has nothing to ask.

    If an open question is still unanswered (< OPEN_QUESTION_H), the
    same item is re-asked and NO second prediction is recorded — one
    question, one prediction, one scoring.

    record=False is the preview mode (operator prompt inspection):
    selection runs read-only and NO prediction is written — the
    prediction never renders into the prompt, so the preview is
    byte-identical to what a real send would assemble.
    """
    track = active_drill_track(user_id)
    if not track:
        return None
    open_pred = db.get_open_prediction(
        user_id, within_hours=OPEN_QUESTION_H)
    last_graded = _last_graded_attempt(user_id, track["id"])
    if open_pred:
        item = next((i for i in db.get_knowledge_items(track["id"])
                     if i["id"] == open_pred["item_id"]), None)
        # A suspended item (user complaint, '그만 물어봐') must not be
        # re-asked just because its prediction is still open — fall
        # through to fresh selection and let the prediction age out.
        if item and item["status"] in _ASKABLE:
            return {"track": track, "item": item,
                    "prediction_id": open_pred["id"], "reask": True,
                    "why": "yesterday's question is still open",
                    "last_graded": last_graded,
                    "source_context": _anchor_window(
                        _material_text(user_id),
                        item.get("anchor_quote", ""))}
    item, why = rank_select(user_id, track, client=client)
    if item is None:
        return None
    pred_id = (_predict(user_id, item, track["id"], client=client)
               if record else None)
    return {"track": track, "item": item, "prediction_id": pred_id,
            "reask": False, "why": why, "last_graded": last_graded,
            "source_context": _anchor_window(_material_text(user_id),
                                             item.get("anchor_quote",
                                                      ""))}


def question_block(ctx):
    """The system-prompt block that hands the planner today's item.
    The elements are the grading rubric — the question must REQUIRE
    them without enumerating them."""
    item = ctx["item"]
    lines = [
        "## Today's drill item (server-selected — ask THIS, "
        "nothing else)",
        f"Track: {ctx['track']['name']}",
        f"Topic to probe: {item['stem']}",
        "A complete answer would cover (do NOT list these in the "
        "question — they are the rubric, not the prompt):",
    ]
    lines += [f"- {e}" for e in item["elements"]]
    lines += [
        f"Source anchor (verbatim from their material): "
        f"\"{item['anchor_quote']}\"" if item["anchor_quote"] else
        "Source anchor: (none — canonical item)",
        f"Why this item today: {ctx['why']}",
    ]
    passage = (ctx.get("source_context") or "").strip()
    if passage:
        lines += [
            "",
            "THE PASSAGE (the anchor's verbatim surroundings — the "
            "question must make sense within this passage alone; a "
            "fact from elsewhere in their document, however true, "
            "does not belong in this question):",
            f"\"\"\"{passage}\"\"\"",
        ]
    if ctx.get("reask"):
        lines.append(
            "This question is STILL OPEN from a previous send — they "
            "never answered. Re-raise it lightly (one line of 'still "
            "curious about...'), don't pretend it's new.")
    lg = ctx.get("last_graded")
    if lg:
        lines += [
            "",
            f"Their previous drill answer (on: {lg['stem']}) was "
            f"already graded server-side: {lg['verdict']}"
            + (f", weaker on {', '.join(lg['missed'])}" if lg["missed"]
               else "") + ".",
            "If you close that loop before today's question, ground "
            "every factual claim in ITS anchor:",
            f"\"{lg['anchor_quote']}\"" if lg["anchor_quote"] else
            "(no anchor — canonical item)",
            "Anything that anchor does not settle, do NOT assert — "
            "not a name, not a year, not a case. Model memory is how "
            "fabrications happen; say you'd check the document "
            "instead.",
        ]
    return "\n".join(lines)


# ── contents generation — the bank's production line ────────────────
#
# The bank is inventory, and inventory drains: every graded answer
# moves an item out of 'untested'. This module is the matching
# production side — user-agnostic by design (the husband's instance
# is with_material over his PDF; a future track may be a textbook
# range or a canonical exam). Called with n at init (~20) and then
# rolling: the low-water check after each grading tops the bank
# back up in the background.

BANK_TARGET = 20     # standing inventory of untested items
BANK_LOW_WATER = 5   # below this, grading triggers a background top-up
_MAX_MINE_PER_CALL = 12

_MINE_SYSTEM = """You are the contents-creation module of a learning \
coach. Mine {n} NEW question-bank items from the user's own material.

THE USER'S STANDING CONTENT PREFERENCES (these govern what you mine):
{prefs}

THE USER'S RECORD (mine MORE of what this record shows them missing \
— and skip concept-level material they demonstrably hold):
{record}

THE USER'S STYLE NOTES (how they answer — calibrate stems and \
elements to the person, e.g. precise fact patterns for someone who \
clarifies setups before answering):
{notes}

Rules:
- anchor_quote MUST be a verbatim passage (8-25 words) copied \
EXACTLY from the material. The server verifies by exact substring \
match and rejects the item otherwise. Never paraphrase inside the \
quote.
- ONE item = ONE passage. Every element must be grounded in the \
text surrounding your anchor — never combine facts from different \
parts of the material into one item (the server checks each item \
against its own passage and rejects splices; field failure: \
questions "no human reading the document would ask").
- Do NOT duplicate the existing bank stems (provided).
- elements = the pieces a complete answer must contain, calibrated \
to what a senior practitioner answering a colleague must say.
- Output ONLY a JSON array (escape internal double quotes):
[{{"stem": ..., "anchor_quote": ..., "section_hint": ..., \
"elements": [...], "kind": "numeric_comparison|multi_part|exception\
|attribution|procedure|concept", "est_difficulty": 1-4}}]"""

_REANCHOR_SYSTEM = """Each item below was REJECTED because its \
anchor_quote is not an exact substring of the material — it was \
paraphrased. For each item, find a real verbatim passage (8-25 \
words, copied character-for-character) that grounds the same stem, \
and return the SAME items with only anchor_quote corrected. If the \
material truly has no passage for a stem, drop that item. Output \
ONLY the JSON array (escape internal double quotes)."""


def _norm_text(s):
    s = (s or "").replace("“", '"').replace("”", '"')
    s = s.replace("‘", "'").replace("’", "'")
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).lower()


def _material_text(user_id):
    """The with_material source: extracted text of the user's file
    materials. '' when the track has nothing to mine from."""
    try:
        mats = db.get_user_materials(user_id)
    except Exception:
        return ""
    return "\n\n".join(
        (m.get("extracted_text") or "") for m in mats
        if m.get("kind") == "file"
        and (m.get("extracted_text") or "").strip())


def _record_summary(track_id):
    lines = []
    for a in db.get_attempts(track_id, limit=40):
        missed = [e["name"] for e in a.get("elements", [])
                  if e.get("verdict") != "hit"]
        lines.append(
            f"- [{a['source']}/{a['verdict']}] {a['question'][:110]}"
            + (f" — weaker on: {'; '.join(missed)}" if missed else ""))
    return "\n".join(lines) or "(no attempts yet)"


def _prefs_block(user_id):
    try:
        prefs = db.get_user_preferences(user_id)
    except Exception:
        prefs = {}
    if not prefs:
        return "(none stated — mine broadly, hardest-to-retain first)"
    return "\n".join(f"- {k}: {v['value']}" for k, v in prefs.items())


_CONTEXT_WINDOW = 1200   # chars of source on each side of an anchor

_VERIFY_TOOL = {
    "name": "submit_verification",
    "description": "Verify each item's elements against its own "
                   "passage.",
    "input_schema": {
        "type": "object",
        "properties": {
            "verdicts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "supported": {"type": "boolean"},
                        "foreign_elements": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Elements NOT supported by "
                                           "this item's own passage.",
                        },
                    },
                    "required": ["index", "supported"],
                },
            },
        },
        "required": ["verdicts"],
    },
}

_VERIFY_SYSTEM = """You are the intake verifier of a question bank. \
For each candidate item you get the item's stem and elements, plus \
THE PASSAGE — the source text surrounding the item's own anchor. \
Field failure this prevents (reported by a real user): items that \
splice facts from DIFFERENT parts of a 50-page document into one \
frame, producing questions "no human reading the document would \
ask". An element is supported only if THIS passage alone grounds \
it; knowledge from elsewhere in the document — however true — makes \
it a foreign element. Judge each item independently; be strict."""


def _anchor_window(source, anchor):
    """The passage around an anchor occurrence in the source, or ''
    when the anchor cannot be located (normalization mismatch)."""
    ns, na = _norm_text(source), _norm_text(anchor)
    pos = ns.find(na)
    if pos < 0 or not na:
        return ""
    # Map the normalized hit back to raw text approximately by
    # proportional position — exact mapping is overkill for a
    # context window.
    ratio = pos / max(1, len(ns))
    center = int(ratio * len(source))
    lo = max(0, center - _CONTEXT_WINDOW)
    hi = min(len(source), center + _CONTEXT_WINDOW + len(anchor))
    return source[lo:hi]


def verify_items(items, source, client=None):
    """Intake coherence check: every element must be supported by
    the item's OWN passage. Returns (ok, rejected) where rejected
    items carry a 'foreign_elements' field. Anchors are assumed
    already substring-verified. On verifier failure everything
    passes (the anchor check remains the hard floor — a broken
    verifier must not empty the bank)."""
    if not items:
        return [], []
    payload = []
    for i, it in enumerate(items):
        payload.append({
            "index": i, "stem": it.get("stem", ""),
            "elements": it.get("elements", []),
            "passage": _anchor_window(source,
                                      it.get("anchor_quote", "")),
        })
    try:
        client = client or anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=2000, system=_VERIFY_SYSTEM,
            messages=[{"role": "user", "content":
                       json.dumps(payload, ensure_ascii=False)}],
            tools=[_VERIFY_TOOL],
            tool_choice={"type": "tool",
                         "name": "submit_verification"})
        v = next((b.input for b in resp.content
                  if getattr(b, "type", "") == "tool_use"), None)
        verdicts = {d["index"]: d for d in (v or {}).get("verdicts", [])}
    except Exception as e:
        print(f"[DRILL] ⚠️ intake verifier failed ({e}) — items pass "
              f"on the anchor check alone", flush=True)
        return list(items), []
    ok, rejected = [], []
    for i, it in enumerate(items):
        d = verdicts.get(i)
        if d is None or d.get("supported"):
            ok.append(it)
        else:
            it = dict(it)
            it["foreign_elements"] = d.get("foreign_elements", [])
            rejected.append(it)
    return ok, rejected


def _json_array_call(client, system, user, max_tokens):
    """One model call that must yield a JSON array; one re-ask on
    invalid JSON (observed: unescaped quotes inside verbatim legal
    anchors)."""
    messages = [{"role": "user", "content": user}]
    for attempt in (1, 2):
        resp = client.messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            messages=messages)
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", "") == "text")
        s = text.find("[")
        try:
            return json.loads(text[s:text.rfind("]") + 1])
        except (json.JSONDecodeError, ValueError) as e:
            if attempt == 2:
                raise
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                 f"Your output is invalid JSON ({e}). Re-output the "
                 f"ENTIRE array as strict valid JSON — escape every "
                 f'internal double quote inside strings as \\".'}]


def generate_items(user_id, track, n, client=None):
    """Mine up to n new items into the track's bank. Returns the
    number actually added. Anchors are verified as verbatim
    substrings of the material BY CODE — mine → verify → re-anchor
    once → drop what still fails. Fabrication dies at intake, not
    at grading."""
    n = min(n, _MAX_MINE_PER_CALL)
    if n <= 0:
        return 0
    source = _material_text(user_id)
    if not source.strip():
        print(f"[DRILL] no material text for {user_id} — "
              f"bank top-up skipped", flush=True)
        return 0
    norm_source = _norm_text(source)
    existing = db.get_knowledge_items(track["id"])
    client = client or anthropic.Anthropic()

    notes = [p["observation"] for p in db.get_person_notes(user_id)]
    system = _MINE_SYSTEM.format(
        n=n, prefs=_prefs_block(user_id),
        record=_record_summary(track["id"]),
        notes="\n".join(f"- {o}" for o in notes[-12:]) or "(none yet)")
    user_msg = ("EXISTING BANK STEMS (do not duplicate):\n"
                + "\n".join(f"- {i['stem']}" for i in existing)
                + f"\n\nMATERIAL:\n{source}")
    try:
        mined = _json_array_call(client, system, user_msg, 8000)
    except Exception as e:
        print(f"[DRILL] ⚠️ mine call failed for {user_id}: {e}",
              flush=True)
        db.log_event(user_id, "drill_error",
                     {"where": "generate_items", "error": str(e)[:300]},
                     source="server")
        return 0

    def anchored(items):
        ok, bad = [], []
        for it in items:
            q = _norm_text(it.get("anchor_quote", ""))
            (ok if q and q in norm_source else bad).append(it)
        return ok, bad

    ok, bad = anchored(mined)
    if bad:
        try:
            fixed = _json_array_call(
                client, _REANCHOR_SYSTEM,
                "REJECTED ITEMS:\n"
                + json.dumps(bad, ensure_ascii=False)
                + f"\n\nMATERIAL:\n{source}", 6000)
            re_ok, still_bad = anchored(fixed)
            ok += re_ok
        except Exception as e:
            print(f"[DRILL] re-anchor call failed: {e}", flush=True)
            still_bad = bad
    else:
        still_bad = []

    ok, spliced = verify_items(ok, source, client=client)
    for it in spliced:
        print(f"[DRILL] ✗ splice rejected: {it.get('stem','')[:60]} "
              f"(foreign: {', '.join(it.get('foreign_elements', []))[:80]})",
              flush=True)

    added = 0
    seen = {_norm_text(i["stem"]) for i in existing}
    for it in ok[:n]:
        if _norm_text(it.get("stem", "")) in seen:
            continue
        try:
            db.add_knowledge_item(
                track["id"], user_id, stem=it.get("stem", ""),
                anchor_type="file_chunk",
                anchor_quote=it.get("anchor_quote", ""),
                section_hint=it.get("section_hint", ""),
                elements=it.get("elements"),
                kind=it.get("kind", ""),
                est_difficulty=it.get("est_difficulty", 2),
                source="contents_module")
            added += 1
        except ValueError:
            continue
    db.log_event(user_id, "bank_refilled",
                 {"track_id": track["id"], "requested": n,
                  "added": added, "rejected": len(still_bad),
                  "splice_rejected": len(spliced)},
                 source="server")
    print(f"[DRILL] bank refilled for {user_id}: +{added} "
          f"(requested {n}, {len(still_bad)} dropped unanchored)",
          flush=True)
    return added


def _topup_if_low(user_id, track):
    """The rolling production trigger: consumption happens through
    grading, so grading checks the untested inventory and refills in
    the background when it runs low."""
    untested = db.get_knowledge_items(track["id"], status="untested")
    if len(untested) >= BANK_LOW_WATER:
        return
    need = BANK_TARGET - len(untested)
    import threading
    threading.Thread(target=generate_items,
                     args=(user_id, track, need), daemon=True).start()
    print(f"[DRILL] bank low ({len(untested)} untested) — "
          f"background top-up of {need} started", flush=True)


def leaks_answer(text, item):
    """True if a drill-question draft contains the answer key: the
    anchor quote, or a majority of the rubric elements verbatim.
    Observed in the smoke test — the model narrated its planning
    ('The anchor shows these are Berkshire Hathaway (1996), ...')
    straight into the outgoing message. A leaked question is worse
    than no question: it grades as a fake 'complete' and poisons
    the ledgers."""
    t = re.sub(r"\s+", " ", text or "").lower()
    anchor = re.sub(r"\s+", " ", item.get("anchor_quote") or "").lower()
    if len(anchor) >= 12 and anchor in t:
        return True
    els = [re.sub(r"\s+", " ", e).lower()
           for e in item.get("elements", [])]
    els = [e for e in els if len(e) >= 8]
    if not els:
        return False
    hits = sum(1 for e in els if e in t)
    return hits >= max(2, (len(els) + 1) // 2)


_GRADE_TOOL = {
    "name": "submit_grading",
    "description": "Grade the user's reply against the open drill "
                   "question, or report that it isn't an answer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "is_answer": {
                "type": "boolean",
                "description": "false if the reply doesn't engage the "
                               "question (smalltalk, a different "
                               "topic, 'skip') — then grade nothing."},
            "verdict": {"type": "string",
                        "enum": ["complete", "partial", "missed"]},
            "elements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "verdict": {"type": "string",
                                    "enum": ["hit", "partial", "miss"]},
                    },
                    "required": ["name", "verdict"]}},
            "self_confidence": {
                "type": "string", "enum": ["high", "medium", "low"],
                "description": "From THEIR OWN hedging language only."},
            "confidence_marker": {
                "type": "string",
                "description": "Verbatim hedge phrase, or ''."},
            "style_note": {
                "type": "string",
                "description": "OPTIONAL condition→response teaching "
                               "note, what-works phrasing. '' unless "
                               "something genuinely new showed."},
            "style_evidence": {
                "type": "string",
                "description": "Verbatim quote backing style_note. "
                               "No quote → no note."},
            "question_complaint": {
                "type": "string",
                "description": "If the reply criticizes the QUESTION "
                               "itself (wrong, nonsensical, mixes "
                               "unrelated things, not from the "
                               "material): their objection, one "
                               "line. '' otherwise. Hedging inside "
                               "an answer is NOT a complaint."},
            "correction_of_coach": {
                "type": "string",
                "description": "If the reply CORRECTS something the "
                               "coach claimed, the corrected content "
                               "(one line). '' otherwise. A wrong "
                               "drill answer is NOT a correction."},
        },
        "required": ["is_answer"],
    },
}

_GRADE_SYSTEM = """You are the grading pass of a learning coach. An \
open drill question is outstanding; the user just replied. Grade \
the reply against the rubric elements, or report is_answer=false if \
it doesn't engage the question.

Rules:
- Grade against the RUBRIC ELEMENTS and the source anchor, not \
against your own knowledge of the domain. If the user says \
something the anchor contradicts, that element is a miss; if the \
user says something beyond the rubric, ignore it.
- self_confidence comes only from their own hedging language \
("I think", "not sure", "아마") — an unhedged fluent answer is \
high even if wrong.
- One answer, four signals: element verdicts, overall verdict, \
self_confidence, and optionally one style observation (with a \
verbatim quote, or leave it empty).
- A wrong answer under quiz pressure is a mistake-log entry, \
never a teaching. Only an explicit correction of the coach's own \
claim goes to correction_of_coach.
- If the reply objects to the QUESTION itself ("this question \
mixes two different rules", "that's not what my file says"), \
report question_complaint — the user is the bank's debugger, and \
their objection retires the item."""

_VALID_VERDICTS = ("complete", "partial", "missed")


def grade_if_answering(user_id, client=None):
    """The four-signals pass. If an open drill question exists and
    the user's latest message answers it: record the attempt, score
    the prediction, move the item's status, and file any style note
    or coach-correction. Returns a summary dict for the reply
    prompt, or None. Never raises — grading must not break the
    reply path."""
    try:
        open_pred = db.get_open_prediction(
            user_id, within_hours=OPEN_QUESTION_H)
        if not open_pred:
            return None
        track = active_drill_track(user_id)
        if not track:
            return None
        item = next((i for i in db.get_knowledge_items(track["id"])
                     if i["id"] == open_pred["item_id"]), None)
        if not item or item["status"] == "suspended":
            return None
        history = db.get_recent_sms_messages(user_id, limit=8)
        if not history or history[-1]["role"] != "user":
            return None
        answer = history[-1]["content"]
        convo = "\n".join(
            f"{'COACH' if m['role'] == 'assistant' else 'USER'}: "
            f"{m['content']}" for m in history)
        payload = {
            "rubric": {"stem": item["stem"],
                       "elements": item["elements"],
                       "anchor_quote": item["anchor_quote"]},
            "recent_conversation": convo,
            "reply_to_grade": answer,
        }
        client = client or anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=700, system=_GRADE_SYSTEM,
            messages=[{"role": "user", "content":
                       json.dumps(payload, ensure_ascii=False)}],
            tools=[_GRADE_TOOL],
            tool_choice={"type": "tool", "name": "submit_grading"})
        g = next((b.input for b in resp.content
                  if getattr(b, "type", "") == "tool_use"), None)
        llm_call_id = db.save_llm_call(
            user_id, "drill_grade", MODEL, _GRADE_SYSTEM,
            [{"role": "user", "content": convo[-2000:]}], {},
            json.dumps(g, ensure_ascii=False) if g else "")
        if g and (g.get("question_complaint") or "").strip():
            # The user is the bank's debugger: an objection to the
            # question retires the item on the spot and files the
            # complaint for the mining loop. (Field origin: 40% of
            # the first live batch "spliced different parts of the
            # document into one frame".)
            db.set_item_status(item["id"], "suspended",
                               source="user_complaint")
            db.log_event(user_id, "drill_question_complaint",
                         {"item_id": item["id"],
                          "complaint": g["question_complaint"][:300],
                          "llm_call_id": llm_call_id},
                         source="drill")
            print(f"[DRILL] question complaint — item {item['id']} "
                  f"suspended: {g['question_complaint'][:80]}",
                  flush=True)
            # The prediction is deliberately left UNSCORED: a bad
            # question is missing data, not his miss — it ages out
            # (the KPI counts only scored rows), and the reask path
            # skips suspended items so it cannot resurface.
        if not g or not g.get("is_answer"):
            return None
        verdict = g.get("verdict")
        if verdict not in _VALID_VERDICTS:
            return None

        attempt_id = db.record_attempt(
            track["id"], user_id, verdict, item_id=item["id"],
            question=item["stem"], answer_verbatim=answer,
            elements=g.get("elements") or [],
            self_confidence=g.get("self_confidence", ""),
            confidence_marker=g.get("confidence_marker", ""))
        try:
            hit = db.score_prediction(open_pred["id"], verdict)
        except ValueError:
            hit = None      # raced with itself — grade stands, KPI safe
        db.set_item_status(
            item["id"], "solid" if verdict == "complete" else "learning",
            source="drill")
        try:
            _topup_if_low(user_id, track)
        except Exception as e:
            print(f"[DRILL] top-up check failed: {e}", flush=True)
        if (g.get("style_note") or "").strip() \
                and (g.get("style_evidence") or "").strip():
            db.add_person_note(user_id, g["style_note"].strip(),
                               evidence=g["style_evidence"].strip(),
                               confidence="low")
        if (g.get("correction_of_coach") or "").strip():
            db.add_taught(track["id"], user_id, quote=answer[:400],
                          teaching=g["correction_of_coach"].strip(),
                          kind="correction_of_coach")
        db.log_event(user_id, "drill_graded",
                     {"item_id": item["id"], "attempt_id": attempt_id,
                      "verdict": verdict,
                      "predicted": open_pred["predicted_verdict"],
                      "prediction_hit": hit,
                      "llm_call_id": llm_call_id}, source="drill")
        return {"item": item, "verdict": verdict,
                "elements": g.get("elements") or [],
                "predicted": open_pred["predicted_verdict"],
                "prediction_hit": hit}
    except Exception as e:
        print(f"[DRILL] ⚠️ grading failed for {user_id}: {e}",
              flush=True)
        return None


def graded_reply_block(graded):
    """Context block for the conversational reply right after a
    grading pass ran. The anchor quote is REQUIRED here: when the
    coach confirms or corrects, it corrects from the user's own
    material, never from model memory (the Rule 102(d)(1) class of
    fabrication dies at this line)."""
    misses = [e["name"] for e in graded["elements"]
              if e.get("verdict") in ("miss", "partial")]
    lines = [
        "## Drill answer just graded (server-side — already in the "
        "ledgers)",
        f"Overall: {graded['verdict']}"
        + (f" — weaker on: {', '.join(misses)}" if misses else ""),
        f"Source anchor (verbatim from their material): "
        f"\"{graded['item']['anchor_quote']}\""
        if graded['item']['anchor_quote'] else
        "Source anchor: (none — canonical item)",
        "When you confirm or correct, ground EVERY factual claim in "
        "the source anchor above. If the anchor doesn't settle a "
        "point, say you'd have to check the document — never fill "
        "the gap from memory.",
        "Do not recite grades or percentages at them; react like a "
        "colleague who heard the answer.",
    ]
    return "\n".join(lines)
