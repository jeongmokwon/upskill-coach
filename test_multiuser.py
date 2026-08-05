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

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
