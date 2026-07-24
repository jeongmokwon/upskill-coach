"""
Trace serialization tests — exploration P1.

Run: ./venv/bin/python test_trace.py  (sqlite)

Builds a two-day story in the event log and asserts the rendered
step-language trace: coach step tokens, hold tokens, user reply
tokens with mechanical latency/word-count, [no reply] and trailing
silence, judge/note annotations, screen-run compression, and the
facts-only rule (raw numbers, no buckets).
"""

import os
import tempfile
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)
os.environ["TZ_OFFSET_HOURS"] = "0"   # test timeline == server timeline

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_trace.db")
db.init_db()

import trace  # noqa: E402

U = "test_user"
PASS = []
NOW = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0)


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def put(kind, payload, ts, source="test"):
    db.log_event(U, kind, payload, source=source)
    conn = db.get_conn()
    conn.execute(
        "UPDATE events SET ts=? WHERE id=(SELECT MAX(id) FROM events)",
        (ts.isoformat(),))
    conn.commit()
    conn.close()


D1 = NOW - timedelta(days=2)   # two days ago
D2 = NOW - timedelta(days=1)   # yesterday

# Day 1: evening send with steps → no reply overnight
put("sms_out", {"text": "저녁 됐다. 3줄만?", "trigger": "cron_evening",
                "steps": [{"tag": "map", "intensity": 1},
                          {"tag": "micro_ask", "intensity": 2}]},
    D1.replace(hour=19, minute=50))

# Day 2: morning send → fast reply → judgment; screens; evening hold
put("sms_out", {"text": "morning. 오늘 저녁?", "trigger": "cron_morning",
                "steps": [{"tag": "connect", "intensity": 1}]},
    D2.replace(hour=7, minute=0))
put("sms_in", {"text": "오늘 저녁에 할게 진짜로"},
    D2.replace(hour=7, minute=3))
put("ignition_judgment", {"score": 2, "trigger": "inbound_reply"},
    D2.replace(hour=7, minute=3, second=30))
for i in range(4):
    put("observation", {"summary": f"Colab open, cell {i}"},
        D2.replace(hour=20, minute=10 + i))
put("cron_tick", {"slot": "evening", "action": "skipped",
                  "reason": "user_skip_today",
                  "steps": [{"tag": "hold", "intensity": None}]},
    D2.replace(hour=19, minute=50))

out = trace.render_trace(U, days=3, now=NOW)
print(out)
print()

check("day headers present",
      out.count("== ") == 2)
check("coach step tokens rendered",
      "coach: map@1, micro_ask@2   (cron_evening)" in out)
check("[no reply] after unanswered evening",
      "[no reply]" in out.split("== ")[2])  # inside day-2 block or before
check("user reply with mechanical latency + word count",
      "user: reply +3m 4w" in out)
check("judge annotation",
      "~ judge: ignition 2/5" in out)
check("hold token with reason",
      "coach: hold   (evening: user_skip_today)" in out)
check("screen run compressed in compact mode",
      "[screen x4]" in out)
check("trailing silence (last user word was yesterday morning)",
      "[silence" in out and "trailing" in out)
check("facts only — no bucket words leak in",
      "fast" not in out and "tired" not in out)

verbose = trace.render_trace(U, days=3, verbose=True, now=NOW)
check("verbose shows message snippets",
      '"저녁 됐다. 3줄만?"' in verbose and '"오늘 저녁에 할게 진짜로"' in verbose)
check("verbose shows individual screens",
      "screen: Colab open, cell 0" in verbose)

check("empty window handled",
      trace.render_trace("nobody", days=3, now=NOW) == "(no events in window)")

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
