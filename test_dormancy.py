"""
Dormancy-gate tests — mechanical enforcement of the zero-demand
re-opening rule (prior Layer 2, now server-detected).

Run: ./venv/bin/python test_dormancy.py  (sqlite; anthropic mocked)
"""

import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "test_user"
os.environ["TUTOR_USER_PHONE"] = "+15550001111"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_dormancy.db")
db.init_db()

import sms  # noqa: E402

U = "test_user"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def backdate_last(kind, ts):
    conn = db.get_conn()
    conn.execute(
        "UPDATE events SET ts=? WHERE id="
        "(SELECT MAX(id) FROM events WHERE kind=?)",
        (ts.isoformat(), kind))
    conn.commit()
    conn.close()


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


PLAN = [{"tag": "elicit_why", "intensity": 2, "intent": "open why q"},
        {"tag": "micro_ask", "intensity": 1, "intent": "one line"}]
db.ensure_user_profile_row(U)
db.check_and_complete_onboarding(U, force=True)  # plans are post-onboarding
db.save_sequence_plan(U, PLAN, rationale="t", source="operator")

# ── 1. fresh user: never messaged → new, not dormant ─────────────────
print("1) fresh user")
check("no inbound ever → not dormant", not sms._is_dormant(U))
prompt, _ = sms._build_system_prompt("evening", U)
check("no dormant mode for a fresh user (sequence assignments "
      "retired 2026-08-12 — nothing to swap in either direction)",
      "Sequence assignment" not in prompt
      and "re-opening after dormancy" not in prompt)

# ── 2. recent reply → active, plan runs ──────────────────────────────
print("2) active user")
db.log_event(U, "sms_in", {"text": "오늘 저녁에 할게"}, source="sms")
backdate_last("sms_in", datetime.now() - timedelta(hours=3))
check("3h silence → not dormant", not sms._is_dormant(U))

# ── 3. 3-day silence → dormant mode replaces the assignment ──────────
print("3) dormant user")
backdate_last("sms_in", datetime.now() - timedelta(hours=72))
check("72h silence → dormant", sms._is_dormant(U))
prompt, _ = sms._build_system_prompt("evening", U)
check("dormant block replaces the plan assignment",
      "re-opening after dormancy" in prompt
      and "Sequence assignment" not in prompt)
check("dormant block pauses the plan and bans asks",
      "PAUSED" in prompt and "No tasks, no bites, no asks" in prompt)

# ── 4. violation detection on the cron path ──────────────────────────
print("4) enforcement")


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            class _B:
                text = ("콜랩 열고 한 줄만 쳐보자!\n"
                        "[STEP: micro_ask@2]\n[EXPECT: reply]")

            class _R:
                content = [_B()]
            return _R()


sms.anthropic.Anthropic = FakeClient
sent = sms.handle_cron_tick("evening")
v = events_of("planner_violation")
check("ask into a dormant channel → planner_violation logged",
      len(v) == 1
      and json.loads(v[-1]["payload"])["forbidden_steps"] == ["micro_ask"]
      and json.loads(v[-1]["payload"])["mode"] == "dormant_reopen")
check("detection tier only — message still sent", sent is not None)


class CompliantClient(FakeClient):
    class messages:
        @staticmethod
        def create(**kwargs):
            class _B:
                text = ("요즘 그 앱 개발은 어때? 그냥 궁금해서 ㅎㅎ\n"
                        "[STEP: connect@1]\n[EXPECT: no_reply]")

            class _R:
                content = [_B()]
            return _R()


sms.anthropic.Anthropic = CompliantClient
sms.handle_cron_tick("evening")
check("compliant re-opening → no new violation",
      len(events_of("planner_violation")) == 1)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
