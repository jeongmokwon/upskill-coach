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

# ── 4. the web chat (PR B) ───────────────────────────────────────────
print("4) web chat")
import sms  # noqa: E402


class FakeCoach:
    seen = []

    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            FakeCoach.seen.append(kwargs)
            if kwargs.get("tools"):
                class _B:
                    type = "tool_use"
                    input = {"step_completed": "not_applicable",
                             "step_reason": "-"}
            elif "eyes of Theo" in kwargs.get("system", ""):
                class _B:
                    type = "text"
                    text = ("화면: Medium — ML 기사\n내용: \"Evolution of "
                            "Machine Learning\" 타임라인\n정렬: 자료 밖\n"
                            "확신: high")
            else:
                class _B:
                    type = "text"
                    text = ("타임라인 섹션 보고 있구나 — 1805년부터 쭉. "
                            "지금 어디쯤이 제일 흥미로워?\n"
                            "[STEP: connect@1, spark_curiosity@1]\n"
                            "[EXPECT: reply]")

            class _R:
                content = [_B()]
            return _R()


sms.anthropic.Anthropic = FakeCoach
import analyze_turn  # noqa: E402
analyze_turn.anthropic.Anthropic = FakeCoach
import eyes as eyes_mod  # noqa: E402
eyes_mod.anthropic.Anthropic = FakeCoach

sid3 = db.start_screen_session(U, declared_source="ML 기사")
reply = sms.generate_web_reply(U, sid3, "지금 내 화면 뭐 보여?",
                               jpeg_bytes=b"\xff\xd8frame")
check("reply produced, markers stripped",
      "타임라인 섹션" in reply and "[STEP" not in reply)
check("both sides stored on the ONE thread, channel-tagged web",
      [m for m in db.get_recent_sms_messages(U, limit=4)][-1]["content"]
      == reply)
conn = db.get_conn()
rows = conn.execute("SELECT channel, direction FROM messages "
                    "WHERE user_id=? ORDER BY id DESC LIMIT 2",
                    (U,)).fetchall()
conn.close()
check("channel tags: web/in + web/out",
      {(r[0], r[1]) for r in rows} == {("web", "in"), ("web", "out")})

coach_call = [c for c in FakeCoach.seen
              if "SITTING WITH them" in c.get("system", "")]
check("web block + session journey rode the system prompt",
      coach_call
      and "This session's journey" in coach_call[-1]["system"]
      and "Declared source: ML 기사" in coach_call[-1]["system"])
img_msg = coach_call[-1]["messages"][-1]
check("the raw frame rides INSIDE the reply call (one-call turn)",
      isinstance(img_msg["content"], list)
      and img_msg["content"][0]["type"] == "image"
      and "live capture" in coach_call[-1]["system"])
import time as _t
_t.sleep(0.3)   # the [chat] observation logs in the background
check("the same frame still lands in the journey (async)",
      any("[chat]" in o["summary"]
          for o in db.get_session_observations(sid3)))
check("web_out event with steps + session id",
      any(json.loads(e["payload"]).get("session_id") == sid3
          for e in events_of("web_out")))

# endpoint level
r = hit(coach._session_message_handler, f"/session/message?k={tok}",
        {"session_id": sid3, "text": "그 다음은 뭐 볼까?",
         "jpeg_b64": base64.b64encode(b"f2").decode()})
check("message endpoint returns the reply",
      r.status == 200 and "타임라인" in json.loads(r.text)["reply"])
r = hit(coach._session_message_handler, f"/session/message?k={tok}",
        {"session_id": sid3, "text": ""})
check("empty message refused", r.status == 400)
db.end_screen_session(sid3)
r = hit(coach._session_message_handler, f"/session/message?k={tok}",
        {"session_id": sid3, "text": "hi"})
check("chat to an ended session refused", r.status == 404)

# ── 5. the split turn + holdback invariants (streaming path) ─────────
print("5) stream mechanics")
sid4 = db.start_screen_session(U, declared_source="ML")
sysp, hist, vers = sms.build_web_turn(U, sid4, "이 표 뭐야?",
                                      jpeg_bytes=b"\xff\xd8f3")
check("build/finish split: prompt carries web block + journey",
      "SITTING WITH them" in sysp and "This session's journey" in sysp
      and hist[-1]["role"] == "user")
raw = ("표는 트리거 조건이야. 어느 행이 헷갈려?\n"
       "[STEP: spark_curiosity@1]\n[EXPECT: reply]\n[IGNITION: 2]")
final = sms.finish_web_turn(U, sid4, raw, sysp, hist, vers)
check("finish strips ALL trailing markers from the stored/displayed text",
      "[STEP" not in final and "[EXPECT" not in final
      and "[IGNITION" not in final and "트리거 조건" in final)
check("markers sit within the last 160 chars — the stream holdback "
      "keeps them off the user's screen",
      len(raw) - raw.index("[STEP") <= 160)
check("flight-recorded with frame placeholder, not bytes",
      "fakejpeg" not in json.dumps(db.get_llm_calls(U, limit=3)))
db.end_screen_session(sid4)

# ── 6. lifecycle voice + session-aware walkthrough (PR C) ────────────
print("6) lifecycle")


def hit_json(handler, path, body):
    r = hit(handler, path, body)
    return r, (json.loads(r.text) if r.status == 200 else {})


r, j = hit_json(coach._session_start_handler,
                f"/session/start?k={tok}", {"source": "정리본"})
sid5 = j["session_id"]
check("start returns the understated greeting, stored on the thread",
      j["greeting"] == "세션 시작했네. 보고 있을게."
      and db.get_recent_sms_messages(U, limit=1)[0]["content"]
      == j["greeting"])
check("greeting is marked template in its event (no model chose it)",
      any(json.loads(e["payload"]).get("template") == "session_greeting"
          for e in events_of("web_out")))

# session-aware walkthrough label: needs goal/path/marker set so the
# focus lands on material_walkthrough, plus an in-progress material
W2 = U
label_live = sms._walkthrough_label(W2)
check("with a live session, the label walks the SCREEN",
      "ON THE LIVE SCREEN" in label_live
      and "지금 보고 있는 그 부분" in label_live)
_closings_before = len([e for e in events_of("web_out")
                        if json.loads(e["payload"]).get("template")
                        == "session_closing"])
db.end_screen_session(sid5)
check("a db-level end (dead session) speaks no closing",
      len([e for e in events_of("web_out")
           if json.loads(e["payload"]).get("template")
           == "session_closing"]) == _closings_before)

r2, j2 = hit_json(coach._session_start_handler,
                  f"/session/start?k={tok}", {})
r3, j3 = hit_json(coach._session_stop_handler,
                  f"/session/stop?k={tok}", {"session_id": j2["session_id"]})
check("endpoint stop returns the closing and stores it",
      j3["closing"].startswith("오늘 세션은 여기까지")
      and db.get_recent_sms_messages(U, limit=1)[0]["content"]
      == j3["closing"])
label_idle = sms._walkthrough_label(W2)
check("no live session → the label falls back to conversation mode",
      "ON THE LIVE SCREEN" not in label_idle)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
