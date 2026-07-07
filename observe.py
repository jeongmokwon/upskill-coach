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

import anthropic

VISION_MODEL = "claude-haiku-4-5-20251001"

_PROMPT = """\
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


def summarize_screenshot(image_bytes, media_type="image/jpeg"):
    """One vision call → compact text observation. Raises on API
    failure — caller decides how to handle."""
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=VISION_MODEL,
        max_tokens=250,
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
                {"type": "text", "text": _PROMPT},
            ],
        }],
    )
    return resp.content[0].text.strip()
