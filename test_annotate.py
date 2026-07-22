"""
T5 acceptance tests — LearnerState v1 + nightly annotation.

Run: python3 test_annotate.py  (sqlite; anthropic client mocked)

WEEK1_ORDER acceptance: snapshots exist and cite event ids as
evidence; re-running a past day overwrites nothing (new row).
"""

import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)
os.environ["TZ_OFFSET_HOURS"] = "0"   # test timeline == server timeline

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_annotate.db")
db.init_db()

import annotate  # noqa: E402

U = "test_user"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def backdate_event(event_id, iso_ts):
    conn = db.get_conn()
    conn.execute("UPDATE events SET ts=? WHERE id=?", (iso_ts, event_id))
    conn.commit()
    conn.close()


def day_iso(days_ago, hour=20):
    return (datetime.now() - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0)


class FakeClient:
    """Returns canned annotation JSON; captures the rendered system
    prompt so tests can assert what the model actually saw."""
    def __init__(self, response_text):
        self.response_text = response_text
        self.seen_system = None
        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.seen_system = kwargs["system"]

                class _Block:
                    text = outer.response_text

                class _Resp:
                    content = [_Block()]
                return _Resp()
        self.messages = _Messages()


# ── fixture: two days of events ──────────────────────────────────────
d1 = day_iso(2).date().isoformat()   # "night one"
d2 = day_iso(1).date().isoformat()   # "night two"

db.log_event(U, "sms_out", {"text": "evening ping", "trigger": "cron_evening"}, source="sms")   # id 1 → d1
db.log_event(U, "sms_in", {"text": "ok starting"}, source="sms")                                # id 2 → d1
db.log_event(U, "observation", {"summary": "Colab open, cell ran"}, source="observe")           # id 3 → d1
db.log_event(U, "sms_out", {"text": "next evening ping"}, source="sms")                         # id 4 → d2
backdate_event(1, day_iso(2, 19).isoformat())
backdate_event(2, day_iso(2, 20).isoformat())
backdate_event(3, day_iso(2, 21).isoformat())
backdate_event(4, day_iso(1, 19).isoformat())

CANNED_D1 = json.dumps({
    "phase": "discovery",
    "momentum": {"label": "engaged", "rationale": "ran a Colab cell after the ping"},
    "last_ignition_at": day_iso(2, 20).isoformat(),
    "friction_signals": [],
    "ego_friction_events": [],
    "channel_state": {"label": "healthy", "note": "sent and answered"},
    "outcome_v1_events": [{"type": "ignition", "at": day_iso(2, 21).isoformat(),
                           "desc": "Colab cell run after evening ping",
                           "event_ids": [2, 3]}],
})

# ── 1. night one ─────────────────────────────────────────────────────
print("1) first night")
fake = FakeClient(CANNED_D1)
state = annotate.annotate_day(U, d1, client=fake)
check("returns parsed state", state is not None and state["phase"] == "discovery")

snaps = db.get_learner_state_snapshots(user_id=U, day=d1)
check("snapshot row exists", len(snaps) == 1)
check("snapshot cites event ids as evidence",
      json.loads(snaps[0]["evidence_json"]) == [2, 3])
check("tagged with schema+prompt+model",
      snaps[0]["schema_version"] == 1
      and len(snaps[0]["prompt_version"]) == 12
      and snaps[0]["model"] == annotate.MODEL)
check("llm call flight-recorded",
      snaps[0]["llm_call_id"]
      and db.get_llm_call(snaps[0]["llm_call_id"]) is not None)
check("model saw only that day's events",
      "[1]" in fake.seen_system and "[3]" in fake.seen_system
      and "[4]" not in fake.seen_system)

# ── 2. night two, via annotate_all ───────────────────────────────────
print("2) second night (annotate_all)")
CANNED_D2 = CANNED_D1.replace('"engaged"', '"warming"')
results = annotate.annotate_all(d2, client=FakeClient(CANNED_D2))
check("active user discovered and annotated", results.get(U) == "ok")
check("after two nights, snapshots exist for both days",
      len(db.get_learner_state_snapshots(user_id=U, day=d1)) == 1
      and len(db.get_learner_state_snapshots(user_id=U, day=d2)) == 1)

# ── 3. re-annotation appends, never overwrites ───────────────────────
print("3) re-annotation")
first_row = db.get_learner_state_snapshots(user_id=U, day=d1)[0]
annotate.annotate_day(U, d1, client=FakeClient(
    CANNED_D1.replace('"engaged"', '"hot"')))
rows = db.get_learner_state_snapshots(user_id=U, day=d1)
check("re-running a past day adds a new row", len(rows) == 2)
unchanged = [r for r in rows if r["id"] == first_row["id"]][0]
check("original row untouched",
      unchanged["state_json"] == first_row["state_json"]
      and unchanged["created_at"] == first_row["created_at"])
check("newest row first (read-time winner)",
      json.loads(rows[0]["state_json"])["momentum"]["label"] == "hot")

# ── 4. edges ─────────────────────────────────────────────────────────
print("4) edges")
check("day with no events → skip, no row",
      annotate.annotate_day(U, "2020-01-01", client=FakeClient(CANNED_D1)) is None
      and len(db.get_learner_state_snapshots(user_id=U, day="2020-01-01")) == 0)

results = annotate.annotate_all(d2, client=FakeClient("sorry, no JSON here"))
check("malformed model output → recorded error, sweep survives",
      results.get(U, "").startswith("error")
      and db.get_last_event(U, "annotation_failed") is not None)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
