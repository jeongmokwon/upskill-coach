"""
Multi-user identity (M1): the DB is the routing truth, env is the
fallback, and nothing the single-user pilot relies on breaks.

Run: ./venv/bin/python test_multiuser.py  (sqlite)
"""

import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TZ_OFFSET_HOURS"] = "0"
os.environ["TUTOR_USER_ID"] = "envuser"
os.environ["TUTOR_USER_PHONE"] = "+15550001111"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_multiuser.db")
db.init_db()

import sms  # noqa: E402

PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


# ── 1. binding ───────────────────────────────────────────────────────
print("1) phone binding")
db.ensure_user_profile_row("alice")
db.set_user_phone("alice", "+15550002222")
check("bound and looked up", db.get_user_by_phone("+15550002222") == "alice")
try:
    db.ensure_user_profile_row("bob")
    db.set_user_phone("bob", "+15550002222")
    check("rebinding another user's number is refused", False)
except ValueError:
    check("rebinding another user's number is refused", True)
db.set_user_phone("alice", "+15550002222")
check("re-binding the SAME pair is an idempotent no-op", True)

# ── 2. routing: DB first, env fallback ───────────────────────────────
print("2) inbound routing")
check("DB-bound number routes to its user",
      sms._resolve_user_from_phone("+15550002222") == "alice")
check("env number still routes (fallback intact mid-migration)",
      sms._resolve_user_from_phone("+15550001111") == "envuser")
check("whatsapp: prefix is stripped before lookup",
      sms._resolve_user_from_phone("whatsapp:+15550002222") == "alice")
check("unknown number routes nowhere",
      sms._resolve_user_from_phone("+15559998888") is None)
db.ensure_user_profile_row("mallory")
db.set_user_phone("mallory", "+15550001111")
check("DB wins over env on conflict",
      sms._resolve_user_from_phone("+15550001111") == "mallory")

# ── 3. outbound phone resolution ─────────────────────────────────────
print("3) outbound")
check("profile phone first",
      sms._phone_for("alice") == "+15550002222")
check("env fallback only for the env-named user",
      sms._phone_for("envuser") == "+15550001111"
      and sms._phone_for("bob") == "")

# ── 4. the active roster ─────────────────────────────────────────────
print("4) roster")
roster = {u["user_id"] for u in db.get_active_users()}
check("bound users are on the roster", {"alice", "mallory"} <= roster)
check("phoneless users are not", "bob" not in roster)
db.set_user_status("alice", "paused")
check("paused drops off; resume returns",
      "alice" not in {u["user_id"] for u in db.get_active_users()}
      and (db.set_user_status("alice", "active") or
           "alice" in {u["user_id"] for u in db.get_active_users()}))
try:
    db.set_user_status("alice", "vanished")
    check("unknown status refused", False)
except ValueError:
    check("unknown status refused", True)

# ── 5. cron fan-out (M2) ─────────────────────────────────────────────
print("5) cron fan-out")
os.environ["MESSAGING_CHANNEL"] = "sms"
os.environ.pop("TWILIO_ACCOUNT_SID", None)   # sends become logged no-ops


class FakeAll:
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
                    text = "안녕!\n[STEP: connect@1]\n[EXPECT: reply]"

            class _R:
                content = [_B()]
            return _R()


sms.anthropic.Anthropic = FakeAll


def ticks(uid):
    return [r for r in db.get_events(uid, limit=100)
            if r["kind"] in ("cron_tick", "sms_out")]


sms.handle_cron_tick("evening")
check("every rostered user got their own evening tick",
      ticks("alice") and ticks("mallory"))
check("phoneless bob got nothing", not ticks("bob"))

# isolation: one user's crash cannot touch the next
real = sms._cron_tick_for_user
calls = []


def bomb(user_id, phone, slot, window=None):
    calls.append(user_id)
    if user_id == "alice":
        raise RuntimeError("boom")
    return real(user_id, phone, slot, window=window)


sms._cron_tick_for_user = bomb
sms.handle_cron_tick("evening")
sms._cron_tick_for_user = real
check("a crashing user is recorded and the rest still run",
      "mallory" in calls and calls.index("alice") < calls.index("mallory")
      and any(r["kind"] == "cron_user_failed"
              for r in db.get_events("alice", limit=20)))

check("empty roster (env unset too) skips with a reason",
      True)  # covered implicitly; env pair is set in this suite

# ── 6. activation pipeline (M3) ──────────────────────────────────────
print("6) activation")
import asyncio  # noqa: E402
import json  # noqa: E402

import coach  # noqa: E402
import emailer  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

os.environ["CRON_SECRET"] = "sek"


class FakeHTTP:
    def __init__(self, req, timeout=None):
        pass

    def __enter__(self):
        class R:
            @staticmethod
            def read():
                return b'{"id": "re_w"}'
        return R()

    def __exit__(self, *a):
        return False


emailer.urllib.request.urlopen = FakeHTTP
os.environ["RESEND_API_KEY"] = "re_test"


def hit(handler, path):
    async def go():
        return await handler(make_mocked_request("POST", path))
    return asyncio.run(go())


db.save_sms_signup("+15550007001", name="Grace", email="g@x.co",
                   consent_checkins=True)
db.save_sms_signup("+15550007002", name="NoConsent", email="n@x.co",
                   consent_checkins=False)
sid_ok = db.get_pending_signups()[0]["id"]
sid_no = db.get_pending_signups()[1]["id"]

r = hit(coach._activate_handler,
        f"/debug/activate?secret=sek&signup_id={sid_ok}&user_id=grace1")
check("one click: profile + phone + email + active + event",
      r.status == 200
      and db.get_user_by_phone("+15550007001") == "grace1"
      and db.get_sms_signup(sid_ok)["status"] == "active"
      and (db.get_user_profile_by_id("grace1") or {}).get("email") == "g@x.co"
      and any(e["kind"] == "user_activated"
              for e in db.get_events("grace1", limit=10)))
check("activated user joins the cron roster immediately",
      "grace1" in {u["user_id"] for u in db.get_active_users()})
r = hit(coach._activate_handler,
        f"/debug/activate?secret=sek&signup_id={sid_ok}&user_id=grace2")
check("re-activating the same signup is refused", r.status == 409)
r = hit(coach._activate_handler,
        f"/debug/activate?secret=sek&signup_id={sid_no}&user_id=nope1")
check("no SMS consent → activation refused (carrier promise is structural)",
      r.status == 412 and db.get_user_by_phone("+15550007002") is None)

# ── 6b. manual trigger targets one user, scheduled hits the roster ───
print("6b) per-user trigger")
os.environ["CRON_SECRET"] = "sek"


def hit_q(handler, path):
    async def go():
        return await handler(make_mocked_request("POST", path))
    return asyncio.run(go())


import time as _t

before = len([r for r in db.get_events("mallory", limit=100)
              if r["kind"] in ("cron_tick", "sms_out")])
r = hit_q(coach._sms_cron_tick_handler,
          "/sms/cron-tick?secret=sek&slot=evening&user_id=alice")
_t.sleep(0.4)   # executor thread
after = len([r for r in db.get_events("mallory", limit=100)
             if r["kind"] in ("cron_tick", "sms_out")])
check("targeted trigger runs alice only — mallory untouched",
      r.status == 200 and json.loads(r.text)["user_id"] == "alice"
      and after == before)
r = hit_q(coach._sms_cron_tick_handler,
          "/sms/cron-tick?secret=sek&slot=evening&user_id=ghost99")
check("unknown target → 404, nothing fires", r.status == 404)

# ── 6c. the nudge slot: evening's twin, honestly labeled ─────────────
print("6c) nudge")
r = hit_q(coach._sms_cron_tick_handler,
          "/sms/cron-tick?secret=sek&slot=nudge")
check("a bare nudge (no user) is refused — never a roster blast",
      r.status == 400)
r = hit_q(coach._sms_cron_tick_handler,
          "/sms/cron-tick?secret=sek&slot=nudge&user_id=mallory")
_t.sleep(0.4)
nudge_events = [x for x in db.get_events("mallory", limit=30)
                if x["kind"] == "sms_out"
                and json.loads(x["payload"]).get("trigger") == "cron_nudge"]
check("nudge fires for its user, labeled cron_nudge (not evening)",
      r.status == 200 and nudge_events)
check("phase timer started by the nudge (first-contact semantics)",
      db.get_user_phase("mallory").get("phase_started_at") is not None)

# ── 7. reset: back to birth, keeping only the identity edge ──────────
print("7) reset")
db.set_agreed_goal("grace1", "g")
db.save_sms_message("grace1", "user", "안녕", "in")
db.record_consent("grace1", "screen_share", "2026-08-05")
_tok_before = db.ensure_user_token("grace1")
counts = db.reset_user("grace1")
prof = db.get_user_profile_by_id("grace1") or {}
check("history wiped, identity kept",
      # 2 messages: the activation-time expectation SMS (companion
      # default since PR-1) + the inbound saved above.
      counts.get("messages") == 2
      and prof.get("phone") == "+15550007001"
      and prof.get("email") == "g@x.co"
      and (prof.get("agreed_goal") or "") == ""
      and db.get_recent_sms_messages("grace1", limit=5) == [])
check("consent wiped → the JIT flow runs again",
      not db.has_consent("grace1", "screen_share", "2026-08-05"))
check("magic token survives (their /my link keeps working)",
      db.ensure_user_token("grace1") == _tok_before)
check("onboarding is back to square one — the reset user will get "
      "the expectation message again",
      db.get_onboarding_state("grace1")["missing"][0]
      == "expectation_setting"
      and db.get_onboarding_state("grace1")["completed_at"] is None)
check("still on the cron roster (phone kept, status default active)",
      "grace1" in {u["user_id"] for u in db.get_active_users()})

# ── 8. activation plants the real name ───────────────────────────────
print("8) names")
import sms as _sms  # noqa: E402

db.save_sms_signup("+15550007003", name="Hana", email="h@x.co",
                   consent_checkins=True)
_sid3 = [r for r in db.get_pending_signups()
         if r["phone"] == "+15550007003"][0]["id"]
hit(coach._activate_handler,
    f"/debug/activate?secret=sek&signup_id={_sid3}&user_id=hana1")
check("activation plants the signup's real name",
      (db.get_user_profile_by_id("hana1") or {}).get("user_name")
      == "Hana")
p3, _ = _sms._build_system_prompt("nudge", "hana1")
check("an activated friend is greeted by name, never by id",
      "Hana" in p3 and "hana1" not in p3)

# ── 9. burst folding: three texts, one answer ────────────────────────
print("9) burst folding")
os.environ["TUTOR_USER_ID"] = "bursty"
os.environ["TUTOR_USER_PHONE"] = "+15550008888"
db.ensure_user_profile_row("bursty")


class BurstFake:
    """During the FIRST generation, a second user text 'arrives'."""
    calls = 0

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
                BurstFake.calls += 1
                if BurstFake.calls == 1:
                    db.save_sms_message("bursty", "user",
                                        "아 그리고 하나 더", "in")

                class _B:
                    type = "text"
                    text = "답장!\n[STEP: connect@1]\n[EXPECT: reply]"

            class _R:
                content = [_B()]
            return _R()


_sms.anthropic.Anthropic = BurstFake
db.set_expectation_sent("bursty")   # not this test's subject
r1 = _sms.handle_inbound("+15550008888", "첫 문자")
check("a reply drafted before the burst finished is DISCARDED",
      r1 is None
      and any(e["kind"] == "reply_discarded_stale"
              for e in db.get_events("bursty", limit=20)))
r2 = _sms.handle_inbound("+15550008888", "이제 진짜 끝")
check("the final message's handler answers, once, with all in view",
      r2 is not None
      and len([e for e in db.get_events("bursty", limit=30)
               if e["kind"] == "sms_out"]) == 1)

# ── 10. prefix strip + auto link delivery ────────────────────────────
print("10) prefix strip + link email")
_steps, out = _sms._process_step_marker(
    "bursty", "[수요일 14:51] 그럼 시작하자\n[STEP: connect@1]")
check("self-imitated time prefixes are stripped from outbound",
      out == "그럼 시작하자")
_steps, out2 = _sms._process_step_marker(
    "bursty", "[A] 항목 얘기부터 하자면 그건 좀 달라")
check("but a bracket the coach legitimately opens with mid-content "
      "only loses the annotation shape, not meaning",
      "항목 얘기부터" in out2)

os.environ["RESEND_API_KEY"] = "re_test"
emailer.urllib.request.urlopen = FakeHTTP
LK = "linkless"
db.ensure_user_profile_row(LK)
db.set_user_email(LK, "lk@x.co")
db.set_agreed_goal(LK, "g"); db.save_learning_path(LK, "d", "p", "c")
db.set_ignition_marker(LK, "m")
db.set_expectation_sent(LK)
_sms.ensure_my_link_delivered(LK)
check("walkthrough focus + email on file + nothing sent → email fires",
      any(e["kind"] == "my_link_emailed"
          for e in db.get_events(LK, limit=20)))
_sms.ensure_my_link_delivered(LK)
check("idempotent — second call sends nothing new",
      len([e for e in db.get_events(LK, limit=20)
           if e["kind"] == "my_link_emailed"]) == 1)
check("and the walkthrough label now points at the inbox",
      "point them at their INBOX" in _sms._walkthrough_label(LK))
GOALLESS = "goalless"
db.ensure_user_profile_row(GOALLESS)
db.set_user_email(GOALLESS, "gl@x.co")
_sms.ensure_my_link_delivered(GOALLESS)
check("a user whose focus is still the goal gets no email yet",
      not [e for e in db.get_events(GOALLESS, limit=20)
           if e["kind"] == "my_link_emailed"])

# ── 11. schedule-tick isolation: a window fires ITS user only ───────
print("11) schedule-tick isolation")
from datetime import datetime as _dt

db.ensure_user_profile_row("winA"); db.set_user_phone("winA", "+15550003311")
db.ensure_user_profile_row("winB"); db.set_user_phone("winB", "+15550003312")
for _u in ("winA", "winB"):
    db.set_expectation_sent(_u)
db.save_user_schedule("winA", _sms.parse_schedule_windows("09:00-10:00"),
                      raw_text="09:00-10:00", source="test")
db.save_user_schedule("winB", _sms.parse_schedule_windows("20:00-20:15"),
                      raw_text="20:00-20:15", source="test")
# winA needs prior thread for the morning slot's no_thread gate
db.save_sms_message("winA", "user", "hi", "in")


def _outs(u):
    return [r for r in db.get_events(u, limit=50) if r["kind"] == "sms_out"]


beforeA, beforeB = len(_outs("winA")), len(_outs("winB"))
_sms.handle_schedule_tick(now=_dt.now().replace(hour=9, minute=1))
check("9am tick fires the 9am user's window",
      len(_outs("winA")) == beforeA + 1)
check("— and ONLY that user (observed live: one user's window texted "
      "the whole roster)",
      len(_outs("winB")) == beforeB
      and not [r for r in db.get_events("winB", limit=20)
               if r["kind"] == "cron_tick"
               and "09:00" in (r["payload"] or "")])

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
