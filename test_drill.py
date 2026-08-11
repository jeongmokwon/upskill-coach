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
    fail = False
    planner_text = ("a client wants to sell restricted stock next "
                    "week — what do you check before they can?\n"
                    "[STEP: connect@1]\n[EXPECT: reply]")
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

                class _B:
                    type = "tool_use"
                    input = DrillFake.payload
            else:
                DrillFake.planner_system = kwargs.get("system", "")

                class _B:
                    type = "text"
                    text = DrillFake.planner_text

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

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
