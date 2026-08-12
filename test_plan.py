"""Sequence-plan retirement suite (2026-08-12, PR-A).

Run: ./venv/bin/python test_plan.py  (sqlite)

The step-sequence machinery is archived: no assignment blocks, no
cursor movement, no deviation events. Data and accessors remain
(archive ≠ delete). The original suite lives in git history.
"""

import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "pl"
os.environ["TUTOR_USER_PHONE"] = "+15550002222"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_plan.db")
db.init_db()

import sms  # noqa: E402

U = "pl"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


PLAN = [{"tag": "elicit_why", "intensity": 2, "intent": "why q"},
        {"tag": "micro_ask", "intensity": 1, "intent": "tiny ask"}]
db.ensure_user_profile_row(U)
db.set_expectation_sent(U)
db.check_and_complete_onboarding(U, force=True)
db.save_sequence_plan(U, PLAN, rationale="t", source="operator")
db.save_sms_message(U, "user", "안녕", "in")

print("1) machinery archived")
check("sms no longer builds assignment blocks",
      not hasattr(sms, "_build_plan_block")
      and not hasattr(sms, "_check_plan_deviation"))
prompt, _ = sms._build_system_prompt("evening", U)
check("an ACTIVE saved plan renders nothing into the prompt",
      "Sequence assignment" not in prompt)

print("2) stray markers still stripped")
out = sms._process_plan_markers(U, "좋아 [ADVANCE] 그럼 [STAY]", "t")
check("[ADVANCE]/[STAY] never reach the user",
      "[ADVANCE]" not in out and "[STAY]" not in out)
out2 = sms._process_plan_markers(U, '흠 [REPLAN: "misfit"]', "t")
check("[REPLAN:] stripped (still evented for the operator)",
      "[REPLAN" not in out2)

print("3) data preserved")
plan = db.get_current_plan(U)
check("saved plans and cursor accessors still work (archive ≠ delete)",
      plan and len(plan["steps"]) == 2 and plan["cursor"] == 0)
db.move_plan_cursor(U, 1, reason="manual", source="test")
check("cursor accessor functional for the operator",
      db.get_current_plan(U)["cursor"] == 1)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
