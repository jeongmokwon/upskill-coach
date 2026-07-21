"""
T6 acceptance tests — infra events + capture-gap detection.

Run: python3 test_infra.py
Uses a throwaway sqlite DB; backdates rows with direct SQL to
simulate elapsed time (sqlite-only test, matching local dev).

The WEEK1_ORDER acceptance line: "killing the observer mid-session
produces a gap event without human action" — test 1 is exactly that,
plus dedupe (a 2-minute sweep must not spam one gap 30×/hour).
"""

import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)   # force sqlite before importing db
os.environ.pop("TUTOR_USER_ID", None)
os.environ.pop("MESSAGING_CHANNEL", None)

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_infra.db")
db.init_db()

import infra  # noqa: E402

U = "test_user"
PASS = []


def check(name, cond):
    status = "✅" if cond else "❌"
    print(f"  {status} {name}")
    PASS.append(bool(cond))


def events_of(kind, user_id=U):
    return [r for r in db.get_events(user_id, limit=500)
            if r["kind"] == kind]


def backdate(sql, params):
    conn = db.get_conn()
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def iso_ago(**kw):
    return (datetime.now() - timedelta(**kw)).isoformat()


# ── 1. capture gap: kill the observer mid-session ────────────────────
print("1) capture gap on a silent open session")
sid = db.start_observe_session(U)
backdate("UPDATE observe_sessions SET started_at=? WHERE session_id=?",
         (iso_ago(minutes=10), sid))

infra.sweep()
gaps = events_of("capture_gap")
check("gap event emitted without human action", len(gaps) == 1)
payload = json.loads(gaps[0]["payload"]) if gaps else {}
check("payload names the session", payload.get("session_id") == sid)
check("payload records observer liveness",
      payload.get("observer_polling") is False)

infra.sweep()
check("second sweep does not duplicate", len(events_of("capture_gap")) == 1)

# capture resumes → the gap is over; a later silence is a NEW gap
backdate("UPDATE events SET ts=? WHERE kind='capture_gap'",
         (iso_ago(minutes=8),))
db.save_observation(sid, U, "capture resumed")
infra.sweep()
check("fresh capture clears the gap", len(events_of("capture_gap")) == 1)

backdate("UPDATE observations SET ts=? WHERE session_id=?",
         (iso_ago(minutes=6), sid))
infra.sweep()
check("a second silence stretch is a second gap event",
      len(events_of("capture_gap")) == 2)

db.end_observe_session(sid)
infra.sweep()
check("closed session stops gap detection",
      len(events_of("capture_gap")) == 2)

# ── 2. cron staleness ────────────────────────────────────────────────
print("2) cron self-check")
os.environ["TUTOR_USER_ID"] = U

infra.sweep()
check("no ticks ever → quiet (fresh deploy)",
      len(events_of("cron_missed")) == 0)

db.log_event(U, "cron_tick", {"slot": "morning", "action": "skipped"},
             source="cron")
backdate("UPDATE events SET ts=? WHERE kind='cron_tick'",
         (iso_ago(hours=26),))
infra.sweep()
missed = events_of("cron_missed")
check("26h-stale slot → cron_missed", len(missed) == 1)
check("names the slot",
      missed and json.loads(missed[0]["payload"]).get("slot") == "morning")
check("evening (no tick ever) stays quiet",
      not any(json.loads(m["payload"]).get("slot") == "evening"
              for m in missed))

infra.sweep()
check("repeat sweep does not duplicate",
      len(events_of("cron_missed")) == 1)

db.log_event(U, "cron_tick", {"slot": "morning", "action": "sent"},
             source="cron")
infra.sweep()
check("fresh tick keeps it quiet", len(events_of("cron_missed")) == 1)

# ── 3. whatsapp sandbox expiry suspicion ─────────────────────────────
print("3) whatsapp expiry suspicion")
os.environ["MESSAGING_CHANNEL"] = "whatsapp"
db.log_event(U, "sms_out", {"text": "evening ping"}, source="sms")
db.log_event(U, "sms_in", {"text": "hi"}, source="sms")
backdate("UPDATE events SET ts=? WHERE kind='sms_in'",
         (iso_ago(hours=80),))

infra.sweep()
sus = events_of("whatsapp_expiry_suspected")
check("sending into 80h silence → suspected", len(sus) == 1)

infra.sweep()
check("daily dedupe holds", len(events_of("whatsapp_expiry_suspected")) == 1)

os.environ["MESSAGING_CHANNEL"] = "sms"
backdate("UPDATE events SET ts=? WHERE kind='whatsapp_expiry_suspected'",
         (iso_ago(hours=30),))
infra.sweep()
check("sms channel → check disabled",
      len(events_of("whatsapp_expiry_suspected")) == 1)

# ── result ───────────────────────────────────────────────────────────
print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
