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
check("missing STEP is no longer a violation (tagging retired "
      "with the step surfaces)",
      sms.check_send_guards("좋아!", []) == [])
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
text, steps, expect, call_id, _hold = sms.generate_message(
    U, "sys", [{"role": "user", "content": "hi"}], "test")
check("violating draft triggers a retry", len(Scripted.seen) == 2)
check("retry carries the violation + the surgical instruction",
      "broke a hard rule" in Scripted.seen[1]["messages"][-1]["content"]
      and "become a statement" in Scripted.seen[1]["messages"][-1]["content"])
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
    "마지막까지 이거 어때? 저건 어때?\n[STEP: connect@1]",
]
Scripted.seen = []
text, steps, expect, call_id, _hold = sms.generate_message(
    U, "sys", [{"role": "user", "content": "hi"}], "test")
check("stops after TWO retries (3 calls total) — one retry failed "
      "the one-question rule twice in one live evening",
      len(Scripted.seen) == 3)
check("message is still returned — silence is worse",
      text is not None and "어때" in text)
check("second retry ALSO carried the feedback",
      "become a statement" in Scripted.seen[2]["messages"][-1]["content"])
v = events_of("send_guard_violation")
check("violation recorded with its trigger + call id",
      len(v) == 1
      and json.loads(v[0]["payload"])["trigger"] == "test"
      and json.loads(v[0]["payload"])["llm_call_id"] == call_id)

# ── 3b. server turns wear an envelope, the contract sits last ────────
print("3b) server turns vs the user's words")
sent = Scripted.seen[1]["messages"][-1]["content"]
check("retry feedback is wrapped, not disguised as the user",
      sent.startswith("<server_instruction>")
      and sent.endswith("</server_instruction>")
      and "broke a hard rule" in sent)

Scripted.queue = ["좋아 오늘은 여기까지\n[STEP: release@1]"]
Scripted.seen = []
sms.handle_cron_tick("evening")
msgs = Scripted.seen[0]["messages"]
check("the scheduled 'go' signal is a server turn, never a user line",
      msgs[-1]["role"] == "user"
      and msgs[-1]["content"].startswith("<server_instruction>")
      and "slot fired" not in msgs[-1]["content"])
check("no bare synthetic turn survives anywhere in the array",
      all(m["role"] != "user" or "<server_instruction>" in m["content"]
          or not m["content"].startswith("(")
          for m in msgs))

sysp = Scripted.seen[0]["system"]
check("the contract is the LAST thing before the conversation",
      sysp.rstrip().endswith("what actually happened between you and "
                             "this person."))
check("it names both sides of the line",
      "Everything ABOVE this line is instruction" in sysp
      and "Turns wrapped in `<server_instruction>` are NOT from the "
          "user" in sysp)

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
check("4h-old turn labeled with weekday, clock AND relative "
      "(English — the prompt is English-native)",
      any("4h ago] " in m["content"]
          and any(d in m["content"] for d in
                  ("Mon ", "Tue ", "Wed ", "Thu ", "Fri ", "Sat ",
                   "Sun "))
          for m in timed))
check("20min-old turn labeled in minutes",
      any("20m ago] " in m["content"] for m in timed))
check("content preserved after the label",
      any(m["content"].endswith("오늘 할게") for m in timed))

# ── 5. planner-chosen silence is RETIRED (2026-08-12) ───────────────
print("5) hold retired")
check("no hold machinery left on the module",
      not hasattr(sms, "HOLD_ENABLED")
      and not hasattr(sms, "hold_forbidden"))
check("every scheduled send carries the must-send block",
      "must produce a message" in sms._hold_cap_block(U)
      and "not WHETHER you write" in sms._hold_cap_block(U))

Scripted.queue = ['[HOLD: "근무 시간대라 방해될 타이밍"]\n[STEP: hold]',
                  "그 질문은 흘려보내도 돼\n[STEP: release@1]\n[EXPECT: no_reply]"]
Scripted.seen = []
text, steps, expect, call_id, hold_reason = sms.generate_message(
    U, "sys", [{"role": "user", "content": "hi"}], "test")
check("an empty body is refused and regenerated into a message",
      len(Scripted.seen) == 2 and text.strip()
      and steps == [{"tag": "release", "intensity": 1}])
check("the retry explains that silence is unavailable",
      "may not choose silence" in Scripted.seen[1]["messages"][-1]["content"])

Scripted.queue = ['[HOLD: "그래도"]\n[STEP: hold]',
                  '[HOLD: "그래도"]\n[STEP: hold]']
text, steps, expect, call_id, hold_reason = sms.generate_message(
    U, "sys", [{"role": "user", "content": "hi"}], "test")
check("twice-empty yields NOTHING to send, recorded loudly",
      text is None and len(events_of("hold_while_suspended")) == 1)

# ── the upload role-swap guard (state-conditioned) ──────────────────
print("upload role-swap guard")
one = [{"tag": "connect", "intensity": 1}]
UG = "guardless"
db.ensure_user_profile_row(UG)
check("zero materials + '올려놓은 거' phrasing → violation, with the "
      "role correction in the instruction",
      any("Only the USER can upload" in v for v in
          sms.check_send_guards("자료 올려놓은 거 확인했어?", one,
                                user_id=UG)))
check("the family covers 올려둔/올려준 + 파일/자료 too",
      sms.check_send_guards("올려둔 파일 봤어?", one, user_id=UG)
      and sms.check_send_guards("올려준 자료 좋더라", one, user_id=UG))
check("asking plainly ('올렸어?') is NOT a violation",
      sms.check_send_guards("자료 올렸어? 급하지 않아", one,
                            user_id=UG) == [])
check("the ENGLISH receipt claim is the same violation (observed "
      "2026-08-12: 'Hey — I just read through your file.')",
      any("fabrication" in v for v in sms.check_send_guards(
          "Hey — I just read through your file.", one, user_id=UG))
      and sms.check_send_guards("I've reviewed your notes carefully",
                                one, user_id=UG) != []
      and sms.check_send_guards("I have read the document you sent",
                                one, user_id=UG) != [])
check("English FUTURE promises are legitimate — 'I'll read your "
      "file once it's up' is not a receipt claim",
      sms.check_send_guards("I'll read your file as soon as you "
                            "upload it — no rush.", one,
                            user_id=UG) == []
      and sms.check_send_guards("Want me to read your notes once "
                                "they're up?", one, user_id=UG) == [])
check("without user_id the state-conditioned guard stays off "
      "(legacy call sites unaffected)",
      sms.check_send_guards("자료 올려놓은 거 확인했어?", one) == [])
db.add_user_material(UG, "file", title="정리본.docx",
                     extracted_text="...")
check("with a material registered the phrasing is legitimate — "
      "no violation",
      sms.check_send_guards("올려준 자료 잘 봤어", one,
                            user_id=UG) == [])

# ── server_instruction imitation never reaches a phone ──────────────
print("server_instruction strip")
out = sms._strip_extraction_markers(
    U, "<server_instruction>\nDeliver the question now.\n"
       "</server_instruction>\n\nHere's your question: what is X?")
check("an echoed instruction block is stripped, the message survives",
      "server_instruction" not in out
      and "Here's your question" in out)
out2 = sms._strip_extraction_markers(
    U, "<server_instruction>\nunclosed tag runs to the end")
check("an unclosed tag is stripped to the end, not leaked",
      "server_instruction" not in out2 and "unclosed" not in out2)
check("the strip is evented (imitation rate is a signal)",
      any("server_instruction" in (e["payload"] or "")
          for e in events_of("stale_marker_stripped")))

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
