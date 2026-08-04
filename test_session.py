"""
Screen co-viewing sessions, PR A: lifecycle, heartbeat-as-liveness,
the eyes pipeline, and the ephemeral-frames guarantee.

Run: ./venv/bin/python test_session.py  (sqlite; anthropic mocked)
"""

import asyncio
import base64
import json
import os
import tempfile
from datetime import datetime, timedelta

os.environ.pop("DATABASE_URL", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_session.db")
db.init_db()

import eyes  # noqa: E402

U = "s1"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


# ── 1. lifecycle ─────────────────────────────────────────────────────
print("1) lifecycle")
db.ensure_user_profile_row(U)
sid = db.start_screen_session(U, declared_source="파생상품 정리본")
ssn = db.get_screen_session(sid)
check("session row + started event",
      ssn["declared_source"] == "파생상품 정리본"
      and ssn["ended_at"] is None
      and len(events_of("session_started")) == 1)
check("fresh heartbeat → session is live",
      db.get_active_screen_session(U)["session_id"] == sid)

conn = db.get_conn()
conn.execute("UPDATE screen_sessions SET last_seen=? WHERE session_id=?",
             ((datetime.now() - timedelta(seconds=90)).isoformat(), sid))
conn.commit(); conn.close()
check("90s without heartbeat → dead (closed laptop, no reaper needed)",
      db.get_active_screen_session(U) is None)
db.touch_screen_session(sid)
check("a heartbeat revives it",
      db.get_active_screen_session(U) is not None)

check("stop closes once, idempotently, with duration",
      db.end_screen_session(sid) is True
      and db.end_screen_session(sid) is False
      and len(events_of("session_stopped")) == 1
      and json.loads(events_of("session_stopped")[0]["payload"])["minutes"]
      is not None)
check("an ended session is never active",
      db.get_active_screen_session(U) is None)

# ── 2. the eyes ──────────────────────────────────────────────────────
print("2) eyes")


class FakeEyes:
    seen = []

    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            FakeEyes.seen.append(kwargs)

            class _B:
                type = "text"
                text = ("화면: Word — 정리본\n내용: \"3.2 트리거 발동 시 "
                        "정산 기준\" 표 보임\n정렬: 3장 초반\n확신: high")

            class _R:
                content = [_B()]
            return _R()


eyes.anthropic.Anthropic = FakeEyes
_mid = db.add_user_material(U, "file", title="정리본.docx",
                            extracted_text="제3장 조기상환... " * 500)
db.set_material_digest(_mid, "3개 장, 41개 항목")

sid2 = db.start_screen_session(U, declared_source="정리본 3장")
obs = eyes.read_frame(U, sid2, b"\xff\xd8fakejpeg", "scroll_settle",
                      declared_source="정리본 3장")
check("observation stored with its event stamp",
      obs.startswith("[scroll_settle]")
      and any("정산 기준" in o["summary"]
              for o in db.get_recent_observations(U, minutes=5)))
sent = FakeEyes.seen[-1]
body_text = sent["messages"][0]["content"][1]["text"]
check("backbone rode along: digest + extracted text + declared source",
      "3개 장, 41개 항목" in body_text and "제3장 조기상환" in body_text
      and "유저가 선언한 오늘의 소스" in body_text)
check("capture reason explained to the eyes",
      "스크롤 후 멈췄다" in body_text)
check("frame_observed event + flight record WITHOUT the image",
      len(events_of("frame_observed")) == 1
      and "[frame 10B]" in db.get_llm_call(
          json.loads(events_of("frame_observed")[0]["payload"])
          ["llm_call_id"])["messages_json"])
check("no raw frame persisted anywhere",
      "fakejpeg" not in open(db.DB_PATH, "rb").read().hex()
      and not any(f.endswith((".jpg", ".jpeg"))
                  for f in os.listdir(os.path.dirname(db.DB_PATH))))
check("frames counter ticked",
      db.get_screen_session(sid2)["frames"] == 1)

# ── 3. endpoints ─────────────────────────────────────────────────────
print("3) endpoints")
import coach  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

tok = db.ensure_user_token(U)


def hit(handler, path, body):
    req = make_mocked_request("POST", path)

    async def _json():
        return body
    req.json = _json

    async def go():
        return await handler(req)
    return asyncio.run(go())


r = hit(coach._session_heartbeat_handler,
        f"/session/heartbeat?k={tok}", {"session_id": sid2})
check("heartbeat endpoint touches the session", r.status == 200)
r = hit(coach._session_frame_handler, f"/session/frame?k={tok}",
        {"session_id": sid2, "event": "dwell",
         "jpeg_b64": base64.b64encode(b"x" * 10).decode()})
check("frame endpoint accepts and queues", r.status == 200)
r = hit(coach._session_frame_handler, f"/session/frame?k=wrong",
        {"session_id": sid2, "event": "dwell", "jpeg_b64": "eA=="})
check("wrong token → 404, no information leak", r.status == 404)
r = hit(coach._session_frame_handler, f"/session/frame?k={tok}",
        {"session_id": sid2, "event": "dwell",
         "jpeg_b64": base64.b64encode(b"x" * (5 * 1024 * 1024)).decode()})
check("oversized frame refused", r.status == 400)
r = hit(coach._session_stop_handler, f"/session/stop?k={tok}",
        {"session_id": sid2})
check("stop endpoint ends the session",
      r.status == 200 and db.get_screen_session(sid2)["ended_at"])
r = hit(coach._session_frame_handler, f"/session/frame?k={tok}",
        {"session_id": sid2, "event": "dwell", "jpeg_b64": "eA=="})
check("frames to an ended session are refused", r.status == 404)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
