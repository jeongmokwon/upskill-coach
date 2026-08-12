"""
P6 nightly-loop tests (P0-D part 2): mechanical EXPECT scoring,
note-update proposals, replan proposal flow.

Run: ./venv/bin/python test_nightly.py  (sqlite; anthropic mocked)
"""

import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)
os.environ["TZ_OFFSET_HOURS"] = "0"
os.environ["TUTOR_USER_ID"] = "test_user"
os.environ.pop("TWILIO_ACCOUNT_SID", None)

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_nightly.db")
db.init_db()

import annotate  # noqa: E402
import genplan  # noqa: E402

U = "test_user"
PASS = []
DAY = (datetime.now() - timedelta(days=1)).date().isoformat()
D = datetime.fromisoformat(DAY)


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def put(kind, payload, ts):
    db.log_event(U, kind, payload, source="test")
    conn = db.get_conn()
    conn.execute(
        "UPDATE events SET ts=? WHERE id=(SELECT MAX(id) FROM events)",
        (ts.isoformat(),))
    conn.commit()
    conn.close()


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


# ── 1. mechanical EXPECT scoring ─────────────────────────────────────
print("1) expect scoring")
# bet A: expect reply → user replied 3m later (hit)
put("sms_out", {"text": "a", "expect": "reply"}, D.replace(hour=20))
put("sms_in", {"text": "ㅇㅇ"}, D.replace(hour=20, minute=3))
# bet B: expect no_reply → silence (hit)
put("sms_out", {"text": "b", "expect": "no_reply"}, D.replace(hour=21))
# bet C: expect advance → silence (miss; mechanical treats as reply-family)
put("sms_out", {"text": "c", "expect": "advance"}, D.replace(hour=22))

n = annotate.score_expectations(U, DAY)
scored = [json.loads(e["payload"]) for e in events_of("expect_scored")]
check("three bets scored", n == 3 and len(scored) == 3)
by = {s["expect"]: s for s in scored}
check("reply bet hit", by["reply"]["hit"] is True
      and by["reply"]["actual_mechanical"] == "reply")
check("no_reply bet hit", by["no_reply"]["hit"] is True)
check("advance bet missed (silence)", by["advance"]["hit"] is False)
check("re-run dedupes", annotate.score_expectations(U, DAY) == 0
      and len(events_of("expect_scored")) == 3)

# cross-midnight reply still counts (bet at 23:00, reply next 07:00)
put("sms_out", {"text": "d", "expect": "reply"}, D.replace(hour=23))
put("sms_in", {"text": "늦게 봤어"},
    (D + timedelta(days=1)).replace(hour=7))
annotate.score_expectations(U, DAY)
late = [s for s in
        (json.loads(e["payload"]) for e in events_of("expect_scored"))
        if s["expect"] == "reply"][-1]
check("cross-midnight reply within window scores as reply",
      late["actual_mechanical"] == "reply" and late["hit"] is True)


# ── 2. note proposals ────────────────────────────────────────────────
print("2) note proposals")
db.check_and_complete_onboarding(U, force=True)
nid, _ = db.save_user_note(U, "직접 지시 잘 받음", when=["micro_ask@2"],
                           expect="advance", confidence="hypothesis")


class ToolFake:
    payload = None

    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            class _B:
                type = "tool_use"
                input = ToolFake.payload

            class _R:
                content = [_B()]
            return _R()


ToolFake.payload = {"proposals": [
    {"action": "confirm", "note_id": nid, "reason": "evidence today"},
    {"action": "retire", "note_id": "ghost123", "reason": "x"},
    {"action": "new", "claim": "새 패턴", "when": ["galaxy@2"],
     "expect": "reply", "evidence_event_ids": [1], "reason": "y"},
    {"action": "new", "claim": "저녁 8시대 답장 빠름",
     "when": ["connect@1"], "expect": "reply",
     "evidence_event_ids": [2], "reason": "z"},
]}
valid = annotate.propose_notes(U, DAY, client=ToolFake())
check("valid proposals kept, unknown note + unknown tag dropped",
      len(valid) == 2
      and {p["action"] for p in valid} == {"confirm", "new"})
ev = json.loads(events_of("note_proposals")[-1]["payload"])
check("note_proposals event carries proposals + llm_call",
      len(ev["proposals"]) == 2
      and db.get_llm_call(ev["llm_call_id"]) is not None)
check("nothing auto-applied to user_notes",
      len(db.get_user_notes(U)) == 1
      and db.get_user_notes(U)[0]["confidence"] == "hypothesis")

# ── 3. replan proposal flow ──────────────────────────────────────────
print("3) replan proposal")
db.save_sequence_plan(U, [
    {"tag": "elicit_why", "intensity": 2, "intent": "why"},
    {"tag": "micro_ask", "intensity": 1, "intent": "one line"}],
    rationale="v1", source="operator")
check("no pending replan → no reasons",
      annotate._pending_replan_reasons(U) == [])
db.log_event(U, "plan_replan_requested",
             {"reason": "user pivoted to a new goal", "trigger": "t"},
             source="sms")
reasons = annotate._pending_replan_reasons(U)
check("request newer than plan → reasons surface",
      reasons == ["user pivoted to a new goal"])

check("replan proposals retired (2026-08-12, PR-A) — genplan no "
      "longer exposes propose_replan",
      not hasattr(genplan, "propose_replan"))

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
