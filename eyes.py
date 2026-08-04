"""
The eyes: one frame of the user's screen → one textual observation
(PR A of the session build; the design was validated live in the
spike — see PROJECT_BRIEF once §7 catches up).

Principles, in order:
- Backbone over pixels. The user's registered materials (digest +
  extracted text) are injected into every read, so the frame's job
  is LOCATING ("section 3.2 of the 정리본"), not re-acquiring
  content. Pixels are read raw only where no backbone covers them.
- Honesty floor. The reader must say '자료 밖' rather than force an
  alignment, and its confidence gates how strongly the coach may
  later speak.
- Raw frames are ephemeral. Bytes live in memory for the seconds
  this call needs, are never written to disk, and are dropped on
  return. The observation TEXT is the only persistent trace.
"""

import base64
import os

import anthropic

import db

MODEL = os.environ.get("EYES_MODEL", "claude-sonnet-4-5")

# How much of the newest material's extracted text rides along as
# backbone. Full documents don't fit every frame read; the digest
# plus this head is enough to align headings, and the walkthrough /
# coach layers hold the rest.
BACKBONE_TEXT_CHARS = 6_000

EYES_PROMPT = """You are the eyes of Theo, an AI learning coach. You are looking at
one frame of the user's screen during a study session they chose to
share. A separate voice will speak to the user based only on what
you report — report exactly what a sharp human sitting next to them
would take in.

Report in Korean, compactly (this is a log entry, not prose):
1. 화면: what app/site/window, and its state.
2. 내용: READ what is actually visible — headings, the text/code/
   problem itself. Quote what matters; never summarize vaguely.
3. 정렬: if the user's registered materials (below) cover this —
   WHERE in them is the user now, and what surrounds that spot. If
   the screen is NOT the registered material, write '자료 밖'
   plainly; never force a match.
4. 확신: high / medium / low.

Hard rules: never claim to see what is not in the frame. Small or
blurry text you cannot read with certainty is reported as unread,
not guessed."""


def build_backbone(user_id):
    """The known-materials context injected into every frame read."""
    mats = db.get_user_materials(user_id)
    if not mats:
        return "## 유저의 등록 자료\n(없음 — 화면을 직접 읽어라)"
    lines = ["## 유저의 등록 자료"]
    for m in mats[:5]:
        head = m.get("title") or m.get("source_url") or m["kind"]
        lines.append(f"\n### {head} ({m['kind']})")
        if m.get("source_url"):
            lines.append(f"링크: {m['source_url']}")
        if m.get("digest"):
            lines.append(f"다이제스트: {m['digest']}")
        if m.get("user_description"):
            lines.append(f"유저의 설명: {m['user_description']}")
    newest = mats[0]
    if newest.get("extracted_text"):
        lines.append("\n### 최신 자료의 본문 (앞부분)")
        lines.append(newest["extracted_text"][:BACKBONE_TEXT_CHARS])
    return "\n".join(lines)


def read_frame(user_id, session_id, jpeg_bytes, event_type,
               declared_source=""):
    """One frame in, one observation out. Stores the observation,
    flight-records the call, and lets the bytes die with this stack
    frame. → observation text or None. Never raises."""
    try:
        b64 = base64.b64encode(jpeg_bytes).decode()
        context = build_backbone(user_id)
        if declared_source:
            context = (f"## 유저가 선언한 오늘의 소스\n{declared_source}\n\n"
                       + context)
        context += (f"\n\n## 캡처 사유\n{event_type} — "
                    + {"context_switch": "창/탭이 바뀌었다",
                       "scroll_settle": "스크롤 후 멈췄다 (읽기 시작)",
                       "dwell": "한 곳에 오래 머물고 있다",
                       "chat": "유저가 방금 메시지를 보냈다 (현재 화면)",
                       "start": "세션 첫 프레임",
                       }.get(event_type, event_type))
        messages = [{"role": "user", "content": [
            {"type": "image",
             "source": {"type": "base64", "media_type": "image/jpeg",
                        "data": b64}},
            {"type": "text", "text": context}]}]
        resp = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=700, system=EYES_PROMPT,
            messages=messages)
        obs = "".join(b.text for b in resp.content
                      if getattr(b, "type", "") == "text").strip()
        if not obs:
            return None
        # Flight-record WITHOUT the image (raw frames are ephemeral
        # by design; the record keeps the context + what was seen).
        call_id = db.save_llm_call(
            user_id, f"eyes_{event_type}", MODEL, EYES_PROMPT,
            [{"role": "user", "content": f"[frame {len(jpeg_bytes)}B] "
                                         + context[:2000]}],
            response_text=obs)
        stamped = f"[{event_type}] {obs}"
        db.save_observation(session_id, user_id, stamped)
        db.touch_screen_session(session_id, frame=True)
        db.log_event(user_id, "frame_observed",
                     {"session_id": session_id, "event": event_type,
                      "bytes": len(jpeg_bytes), "llm_call_id": call_id},
                     source="eyes")
        return stamped
    except Exception as e:
        print(f"[EYES] ⚠️ read failed ({event_type}): {e}", flush=True)
        return None
