"""Ignition retirement suite (2026-08-12, PR-A).

Run: ./venv/bin/python test_ignition.py  (sqlite)

The ignition→flow frame is shelved: these tests pin the RETIRED
state so a regression (an ignition block sneaking back into a
prompt, a marker writing state again) fails loudly. The original
ignition test suite lives in git history with the machinery.
"""

import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "ig"
os.environ["TUTOR_USER_PHONE"] = "+15550002222"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_ignition.db")
db.init_db()

import sms  # noqa: E402

U = "ig"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


db.ensure_user_profile_row(U)
db.set_expectation_sent(U)
db.save_sms_message(U, "user", "안녕", "in")

print("1) checklist")
check("ignition_marker is no longer an onboarding field",
      "ignition_marker" not in db.ONBOARDING_FIELDS
      and "ignition_marker" not in db.get_onboarding_state(U)["missing"])

print("2) prompts")
reply_prompt, _ = sms._build_system_prompt_for_reply(U)
evening_prompt, _ = sms._build_system_prompt("evening", U)
check("no ignition judgment block anywhere ('ignition' as an EXPECT "
      "vocab token is step-surface territory, tier 2)",
      "Ignition judgment" not in reply_prompt
      and "Ignition judgment" not in evening_prompt
      and "[IGNITION" not in reply_prompt
      and "measuring instrument" not in reply_prompt)
check("placeholders carry no ignition marker",
      "ignition_marker" not in sms._build_placeholders(U))

print("3) markers")
out = sms._process_ignition_markers(U, "좋아!\n[IGNITION: 4]", "t")
check("[IGNITION: n] stripped, nothing recorded",
      "[IGNITION" not in out and out == "좋아!"
      and events_of("ignition_judgment") == [])

print("4) data preserved")
db.set_ignition_marker(U, "opens the file", source="test")
check("column and accessor still exist (archive ≠ delete)",
      db.get_user_phase(U)["ignition_marker"] == "opens the file")

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
