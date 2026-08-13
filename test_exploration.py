"""
Exploration P2/P3/P5 tests — features, user notes, planner flip.

Run: ./venv/bin/python test_exploration.py  (sqlite; anthropic mocked)
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

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_exploration.db")
db.init_db()

import features  # noqa: E402
import notes  # noqa: E402
import sms  # noqa: E402

U = "test_user"
PASS = []
NOW = datetime.now()


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def put(kind, payload, ts):
    db.log_event(U, kind, payload, source="test")
    conn = db.get_conn()
    conn.execute(
        "UPDATE events SET ts=? WHERE id=(SELECT MAX(id) FROM events)",
        (ts.isoformat(),))
    conn.commit()
    conn.close()


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


# ── P2: features ─────────────────────────────────────────────────────
print("P2) features")
f = features.compute_features(U, now=NOW)
check("fresh user degrades to neutral opener",
      f["position"] == "opener" and f["user_signal"] == "neutral"
      and f["silence_streak_days"] is None)

put("sms_out", {"text": "저녁!", "trigger": "cron_evening",
                "steps": [{"tag": "micro_ask", "intensity": 2}]},
    NOW - timedelta(days=2, hours=2))
put("sms_in", {"text": "ㅇㅇ"},
    NOW - timedelta(days=2, hours=1, minutes=58))
f = features.compute_features(U, now=NOW)
check("silence streak counts days since last reply",
      f["silence_streak_days"] == 2)
check("last reply latency bucketed (2min → fast)",
      f["reply_latency"] == "fast")
check("last_steps carries the previous send's tags",
      f["last_steps"] == ["micro_ask"])
check("opener after long gap", f["position"] == "opener")

late = NOW.replace(hour=23, minute=0)
put("sms_out", {"text": "how", "trigger": "cron_evening", "steps": []},
    late - timedelta(minutes=10))
put("sms_in", {"text": "아 몰라"}, late)
f = features.compute_features(U, now=late + timedelta(minutes=5))
check("short late reply → tired; recent exchange → mid",
      f["user_signal"] == "tired" and f["position"] == "mid")

# ── P3: user notes ───────────────────────────────────────────────────
print("P3) user notes")
nid, v = db.save_user_note(
    U, "콜드오픈 직접 지시는 죽는다",
    given={"position": "opener", "silence_streak_days": ">=2"},
    when=["micro_ask@3"], expect="no_reply",
    evidence=[{"day": "2026-07-22", "event_ids": [1, 2]}],
    confidence="hypothesis", source="operator")
check("note saved with version 1", v == 1)
nid2, v2 = db.save_user_note(
    U, "콜드오픈 직접 지시는 죽는다 (확정)",
    when=["micro_ask@3"], expect="no_reply",
    confidence="confirmed", source="nightly", note_id=nid)
check("revision bumps version, same note_id", nid2 == nid and v2 == 2)
active = db.get_user_notes(U)
check("latest version wins", len(active) == 1
      and active[0]["confidence"] == "confirmed")
check("note_saved events emitted", len(events_of("note_saved")) == 2)

db.save_user_note(U, "retired claim", confidence="retired", note_id=nid)
check("retired notes filtered by default",
      len(db.get_user_notes(U)) == 0
      and len(db.get_user_notes(U, include_retired=True)) == 1)

db.save_user_note(U, "validate 거친 뒤 작은 ask는 받는다",
                  given={"position": "mid"},
                  when=["validate@2", "micro_ask@1"], expect="reply",
                  confidence="hypothesis")
block = notes.render_notes_block(U)
check("rendered block shows condition → move → expect",
      "validate@2, micro_ask@1" in block and "expect reply" in block)
check("empty-notes user renders empty block",
      notes.render_notes_block("nobody") == "")

# ── P5: assembly + EXPECT + planner hold ─────────────────────────────
print("P5) planner")
prompt, versions = sms._build_system_prompt("evening", U)
check("during onboarding the prior stays out — nothing to run yet",
      "Policy prior" not in prompt and "prior" not in versions)
check("notes block present, claims only while onboarding runs",
      "What is KNOWN about this user" in prompt
      and "validate@2, micro_ask@1" not in prompt)
check("trajectory block present — rhythm without move tags "
      "(step surfaces removed)",
      "Recent trajectory" in prompt and "coach: sent" in prompt
      and "micro_ask@2" not in prompt)
check("no tagging asks at all during onboarding (step surfaces "
      "removed)",
      "[EXPECT:" not in prompt and "[STEP:" not in prompt
      and "### Vocabulary" not in prompt)

db.check_and_complete_onboarding(U, force=True)
done, done_versions = sms._build_system_prompt("evening", U)
check("move chains stay OFF the notes after completion too "
      "(step surfaces removed)",
      "validate@2, micro_ask@1" not in done)
check("completion brings back rules 2-8 — but NOT the prior, and "
      "NOT the vocabulary (step surfaces removed 2026-08-12)",
      "Policy prior" not in done and "prior" not in done_versions
      and "### Vocabulary" not in done
      and "One cognitive altitude at a time" in done)

expect, text = sms._process_expect_marker("안녕\n[EXPECT: advance]")
check("EXPECT parsed and stripped",
      expect == "advance" and "[EXPECT" not in text)


class FakeSend:
    def __init__(self, response_text):
        class _M:
            @staticmethod
            def create(**kwargs):
                class _B:
                    text = response_text

                class _R:
                    content = [_B()]
                return _R()
        self.messages = _M


sms.anthropic.Anthropic = lambda *a, **kw: FakeSend(
    "오늘 3줄 가자!\n[STEP: evoke_mastery@1, micro_ask@2]\n[EXPECT: reply]")
sent = sms.handle_cron_tick("evening")
out = json.loads(events_of("sms_out")[-1]["payload"])
check("cron path stores steps + expect",
      out.get("expect") == "reply"
      and out.get("steps")[0]["tag"] == "evoke_mastery"
      and "[EXPECT" not in sent)

sms.anthropic.Anthropic = lambda *a, **kw: FakeSend("[STEP: hold]")
held = sms.handle_cron_tick("evening")
check("hold retired (2026-08-12): a twice-empty body sends nothing "
      "and is recorded as a suspension violation, never as a "
      "planner choice",
      held is None
      and len(events_of("hold_while_suspended")) >= 1
      and not any(json.loads(t["payload"]).get("action")
                  == "held_by_planner"
                  for t in events_of("cron_tick")))

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
