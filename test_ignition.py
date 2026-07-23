"""
Ignition instrumentation tests.

Run: ./venv/bin/python test_ignition.py  (sqlite; anthropic mocked,
Twilio unconfigured)

Covers: per-user ignition marker persistence ([IGNITION_DEF:] →
user_profiles + event), real-time judgment ([IGNITION: n] →
ignition_judgment event with trigger + marker context), stripping of
both markers from user-visible text, placeholder exposure to prompts,
and the end-to-end inbound reply path.
"""

import json
import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "test_user"
os.environ["TUTOR_USER_PHONE"] = "+15550001111"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_ignition.db")
db.init_db()

import sms  # noqa: E402

U = "test_user"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind):
    return [r for r in db.get_events(U, limit=200) if r["kind"] == kind]


# ── 1. definition marker ─────────────────────────────────────────────
print("1) [IGNITION_DEF:]")
text = sms._process_ignition_markers(
    U, '좋아, 그걸로 하자!\n[IGNITION_DEF: "노트북 앞에 앉아 콜랩에 코드 타이핑"]',
    trigger="cron_evening")
check("marker stripped", "[IGNITION_DEF" not in text and text == "좋아, 그걸로 하자!")
check("marker persisted to profile",
      db.get_user_phase(U)["ignition_marker"] == "노트북 앞에 앉아 콜랩에 코드 타이핑")
check("ignition_def_set event emitted",
      len(events_of("ignition_def_set")) == 1)

# refinement overwrites
sms._process_ignition_markers(
    U, 'x [IGNITION_DEF: "콜랩에 코드 타이핑 시작"]', trigger="inbound_reply")
check("redefinition refines the marker",
      db.get_user_phase(U)["ignition_marker"] == "콜랩에 코드 타이핑 시작"
      and len(events_of("ignition_def_set")) == 2)

# ── 2. judgment marker ───────────────────────────────────────────────
print("2) [IGNITION: n]")
text = sms._process_ignition_markers(
    U, "오 벌써 열었네! 그 첫 줄부터 가자.\n[IGNITION: 4]",
    trigger="inbound_reply")
check("score stripped", "[IGNITION" not in text)
judgments = events_of("ignition_judgment")
p = json.loads(judgments[-1]["payload"]) if judgments else {}
check("judgment event with score + trigger + marker context",
      p.get("score") == 4 and p.get("trigger") == "inbound_reply"
      and p.get("marker") == "콜랩에 코드 타이핑 시작")

text = sms._process_ignition_markers(U, "no markers", trigger="x")
check("no markers → text unchanged, no new events",
      text == "no markers" and len(events_of("ignition_judgment")) == 1)

# ── 3. placeholders reach the prompts ────────────────────────────────
print("3) placeholders")
fields = sms._build_placeholders(U)
check("ignition_marker exposed to prompt templates",
      fields["ignition_marker"] == "콜랩에 코드 타이핑 시작")

prompt, _ = sms._build_system_prompt_for_reply(U)
check("rendered reply prompt carries the user's marker",
      "콜랩에 코드 타이핑 시작" in prompt)

# ── 4. end-to-end inbound reply path ─────────────────────────────────
print("4) inbound path")

class FakeAnthropicClient:
    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            class _Block:
                text = ("코드 돌아간 거야?? 좋아, 다음 줄 가자.\n"
                        "[IGNITION: 5]\n[STEP: evoke_mastery@1, micro_ask@2]")

            class _Resp:
                content = [_Block()]
            return _Resp()


sms.anthropic.Anthropic = FakeAnthropicClient
reply = sms.handle_inbound("+15550001111", "콜랩 켰고 방금 첫 줄 돌렸어!")
check("both markers stripped from outbound",
      reply is not None and "[IGNITION" not in reply and "[STEP" not in reply)

j = json.loads(events_of("ignition_judgment")[-1]["payload"])
check("live score 5 recorded from the reply path",
      j["score"] == 5 and j["trigger"] == "inbound_reply")

out = json.loads(events_of("sms_out")[-1]["payload"])
check("steps still recorded alongside",
      out.get("steps") == [{"tag": "evoke_mastery", "intensity": 1},
                           {"tag": "micro_ask", "intensity": 2}])

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
