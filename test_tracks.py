"""Life-track generation experiment tests (2026-08-18).

Run: ./venv/bin/python test_tracks.py  (sqlite; anthropic mocked)

The claims under test: the lane gate keeps the husband's path
untouched (no block, no ops run); track configs are born whole and
validated by code (unknown parts rejected AND founder-notified,
surfacing schemas enforced); config evolves via conversation ops
(update / hold / revive / retire) while part_type stays immutable;
items are generic-but-contracted (per-part minimum payload, merge
patching, resolve stamps); duplicate creates collapse onto the
existing track; and ownership is checked on every op.
"""

import json
import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "jm"
os.environ["TUTOR_USER_PHONE"] = "+15550001111"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_tracks.db")
db.init_db()

import tracks_ops  # noqa: E402
import sms  # noqa: E402

PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(user, kind):
    return [dict(r, payload=json.loads(r["payload"]))
            for r in db.get_events(user, limit=500)
            if r["kind"] == kind]


U = "jm"
db.ensure_user_profile_row(U)

print("\n── lane gate ──")
check("lane closed by default", not db.tracks_lane_open(U))
check("ops run is a no-op when lane closed",
      tracks_ops.run(U) is None)
check("tracks block empty when lane closed",
      sms._tracks_block(U) == "")
check("enable opens the lane", db.enable_tracks(U) and
      db.tracks_lane_open(U))
check("enable is idempotent", db.enable_tracks(U) is False)
check("enable evented", len(events_of(U, "tracks_enabled")) == 1)

print("\n── create: validation is code, not vibes ──")
applied, rejected = tracks_ops.apply_ops(U, [
    {"op": "create_track", "evidence": "please track groceries",
     "name": "장보기", "role": "grocery capture list",
     "part_type": "capture_list", "surfacing": {"kind": "on_demand"}},
    {"op": "create_track", "evidence": "watch my stocks",
     "name": "주식", "role": "stock watch",
     "part_type": "price_watch", "surfacing": {"kind": "on_demand"}},
    {"op": "create_track", "evidence": "call my parents",
     "name": "부모님 전화", "role": "call cadence",
     "part_type": "cadence",
     "surfacing": {"kind": "threshold", "days": 14}},
    {"op": "create_track", "evidence": "bad surfacing",
     "name": "청소", "role": "cleaning",
     "part_type": "cadence", "surfacing": {"kind": "threshold"}},
])
check("valid creates applied", len(applied) == 2)
check("unknown part rejected", any("unknown part" in r for r in rejected))
check("unknown part → founder notify event",
      len(events_of(U, "new_part_requested")) == 1)
check("threshold without days rejected",
      any("days" in r for r in rejected))
tracks = db.get_life_tracks(U)
check("two tracks live", len(tracks) == 2)
grocery = next(t for t in tracks if t["name"] == "장보기")
parents = next(t for t in tracks if t["name"] == "부모님 전화")

applied2, rejected2 = tracks_ops.apply_ops(U, [
    {"op": "create_track", "evidence": "groceries again",
     "name": "장보기", "role": "dup", "part_type": "capture_list",
     "surfacing": {"kind": "on_demand"}}])
check("duplicate name collapses onto existing",
      not applied2 and any("duplicate" in r for r in rejected2))

print("\n── config evolves; the machine does not ──")
applied3, rejected3 = tracks_ops.apply_ops(U, [
    {"op": "update_track", "evidence": "make it 3 weeks",
     "track_id": parents["id"],
     "fields": {"surfacing": {"kind": "threshold", "days": 21},
                "donts": ["카운팅 말투 금지"]}},
    {"op": "update_track", "evidence": "hack",
     "track_id": parents["id"], "fields": {"part_type": "research"}},
    {"op": "update_track", "evidence": "not mine",
     "track_id": 99999, "fields": {"role": "x"}},
])
check("config update applied", any("update:" in a for a in applied3))
row = next(t for t in db.get_life_tracks(U)
           if t["id"] == parents["id"])
check("surfacing actually moved",
      json.loads(row["surfacing"])["days"] == 21)
check("donts actually moved",
      json.loads(row["donts"]) == ["카운팅 말투 금지"])
check("part_type immutable (zone B)",
      any("immutable" in r for r in rejected3))
check("foreign track rejected",
      any("not our track" in r for r in rejected3))

applied4, _ = tracks_ops.apply_ops(U, [
    {"op": "set_track_status", "evidence": "pause this",
     "track_id": grocery["id"], "status": "held", "reason": "later"}])
check("hold from conversation", any("held" in a for a in applied4))
check("held track still visible to ops (revivable)",
      any(t["id"] == grocery["id"]
          for t in db.get_life_tracks(U, statuses=("held",))))
applied5, _ = tracks_ops.apply_ops(U, [
    {"op": "set_track_status", "evidence": "bring it back",
     "track_id": grocery["id"], "status": "active"}])
check("revive from conversation", any("active" in a for a in applied5))

print("\n── items: generic but contracted ──")
appliedI, rejectedI = tracks_ops.apply_ops(U, [
    {"op": "add_item", "evidence": "we're out of milk",
     "track_id": grocery["id"], "payload": {"text": "우유"}},
    {"op": "add_item", "evidence": "empty payload",
     "track_id": grocery["id"], "payload": {}},
    {"op": "add_item", "evidence": "called mom today",
     "track_id": parents["id"],
     "payload": {"task": "엄마 통화", "last_done": "2026-08-18"}},
])
check("valid items in", len(appliedI) == 2)
check("part minimum enforced (capture_list needs text)",
      any("needs 'text'" in r for r in rejectedI))
items = db.get_track_items(grocery["id"])
check("grocery item open", len(items) == 1)
iid = items[0]["id"]

appliedP, _ = tracks_ops.apply_ops(U, [
    {"op": "update_item", "evidence": "2 cartons",
     "item_id": iid, "payload": {"qty": 2}}])
merged = json.loads(db.get_track_item(iid)["payload"])
check("payload merge keeps old keys",
      merged.get("text") == "우유" and merged.get("qty") == 2)

appliedR, _ = tracks_ops.apply_ops(U, [
    {"op": "resolve_item", "evidence": "bought it", "item_id": iid}])
check("resolve stamps resolved_at",
      db.get_track_item(iid)["status"] == "resolved"
      and db.get_track_item(iid)["resolved_at"])
check("resolved item leaves the open list",
      db.get_track_items(grocery["id"], status="open") == [])

_, rejectedO = tracks_ops.apply_ops("someone_else", [
    {"op": "resolve_item", "evidence": "not mine", "item_id": iid}])
check("item ownership checked",
      any("not our item" in r for r in rejectedO))

print("\n── prompt block + lane isolation ──")
blk = sms._tracks_block(U)
check("block renders tracks", "장보기" in blk and "부모님 전화" in blk)
check("block carries the capability stance",
      "UNLIMITED" in blk and "promise" in blk.lower())
check("block splits 전능 from 전지 (facts stay honest)",
      "knowledge is NOT" in blk and "invent facts" in blk)
check("block bans machinery vocabulary + meta-questions",
      "machinery language" in blk and "어디부터" in blk)
db.ensure_user_profile_row("hub")
check("drill user: lane closed, no block, no ops",
      not db.tracks_lane_open("hub")
      and sms._tracks_block("hub") == ""
      and tracks_ops.run("hub") is None)
legacy = db.create_track("hub", "Cleary PDF", mode="drill",
                         source="test")
check("legacy drill track untouched by life accessors",
      db.get_life_tracks("hub") == []
      and db.update_track_config(legacy, {"role": "x"}) is None)

print("\n── drill user + open lane coexist (the husband's next state) ──")
db.enable_tracks("hub", source="test")
hub_prompt, _v = sms._build_system_prompt_for_reply("hub")
check("drill freelance guard still present",
      "No freelance drill questions" in hub_prompt)
check("tracks block joins the same prompt",
      "Life tracks" in hub_prompt)
check("ops hop now live for him", db.tracks_lane_open("hub"))

print("\n── surfacing validator matrix ──")
ok = tracks_ops.validate_surfacing
check("on_demand ok", ok({"kind": "on_demand"}) is None)
check("slot needs morning|evening",
      ok({"kind": "slot", "slot": "noon"}) is not None
      and ok({"kind": "slot", "slot": "evening"}) is None)
check("event_date ok", ok({"kind": "event_date"}) is None)
check("garbage rejected", ok("threshold") is not None
      and ok({"kind": "hourly"}) is not None)

print(f"\n{'='*40}\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
