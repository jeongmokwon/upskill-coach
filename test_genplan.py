"""
Initial plan generation tests — P0-B.

Run: ./venv/bin/python test_genplan.py  (sqlite; anthropic mocked)

Covers: forced-tool-call happy path (notes + plan + events + T2b
records), validation retry with feedback, persistent failure with
ZERO partial writes, semantic validation specifics, and the
onboarding-completion hook firing generation in the background.
"""

import json
import os
import tempfile
import time

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "hub"
os.environ["TUTOR_USER_PHONE"] = "+15550002222"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_genplan.db")
db.init_db()

import genplan  # noqa: E402
import sms  # noqa: E402

U = "hub"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


class ToolBlock:
    type = "tool_use"
    name = "submit_initial_plan"

    def __init__(self, payload):
        self.input = payload


class FakeAnthropic:
    """Returns queued tool payloads, one per attempt; records calls."""
    queue = []
    seen = []

    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            FakeAnthropic.seen.append(kwargs)

            class _Resp:
                content = [ToolBlock(FakeAnthropic.queue.pop(0))]
            return _Resp()


genplan.anthropic.Anthropic = FakeAnthropic

VALID = {
    "notes": [
        {"claim": "직접 지시에 되받음 — 선택지형이 안전",
         "given": {"position": "opener"},
         "when": ["choice_offer@2"], "expect": "reply",
         "evidence_quotes": ["그건 내 방식대로 할게"]},
        {"claim": "저녁 8시 이후에만 여유",
         "when": ["implementation_cue@2"], "expect": "reply",
         "evidence_quotes": ["애들 재우고 나면 시간 있어"]},
    ],
    "plan": {
        "steps": [
            {"tag": "elicit_why", "intensity": 2,
             "intent": "his own words on why"},
            {"tag": "choice_offer", "intensity": 2,
             "intent": "two tiny options, he picks"},
            {"tag": "handoff", "intensity": 1,
             "intent": "merge into his project"},
        ],
        "rationale": "choice_offer per note 1; opener per note 2",
    },
    # The same call also produces the profile brief (step ④); `wants`
    # quotes must be verbatim from the fixture conversation below.
    "profile": {
        "job": "",
        "learning_types": ["project_completion"],
        "materials": [],
        "wants": [{"quote": "그건 내 방식대로 할게",
                   "meaning": "자율성 우선 — 지시보다 선택지"}],
        "personality": "짧고 단호, 저녁에만 여유",
        "path_kind": "deliverable",
        "rationale": "'내 방식대로'라는 표현이 자율성 신호",
    },
}

# fixture conversation
db.ensure_user_profile_row(U)
db.save_sms_message(U, "assistant", "처음 뵙겠습니다!", "out")
db.save_sms_message(U, "user", "그건 내 방식대로 할게", "in")
db.save_sms_message(U, "user", "애들 재우고 나면 시간 있어", "in")
db.set_agreed_goal(U, "make an app with ML")
db.set_ignition_marker(U, "opens the editor and types")

# ── 1. happy path ────────────────────────────────────────────────────
print("1) happy path")
FakeAnthropic.queue = [json.loads(json.dumps(VALID))]
result = genplan.generate(U)
check("returns summary", result and result["notes"] == 2)
notes = db.get_user_notes(U)
check("notes saved as p7 hypotheses with quote evidence",
      len(notes) == 2 and notes[0]["source"] == "p7"
      and notes[0]["confidence"] == "hypothesis"
      and "그건 내 방식대로" in notes[0]["evidence_json"])
plan = db.get_current_plan(U)
check("plan saved (3 steps, cursor 0, source recorded)",
      plan and len(plan["steps"]) == 3 and plan["cursor"] == 0)
check("plan_generated event + T2b record",
      len(events_of("plan_generated")) == 1
      and db.get_llm_call(result["llm_call_id"]) is not None)
sys_prompt = FakeAnthropic.seen[0]["system"]
check("call carried prior + vocabulary + deliverables + tool forcing",
      "Policy prior" in sys_prompt and "Vocabulary" in sys_prompt
      and "make an app with ML" in sys_prompt
      and FakeAnthropic.seen[0]["tool_choice"]["name"] == "submit_initial_plan")

# ── PR 5: the walkthrough's output feeds the generation ──────────────
_gm = db.add_user_material(U, "file", title="정리본.docx",
                           extracted_text="...")
db.set_material_digest(_gm, "3 sections, 41 recall items")
db.update_material_walkthrough(
    _gm, user_description="업무에서 정리한 파일",
    wants=[{"quote": "바로 대답하고 싶어", "meaning": "recall"}],
    status="validated")
db.set_agreed_offer(U, "매일 저녁 질문 하나씩")
sys2, _ = genplan._build_system(U)
check("materials section: digest + user's words + status ride along",
      "Their materials" in sys2 and "41 recall items" in sys2
      and "바로 대답하고 싶어" in sys2 and "validated" in sys2)
check("the standing offer is a deliverable the plan must serve",
      "매일 저녁 질문 하나씩" in sys2)
check("a materials-less user renders no materials section",
      "Their materials" not in genplan._build_system("hub2")[0]
      or db.get_user_materials("hub2"))

# ── 2. invalid → feedback retry → valid ──────────────────────────────
print("2) retry")
bad = json.loads(json.dumps(VALID))
bad["plan"]["steps"][0]["tag"] = "galaxy_brain"
FakeAnthropic.queue = [bad, json.loads(json.dumps(VALID))]
FakeAnthropic.seen = []
result = genplan.generate(U)
check("second attempt succeeds", result is not None)
check("retry carried validation feedback",
      len(FakeAnthropic.seen) == 2
      and "failed validation" in FakeAnthropic.seen[1]["messages"][-1]["content"])

# ── 3. persistent failure → zero partial writes ──────────────────────
print("3) persistent failure")
notes_before = len(db.get_user_notes(U))
plan_before = db.get_current_plan(U)["version"]
bad2 = json.loads(json.dumps(VALID))
bad2["plan"]["steps"] = bad2["plan"]["steps"][:1]        # only 1 step
FakeAnthropic.queue = [json.loads(json.dumps(bad2)) for _ in range(3)]
result = genplan.generate(U)
check("returns None + failure event",
      result is None and len(events_of("p7_generation_failed")) == 1)
check("NOTHING partially saved",
      len(db.get_user_notes(U)) == notes_before
      and db.get_current_plan(U)["version"] == plan_before)

# ── 4. validation specifics ──────────────────────────────────────────
print("4) validation")
v = json.loads(json.dumps(VALID))
v["notes"][0]["expect"] = "maybe"
errs = genplan._validate(v)
check("unknown expect caught", any("expect" in e for e in errs))
v2 = json.loads(json.dumps(VALID))
v2["plan"]["steps"][0]["tag"] = "hold"
check("hold not plannable", any("plannable" in e
                                for e in genplan._validate(v2)))

# ── 5. completion hook fires generation in background ────────────────
print("5) completion hook")
U2 = os.environ["TUTOR_USER_ID"] = "hub2"
os.environ["TUTOR_USER_PHONE"] = "+15550003333"
db.ensure_user_profile_row(U2)
db.set_agreed_goal(U2, "g")
db.set_ignition_marker(U2, "m")
db.save_learning_path(U2, "d", "p", "c")
db.set_agreed_offer(U2, "o")
db.set_agreed_bite(U2, "tiny task", source="analyze")
_m2 = db.add_user_material(U2, "link", title="course",
                           source_url="https://x.co/c")
db.update_material_walkthrough(_m2, status="validated")

called = []
genplan.generate_async = lambda uid: called.append(uid)  # spy


class FakeTurn:
    """sms and analyze_turn share the SAME anthropic module object, so
    one fake serves both calls and dispatches on whether the call is a
    forced tool call (analysis) or free text (generation)."""
    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            if kwargs.get("tools"):
                class _B:
                    type = "tool_use"
                    input = {"schedule": "20:00-21:00",
                             "step_completed": "not_applicable",
                             "step_reason": "-"}
            else:
                class _B:
                    type = "text"
                    text = ("좋아 그 시간으로 하자!\n"
                            "[STEP: secure_commit@1]\n[EXPECT: reply]")

            class _R:
                content = [_B()]
            return _R()


sms.anthropic.Anthropic = FakeTurn
sms.handle_inbound("+15550003333", "저녁 8시가 좋아")
time.sleep(0.1)
check("last field fill → completion → generation hook fired",
      called == [U2]
      and db.get_onboarding_state(U2)["completed_at"] is not None)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
