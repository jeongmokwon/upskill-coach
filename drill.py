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
_RECENT_DAYS = 3     # asked within this window → stand down


def active_drill_track(user_id):
    tracks = db.get_tracks(user_id, mode="drill")
    return tracks[0] if tracks else None


def _age_hours(ts):
    try:
        return (datetime.now()
                - datetime.fromisoformat(ts)).total_seconds() / 3600
    except Exception:
        return None


def select_item(user_id, track_id):
    """Pick the next item to drill. Returns (item, why) or (None, reason).

    Deterministic given the ledgers: same state, same pick — a
    selection you can rerun on your laptop to see why Theo asked
    what it asked.
    """
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

Base rates from this user's history: fluent on core mechanics; \
misses concentrate on second branches of comparisons, tails of \
multi-part lists, rarely-used exceptions, and attributions \
(which rule number says what)."""


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


def prepare_scheduled_question(user_id, client=None):
    """Everything the send path needs to fire a drill question, or
    None if this user isn't a drill user / has nothing to ask.

    If an open question is still unanswered (< OPEN_QUESTION_H), the
    same item is re-asked and NO second prediction is recorded — one
    question, one prediction, one scoring.
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
        if item:
            return {"track": track, "item": item,
                    "prediction_id": open_pred["id"], "reask": True,
                    "why": "yesterday's question is still open",
                    "last_graded": last_graded}
    item, why = select_item(user_id, track["id"])
    if item is None:
        return None
    pred_id = _predict(user_id, item, track["id"], client=client)
    return {"track": track, "item": item, "prediction_id": pred_id,
            "reask": False, "why": why, "last_graded": last_graded}


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
claim goes to correction_of_coach."""

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
        if not item:
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
