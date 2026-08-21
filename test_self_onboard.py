"""Text-STUCK self-onboarding tests (2026-08-21).

Run: python test_self_onboard.py  (sqlite; anthropic mocked, Twilio
unset)

Claims under test: ANY first text from an unknown number self-onboards (no
keyword gate, founder decision) — auto user_id (u + last4 + hex, collision-
safe), phone bound, companion lane open, loud event — and the normal
inbound flow then sends the NO-EMAIL expectation variant plus a real
companion reply; the second message routes to the same
user; users WITH an email still get the main expectation copy.
"""

import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "jm"
os.environ["TUTOR_USER_PHONE"] = "+15550001111"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("MESSAGING_CHANNEL", None)

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_self_onboard.db")
db.init_db()

import sms  # noqa: E402

PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            class _B:
                text = ("What kind of work are you building?\n"
                        "[PERSONA_CHECK: clean]")

            class _R:
                content = [_B()]
                stop_reason = "end_turn"
            return _R()


sms.anthropic.Anthropic = FakeClient

print("ANY first text from an unknown number self-onboards")
out = sms.handle_inbound("+19998887777",
                         "STUCK. I keep circling my pricing page.")
uid = db.get_user_by_phone("+19998887777")
check("user auto-created", uid is not None)
check("id shape: u + last4 + 4 hex",
      uid.startswith("u7777") and len(uid) == 9 and uid.isalnum())
check("companion lane open", db.tracks_lane_open(uid))
check("loud event", any(r["kind"] == "user_self_onboarded"
                        for r in db.get_events(uid, limit=10)))
msgs = db.get_recent_sms_messages(uid, limit=10)
assistant = [m["content"] for m in msgs if m["role"] == "assistant"]
check("no-email expectation variant sent first",
      assistant and "tell me\nyour email" in assistant[0].replace(
          "tell me your email", "tell me\nyour email")
      and "welcome email" not in assistant[0])
check("real companion reply follows",
      out == "What kind of work are you building?"
      and assistant[-1] == out)
check("expectation stamped", not sms._expectation_due(uid))

print("second message routes to the same user")
out = sms.handle_inbound("+19998887777", "mostly the pricing")
check("same user, replied",
      db.get_user_by_phone("+19998887777") == uid and out is not None)
check("no second user for the number",
      len([u for u in db.get_active_users()
           if u["user_id"].startswith("u7777")]) == 1)

print("no keyword gate (founder decision)")
sms.handle_inbound("+19998886666", "hello? what is this number")
check("keyword-less first text onboards too",
      db.get_user_by_phone("+19998886666") is not None)

print("users with an email keep the main copy")
db.ensure_user_profile_row("hasmail")
db.set_user_phone("hasmail", "+19998885555")
db.set_user_email("hasmail", "a@b.co")
sent = sms.send_expectation_message("hasmail", "+19998885555", "test")
check("main copy (welcome email line) for email users",
      "welcome email" in sent)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
