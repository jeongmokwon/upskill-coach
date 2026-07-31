"""
Onboarding state-machine tests (P0-A).

Run: ./venv/bin/python test_onboarding.py  (sqlite; anthropic mocked)

The claim under test: completion is a DERIVED predicate over five
stored fields — the LLM fills fields via markers, the server flips
started/completed timestamps and the phase; the checklist block
steers every onboarding call toward the missing fields.
"""

import json
import os
import tempfile
from datetime import datetime

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "hub"
os.environ["TUTOR_USER_PHONE"] = "+15550002222"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_onboarding.db")
db.init_db()

import sms  # noqa: E402

U = "hub"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


# ── 1. fresh user: empty checklist, discovery, checklist block ───────
print("1) fresh user")
db.ensure_user_profile_row(U)
s = db.get_onboarding_state(U)
check("all five fields missing", s["missing"] == list(db.ONBOARDING_FIELDS)
      and s["started_at"] is None and s["completed_at"] is None)
check("the first concrete task is NOT an onboarding field — it "
      "belongs to the first session, after a plan exists",
      "bite" not in db.ONBOARDING_FIELDS and "offer" in db.ONBOARDING_FIELDS)

prompt, _ = sms._build_system_prompt("evening", U)
check("checklist block injected, focused on the first missing field",
      "Onboarding — what is still unsettled" in prompt
      and "This message's focus: their goal" in prompt
      and "Sequence assignment" not in prompt)

# ── 2. markers fill fields one by one ────────────────────────────────
print("2) field fills")
db.set_agreed_bite(U, "read 3.1 and note confusions", source="analyze")
check("early bite saves WITHOUT phase flip",
      db.get_user_phase(U)["phase"] == "discovery"
      and db.get_user_phase(U)["agreed_first_bite"] != ""
      and len(events_of("bite_committed")) == 1)
check("and it moves no onboarding field either way",
      "bite" not in db.get_onboarding_state(U)["filled"]
      and "bite" not in db.get_onboarding_state(U)["missing"])

db.save_learning_path(U, "career change into ML", "tiny classifier",
                      "trained & evaluated", source="analyze")
check("path saved", db.get_current_path(U)["direction"]
      == "career change into ML")

check("schedule windows parse (shared with the analysis call)",
      sms.parse_schedule_windows("20:00-22:00, 08:00-08:30")[0]
      == {"start": "20:00", "end": "22:00"}
      and sms.parse_schedule_windows("8pm to 10") == [])
db.save_user_schedule(U, sms.parse_schedule_windows("20:00-22:00"),
                      raw_text="20:00-22:00", source="analyze")

db.set_agreed_goal(U, "become the ML-capable founder")
db.set_agreed_offer(U, "daily question drills from your notes")
check("not complete while ignition marker missing",
      not db.check_and_complete_onboarding(U)
      and db.get_onboarding_state(U)["missing"] == ["ignition_marker"])

# ── 3. last field → completion flips, phase transitions ──────────────
print("3) completion")
db.set_ignition_marker(U, "opens the notebook and types")
check("last fill completes onboarding",
      db.check_and_complete_onboarding(U) is True)
s = db.get_onboarding_state(U)
check("completed_at stamped, event emitted",
      s["completed_at"] and len(events_of("onboarding_completed")) == 1)
check("phase flipped via onboarding, with event",
      db.get_user_phase(U)["phase"] == "first_bite"
      and any(json.loads(e["payload"]).get("via") == "onboarding_completed"
              for e in events_of("phase_transition")))
check("idempotent — second check is a no-op",
      db.check_and_complete_onboarding(U) is False
      and len(events_of("onboarding_completed")) == 1)

prompt, _ = sms._build_system_prompt("evening", U)
check("checklist block gone after completion",
      "Onboarding checklist" not in prompt)

# ── 4. started_at stamped by the first send; force-backfill ──────────
print("4) started_at + force")


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            class _B:
                text = "응 좋아, 내일 봐!\n[STEP: connect@1]\n[EXPECT: reply]"

            class _R:
                content = [_B()]
            return _R()


sms.anthropic.Anthropic = FakeClient
# P0-C: this user agreed a schedule above, so fixed crons stand down
# (user_schedule_active) — the send now arrives via the hourly
# schedule tick hitting the 20:00 window's start hour.
sms.handle_schedule_tick(now=datetime.now().replace(hour=20, minute=5))
check("first coach send stamps onboarding_started_at",
      db.get_onboarding_state(U)["started_at"] is not None
      and len(events_of("onboarding_started")) == 1)

# ── the focus block rides second, right under the clock ─────────────
print("focus block placement")
U3 = "placement"
db.ensure_user_profile_row(U3)
for label, p in (("scheduled", sms._build_system_prompt("evening", U3)[0]),
                 ("reply", sms._build_system_prompt_for_reply(U3)[0])):
    heads = [ln for ln in p.split("\n") if ln.startswith("## ")]
    check(f"{label}: clock first, then what this message is for",
          heads[0].startswith("## Right now")
          and heads[1].startswith("## Onboarding"))

check("ignition judgment is asked only about an inbound reply",
      "## Ignition judgment" in sms._build_system_prompt_for_reply(U)[0]
      and "## Ignition judgment" not in sms._build_system_prompt("evening", U)[0])

U2 = "veteran"   # deliberately NO ensure_user_profile_row: a forced
                 # completion used to log success and write nothing
check("force completes an incomplete user (operator backfill)",
      db.check_and_complete_onboarding(U2, force=True) is True
      and db.get_onboarding_state(U2)["completed_at"] is not None)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
