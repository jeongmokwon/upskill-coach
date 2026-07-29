"""
Sequence-plan (exploration v2) + step-judge (P0-D) tests.

Run: ./venv/bin/python test_plan.py  (sqlite; anthropic mocked)

Structural claims under test: the LLM's prompt contains ONLY the
current step (next by name); the CURSOR is owned by the dedicated
server-side judge call (generation's [ADVANCE] is a stray);
deviations (playing a later step than the cursor) are detected from
the step tags.
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


# Plans exist only post-onboarding.
db.ensure_user_profile_row(U)
db.check_and_complete_onboarding(U, force=True)

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
      "step 1 of 3" in block and "elicit_why@2" in block)
check("next step by NAME only — later intents absent",
      "Next (name only, do NOT perform): micro_ask" in block
      and "dictation-level 3-liner" not in block)
check("assignment tells the model the server owns the cursor",
      "judged SERVER-SIDE" in block and "[ADVANCE]" not in block)
check("no plan → empty block", sms._build_plan_block("nobody") == "")

# ── 3. markers: ADVANCE is a stray, REPLAN still records ─────────────
print("3) plan markers")
text = sms._process_plan_markers(U, "좋아!\n[ADVANCE]", trigger="t")
check("[ADVANCE] stripped but cursor UNCHANGED (judge owns it)",
      "[ADVANCE" not in text and db.get_current_plan(U)["cursor"] == 0)
text = sms._process_plan_markers(U, "x [STAY]", trigger="t")
check("[STAY] stripped no-op", "[STAY" not in text)
text = sms._process_plan_markers(
    U, 'x [REPLAN: "user pivoted entirely"]', trigger="t")
check("[REPLAN] logged",
      "[REPLAN" not in text
      and len(events_of("plan_replan_requested")) == 1)

# ── 4. the judge owns the cursor (end-to-end inbound) ────────────────
print("4) step judge")


class QueueFake:
    """Serves queued responses: dicts → tool_use blocks, strings →
    text blocks. handle_inbound makes judge call first, reply second."""
    queue = []

    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            item = QueueFake.queue.pop(0)

            class _Resp:
                pass
            if isinstance(item, dict):
                class _B:
                    type = "tool_use"
                    input = item
                _Resp.content = [_B()]
            else:
                class _B:
                    type = "text"
                    text = item
                _Resp.content = [_B()]
            return _Resp()


sms.anthropic.Anthropic = QueueFake

QueueFake.queue = [
    {"completed": "yes", "reason": "he articulated a concrete why"},
    ("오 좋은 이유다. 그럼 손 풀자 — 한 줄만: x = torch.tensor([2.0])\n"
     "[STEP: micro_ask@1]\n[EXPECT: advance]"),
]
reply = sms.handle_inbound("+15550001111", "스타트업에 ML을 직접 쓰고 싶어서야")
check("judge yes → cursor advanced BEFORE reply",
      db.get_current_plan(U)["cursor"] == 1
      and len(events_of("step_judged")) == 1
      and json.loads(events_of("step_judged")[0]["payload"])["completed"] == "yes")
check("reply clean + steps/expect recorded",
      reply and "[STEP" not in reply
      and json.loads(events_of("sms_out")[-1]["payload"])["expect"] == "advance")
check("judge call flight-recorded",
      db.get_llm_call(json.loads(events_of("step_judged")[0]["payload"])
                      ["llm_call_id"]) is not None)

QueueFake.queue = [
    {"completed": "no", "reason": "he acknowledged but did not act"},
    "천천히 해도 돼. 준비되면 그 한 줄부터.\n[STEP: validate@1]\n[EXPECT: reply]",
]
sms.handle_inbound("+15550001111", "음 알겠어")
check("judge no → cursor stays", db.get_current_plan(U)["cursor"] == 1)

QueueFake.queue = [
    {"completed": "uncertain", "reason": "ambiguous"},
    "그 줄 쳐보니 어때?\n[STEP: micro_ask@1]\n[EXPECT: reply]",
]
sms.handle_inbound("+15550001111", "ㅎㅎ")
check("uncertain → cursor stays", db.get_current_plan(U)["cursor"] == 1)

# ── 5. deviation net ─────────────────────────────────────────────────
print("5) deviation")
QueueFake.queue = [
    {"completed": "no", "reason": "not yet"},
    ("이제 캬파시 영상 10:32부터 틀어!\n"
     "[STEP: handoff@2]\n[EXPECT: reply]"),
]
sms.handle_inbound("+15550001111", "좀 이따가")
dev = events_of("planner_deviation")
check("playing a later step than the cursor → planner_deviation",
      len(dev) == 1
      and json.loads(dev[0]["payload"])["played_ahead"] == ["handoff"])

# ── 6. completion through the judge ──────────────────────────────────
print("6) completion")
QueueFake.queue = [
    {"completed": "yes", "reason": "he typed the line and reported output"},
    "됐다!! 다음 줄 가자.\n[STEP: evoke_mastery@1]\n[EXPECT: reply]",
]
sms.handle_inbound("+15550001111", "쳤어. tensor([2.]) 나왔어")
QueueFake.queue = [
    {"completed": "yes", "reason": "he merged into the main task"},
    "고고. 이제 네 거다.\n[STEP: release@1]\n[EXPECT: no_reply]",
]
sms.handle_inbound("+15550001111", "영상 틀었고 노트 열었어")
check("two more yes verdicts complete the plan",
      db.get_current_plan(U)["cursor"] == 3
      and len(events_of("plan_completed")) == 1)
check("completed plan renders free-mode assignment",
      "COMPLETE" in sms._build_plan_block(U))

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
