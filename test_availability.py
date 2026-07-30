"""
Availability grid tests (brief §7) — the derived day×hour projection.

Covers: self-report seeding, observed replies raising their hours,
fast-vs-slow weighting, determinism/idempotency (no new version for
an unchanged grid), a changed grid producing a version + event, the
disagreement line, and the empty-user case.

Run: ./venv/bin/python test_availability.py  (sqlite, no LLM)
"""

import json
import os
import tempfile
from datetime import datetime

os.environ.pop("DATABASE_URL", None)
os.environ["TZ_OFFSET_HOURS"] = "0"     # local == server clock
os.environ["TUTOR_USER_ID"] = "test_user"
os.environ.pop("TWILIO_ACCOUNT_SID", None)

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_availability.db")
db.init_db()

import availability  # noqa: E402

U = "test_user"
EMPTY_U = "ghost_user"
PASS = []

# Fixed week so weekday keys are deterministic: 2026-07-06 is a Monday.
NOW = datetime(2026, 7, 10, 12, 0)


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def put(kind, payload, ts):
    """Log an event, then backdate it with direct SQL (the harness
    trick from test_nightly.py) to place it at a chosen hour."""
    db.log_event(U, kind, payload, source="test")
    conn = db.get_conn()
    conn.execute(
        "UPDATE events SET ts=? WHERE id=(SELECT MAX(id) FROM events)",
        (ts.isoformat(),))
    conn.commit()
    conn.close()


def exchange(day, out_h, out_m, in_h, in_m):
    """A coach send and the reply to it, on 2026-07-`day`."""
    put("sms_out", {"text": "ping"}, datetime(2026, 7, day, out_h, out_m))
    put("sms_in", {"text": "ㅇㅇ"}, datetime(2026, 7, day, in_h, in_m))


def events_of(kind, user=U):
    return [r for r in db.get_events(user, limit=300) if r["kind"] == kind]


# ── 0. a user with no data at all ────────────────────────────────────
print("0) empty user")
empty = availability.recompute(EMPTY_U, now=NOW)
check("all seven days present, no cells",
      set(empty["grid"]) == set(availability.DOWS)
      and all(not v for v in empty["grid"].values()))
check("empty user renders without error",
      "no self-reported window and no replies yet"
      in availability.disagreement_line(empty)
      and len(availability.render_grid(empty).splitlines()) == 25)


# ── 1. self-report seeds the right cells ─────────────────────────────
print("1) self-report seeding")
db.save_user_schedule(U, [{"start": "20:00", "end": "22:00"}],
                      raw_text="저녁 8시~10시", source="test")
seeded = availability.recompute(U, now=NOW)
check("every day seeded at 20 and 21",
      all(seeded["grid"][d] == {"20": 0.5, "21": 0.5}
          for d in availability.DOWS))
check("the window's end hour is not claimed",
      "22" not in seeded["grid"]["mon"]
      and "19" not in seeded["grid"]["mon"])
check("sources record the self-report contribution",
      seeded["sources"]["cells"]["mon"]["20"]["self_report"] == 0.5
      and seeded["sources"]["meta"]["self_report_windows"]
      == [{"start": "20:00", "end": "22:00"}])


# ── 2. observed replies raise their hours ────────────────────────────
print("2) observed behaviour")
exchange(6, 20, 20, 20, 30)    # mon: fast reply INSIDE the window
exchange(6, 22, 0, 22, 5)      # mon: fast reply at 22
exchange(7, 22, 10, 22, 14)    # tue: fast reply at 22
exchange(8, 22, 0, 22, 3)      # wed: fast reply at 22
exchange(9, 16, 0, 22, 0)      # thu: SLOW reply (6h) at 22

r = availability.recompute(U, now=NOW)
check("a fast reply raises a self-reported hour above its seed",
      r["grid"]["mon"]["20"] == 0.85 and seeded["grid"]["mon"]["20"] == 0.5)
check("a fast reply lights an hour the user never claimed",
      r["grid"]["mon"]["22"] == 0.35
      and "22" not in seeded["grid"]["mon"])
check("a fast reply weighs more than a slow one",
      r["grid"]["mon"]["22"] > r["grid"]["thu"]["22"]
      and r["sources"]["cells"]["mon"]["22"]["replies"]["fast"] == 1
      and r["sources"]["cells"]["thu"]["22"]["replies"]["slow"] == 1)
check("an unobserved self-reported cell keeps its seed score",
      r["grid"]["sat"]["20"] == 0.5)
check("sources cite the raw events behind a cell",
      len(r["sources"]["cells"]["mon"]["22"]["event_ids"]) == 1
      and r["sources"]["meta"]["observed_events"] == 5)


# ── 3. the disagreement line ─────────────────────────────────────────
print("3) self-report vs observed")
line = availability.disagreement_line(r)
print(f"     {line}")
check("mismatch reported with both sides and the counts",
      "20:00-22:00" in line and "22:00-23:00" in line
      and "4 of 5" in line and "outside" in line)


# ── 4. determinism + idempotency ─────────────────────────────────────
print("4) idempotency")
again = availability.recompute(U, now=NOW)
check("same events in → identical grid out",
      json.dumps(again["grid"], sort_keys=True)
      == json.dumps(r["grid"], sort_keys=True))

first = availability.update(U, now=NOW)
check("first update writes v1 + one event",
      first["changed"] and first["version"] == 1
      and len(events_of("availability_updated")) == 1
      and len(db.get_availability_snapshots(U)) == 1)

second = availability.update(U, now=NOW)
check("unchanged grid writes no new version and no event",
      second["changed"] is False and second["version"] == 1
      and len(events_of("availability_updated")) == 1
      and len(db.get_availability_snapshots(U)) == 1)

stored = json.loads(db.get_availability_snapshot(U)["grid_json"])
check("the stored snapshot is exactly the recomputed grid",
      stored == r["grid"]
      and db.get_availability_snapshot(U)["method_version"]
      == availability.METHOD_VERSION)


# ── 5. changed input → new version + event ───────────────────────────
print("5) change detection")
exchange(10, 9, 0, 9, 10)      # fri morning: a brand-new hour
third = availability.update(U, now=NOW)
ev = json.loads(events_of("availability_updated")[-1]["payload"])
check("new evidence writes v2 and emits the event",
      third["changed"] and third["version"] == 2
      and len(events_of("availability_updated")) == 2
      and ev["version"] == 2)
check("the event names the moved cell",
      any(c.startswith("fri 09") for c in ev["changed_cells"])
      and third["grid"]["fri"]["9"] == 0.35)
check("v1 survives untouched (append-only)",
      len(db.get_availability_snapshots(U)) == 2
      and json.loads(db.get_availability_snapshots(U)[-1]["grid_json"])
      == r["grid"])


# ── 6. the operator view renders ─────────────────────────────────────
print("6) operator view")
view = availability.render(U, now=NOW)
check("view carries the grid, the versions and the finding",
      "# availability — test_user" in view
      and "mon  tue" in view and "22:00" in view
      and "outside the agreed window" in view)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
