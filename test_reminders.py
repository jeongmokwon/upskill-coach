"""Reminders v0 tests (2026-08-20).

Run: python test_reminders.py  (sqlite; sms.send_nudge mocked)

Claims under test: operator time parsing (LA wall-clock default,
explicit offset honored); due selection is a clean time cut; a fired
one-shot closes and never refires; a weekday recurrence advances to
the next Mon-Fri at the same LA wall-clock time (Friday→Monday,
across the November DST fall-back too); a failed send still
closes/advances (no fire-loop); cancel is ownership-checked.
"""

import os
import tempfile
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "jm"
os.environ["TUTOR_USER_PHONE"] = "+15550001111"
os.environ.pop("TWILIO_ACCOUNT_SID", None)

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_reminders.db")
db.init_db()

import reminders  # noqa: E402
import sms  # noqa: E402

LA = ZoneInfo("America/Los_Angeles")
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


sent = []


def fake_nudge(user_id, instruction):
    sent.append((user_id, instruction))
    return "ok"


sms.send_nudge = fake_nudge


print("parse_operator_time")
utc = reminders.parse_operator_time("2026-08-21 16:45")
back = datetime.fromisoformat(utc).astimezone(LA)
check("naked spec is LA wall-clock", (back.hour, back.minute) == (16, 45))
check("stored as aware UTC ISO", utc.endswith("+00:00"))
explicit = reminders.parse_operator_time("2026-08-21T16:45:00+00:00")
check("explicit offset honored",
      datetime.fromisoformat(explicit).hour == 16)

print("one-shot: due cut, fire, close")
rid = db.create_reminder("jm", reminders.parse_operator_time(
    "2026-08-22 09:00"), "Grocery reminder.")
before = datetime(2026, 8, 22, 8, 59, tzinfo=LA).astimezone(timezone.utc)
after = datetime(2026, 8, 22, 9, 1, tzinfo=LA).astimezone(timezone.utc)
check("not due a minute early",
      reminders.run_due(now=before) == [] and not sent)
report = reminders.run_due(now=after)
check("fires once due", len(report) == 1 and len(sent) == 1)
check("send_nudge got the instruction",
      sent[0] == ("jm", "Grocery reminder."))
check("one-shot closes",
      db.get_reminders("jm", status="done")[0]["id"] == rid)
check("closed row never refires", reminders.run_due(now=after) == [])

print("weekdays recurrence")
sent.clear()
rid2 = db.create_reminder("jm", reminders.parse_operator_time(
    "2026-08-20 16:30"), "Pencil down.", recur="weekdays")
thu = datetime(2026, 8, 20, 16, 31, tzinfo=LA).astimezone(timezone.utc)
reminders.run_due(now=thu)
row = db.get_reminders("jm", status="open")[0]
nxt = datetime.fromisoformat(row["fire_at"]).astimezone(LA)
check("Thu fires → advances to Fri same wall-clock",
      (nxt.date().isoformat(), nxt.hour, nxt.minute)
      == ("2026-08-21", 16, 30))
fri = datetime(2026, 8, 21, 16, 31, tzinfo=LA).astimezone(timezone.utc)
reminders.run_due(now=fri)
nxt = datetime.fromisoformat(
    db.get_reminders("jm", status="open")[0]["fire_at"]).astimezone(LA)
check("Fri fires → skips weekend to Mon",
      (nxt.date().isoformat(), nxt.hour, nxt.minute)
      == ("2026-08-24", 16, 30))
check("recurring stays open, fired twice", len(sent) == 2)

print("DST fall-back keeps wall-clock (Nov 2026: PDT→PST)")
fire_fri_nov = datetime(2026, 10, 30, 16, 30, tzinfo=LA)  # Fri, PDT
nxt_iso = reminders.next_weekday_fire(
    fire_fri_nov.astimezone(timezone.utc).isoformat(),
    now=fire_fri_nov.astimezone(timezone.utc))
nxt = datetime.fromisoformat(nxt_iso).astimezone(LA)
check("Mon after fall-back is still 16:30 LA",
      (nxt.date().isoformat(), nxt.hour, nxt.minute)
      == ("2026-11-02", 16, 30))

print("downed server catches up without replaying")
stale = datetime(2026, 8, 24, 16, 30, tzinfo=LA)  # Mon
late_now = datetime(2026, 8, 26, 10, 0, tzinfo=LA)  # Wed morning
nxt = datetime.fromisoformat(reminders.next_weekday_fire(
    stale.astimezone(timezone.utc).isoformat(),
    now=late_now.astimezone(timezone.utc))).astimezone(LA)
check("advance lands in the future, same wall-clock",
      (nxt.date().isoformat(), nxt.hour) == ("2026-08-26", 16))

print("failed send still closes (no fire-loop)")
sms.send_nudge = lambda u, i: None
rid3 = db.create_reminder("jm", reminders.parse_operator_time(
    "2026-08-22 10:00"), "Doomed.")
report = reminders.run_due(now=datetime(2026, 8, 22, 10, 1,
    tzinfo=LA).astimezone(timezone.utc))
check("reported as not sent",
      len(report) == 1 and report[0]["sent"] is False)
check("still closed", any(r["id"] == rid3
      for r in db.get_reminders("jm", status="done")))

print("cancel is ownership-checked")
rid4 = db.create_reminder("jm", reminders.parse_operator_time(
    "2026-09-01 09:00"), "Cancellable.")
check("wrong owner refused", db.cancel_reminder(rid4, "someone") is False)
check("owner cancels", db.cancel_reminder(rid4, "jm") is True)
report = reminders.run_due(now=datetime(2026, 9, 1, 9, 1, tzinfo=LA)
                           .astimezone(timezone.utc))
check("cancelled row not due",
      all(r["reminder_id"] != rid4 for r in report))

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
