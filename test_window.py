"""
WhatsApp 24-hour window gate + prominent clock block.

Run: ./venv/bin/python test_window.py  (sqlite; anthropic mocked)

Observed failure this covers: a scheduled send 33h after the user's
last message was accepted by Twilio, logged by us as sent, and never
delivered by WhatsApp — a ghost send that also fakes a "no reply".
"""

import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "w1"
os.environ["TUTOR_USER_PHONE"] = "+15550004444"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_window.db")
db.init_db()

import sms  # noqa: E402

U = "w1"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


def backdate_last_inbound(hours):
    conn = db.get_conn()
    conn.execute(
        "UPDATE events SET ts=? WHERE id=(SELECT MAX(id) FROM events "
        "WHERE kind='sms_in')",
        ((datetime.now() - timedelta(hours=hours)).isoformat(),))
    conn.commit()
    conn.close()


class Fake:
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
            else:
                class _B:
                    type = "text"
                    text = "좋아, 오늘 한 줄만!\n[STEP: micro_ask@1]\n[EXPECT: reply]"

            class _R:
                content = [_B()]
            return _R()


sms.anthropic.Anthropic = Fake
db.ensure_user_profile_row(U)
db.check_and_complete_onboarding(U, force=True)

# ── 1. the predicate ─────────────────────────────────────────────────
print("1) window predicate")
os.environ["MESSAGING_CHANNEL"] = "sms"
check("SMS has no window — never closed", not sms.whatsapp_window_closed(U))

os.environ["MESSAGING_CHANNEL"] = "whatsapp"
check("user who never wrote → window closed",
      sms.whatsapp_window_closed(U))

db.log_event(U, "sms_in", {"text": "hi"}, source="sms")
check("fresh inbound opens it", not sms.whatsapp_window_closed(U))

backdate_last_inbound(23)
check("23h silent → still open", not sms.whatsapp_window_closed(U))
backdate_last_inbound(33)
check("33h silent (the observed case) → closed",
      sms.whatsapp_window_closed(U))

# ── 2. the gate refuses the send ─────────────────────────────────────
print("2) gate")
sent = sms.handle_cron_tick("evening")
check("nothing sent while the window is closed", sent is None)
w = events_of("whatsapp_window_closed")
check("refusal recorded with the silence measured",
      len(w) == 1
      and json.loads(w[0]["payload"])["slot"] == "evening"
      and json.loads(w[0]["payload"])["silent_hours"] >= 32)
check("no phantom sms_out was written",
      len(events_of("sms_out")) == 0)
check("but the tick itself IS recorded — a gated slot is not a dead cron",
      any(json.loads(e["payload"]).get("action") == "refused_window_closed"
          for e in events_of("cron_tick")))

backdate_last_inbound(2)
sent = sms.handle_cron_tick("evening")
check("window open again → the send goes through",
      sent is not None and len(events_of("sms_out")) == 1)

os.environ["MESSAGING_CHANNEL"] = "sms"
backdate_last_inbound(100)
sent = sms.handle_cron_tick("evening")
check("on SMS the same silence does not block",
      sent is not None and len(events_of("sms_out")) == 2)

# ── 2b. operator override (Twilio swallows the sandbox `join`) ───────
print("2b) operator override")
os.environ["MESSAGING_CHANNEL"] = "whatsapp"
backdate_last_inbound(40)
check("closed before the override", sms.whatsapp_window_closed(U))

sms.mark_whatsapp_window_open(U, note="husband re-joined the sandbox")
check("override reopens the window without faking a user turn",
      not sms.whatsapp_window_closed(U)
      and len(events_of("whatsapp_window_opened")) == 1
      and len([e for e in db.get_events(U, limit=300)
               if e["kind"] == "sms_in"]) == 1)   # still just the real one

sent = sms.handle_cron_tick("evening")
check("and the send now goes through", sent is not None)

conn = db.get_conn()
conn.execute("UPDATE events SET ts=? WHERE kind='whatsapp_window_opened'",
             ((datetime.now() - timedelta(hours=30)).isoformat(),))
conn.commit()
conn.close()
check("an old override ages out like a real message would",
      sms.whatsapp_window_closed(U))

# ── 3. the clock block ───────────────────────────────────────────────
print("3) clock")
block = sms._clock_block()
now_local = datetime.now() + timedelta(hours=sms.TZ_OFFSET_H)
check("states the user's wall clock in words",
      now_local.strftime("%H:%M") in block and "요일" in block)
check("states the hour and stops — no standing order to greet",
      "must make sense at this hour" not in block
      and len(block) < 200)

prompt, _ = sms._build_system_prompt("evening", U)
check("clock rides at the TOP of the assembled prompt",
      prompt.lstrip().startswith("## Right now, for this user"))

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
