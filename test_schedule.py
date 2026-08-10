"""
Per-user send-schedule tests (P0-C).

Run: ./venv/bin/python test_schedule.py  (sqlite; anthropic mocked)

The claim under test: the hourly schedule tick fires each agreed
window once per local day (deduped via the event log), maps window
start hour → morning/evening slot semantics, and — critically — a
user WITH a schedule is no longer served by the legacy fixed crons
(no double sends), while a user without one keeps them unchanged.

Clock injection: `now` is passed into handle_schedule_tick (pattern:
trace.render_trace) — datetime is never monkeypatched. Event rows are
stamped with the REAL clock, so injected `now` values are derived
from datetime.now() to keep dedup-age arithmetic valid at any run
time of day.
"""

import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "hub"
os.environ["TUTOR_USER_PHONE"] = "+15550002222"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ["TZ_OFFSET_HOURS"] = "0"  # local == server; hours line up

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_schedule.db")
db.init_db()

import sms  # noqa: E402

db.ensure_user_profile_row("hub")
db.set_expectation_sent("hub")   # scheduling, not expectation, under test

U = "hub"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def cron_events(user=U):
    return [dict(r, payload=json.loads(r["payload"]))
            for r in db.get_events(user, limit=500)
            if r["kind"] == "cron_tick"]


def sms_out_events(user=U):
    return [dict(r, payload=json.loads(r["payload"]))
            for r in db.get_events(user, limit=500)
            if r["kind"] == "sms_out"]


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            class _B:
                text = "저녁이다, 오늘 어땠어?\n[STEP: connect@1]\n[EXPECT: reply]"

            class _R:
                content = [_B()]
            return _R()


sms.anthropic.Anthropic = FakeClient

db.ensure_user_profile_row(U)

# Event rows carry real datetime.now() timestamps, so all injected
# clocks are anchored to the real clock: a window that starts at the
# CURRENT real hour, "later same day" = same hour :30, "next day" =
# +24h. Ages stay within/beyond the 20h dedup horizon at any runtime.
real = datetime.now()
H = real.hour
token1 = f"{H:02d}:00-{(H + 1) % 24:02d}:17"
win1 = [{"start": f"{H:02d}:00", "end": f"{(H + 1) % 24:02d}:17"}]

# ── 1. no schedule → tick is a no-op ─────────────────────────────────
print("1) no schedule")
r = sms.handle_schedule_tick(now=real)
check("tick without schedule returns None, logs nothing",
      r is None and cron_events() == [])

# ── 2. fixed cron without schedule → unchanged behavior ──────────────
print("2) fixed cron, no schedule")
r = sms.handle_cron_tick("evening")
ev = cron_events()
check("legacy evening cron still fires for a schedule-less user",
      r is not None and len(ev) == 1 and ev[-1]["payload"]["action"] == "fired"
      and "window" not in ev[-1]["payload"])

# ── 3. fixed cron with schedule → suppressed ─────────────────────────
print("3) fixed-slot suppression")
db.save_user_schedule(U, win1, raw_text=token1, source="test")
r = sms.handle_cron_tick("evening")
ev = cron_events()
check("fixed evening cron skipped with reason user_schedule_active",
      r is None and ev[-1]["payload"]["action"] == "skipped"
      and ev[-1]["payload"]["reason"] == "user_schedule_active")
r = sms.handle_cron_tick("morning")
check("fixed morning cron also suppressed",
      r is None and cron_events()[-1]["payload"]["reason"] == "user_schedule_active")

# ── 4. window start hour match → fires, with window in payloads ──────
print("4) window fires")
# Seed the thread so a morning-mapped window doesn't skip on
# no_thread_this_phase. save_sms_message stamps an isoformat
# timestamp (later than phase_started_at, which section 2's evening
# fire set), so the row is visible to the `timestamp > since` filter.
db.save_sms_message(U, "user", "내일 봐요", "in")
r = sms.handle_schedule_tick(now=real)
ev = cron_events()
check("tick at the window's start hour fires", r is not None)
check("cron_tick event carries the window token",
      ev[-1]["payload"]["action"] == "fired"
      and ev[-1]["payload"].get("window") == token1)
check("sms_out event carries the window token",
      sms_out_events()[-1]["payload"].get("window") == token1)
fired_before = len([e for e in ev if e["payload"].get("window") == token1])

# ── 5. same window later the same day → deduped ──────────────────────
print("5) same-day dedup")
r = sms.handle_schedule_tick(now=real.replace(minute=30, second=0))
n = len([e for e in cron_events() if e["payload"].get("window") == token1])
check("second tick in the same hour is deduped",
      r is None and n == fired_before)
r = sms.handle_schedule_tick(
    now=real.replace(minute=30, second=0) + timedelta(hours=1))
n = len([e for e in cron_events() if e["payload"].get("window") == token1])
check("tick at a non-start hour is a no-op", r is None and n == fired_before)

# ── 6. next day → fires again ────────────────────────────────────────
print("6) next-day re-fire")
r = sms.handle_schedule_tick(now=real + timedelta(days=1))
n = len([e for e in cron_events() if e["payload"].get("window") == token1])
check("same window fires again the next day",
      r is not None and n == fired_before + 1)

# ── 7. slot semantics by window start hour ───────────────────────────
print("7) morning/evening semantics")
# New schedule version supersedes win1. Distinct ':30' end minutes so
# these tokens can't collide with win1's ':17' token in dedup matching.
db.save_user_schedule(
    U, [{"start": "09:00", "end": "09:30"}, {"start": "21:00", "end": "21:30"}],
    raw_text="09:00-09:30, 21:00-21:30", source="test")
day3 = (real + timedelta(days=2))
sms.handle_schedule_tick(now=day3.replace(hour=9, minute=5))
ev = cron_events()
check("window starting before 12:00 runs morning-slot semantics",
      ev[-1]["payload"].get("window") == "09:00-09:30"
      and ev[-1]["payload"]["slot"] == "morning")
sms.handle_schedule_tick(now=day3.replace(hour=21, minute=5))
ev = cron_events()
check("window starting 12:00+ runs evening-slot semantics",
      ev[-1]["payload"].get("window") == "21:00-21:30"
      and ev[-1]["payload"]["slot"] == "evening")

# ── 8. debug view state ──────────────────────────────────────────────
print("8) schedule_status")
# Default clock here: the fired events carry real timestamps, so the
# real "now" is the one that makes the fired-today ages meaningful.
status = sms.schedule_status(U)
by_win = {w["window"]: w for w in status["windows"]}
check("status shows latest version's windows with fired-today flags",
      status["schedule"]["version"] == 2
      and by_win["09:00-09:30"]["slot"] == "morning"
      and by_win["21:00-21:30"]["fired_today"] is True
      and by_win["21:00-21:30"]["last_fired"] is not None)

# ── day-scoped windows: a schedule is a weekly table ────────────────
print("day scopes")
check("@weekdays parses to mon-fri",
      sms.parse_schedule_windows("20:00-20:15@weekdays")
      == [{"start": "20:00", "end": "20:15", "days": [0, 1, 2, 3, 4]}])
check("@mon wed fri parses; bad day token rejects the whole schedule",
      sms.parse_schedule_windows("09:00-10:00@mon wed fri")[0]["days"]
      == [0, 2, 4]
      and sms.parse_schedule_windows("09:00-10:00@monday") == [])
check("no scope stays day-free (daily), back-compat",
      "days" not in sms.parse_schedule_windows("20:00-20:15")[0])

UW = "weekdayer"
db.ensure_user_profile_row(UW)
db.set_user_phone(UW, "+15550004411")
db.set_expectation_sent(UW)
db.save_sms_message(UW, "user", "hi", "in")
db.save_user_schedule(UW, sms.parse_schedule_windows("09:00-10:00@weekdays"),
                      raw_text="09:00-10:00@weekdays", source="test")


def _outs(u):
    return [r for r in db.get_events(u, limit=50) if r["kind"] == "sms_out"]


from datetime import datetime as _dt2
import datetime as _dtmod
# find a Saturday and a Monday at 9am local (TZ offset 0 in tests)
base = _dt2.now().replace(hour=9, minute=1)
sat = base + _dtmod.timedelta(days=(5 - base.weekday()) % 7)
mon = base + _dtmod.timedelta(days=(0 - base.weekday()) % 7 or 7)
before = len(_outs(UW))
sms._schedule_tick_for_user(UW, now=sat)
check("Saturday 9am: a weekdays-only window stays silent "
      "(operator's live request: 앞으로 주말은 스킵)",
      len(_outs(UW)) == before)
sms._schedule_tick_for_user(UW, now=mon)
check("Monday 9am: it fires",
      len(_outs(UW)) == before + 1)

# ── pause: a promised silence is a stored state, not theater ────────
print("pause gate")
from datetime import timedelta as _td2
UP = "pauser"
db.ensure_user_profile_row(UP)
db.set_expectation_sent(UP)
db.set_pause(UP, (_dt2.now() + _td2(days=2)).isoformat(), source="test")
r = sms._cron_tick_for_user(UP, "+15550004412", "evening")
check("active pause blocks the proactive send, with the reason "
      "recorded (observed live: coach agreed to pause in words while "
      "the cron kept firing)",
      r is None
      and any("paused_until" in (e["payload"] or "")
              for e in db.get_events(UP, limit=10)
              if e["kind"] == "cron_tick"))
db.set_pause(UP, (_dt2.now() - _td2(hours=1)).isoformat(), source="test")
check("an expired pause no longer blocks",
      db.get_pause(UP) is None)
db.set_pause(UP, (_dt2.now() + _td2(days=1)).isoformat(), source="test")
db.set_pause(UP, "", source="analyze")
check("'none' clears an active pause",
      db.get_pause(UP) is None
      and any(e["kind"] == "pause_cleared"
              for e in db.get_events(UP, limit=10)))

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
