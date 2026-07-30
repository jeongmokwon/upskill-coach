"""
Send-guard tests (step ②): one question per message, [STEP:]
presence, one regeneration attempt, send-anyway-and-log, plus
relative time labels on history.

Run: ./venv/bin/python test_guards.py  (sqlite; anthropic mocked)
"""

import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "g1"
os.environ["TUTOR_USER_PHONE"] = "+15550007777"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_guards.db")
db.init_db()

import sms  # noqa: E402

U = "g1"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


# ── 1. the guard predicate ───────────────────────────────────────────
print("1) guard predicate")
one = [{"tag": "connect", "intensity": 1}]
check("one question passes",
      sms.check_send_guards("오늘 좀 어땠어?", one) == [])
check("two questions caught",
      any("questions" in v for v in
          sms.check_send_guards("어떤 형태야? 외워본 적은 있어?", one)))
check("questions across bubbles counted together",
      any("questions" in v for v in
          sms.check_send_guards("어떤 형태야?\n---\n해봤어?", one)))
check("full-width ？ counted",
      any("questions" in v for v in
          sms.check_send_guards("이거 맞아？ 저건？", one)))
check("missing STEP caught",
      any("STEP" in v for v in sms.check_send_guards("좋아!", [])))
check("statement with no question is fine",
      sms.check_send_guards("오늘은 여기까지 하자. 내일 봐", one) == [])


# ── 2. regeneration then compliance ──────────────────────────────────
print("2) regeneration")


class Scripted:
    """Serves queued texts for generation; tool_use for the analysis
    call (both share one anthropic module)."""
    queue = []
    seen = []

    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            if kwargs.get("tools"):
                class _B:
                    type = "tool_use"
                    input = {"step_completed": "not_applicable",
                             "step_reason": "-"}

                class _R:
                    content = [_B()]
                return _R()
            Scripted.seen.append(kwargs)

            class _B:
                type = "text"
                text = Scripted.queue.pop(0)

            class _R:
                content = [_B()]
            return _R()


sms.anthropic.Anthropic = Scripted
db.ensure_user_profile_row(U)
db.check_and_complete_onboarding(U, force=True)

Scripted.queue = [
    "그 자료 어떤 형태야? 그리고 외워본 적 있어?\n[STEP: elicit_why@2]",
    "그 자료 워드 같은 데 정리해둔 거야?\n[STEP: elicit_why@1]",
]
Scripted.seen = []
text, steps, expect, call_id = sms.generate_message(
    U, "sys", [{"role": "user", "content": "hi"}], "test")
check("violating draft triggers exactly one retry", len(Scripted.seen) == 2)
check("retry carried the violation as feedback",
      "broke a hard rule" in Scripted.seen[1]["messages"][-1]["content"]
      and "questions" in Scripted.seen[1]["messages"][-1]["content"])
check("compliant rewrite is what gets returned",
      text.count("?") == 1 and "외워본 적" not in text)
check("clean retry logs no violation",
      len(events_of("send_guard_violation")) == 0)
check("both attempts flight-recorded",
      db.get_llm_call(call_id) is not None
      and len([r for r in db.get_events(U, limit=300)]) >= 1)

# ── 3. still violating → send anyway, but recorded ───────────────────
print("3) send-anyway")
Scripted.queue = [
    "이거 어때? 저건 어때?\n[STEP: connect@1]",
    "그래도 이거 어때? 저건 어때?\n[STEP: connect@1]",
]
Scripted.seen = []
text, steps, expect, call_id = sms.generate_message(
    U, "sys", [{"role": "user", "content": "hi"}], "test")
check("stops after one retry (2 calls total)", len(Scripted.seen) == 2)
check("message is still returned — silence is worse",
      text is not None and "어때" in text)
v = events_of("send_guard_violation")
check("violation recorded with its trigger + call id",
      len(v) == 1
      and json.loads(v[0]["payload"])["trigger"] == "test"
      and json.loads(v[0]["payload"])["llm_call_id"] == call_id)

# ── 4. relative time labels on history ───────────────────────────────
print("4) time labels")
db.save_sms_message(U, "assistant", "어제 얘기한 거 어때", "out")
db.save_sms_message(U, "user", "오늘 할게", "in")
conn = db.get_conn()
conn.execute("UPDATE messages SET timestamp=? WHERE content=?",
             ((datetime.now() - timedelta(hours=4)).isoformat(),
              "어제 얘기한 거 어때"))
conn.execute("UPDATE messages SET timestamp=? WHERE content=?",
             ((datetime.now() - timedelta(minutes=20)).isoformat(),
              "오늘 할게"))
conn.commit()
conn.close()

plain = db.get_recent_sms_messages(U, limit=10)
timed = db.get_recent_sms_messages(U, limit=10, with_time=True)
check("default rendering unchanged (no labels)",
      all(not m["content"].startswith("[") for m in plain))
check("4h-old turn labeled in hours",
      any(m["content"].startswith("[4시간 전] ") for m in timed))
check("20min-old turn labeled in minutes",
      any(m["content"].startswith("[20분 전] ") for m in timed))
check("content preserved after the label",
      any(m["content"].endswith("오늘 할게") for m in timed))

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
