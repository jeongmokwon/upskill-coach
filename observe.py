"""
Screen-observation summarizer (server side).

Receives screenshot bytes uploaded by the local agent (observer.py),
runs one small vision-model call, and returns a compact TEXT
observation for the companion brain. Images are processed in memory
and never written to disk — Render's filesystem is ephemeral, and
text is all the tutor needs (plus it's the privacy-friendlier shape
to retain).

Model choice: Haiku — observations fire every ~60s during a session,
so per-call cost matters more than nuance. The heavy pedagogical
reasoning happens later, in the tutor conversation, on Sonnet.
"""

import base64
import threading
import time

import anthropic

# Two vision tiers:
#   ambient (timer captures)   — Haiku, short gist. Fires every ~60s;
#                                per-call cost dominates.
#   deep (on-demand captures)  — Sonnet, verbatim transcription. Fires
#                                only when the user texts mid-session,
#                                i.e. when they're explicitly asking
#                                the tutor to look. Getting the actual
#                                code/error right matters more than
#                                pennies here: a shallow summary makes
#                                the tutor fill gaps from chat history
#                                and fabricate "what it sees".
VISION_MODEL_AMBIENT = "claude-haiku-4-5-20251001"
VISION_MODEL_DEEP = "claude-sonnet-4-5"

# ─── On-demand capture requests ──────────────────────────────────────
#
# When the user texts the tutor mid-study, the reply should see the
# CURRENT screen, not one up to 60s stale. The server can't reach the
# laptop directly, so the local agent long-polls /observe/poll and the
# inbound handler drops a request flag here. In-memory is fine: single
# process on Render, and a lost flag on restart just means one capture
# happens on the next timer tick instead.

_capture_requests = {}
_cr_lock = threading.Lock()


def request_capture(user_id):
    with _cr_lock:
        _capture_requests[user_id] = time.time()


def consume_capture_request(user_id, max_age=30):
    """Agent-side poll: pop the request if one is pending and fresh.
    Stale requests (>max_age s) are dropped — the moment has passed."""
    with _cr_lock:
        ts = _capture_requests.pop(user_id, None)
    return bool(ts and time.time() - ts <= max_age)

_PROMPT_AMBIENT = """\
You are the eyes of a learning companion. This is one screenshot of
the learner's laptop taken during a study session. In 1-3 short
sentences, note:

1. The active app / window and what the person appears to be doing.
2. Study-relevant specifics if visible: file names, code, error
   messages (quote errors verbatim if short), doc/tutorial titles.
3. Signals of avoidance if obvious: entertainment sites, social
   feeds, video not related to study.

Be factual and neutral — no judgment, no advice. If the screen is
ambiguous, say what you can see without guessing. Output plain text
only."""

_PROMPT_DEEP = """\
You are the eyes of a learning companion. The learner just texted
their tutor mid-study and this screenshot was captured on demand —
the tutor is about to answer a question about what is on this
screen, so PRECISION matters more than brevity.

1. Name the active app/window and what the learner appears to be
   doing.
2. If code is visible (editor, notebook cell, terminal), TRANSCRIBE
   it verbatim, preserving line structure — up to ~40 lines. If some
   text is too small/blurry to read reliably, write [unreadable]
   rather than guessing at it.
3. Transcribe any visible error message or output verbatim.
4. Note other visible windows/tabs briefly.

NEVER invent or complete code you cannot actually read. A wrong
transcription is far worse than an [unreadable] marker — the tutor
will act on what you write. Output plain text only."""


def summarize_screenshot(image_bytes, media_type="image/jpeg", deep=False):
    """One vision call → text observation. `deep` (on-demand
    captures) uses the stronger model + verbatim transcription; the
    default ambient tier stays cheap. Raises on API failure — caller
    decides how to handle."""
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=VISION_MODEL_DEEP if deep else VISION_MODEL_AMBIENT,
        max_tokens=900 if deep else 250,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
                    },
                },
                {"type": "text", "text": _PROMPT_DEEP if deep else _PROMPT_AMBIENT},
            ],
        }],
    )
    return resp.content[0].text.strip()
