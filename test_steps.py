"""
Step-vocabulary instrumentation tests.

Run: ./venv/bin/python test_steps.py  (sqlite; anthropic mocked,
Twilio unconfigured so send_sms no-ops)

Covers: [STEP: tag@n] parsing (order, intensity clamp/default,
unknown tags stored verbatim, hold/none carry no intensity), marker
stripped from user-visible text but preserved in the llm_calls raw
record, steps landing in sms_out event payloads end-to-end through
handle_cron_tick, and server-side `hold` tagging on skipped slots.
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

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_steps.db")
db.init_db()

import sms  # noqa: E402

U = "test_user"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind):
    return [r for r in db.get_events(U, limit=200) if r["kind"] == kind]


# ── 1. parser unit tests ─────────────────────────────────────────────
print("1) parser")
steps, text = sms._process_step_marker(U, "안녕!\n\n[STEP: validate@2, micro_ask@1]")
check("multi-tag, utterance order",
      steps == [{"tag": "validate", "intensity": 2},
                {"tag": "micro_ask", "intensity": 1}])
check("marker stripped", "[STEP" not in text and text == "안녕!")

steps, _ = sms._process_step_marker(U, "x [STEP: elicit_why]")
check("missing intensity defaults to 2",
      steps == [{"tag": "elicit_why", "intensity": 2}])

steps, _ = sms._process_step_marker(U, "x [STEP: micro_ask@7, release@0]")
check("intensity clamps to 1-3",
      steps[0]["intensity"] == 3 and steps[1]["intensity"] == 1)

steps, _ = sms._process_step_marker(U, "x [STEP: none, hold]")
check("hold/none carry no intensity",
      all(s["intensity"] is None for s in steps))

steps, _ = sms._process_step_marker(U, "x [STEP: galaxy_brain@2]")
check("unknown tag stored verbatim",
      steps == [{"tag": "galaxy_brain", "intensity": 2}])

steps, text = sms._process_step_marker(U, "no marker here")
check("no marker → empty list, text unchanged",
      steps == [] and text == "no marker here")


# ── 2. end-to-end through handle_cron_tick ───────────────────────────
print("2) cron path")

class FakeAnthropicClient:
    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            class _Block:
                text = ("저녁이다! 어제 grad 뽑은 거 대단했어. "
                        "오늘 3줄만 더 가볼까?\n"
                        "[STEP: evoke_mastery@2, micro_ask@2]")

            class _Resp:
                content = [_Block()]
            return _Resp()


sms.anthropic.Anthropic = FakeAnthropicClient
sent = sms.handle_cron_tick("evening")
check("marker stripped from outbound text",
      sent is not None and "[STEP" not in sent)

outs = events_of("sms_out")
check("sms_out event carries steps",
      outs and json.loads(outs[-1]["payload"]).get("steps") ==
      [{"tag": "evoke_mastery", "intensity": 2},
       {"tag": "micro_ask", "intensity": 2}])

payload = json.loads(outs[-1]["payload"]) if outs else {}
raw = db.get_llm_call(payload.get("llm_call_id")) if payload.get("llm_call_id") else None
check("llm_calls raw response retains the marker",
      raw is not None and "[STEP: evoke_mastery@2" in raw["response_text"])

# ── 3. server-side hold tagging on skipped slots ─────────────────────
print("3) hold")
sms.handle_cron_tick("lunch")   # daytime slot — always skipped
ticks = [json.loads(t["payload"]) for t in events_of("cron_tick")]
skipped = [t for t in ticks if t.get("action") == "skipped"]
check("skipped slot tagged hold by the server",
      skipped and skipped[-1].get("steps") ==
      [{"tag": "hold", "intensity": None}])

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
