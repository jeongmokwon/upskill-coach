"""STOP opt-out tests (2026-08-20, PR-3).

Run: python test_stop_optout.py  (sqlite; Twilio unset — send_sms
records/prints without sending)

Claims under test: STOP (and carrier synonyms) permanently stops the
user — ack sent first, then status flips; every send path is refused
at the send_sms choke point afterwards (nudge, reminders, /debug/say
all ride it); a stopped user's other messages get no reply but are
recorded; START (only for stopped users) reactivates with an ack; a
conversational "yes" from an active user toggles nothing; "skip"
still means skip-today; the cron roster excludes stopped users.
"""

import json
import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "jm"
os.environ["TUTOR_USER_PHONE"] = "+15550001111"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("MESSAGING_CHANNEL", None)

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_stop.db")
db.init_db()

import sms  # noqa: E402

PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(uid, kind):
    return [r for r in db.get_events(uid, limit=100)
            if r["kind"] == kind]


def inbound(uid, phone, text):
    db.save_sms_message(uid, "user", text, "in")
    return sms._reply_to_inbound(uid, phone, text,
                                 db.get_last_user_message_id(uid))


U = "stopper"
db.ensure_user_profile_row(U)
db.set_user_phone(U, "+15550004001")
db.set_expectation_sent(U)

print("STOP flow")
out = inbound(U, "+15550004001", "STOP")
check("ack text returned", out == sms.STOP_ACK)
check("status flipped to stopped",
      (db.get_user_profile_by_id(U) or {}).get("status") == "stopped")
check("ack recorded before the flip (in history)",
      db.get_recent_sms_messages(U, limit=3)[-1]["content"]
      == sms.STOP_ACK)
check("roster excludes stopped users",
      U not in {u["user_id"] for u in db.get_active_users()})

print("all send paths refused at the choke point")
check("raw send refused",
      sms.send_sms("+15550004001", "hello?", user_id=U) is None
      and events_of(U, "send_refused_stopped"))
check("nudge refused end-to-end",
      sms.send_nudge(U, "say something nice") is None)

print("stopped user's other messages: recorded, unanswered")
out = inbound(U, "+15550004001", "actually hmm")
check("no reply", out is None)
check("recorded", events_of(U, "inbound_while_stopped"))

print("START flow")
out = inbound(U, "+15550004001", "START")
check("reactivation ack", out == sms.START_ACK)
check("status active again",
      (db.get_user_profile_by_id(U) or {}).get("status") == "active")
check("back on the roster",
      U in {u["user_id"] for u in db.get_active_users()})

print("no accidental toggles")
V = "chatty"
db.ensure_user_profile_row(V)
db.set_user_phone(V, "+15550004002")
db.set_expectation_sent(V)


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            class _B:
                text = "Got it."

            class _R:
                content = [_B()]
                stop_reason = "end_turn"
            return _R()


sms.anthropic.Anthropic = FakeClient
inbound(V, "+15550004002", "yes")
check("a conversational 'yes' from an active user changes nothing",
      (db.get_user_profile_by_id(V) or {}).get("status") == "active"
      and not events_of(V, "status_changed"))
out = inbound(V, "+15550004002", "skip")
check("'skip' still means skip-today, not opt-out",
      out == "ok, no more pings today. talk tomorrow."
      and (db.get_user_profile_by_id(V) or {}).get("status") == "active")
check("'stop' is no longer a skip token",
      "stop" not in sms.SKIP_TOKENS)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
