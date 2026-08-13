"""v3 drill engine tests (PR-2 — 출제 전환).

Run: ./venv/bin/python test_drill.py  (sqlite; anthropic mocked)

The claims under test: selection is deterministic and prefers
evidence of misses; the prediction is recorded BEFORE the question
and survives even a failed prediction call; an open question re-asks
instead of stacking; the morning slot fires a drill user's question
with no thread (the missing-mornings root cause); and one answer
becomes four signals in the ledgers.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "hub"
os.environ["TUTOR_USER_PHONE"] = "+15550002222"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_drill.db")
db.init_db()

import drill  # noqa: E402
import sms  # noqa: E402

PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(user, kind):
    return [dict(r, payload=json.loads(r["payload"]))
            for r in db.get_events(user, limit=500)
            if r["kind"] == kind]


class DrillFake:
    """One dispatcher for every LLM call in the test: sms and drill
    share the same anthropic module object, so patching must be
    unified. Tool-forced calls (drill's prediction/grading) return
    `payload`; plain calls (the planner) return the scripted
    question text."""
    payload = None
    payload_by_tool = {}   # route by forced tool name when set
    fail = False
    planner_text = ("a client wants to sell restricted stock next "
                    "week — what do you check before they can?\n"
                    "[STEP: connect@1]\n[EXPECT: reply]")
    planner_queue = []      # scripted multi-call sequences pop first
    planner_system = ""
    seen = []

    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            DrillFake.seen.append(kwargs)
            if kwargs.get("tools"):
                if DrillFake.fail:
                    raise RuntimeError("api down")
                _name = kwargs["tools"][0]["name"]
                _p = DrillFake.payload_by_tool.get(_name,
                                                   DrillFake.payload)

                class _B:
                    type = "tool_use"
                    input = _p
            else:
                DrillFake.planner_system = kwargs.get("system", "")
                _t = (DrillFake.planner_queue.pop(0)
                      if DrillFake.planner_queue
                      else DrillFake.planner_text)

                class _B:
                    type = "text"
                    text = _t

            class _R:
                content = [_B()]
            return _R()


drill.anthropic.Anthropic = DrillFake
sms.anthropic.Anthropic = DrillFake


# ── 1. selection ────────────────────────────────────────────────────
print("1) selection")
U1 = "sel"
db.ensure_user_profile_row(U1)
t1 = db.create_track(U1, "PDF", mode="drill")
i_missed = db.add_knowledge_item(t1, U1, stem="missed before",
                                 anchor_type="canonical",
                                 kind="exception", est_difficulty=2)
i_hard = db.add_knowledge_item(t1, U1, stem="hard untested",
                               anchor_type="canonical",
                               kind="numeric_comparison",
                               est_difficulty=4)
i_easy = db.add_knowledge_item(t1, U1, stem="easy untested",
                               anchor_type="canonical", kind="concept",
                               est_difficulty=1)
i_solid = db.add_knowledge_item(t1, U1, stem="already solid",
                                anchor_type="canonical", kind="concept",
                                est_difficulty=4)
db.set_item_status(i_solid, "solid")
old = (datetime.now() - timedelta(days=10)).isoformat()
db.record_attempt(t1, U1, "missed", item_id=i_missed, ts=old)

pick, why = drill.select_item(U1, t1)
check("a previously-missed item outranks hard untested material",
      pick["id"] == i_missed and "missed before" in why)
check("solid items are out of circulation",
      all(p["id"] != i_solid
          for p in [drill.select_item(U1, t1)[0]]))

db.record_attempt(t1, U1, "complete", item_id=i_missed)  # asked just now
pick2, why2 = drill.select_item(U1, t1)
check("recently-asked stands down (감쇠) → hard untested is next",
      pick2["id"] == i_hard and "untested" in why2)

pick_a = drill.select_item(U1, t1)
pick_b = drill.select_item(U1, t1)
check("selection is deterministic — same ledgers, same pick",
      pick_a[0]["id"] == pick_b[0]["id"])

# 4 predictions on record → the 5th send is the likely-hit probe.
for _ in range(4):
    db.record_prediction(i_hard, U1, "missed", 4, "")
pick3, why3 = drill.select_item(U1, t1)
check("every 5th send probes a likely-hit to test the predictor "
      "both ways",
      "likely-hit" in why3 and pick3["id"] == i_easy)

# ── 2. prediction before question ───────────────────────────────────
print("2) prediction")
U2 = "pred"
db.ensure_user_profile_row(U2)
t2 = db.create_track(U2, "PDF", mode="drill")
item2 = db.add_knowledge_item(t2, U2, stem="rule x",
                              anchor_type="file_chunk",
                              anchor_quote="the anchor text",
                              elements=["a", "b"], kind="multi_part",
                              est_difficulty=3)

DrillFake.payload = {"predicted_verdict": "partial",
                     "predicted_difficulty": 3,
                     "reason": "second branch risk"}
ctx = drill.prepare_scheduled_question(U2)
check("prepare returns the item with a recorded prediction",
      ctx and ctx["item"]["id"] == item2 and not ctx["reask"]
      and db.get_predictions(U2)[0]["predicted_verdict"] == "partial")
check("the prediction exists BEFORE any question was sent",
      db.get_open_prediction(U2)["id"] == ctx["prediction_id"])

ctx_re = drill.prepare_scheduled_question(U2)
check("open question → re-ask same item, NO second prediction",
      ctx_re["reask"] and ctx_re["item"]["id"] == item2
      and len(db.get_predictions(U2)) == 1)

db.score_prediction(ctx["prediction_id"], "partial")
DrillFake.fail = True
ctx_fb = drill.prepare_scheduled_question(U2)
DrillFake.fail = False
check("prediction call failure → fallback prediction recorded, "
      "loop intact and honest",
      ctx_fb and not ctx_fb["reask"]
      and "prediction call failed"
      in db.get_predictions(U2)[0]["reason"])
check("no drill track → no drill send",
      drill.prepare_scheduled_question("nobody") is None)

# ── 3. the scheduled send (root-cause fix) ──────────────────────────
print("3) scheduled send")
U3 = "hub"
db.ensure_user_profile_row(U3)
db.set_expectation_sent(U3)
t3 = db.create_track(U3, "회사 PDF", mode="drill")
item3 = db.add_knowledge_item(
    t3, U3, stem="Rule 144 volume condition",
    anchor_type="file_chunk",
    anchor_quote="greater of 1% of outstanding or average weekly",
    elements=["greater-of 구조", "1% outstanding", "4-week average"],
    kind="numeric_comparison", est_difficulty=3)


DrillFake.payload = {"predicted_verdict": "partial",
                     "predicted_difficulty": 3,
                     "reason": "second branch risk"}

sent = sms._cron_tick_for_user(U3, "+15550002222", "morning")
check("morning fires for a drill user with NO thread "
      "(no_thread_this_phase no longer eats the question)",
      sent is not None and "restricted stock" in sent)
qevents = events_of(U3, "drill_question_sent")
check("drill_question_sent event carries item + prediction",
      len(qevents) == 1
      and qevents[0]["payload"]["item_id"] == item3
      and qevents[0]["payload"]["prediction_id"]
      == db.get_open_prediction(U3)["id"])
check("sms_out trigger marks the drill path",
      events_of(U3, "sms_out")[0]["payload"]["trigger"]
      == "cron_morning_drill")
check("planner was handed the item, the rubric, and the anchor — "
      "and the drill mode prompt",
      "Rule 144 volume condition" in DrillFake.planner_system
      and "greater of 1% of outstanding" in DrillFake.planner_system
      and "one question worth answering"
      in DrillFake.planner_system.lower())

sent2 = sms._cron_tick_for_user(U3, "+15550002222", "morning")
check("second tick re-asks the open question (one prediction still)",
      sent2 is not None and len(db.get_predictions(U3)) == 1
      and events_of(U3, "drill_question_sent")[-1]["payload"]["reask"])

# ── 4. grading: one answer, four signals ────────────────────────────
print("4) grading")
db.save_sms_message(U3, "user",
                    "manner of sale and volume — I think it's 1% of "
                    "outstanding, not sure about the averaging part",
                    "in")
DrillFake.payload = {
    "is_answer": True, "verdict": "partial",
    "elements": [{"name": "greater-of 구조", "verdict": "miss"},
                 {"name": "1% outstanding", "verdict": "hit"},
                 {"name": "4-week average", "verdict": "partial"}],
    "self_confidence": "low",
    "confidence_marker": "not sure about the averaging part",
    "style_note": "hedges precisely where he is actually weak → his "
                  "own uncertainty is a reliable signal",
    "style_evidence": "not sure about the averaging part",
    "correction_of_coach": "",
}
graded = drill.grade_if_answering(U3)
attempts = db.get_attempts(t3)
check("attempt recorded with element verdicts + self_confidence",
      graded and graded["verdict"] == "partial"
      and attempts[0]["item_id"] == item3
      and attempts[0]["self_confidence"] == "low"
      and any(e["verdict"] == "miss" for e in attempts[0]["elements"]))
check("prediction scored against the real verdict (hit)",
      db.get_open_prediction(U3) is None
      and db.prediction_stats(U3)["hits"] == 1
      and graded["prediction_hit"] is True)
check("item moved untested → learning on a partial",
      db.get_knowledge_items(t3)[0]["status"] == "learning")
check("style note filed with verbatim evidence",
      any("uncertainty" in n["observation"]
          for n in db.get_person_notes(U3)))
check("a wrong drill answer is NOT a teaching",
      db.get_taught(t3) == [])
check("drill_graded event ties attempt to prediction",
      events_of(U3, "drill_graded")[0]["payload"]["prediction_hit"])

block = drill.graded_reply_block(graded)
check("reply block grounds correction in the anchor quote",
      "greater of 1% of outstanding" in block
      and "never fill the gap" in block)

# Non-answer: nothing is written.
DrillFake.payload = {"predicted_verdict": "missed",
                     "predicted_difficulty": 4, "reason": "tail risk"}
sms._cron_tick_for_user(U3, "+15550002222", "evening")
db.save_sms_message(U3, "user", "바빠서 이따 할게", "in")
DrillFake.payload = {"is_answer": False}
check("a non-answer grades nothing and the question stays open",
      drill.grade_if_answering(U3) is None
      and db.get_open_prediction(U3) is not None
      and len(db.get_attempts(t3)) == 1)

# A correction of the coach's claim DOES reach the taught ledger.
db.save_sms_message(U3, "user",
                    "no — 102(d)(1) is the ADTV exception, you're "
                    "mixing it up", "in")
DrillFake.payload = {
    "is_answer": True, "verdict": "complete",
    "elements": [{"name": "greater-of 구조", "verdict": "hit"}],
    "self_confidence": "high", "confidence_marker": "",
    "correction_of_coach": "102(d)(1) is the ADTV exception, not a "
                           "hedging carveout",
}
drill.grade_if_answering(U3)
check("an explicit correction of the coach lands in the taught "
      "ledger",
      any(t["kind"] == "correction_of_coach"
          for t in db.get_taught(t3)))

# ── 4b. answer-leak guard + loop-closing anchor ─────────────────────
print("4b) leak guard & loop-closing anchor")
item3_row = db.get_knowledge_items(t3)[0]
check("leaks_answer: anchor verbatim or majority-of-rubric = leak; "
      "a clean question is not",
      drill.leaks_answer("the anchor shows greater of 1% of "
                         "outstanding or average weekly", item3_row)
      and drill.leaks_answer("covers 1% outstanding and the 4-week "
                             "average", item3_row)
      and not drill.leaks_answer("a client wants to sell restricted "
                                 "stock — what do you check?",
                                 item3_row))

# item3 went solid on the 'complete' above — give the bank a fresh
# item so selection has something to serve.
item3b = db.add_knowledge_item(
    t3, U3, stem="Regulation BTR blackout trading ban",
    anchor_type="file_chunk",
    anchor_quote="no directors or officers may trade during the "
                 "pension blackout",
    elements=["blackout period definition", "pension fund condition"],
    kind="exception", est_difficulty=3)
DrillFake.payload = {"predicted_verdict": "partial",
                     "predicted_difficulty": 3, "reason": "r"}
ctx_lg = drill.prepare_scheduled_question(U3)
check("prepare carries the last graded attempt WITH its anchor "
      "(loop-closing stays inside the material)",
      ctx_lg["last_graded"]
      and ctx_lg["last_graded"]["anchor_quote"]
      == "greater of 1% of outstanding or average weekly"
      and "greater of 1% of outstanding"
      in drill.question_block(ctx_lg))

leaky = ("reminder: no directors or officers may trade during the "
         "pension blackout — now, when does that apply?\n"
         "[STEP: spark_curiosity@2]")
DrillFake.planner_text = leaky
n_leak_before = len(events_of(U3, "drill_answer_leak"))
sent_leak = sms._cron_tick_for_user(U3, "+15550002222", "evening")
DrillFake.planner_text = ("a client wants to sell restricted stock "
                          "next week — what do you check before "
                          "they can?\n[STEP: connect@1]"
                          "\n[EXPECT: reply]")
check("a question that ships its own answer key is held, twice-"
      "checked, and logged",
      sent_leak is None
      and len(events_of(U3, "drill_answer_leak")) - n_leak_before == 2
      and events_of(U3, "cron_tick")[-1]["payload"]["reason"]
      == "drill_answer_leak")

# ── 4c. preview mode is read-only ───────────────────────────────────
print("4c) preview mode")
open_pv = db.get_open_prediction(U3)
ctx_pv_reask = drill.prepare_scheduled_question(U3, record=False)
check("preview of an open question re-asks with the EXISTING "
      "prediction (still nothing written)",
      ctx_pv_reask["reask"]
      and ctx_pv_reask["prediction_id"] == open_pv["id"])
db.score_prediction(open_pv["id"], "missed")   # close it out
n_preds_before = len(db.get_predictions(U3))
ctx_pv = drill.prepare_scheduled_question(U3, record=False)
check("record=False on a fresh selection writes NO prediction "
      "(operator preview is read-only)",
      ctx_pv is not None and ctx_pv["prediction_id"] is None
      and not ctx_pv["reask"]
      and len(db.get_predictions(U3)) == n_preds_before)

# ── 5. non-drill users untouched ────────────────────────────────────
print("5) non-drill users")
U5 = "plain"
db.ensure_user_profile_row(U5)
db.set_expectation_sent(U5)
sent5 = sms._cron_tick_for_user(U5, "+15550005555", "morning")
check("no track → morning keeps the old thread-keeping gate (skip)",
      sent5 is None
      and events_of(U5, "cron_tick")[-1]["payload"]["reason"]
      == "no_thread_this_phase")
check("no track → inbound grading is a no-op",
      drill.grade_if_answering(U5) is None)

# ── 6. contents module + rebank ─────────────────────────────────────
print("6) contents module")
U6 = "bank6"
db.ensure_user_profile_row(U6)
t6 = db.create_track(U6, "PDF", mode="drill")

try:
    db.add_knowledge_item(t6, U6, stem="no anchor, no pen",
                          anchor_type="file_chunk")
    check("anchorless item outside the needs_anchor pen still raises",
          False)
except ValueError:
    check("anchorless item outside the needs_anchor pen still raises",
          True)
held = db.add_knowledge_item(t6, U6, stem="ask the user for grounding",
                             anchor_type="file_chunk",
                             status="needs_anchor")
live = db.add_knowledge_item(t6, U6, stem="normal item",
                             anchor_type="canonical", kind="concept",
                             est_difficulty=2)
pick6, _ = drill.select_item(U6, t6)
check("needs_anchor items exist but never circulate",
      pick6["id"] == live
      and all(i["id"] != held
              for i in [pick6]))

a6 = db.record_attempt(t6, U6, "partial", item_id=live,
                       question="q", answer_verbatim="ans")
wiped = db.delete_track_items(t6)
check("wipe clears the bank but keeps attempts (links nulled)",
      wiped == 2 and db.get_knowledge_items(t6) == []
      and db.get_attempts(t6)[0]["item_id"] is None)

MATERIAL = ("Section 1. The cooling-off period for former "
            "affiliates is three months after affiliate status "
            "ends. Section 2. The volume limit is the greater of "
            "one percent of outstanding or four-week average "
            "weekly volume reported.")
db.add_user_material(U6, "file", title="notes.docx",
                     extracted_text=MATERIAL)
good = {"stem": "cooling-off for former affiliates",
        "anchor_quote": "cooling-off period for former affiliates "
                        "is three months",
        "section_hint": "s1", "elements": ["three months",
                                           "after status ends"],
        "kind": "numeric_comparison", "est_difficulty": 3}
bad = {"stem": "volume limit calculation",
       "anchor_quote": "the volume cap is 1% or the 4-week ADTV",
       "section_hint": "s2", "elements": ["greater-of"],
       "kind": "numeric_comparison", "est_difficulty": 2}
fixed = dict(bad, anchor_quote="greater of one percent of "
                               "outstanding or four-week average")
DrillFake.planner_queue = [json.dumps([good, bad]),
                           json.dumps([fixed])]
added = drill.generate_items(U6, db.get_tracks(U6)[0], 5)
stems6 = [i["stem"] for i in db.get_knowledge_items(t6)]
check("mine → verify → re-anchor once: paraphrased anchor fixed via "
      "retry, both items land as untested",
      added == 2 and sorted(stems6) == ["cooling-off for former "
                                        "affiliates",
                                        "volume limit calculation"]
      and all(i["status"] == "untested"
              for i in db.get_knowledge_items(t6)))
check("bank_refilled event records the intake audit",
      events_of(U6, "bank_refilled")[-1]["payload"]["added"] == 2)

DrillFake.planner_queue = [json.dumps([bad]), json.dumps([bad])]
added2 = drill.generate_items(U6, db.get_tracks(U6)[0], 3)
check("an anchor that still fails after retry is dropped, not "
      "trusted",
      added2 == 0 and len(db.get_knowledge_items(t6)) == 2)

topped = []
real_gen = drill.generate_items
drill.generate_items = lambda u, t, n, client=None: topped.append(n)
import time as _t
drill._topup_if_low(U6, db.get_tracks(U6)[0])
for _ in range(50):
    if topped:
        break
    _t.sleep(0.05)
drill.generate_items = real_gen
check("low inventory triggers a background top-up to target "
      "(2 untested → asks for 18)",
      topped == [drill.BANK_TARGET - 2])

# ── 7. rebank endpoint ──────────────────────────────────────────────
print("7) rebank endpoint")
import asyncio  # noqa: E402

os.environ["CRON_SECRET"] = "s3cret"
import coach  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402
REBANK = {
    "user_id": U6, "track_id": t6, "wipe": True,
    "items": [
        {"stem": "seed from his answer", "anchor_type": "file_chunk",
         "anchor_quote": "volume limit is the greater of one percent",
         "elements": ["a"], "kind": "numeric_comparison",
         "est_difficulty": 2, "status": "learning",
         "link_attempt_ids": [a6]},
        {"stem": "held for his grounding",
         "anchor_type": "file_chunk", "anchor_quote": "",
         "elements": ["b"], "kind": "concept", "est_difficulty": 2,
         "status": "needs_anchor"},
        {"stem": "fresh mined", "anchor_type": "file_chunk",
         "anchor_quote": "four-week average weekly volume",
         "elements": ["c"], "kind": "procedure", "est_difficulty": 2,
         "status": "untested"},
        {"stem": "broken: anchorless but not held",
         "anchor_type": "file_chunk", "anchor_quote": "",
         "elements": [], "kind": "concept", "est_difficulty": 1,
         "status": "untested"},
    ],
}


def hit_rebank(body):
    req = make_mocked_request("POST", "/debug/rebank?secret=s3cret")

    async def _json():
        return body
    req.json = _json

    async def go():
        return await coach._rebank_handler(req)
    return asyncio.run(go())


r6 = hit_rebank(REBANK)
j6 = json.loads(r6.text)
rows6 = db.get_knowledge_items(t6)
check("rebank: wipe + reviewed set in one call, statuses preserved",
      r6.status == 200 and j6["wiped"] == 2 and j6["added"] == 3
      and sorted(i["status"] for i in rows6)
      == ["learning", "needs_anchor", "untested"])
check("seed item linked back to the attempt it was born from",
      j6["linked"] == 1
      and db.get_attempts(t6)[0]["item_id"]
      == next(i["id"] for i in rows6
              if i["stem"] == "seed from his answer"))
check("anchorless non-hold item refused and reported",
      len(j6["skipped"]) == 1 and "anchor" in j6["skipped"][0])

# ── 8. ② rank selection + ④ person block ───────────────────────────
print("8) rank select + person block")
U8 = "rank8"
db.ensure_user_profile_row(U8)
t8 = db.create_track(U8, "PDF", mode="drill")
i_hard8 = db.add_knowledge_item(t8, U8, stem="tail of the safe "
                                "harbor list", anchor_type="canonical",
                                kind="multi_part", est_difficulty=4)
i_easy8 = db.add_knowledge_item(t8, U8, stem="basic concept",
                                anchor_type="canonical",
                                kind="concept", est_difficulty=1)
db.add_person_note(U8, "clarifies the fact pattern before answering "
                       "→ give precise setups", evidence="ev",
                   confidence="high")
db.set_user_preference(U8, "drill_scope", "Article I focus",
                       evidence="Article I", source="t")

DrillFake.payload_by_tool = {
    "submit_selection": {"item_id": i_easy8, "p_miss": "high",
                         "why": "matches his list-tail slips"}}
track8 = db.get_tracks(U8)[0]
pick8, why8 = drill.rank_select(U8, track8)
check("② the model's ranked pick wins, with p_miss + record-tied "
      "reason",
      pick8["id"] == i_easy8 and "p_miss=high" in why8
      and "list-tail" in why8)
rank_call = json.loads(
    [k for k in DrillFake.seen if k.get("tools")
     and k["tools"][0]["name"] == "submit_selection"][-1]
    ["messages"][0]["content"])
check("ranking sees the record, the person notes, AND the content "
      "preferences",
      "attempt_record" in rank_call
      and any("precise setups" in s
              for s in rank_call["style_notes"])
      and "Article I focus" in rank_call["content_preferences"])

DrillFake.payload_by_tool = {
    "submit_selection": {"item_id": 99999, "p_miss": "low",
                         "why": "hallucinated id"}}
pick8b, why8b = drill.rank_select(U8, track8)
check("invalid pick → deterministic scorer fallback, labeled",
      pick8b is not None and why8b.startswith("(fallback scoring)"))
DrillFake.payload_by_tool = {}

ctx8 = {"track": track8, "item": db.get_knowledge_items(t8)[0],
        "prediction_id": 1, "reask": False, "why": "w"}
sp8, _ = sms._build_drill_prompt(U8, ctx8)
check("④ the person ledger rides in the question-writing prompt",
      "How this user answers" in sp8 and "precise setups" in sp8)

db.add_user_material(U8, "file", title="m.docx",
                     extracted_text="The safe harbor requires notice "
                                    "to the exchange within ten days "
                                    "of the transaction closing.")
DrillFake.planner_queue = [json.dumps([{
    "stem": "safe harbor notice window",
    "anchor_quote": "notice to the exchange within ten days",
    "section_hint": "s1", "elements": ["ten days", "to the exchange"],
    "kind": "numeric_comparison", "est_difficulty": 2}])]
drill.generate_items(U8, track8, 1)
mine_sys = [k for k in DrillFake.seen if not k.get("tools")
            and "contents-creation" in k.get("system", "")][-1]["system"]
check("mining also reads the person ledger now",
      "STYLE NOTES" in mine_sys and "precise setups" in mine_sys)

# ── 9. splice defense (field: 40% of first live batch) ─────────────
print("9) intake verification + complaint loop")
U9 = "spl9"
db.ensure_user_profile_row(U9)
t9 = db.create_track(U9, "PDF", mode="drill")
SRC = ("Chapter 1. The blackout period begins three days before "
       "the pension fund announcement and covers all directors. "
       + "x" * 3000 +
       " Chapter 9. The tender offer window lasts twenty business "
       "days from commencement of the offer.")
db.add_user_material(U9, "file", title="m.docx", extracted_text=SRC)

good9 = {"stem": "blackout period start",
         "anchor_quote": "blackout period begins three days before",
         "section_hint": "ch1",
         "elements": ["three days before the announcement",
                      "covers all directors"],
         "kind": "procedure", "est_difficulty": 2}
splice9 = {"stem": "blackout and tender combined",
           "anchor_quote": "blackout period begins three days before",
           "section_hint": "ch1",
           "elements": ["three days before the announcement",
                        "twenty business days tender window"],
           "kind": "multi_part", "est_difficulty": 3}
DrillFake.planner_queue = [json.dumps([good9, splice9])]
DrillFake.payload_by_tool = {
    "submit_verification": {"verdicts": [
        {"index": 0, "supported": True},
        {"index": 1, "supported": False,
         "foreign_elements": ["twenty business days tender window"]},
    ]}}
added9 = drill.generate_items(U9, db.get_tracks(U9)[0], 5)
stems9 = [i["stem"] for i in db.get_knowledge_items(t9)]
check("a spliced item (anchor ch1 + element ch9) dies at intake; "
      "the coherent one lands",
      added9 == 1 and stems9 == ["blackout period start"])
check("the intake audit records the splice rejection",
      events_of(U9, "bank_refilled")[-1]["payload"]["splice_rejected"]
      == 1)

ctx9 = drill.prepare_scheduled_question(U9, record=False)
check("the question ships with THE PASSAGE — anchor surroundings "
      "ride into the prompt",
      "THE PASSAGE" in drill.question_block(ctx9)
      and "covers all directors" in drill.question_block(ctx9)
      and "twenty business days" not in drill.question_block(ctx9))

DrillFake.payload_by_tool = {"submit_prediction": {
    "predicted_verdict": "partial", "predicted_difficulty": 2,
    "reason": "r"}}
ctx9b = drill.prepare_scheduled_question(U9)
db.save_sms_message(U9, "user",
                    "This question doesn't make sense — my file "
                    "never connects these two things.", "in")
DrillFake.payload_by_tool["submit_grading"] = {
    "is_answer": False,
    "question_complaint": "question mixes two unrelated rules"}
check("a question complaint retires the item and files the event, "
      "and grades nothing",
      drill.grade_if_answering(U9) is None
      and db.get_knowledge_items(t9)[0]["status"] == "suspended"
      and events_of(U9, "drill_question_complaint")[-1]["payload"]
      ["complaint"] == "question mixes two unrelated rules"
      and db.get_attempts(t9) == [])
check("the voided prediction stays UNscored (a bad question is "
      "missing data, not his miss)",
      db.prediction_stats(U9)["scored"] == 0)
check("the open prediction of a suspended item does not resurface "
      "as a re-ask",
      (drill.prepare_scheduled_question(U9, record=False) or
       {"item": {"id": None}})["item"]["id"]
      != db.get_knowledge_items(t9)[0]["id"])
DrillFake.payload_by_tool = {}

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
