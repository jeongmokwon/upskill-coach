"""
Sequence-plan (exploration v2) tests.

Run: ./venv/bin/python test_plan.py  (sqlite; anthropic mocked)

The structural claim under test: the LLM's prompt contains ONLY the
current step as an assignment (next step name-only), so a sequence
cannot be collapsed into one send; cursor movement happens through
[ADVANCE] judgments parsed server-side.
"""

import json
import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "test_user"
os.environ["TUTOR_USER_PHONE"] = "+15550001111"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_plan.db")
db.init_db()

import sms  # noqa: E402

U = "test_user"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


PLAN = [
    {"tag": "elicit_why", "intensity": 2,
     "intent": "open question that makes him write his why — his words"},
    {"tag": "micro_ask", "intensity": 1,
     "intent": "dictation-level 3-liner, only after he has answered"},
    {"tag": "handoff", "intensity": 1,
     "intent": "merge into the main task once hands are moving"},
]

# ── 1. plan storage + cursor state ───────────────────────────────────
print("1) plan storage")
v = db.save_sequence_plan(U, PLAN, rationale="bootstrap", source="operator")
check("plan v1 saved, cursor reset", v == 1
      and db.get_current_plan(U)["cursor"] == 0
      and len(events_of("sequence_plan_set")) == 1)

v2 = db.save_sequence_plan(U, PLAN[1:], rationale="revised")
check("new version appends and resets cursor",
      v2 == 2 and db.get_current_plan(U)["version"] == 2)
db.save_sequence_plan(U, PLAN, rationale="back to full", source="operator")

# ── 2. assignment block: current step only ───────────────────────────
print("2) assignment block")
block = sms._build_plan_block(U)
check("current step is the assignment",
      "step 1 of 3" in block and "elicit_why@2" in block
      and "his words" in block)
check("next step by NAME only — later intents absent",
      "Next (name only, do NOT perform): micro_ask" in block
      and "dictation-level 3-liner" not in block
      and "handoff" not in block)
check("no plan → empty block (free mode)",
      sms._build_plan_block("nobody") == "")

prompt, _ = sms._build_system_prompt("evening", U)
check("assignment block rides at the END of the system prompt",
      prompt.rstrip().endswith(prompt.split("## Sequence assignment")[-1].rstrip())
      and "step 1 of 3" in prompt)

# ── 3. markers move the cursor ───────────────────────────────────────
print("3) plan markers")
text = sms._process_plan_markers(U, "좋다, 그럼 다음으로.\n[ADVANCE]",
                                 trigger="inbound_reply")
check("[ADVANCE] stripped and cursor moved",
      "[ADVANCE" not in text and db.get_current_plan(U)["cursor"] == 1
      and len(events_of("plan_cursor_moved")) >= 1)

block = sms._build_plan_block(U)
check("assignment follows the cursor",
      "step 2 of 3" in block and "micro_ask@1" in block)

text = sms._process_plan_markers(U, "x [STAY]", trigger="inbound_reply")
check("[STAY] is a stripped no-op",
      "[STAY" not in text and db.get_current_plan(U)["cursor"] == 1)

text = sms._process_plan_markers(
    U, 'x [REPLAN: "user pivoted to a new goal entirely"]',
    trigger="inbound_reply")
check("[REPLAN] logged, cursor unchanged",
      "[REPLAN" not in text and db.get_current_plan(U)["cursor"] == 1
      and json.loads(events_of("plan_replan_requested")[-1]["payload"])
          ["reason"].startswith("user pivoted"))

sms._process_plan_markers(U, "[ADVANCE]", trigger="inbound_reply")
sms._process_plan_markers(U, "[ADVANCE]", trigger="inbound_reply")
check("advancing past the end → plan_completed",
      len(events_of("plan_completed")) == 1)
check("completed plan renders free-mode assignment",
      "COMPLETE" in sms._build_plan_block(U))

# ── 4. end-to-end: reply path with plan judgment ─────────────────────
print("4) end-to-end")
db.save_sequence_plan(U, PLAN, rationale="e2e", source="operator")


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            class _B:
                text = ("오 진짜 좋은 이유네. 그럼 손 한번 풀어보자 — "
                        "콜랩에 이 한 줄만: x = torch.tensor([2.0])\n"
                        "[ADVANCE]\n[STEP: micro_ask@1]\n[EXPECT: advance]")

            class _R:
                content = [_B()]
            return _R()


sms.anthropic.Anthropic = FakeClient
reply = sms.handle_inbound("+15550001111",
                           "ML로 내 스타트업 제품을 직접 만들고 싶어서야")
check("reply clean of all markers",
      reply and "[ADVANCE" not in reply and "[STEP" not in reply
      and "[EXPECT" not in reply)
check("cursor advanced by the judgment",
      db.get_current_plan(U)["cursor"] == 1)
out = json.loads(events_of("sms_out")[-1]["payload"])
check("steps + expect still recorded alongside",
      out.get("steps") == [{"tag": "micro_ask", "intensity": 1}]
      and out.get("expect") == "advance")

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
