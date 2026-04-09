"""
Upskill Coach v3
- Knowledge Graph based learning coach
- Auto domain classification + diagnostic questions
- Screen capture + voice/text Q&A
- Mastery tracking + personalized exercises

Usage:
    source venv/bin/activate
    python coach.py
"""

import os
import sys
import base64
import time
import json
import asyncio
import threading
import anthropic
import http.server
import aiohttp
from aiohttp import web

print("[BOOT] coach.py starting...", flush=True)

try:
    sys.stdin.reconfigure(encoding='utf-8')
except Exception:
    pass  # No stdin on Render/server environments

print("[BOOT] importing db...", flush=True)
import db
print("[BOOT] db imported OK", flush=True)



# ─── Config ───────────────────────────────────────────────────────────
client = None  # Initialized lazily when API key is available
HTTP_PORT = int(os.environ.get("PORT", 8765))
WS_PORT = int(os.environ.get("WS_PORT", 8766))
BIND_HOST = os.environ.get("BIND_HOST", "localhost")  # "0.0.0.0" on Render

def get_client():
    global client
    if client is None:
        client = anthropic.Anthropic()
    return client

# ─── WebSocket + HTTP Server ─────────────────────────────────────────
ws_clients = set()
_followups_stopped = False
ws_loop = None


# ─── Per-connection session context (multi-user support) ─────────────

class ClientCtx:
    """Per-WebSocket-connection session state."""
    __slots__ = ("ws", "user_id", "user_profile", "study_topic",
                 "section_id", "followups_stopped", "db_session_id",
                 "teaching_style")

    def __init__(self, ws):
        self.ws = ws
        self.user_id = ""
        self.user_profile = {}
        self.study_topic = ""
        self.section_id = ""
        self.followups_stopped = False
        self.db_session_id = ""
        self.teaching_style = {}


# Map websocket → ClientCtx
ws_sessions = {}

# Thread-local: each handler thread gets its own ctx
_tls = threading.local()


def _set_ctx(ctx):
    """Set the current thread's client context."""
    _tls.ctx = ctx
    # Also set db thread-local so db.py uses the right user/session
    if ctx and ctx.user_id:
        db.set_thread_user(ctx.user_id, ctx.db_session_id)


def _ctx():
    """Get current thread's client context."""
    return getattr(_tls, 'ctx', None)


def send_to_client(msg):
    """Send message to the current thread's client websocket."""
    ctx = _ctx()
    if not (ctx and ctx.ws and ws_loop):
        print(f"  [WS] No client context, dropping: {msg.get('type', '?')}")
        return
    data = json.dumps(msg)
    try:
        asyncio.run_coroutine_threadsafe(ctx.ws.send_str(data), ws_loop)
    except Exception as e:
        print(f"  [WS] Send to client failed: {e}")


def _spawn(handler, args, ws):
    """Spawn a handler thread with per-connection context."""
    ctx = ws_sessions.get(ws)
    def _run():
        _set_ctx(ctx)
        handler(*args)
    threading.Thread(target=_run, daemon=True).start()

async def ws_handler(request):
    """aiohttp WebSocket handler."""
    print(f"[WS] New connection from {request.remote}, headers={dict(request.headers)}", flush=True)
    websocket = web.WebSocketResponse()
    try:
        await websocket.prepare(request)
    except Exception as e:
        print(f"[WS] ❌ prepare() failed: {e}", flush=True)
        import traceback
        traceback.print_exc()
        return web.Response(text="WebSocket upgrade failed", status=400)
    print(f"[WS] WebSocket prepared OK", flush=True)

    ws_clients.add(websocket)

    # Create per-connection context
    ctx = ClientCtx(websocket)
    ws_sessions[websocket] = ctx

    # Send current state to newly connected client
    try:
        await websocket.send_str(json.dumps({"type": "waiting_identify"}))
    except Exception:
        pass

    try:
        async for raw_msg in websocket:
            if raw_msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    msg = json.loads(raw_msg.data)
                    msg_type = msg.get("type")
                    if msg_type == "identify":
                        try:
                            handle_identify(msg, websocket)
                        except Exception as _e:
                            import traceback as _tb
                            print(f"[WS] ❌ handle_identify EXCEPTION: {_e}", flush=True)
                            _tb.print_exc()
                            # Don't break the WS — try to send a fallback so the UI moves on
                            try:
                                await websocket.send_str(json.dumps({
                                    "type": "error",
                                    "message": f"server identify failed: {_e}",
                                }))
                                await websocket.send_str(json.dumps({"type": "show_onboarding"}))
                            except Exception:
                                pass
                    elif msg_type == "explain_animation":
                        _spawn(handle_explain_animation, (msg,), websocket)
                    elif msg_type == "chat_init":
                        _set_ctx(ctx)
                        handle_chat_init(msg)
                    elif msg_type == "chat_message":
                        _spawn(handle_chat_message, (msg,), websocket)
                    elif msg_type == "onboarding_submit":
                        _spawn(handle_onboarding_submit, (msg,), websocket)
                    elif msg_type == "quiz_answer":
                        handle_quiz_answer(msg)
                    elif msg_type == "quiz_continue":
                        _quiz_done.set()
                        await websocket.send_str(json.dumps({"type": "show_code_editor"}))
                    elif msg_type == "stop_followups":
                        ctx.followups_stopped = True
                        print("  [WS] Follow-ups stopped by user")
                    elif msg_type == "update_settings":
                        d = msg.get("difficulty")
                        c = msg.get("condition")
                        prof = ctx.user_profile
                        if d is not None:
                            prof["difficulty"] = int(d)
                        if c is not None:
                            prof["user_condition"] = int(c)
                        if prof.get("user_id"):
                            db.set_thread_user(prof["user_id"])
                            db.update_user_profile(
                                prof["user_id"],
                                difficulty=prof.get("difficulty", 3),
                                user_condition=prof.get("user_condition", 3),
                            )
                        print(f"  [WS] Settings updated → difficulty={prof.get('difficulty')}, condition={prof.get('user_condition')}")
                except json.JSONDecodeError:
                    pass
                except Exception as _outer_e:
                    import traceback as _tb2
                    print(f"[WS] ❌ message handler EXCEPTION ({msg_type}): {_outer_e}", flush=True)
                    _tb2.print_exc()
                    # Keep the connection alive; do not re-raise
            elif raw_msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                break
    except Exception as _ws_e:
        import traceback as _tb3
        print(f"[WS] ❌ ws_handler loop EXCEPTION: {_ws_e}", flush=True)
        _tb3.print_exc()
    finally:
        # Capture context BEFORE removing from ws_sessions so the analyzer
        # thread below can still use it.
        final_ctx = ws_sessions.pop(websocket, None)
        ws_clients.discard(websocket)
        if final_ctx and final_ctx.user_id:
            print(f"  [WS] Client disconnected: {final_ctx.user_id} — running session analyzer", flush=True)

            def _run_session_analyzer(_c=final_ctx):
                # Rehydrate per-thread context so db.py uses the right
                # user_id / session_id when writing insights.
                _set_ctx(_c)
                try:
                    analyze_session_and_save()
                except Exception as _ae:
                    print(f"  [Insight] Analyzer thread failed: {_ae}", flush=True)
                try:
                    db.end_session()
                    print(f"  [WS] Session closed for {_c.user_id}", flush=True)
                except Exception as _ee:
                    print(f"  [WS] end_session failed: {_ee}", flush=True)

            threading.Thread(target=_run_session_analyzer, daemon=True).start()

    return websocket

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


async def _health_handler(request):
    """Health check endpoint for Render."""
    print(f"[HTTP] Health check {request.method}", flush=True)
    return web.Response(text="ok")


async def _root_handler(request):
    """Handle root path — WebSocket upgrade or serve index.html."""
    # Check if this is a WebSocket upgrade request
    if request.headers.get("Upgrade", "").lower() == "websocket":
        print(f"[WS] Upgrade on / from {request.remote}", flush=True)
        return await ws_handler(request)

    print(f"[HTTP] Serving index.html to {request.remote}", flush=True)
    file_path = os.path.join(PROJECT_DIR, "index.html")
    if os.path.isfile(file_path):
        return web.FileResponse(file_path)
    return web.Response(text="Not Found", status=404)


async def _static_handler(request):
    """Serve static files from project directory."""
    rel_path = request.match_info.get("path", "")
    file_path = os.path.join(PROJECT_DIR, rel_path)
    if os.path.isfile(file_path):
        return web.FileResponse(file_path)
    return web.Response(text="Not Found", status=404)


@web.middleware
async def _log_middleware(request, handler):
    """Log every incoming request for debugging."""
    upgrade = request.headers.get("Upgrade", "")
    print(f"[REQ] {request.method} {request.path} from={request.remote} upgrade={upgrade}", flush=True)
    return await handler(request)


def start_ws_server():
    """Start combined WebSocket + HTTP server on a single port using aiohttp."""
    global ws_loop
    ws_loop = asyncio.new_event_loop()

    async def _run():
        app = web.Application(middlewares=[_log_middleware])
        app.router.add_get("/health", _health_handler)
        app.router.add_get("/ws", ws_handler)
        app.router.add_get("/", _root_handler)
        app.router.add_get("/{path:.*}", _static_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, BIND_HOST, HTTP_PORT)
        await site.start()
        print(f"🌐 Browser UI: http://{BIND_HOST}:{HTTP_PORT}", flush=True)
        await asyncio.Future()  # run forever

    def _thread():
        try:
            asyncio.set_event_loop(ws_loop)
            ws_loop.run_until_complete(_run())
        except Exception as e:
            print(f"❌ Server crashed: {e}", flush=True)
            import traceback
            traceback.print_exc()

    threading.Thread(target=_thread, daemon=True).start()

# ─── User profile ─────────────────────────────────────────────────────


def get_user_context_str():
    """Build an 'About the user' block for Claude system prompts."""
    prof = _ctx().user_profile
    if not prof:
        return ""
    study_topic = _ctx().study_topic
    parts = [f"Name: {prof.get('user_name', 'unknown')}"]
    if prof.get("studying"):
        parts.append(f"Currently studying: {prof['studying']}")
    if prof.get("goal"):
        parts.append(f"Learning goal: {prof['goal']}")
    if prof.get("background"):
        parts.append(f"Background: {prof['background']}")
    if study_topic and study_topic != prof.get("studying"):
        parts.append(f"Session topic: {study_topic}")

    # Hint preference
    hint_pref = prof.get("hint_preference", "hints")
    if hint_pref == "solo":
        parts.append("Hint preference: user prefers to figure things out on their own — do NOT give hints or corrections proactively. Only point out errors when asked.")
    else:
        parts.append("Hint preference: user wants hints — proactively guide them before they make mistakes.")

    diff = prof.get("difficulty", 3)
    cond = prof.get("user_condition", 3)
    parts.append(f"Difficulty setting: {diff}/5")
    parts.append(f"User condition: {cond}/5")

    lines = "\n".join(f"* {p}" for p in parts)

    # Adaptive instructions based on difficulty & condition
    diff_guide = {
        1: "Explain at a very basic level. Use simple words, short sentences, and lots of analogies. Assume no prior knowledge of this specific topic.",
        2: "Explain clearly with some simplification. Define technical terms when first used.",
        3: "Explain at an intermediate level. You can use technical terms but still provide context.",
        4: "Explain at an advanced level. Be concise, focus on nuances and edge cases.",
        5: "Expert-level explanation. Be dense, precise, skip basics. Focus on deep insights and subtle details.",
    }
    cond_guide = {
        1: "User is very tired/low energy. Make explanations EXTREMELY visual and intuitive. Use diagrams, animations, and metaphors heavily. Keep text minimal. Break everything into tiny digestible pieces. The goal is: even with brain off, they should absorb something.",
        2: "User is a bit tired. Lean heavily on visuals and analogies. Keep explanations short and punchy.",
        3: "User is in normal condition. Balance text and visuals.",
        4: "User is focused. You can go faster, include more detail per section.",
        5: "User is very sharp and focused. Prioritize speed and density. Cover more ground quickly. Less hand-holding, more substance.",
    }

    # ── Derive coaching style from onboarding signals ──
    # Quiz result → cognitive speed indicator
    quiz_insight = ""
    if _quiz_result:
        q_correct = _quiz_result.get("correct", False)
        q_time = _quiz_result.get("time_ms", 0)
        if q_correct and q_time < 10000:
            quiz_insight = "Onboarding quiz: solved quickly and correctly → fast pattern recognition. User can handle denser explanations."
        elif q_correct:
            quiz_insight = "Onboarding quiz: solved correctly but took time → methodical thinker. Give clear step-by-step breakdowns."
        else:
            quiz_insight = "Onboarding quiz: answered incorrectly → may struggle with abstract patterns. Use extra-concrete examples, go slower, more encouragement."

    # Hint pref → hint frequency
    hint_pref = prof.get("hint_preference", "hints")
    if hint_pref == "solo":
        hint_rule = "Hint frequency: LOW. User wants to struggle and discover. Only give hints when explicitly asked. Let them make mistakes — that's how they learn."
        tone_rule = "Tone: PUSHING. Be direct, challenge them. 'Try again', 'What do you think happens if...?'. Don't coddle."
    else:
        hint_rule = "Hint frequency: HIGH. Proactively offer hints before the user gets stuck. Guide them step by step."
        tone_rule = "Tone: CHEERING. Be encouraging and supportive. 'Great job!', 'You're getting closer!', 'Almost there!'. Celebrate small wins."

    # Condition adjusts cheering/pushing intensity
    if cond <= 2 and hint_pref == "solo":
        tone_rule += " But since user is tired, soften the pushing slightly — still challenge, but be warmer."
    elif cond >= 4 and hint_pref == "hints":
        tone_rule += " User is sharp — you can give hints more efficiently, skip obvious ones."

    # Granularity from difficulty + condition combo
    granularity = ""
    if diff <= 2 or cond <= 2:
        granularity = "Granularity: FINE. Break concepts into very small pieces. One idea per paragraph. Lots of examples."
    elif diff >= 4 and cond >= 4:
        granularity = "Granularity: COARSE. Compress information. Skip basics, focus on insights. User can fill in gaps."
    else:
        granularity = "Granularity: MEDIUM. Explain clearly but don't over-explain. Include examples for non-obvious concepts."


    return f"""About the user:
{lines}

IMPORTANT — ADAPTIVE TEACHING RULES:
1. Difficulty {diff}/5: {diff_guide.get(diff, diff_guide[3])}
2. Condition {cond}/5: {cond_guide.get(cond, cond_guide[3])}
3. {quiz_insight}
4. {hint_rule}
5. {tone_rule}
6. {granularity}
7. Tailor your response to this user's background. If they have a programming background (e.g. Swift), use analogies from that language. If they are new to a topic (e.g. Python, ML), explain fundamentals clearly. Always keep their learning goal in mind.

{_build_insights_block()}
{_build_teaching_style_block()}"""


def _build_teaching_style_block():
    """Build teaching style block from extracted style."""
    style = _ctx().teaching_style or _teaching_style
    if not style:
        return ""
    return f"""Based on insights, this user responds best to:
- Explanation style: {style.get('explanation_style', 'N/A')}
- Pacing: {style.get('pacing', 'N/A')}
- Challenge level: {style.get('challenge_level', 'N/A')}
- Flow: {style.get('conversation_flow', 'N/A')}"""


def _build_insights_block():
    """Build PREVIOUS SESSION INSIGHTS block from DB."""
    try:
        recent = db.get_recent_insights(3)
        if not recent:
            return ""
        parts = []
        for ins in reversed(recent):
            analysis = ins.get("analysis", "{}")
            if isinstance(analysis, str):
                try:
                    parsed = json.loads(analysis)
                    # Extract key fields concisely
                    weak = parsed.get("weak_concepts", [])
                    strong = parsed.get("strong_concepts", [])
                    hint = parsed.get("next_session_hint", "")
                    errors = parsed.get("error_patterns", [])
                    summary = []
                    if weak: summary.append(f"Weak: {', '.join(weak)}")
                    if strong: summary.append(f"Strong: {', '.join(strong)}")
                    if errors: summary.append(f"Error patterns: {', '.join(errors)}")
                    if hint: summary.append(f"Hint: {hint}")
                    parts.append(" | ".join(summary))
                except json.JSONDecodeError:
                    parts.append(analysis[:200])
            else:
                parts.append(str(analysis)[:200])
        if parts:
            return "PREVIOUS SESSION INSIGHTS:\n" + "\n".join(f"- {p}" for p in parts)
    except Exception:
        pass
    return ""


import re as _re



def handle_identify(msg, websocket):
    """Handle identify message from browser with localStorage session_id."""
    session_id = msg.get("session_id", "")
    if not session_id:
        asyncio.run_coroutine_threadsafe(
            websocket.send_str(json.dumps({"type": "show_onboarding"})),
            ws_loop,
        )
        return

    # Look up existing profile by session_id
    profile = db.get_user_profile_by_id(session_id)
    if profile:
        db.set_user_id(profile["user_id"])
        ctx = ws_sessions.get(websocket)
        if ctx:
            ctx.user_profile = profile
            ctx.study_topic = profile.get("studying", "")
            ctx.user_id = profile["user_id"]
            _set_ctx(ctx)
        study_topic = profile.get("studying", "")

        # Start DB session
        db.start_session(study_topic=study_topic)
        if ctx:
            ctx.db_session_id = db.get_session_id()
        db.touch_activity()

        _recent = db.get_recent_insights(3)
        print(f"  [Server] Returning user: {session_id} — studying: {study_topic} — recent insights: {len(_recent)}", flush=True)
        if _recent:
            extract_teaching_style()
            if ctx and ctx.teaching_style:
                print(f"  [Style] Applied to session for {profile['user_id']}: keys={list(ctx.teaching_style.keys())}", flush=True)
            else:
                print(f"  [Style] extract_teaching_style() ran but ctx.teaching_style is empty", flush=True)
        else:
            print(f"  [Style] Skipping — no previous insights for this user", flush=True)

        # Send state
        asyncio.run_coroutine_threadsafe(
            websocket.send_str(json.dumps({"type": "connected", "study_context": study_topic})),
            ws_loop,
        )
        asyncio.run_coroutine_threadsafe(
            websocket.send_str(json.dumps({
                "type": "init_settings",
                "difficulty": profile.get("difficulty", 3),
                "condition": profile.get("user_condition", 3),
            })),
            ws_loop,
        )
        asyncio.run_coroutine_threadsafe(
            websocket.send_str(json.dumps({"type": "show_code_editor"})),
            ws_loop,
        )
    else:
        # No profile for this session_id — show onboarding
        asyncio.run_coroutine_threadsafe(
            websocket.send_str(json.dumps({"type": "show_onboarding"})),
            ws_loop,
        )


def handle_onboarding_submit(msg):
    """Handle onboarding form submission from browser."""
    session_id = msg.get("session_id", "")
    studying = msg.get("studying", "").strip() or "ML/AI"
    goal = msg.get("goal", "").strip() or "Learn and grow"
    hint_preference = msg.get("hint_preference", "hints")
    difficulty = int(msg.get("difficulty", 3))
    condition = int(msg.get("condition", 3))

    uid = db.create_user_profile(
        "anonymous", goal=goal, background="", studying=studying,
        hint_preference=hint_preference, difficulty=difficulty,
        user_condition=condition, user_id=session_id,
    )
    db.set_user_id(uid)
    _ctx().user_id = uid
    _ctx().user_profile = {
        "user_id": uid, "user_name": "anonymous", "goal": goal,
        "background": "", "studying": studying,
        "hint_preference": hint_preference,
        "difficulty": difficulty, "user_condition": condition,
    }

    _ctx().study_topic = studying

    # Start DB session
    db.start_session(study_topic=studying)
    _ctx().db_session_id = db.get_session_id()
    db.touch_activity()
    # First session — no insights yet, skip API call

    print(f"  [Server] Onboarded: {uid} — studying: {studying}")

    # Send state to client
    send_to_client({"type": "connected", "study_context": studying})
    send_to_client({
        "type": "init_settings",
        "difficulty": difficulty,
        "condition": condition,
    })
    send_to_client({"type": "show_code_editor"})


# ─── Onboarding Quiz ─────────────────────────────────────────────────
_quiz_done = threading.Event()

_quiz_result = {}  # Stored for system prompt injection

def handle_quiz_answer(msg):
    """Log quiz answer to DB and store result for prompt injection."""
    global _quiz_result
    chosen = msg.get("chosen", "")
    correct = msg.get("correct", "")
    is_correct = msg.get("isCorrect", False)
    time_ms = msg.get("timeMs", 0)

    _quiz_result = {
        "correct": is_correct,
        "time_ms": time_ms,
        "chosen": chosen,
    }

    print(f"  [Quiz] Answer: {chosen.upper()} ({'✓' if is_correct else '✗'}) in {time_ms}ms")

    db.log_practice(
        practice_question="onboarding_quiz: pattern recognition — what comes in place of ?",
        user_answer=chosen,
        is_correct=is_correct,
        time_taken_seconds=time_ms / 1000.0,
        study_topic="onboarding",
        practice_topic="pattern_recognition",
    )


# ─── Chat with Claude ────────────────────────────────────────────────
conversation_history = []
current_study_topic = ""  # For DB logging
current_section_id = None  # Currently highlighted TOC section


_last_explain_plan = {}  # Stores plan data for section regeneration (legacy; write-only after step 2, cleanup in step 5)


def handle_explain_animation(msg):
    """Template-based explanation: single orchestrator call classifies + extracts data, browser renders."""
    global _last_explain_plan
    selected_code = msg.get("selectedCode", "")
    full_code = msg.get("fullCode", "")
    context = msg.get("context", "")

    print(f"  [Explain] Template orchestrator: {selected_code[:60]}")

    user_ctx = get_user_context_str()

    plan_system = f"""You are a world-class programming tutor creating an animated visual explanation.

{user_ctx}

The student selected code and wants a visual explanation. You must:
1. Break the concept into 5-12 MICRO-SECTIONS (one idea per section)
2. Classify each section into a TEMPLATE TYPE
3. Extract structured DATA for each template

AVAILABLE TEMPLATE TYPES:

1. "linear_sequence" — Steps in order: A → B → C
   data: {{ "label": "title", "steps": [{{"text": "step1", "sub": "detail"}}, ...] }}

2. "transformation" — Input → Process → Output
   data: {{ "label": "title", "input": {{"text": "x", "sub": "detail"}}, "process": {{"text": "fn()"}}, "output": {{"text": "y", "sub": "detail"}}, "caption": "..." }}

3. "matrix" — Grid/table of values
   data: {{ "label": "title", "headers": {{"rows": ["r1","r2"], "cols": ["c1","c2"]}}, "cells": [[1,2],[3,4]], "highlight": [[0,0]], "caption": "..." }}

4. "many_to_many" — Multiple inputs → multiple outputs with connections
   data: {{ "label": "title", "inputs": [{{"text": "a"}}, ...], "outputs": [{{"text": "b"}}, ...], "connections": [[0,0],[1,1]], "caption": "..." }}

5. "tree" — Hierarchical branching
   data: {{ "label": "title", "root": {{"text": "root", "children": [{{"text": "child", "children": [...]}}]}}, "caption": "..." }}

6. "before_after" — Side-by-side before/after states
   data: {{ "label": "title", "before": {{"title": "Before", "items": ["a","b"]}}, "after": {{"title": "After", "items": ["x","y"]}}, "highlight": [1], "caption": "..." }}

7. "one_to_many" — One input splits into multiple outputs
   data: {{ "label": "title", "source": {{"text": "input", "sub": "detail"}}, "targets": [{{"text": "out1", "sub": "detail"}}, ...], "caption": "..." }}

8. "many_to_one" — Multiple inputs merge into one output
   data: {{ "label": "title", "sources": [{{"text": "in1"}}, ...], "target": {{"text": "output", "sub": "detail"}}, "caption": "..." }}

9. "comparison" — Side-by-side comparison of two concepts
   data: {{ "label": "title", "left": {{"title": "A", "items": ["x","y"]}}, "right": {{"title": "B", "items": ["x","y"]}}, "caption": "..." }}

10. "cycle" — Circular flow
    data: {{ "label": "title", "nodes": [{{"text": "step1"}}, ...], "caption": "..." }}

11. "distribution" — Bar chart / proportions
    data: {{ "label": "title", "items": [{{"label": "a", "value": 35}}, ...], "unit": "optional", "caption": "..." }}

12. "inclusion" — Nested containment
    data: {{ "label": "title", "sets": [{{"text": "outer", "children": [{{"text": "inner", "children": [...]}}]}}], "caption": "..." }}

CRITICAL RULES:
- Each section = ONE visual + ONE sentence. That's it.
- Use CONCRETE values from the actual code (real strings, real numbers, real variable names)
- Choose the template type that best matches the concept structure
- "sub" fields are optional short annotations shown below the main text
- Keep "caption" to ONE sentence max
- "highlight" in matrix/before_after = indices of cells/items to emphasize
- For connections in many_to_many: [[fromIdx, toIdx], ...]
- Tree children can nest but keep depth ≤ 3
- Distribution values are relative (will be normalized to percentages)

Return ONLY a JSON object:
{{
  "title": "<overall title>",
  "sections": [
    {{
      "id": "section-1",
      "purpose": "<ONE thing this section shows>",
      "template": "<one of the 12 types>",
      "data": {{ ... template-specific data ... }}
    }}
  ]
}}"""

    plan_messages = [
        {"role": "user", "content": f"Selected code:\n```\n{selected_code}\n```\n\nFull code:\n```python\n{full_code}\n```\n\nContext: {context[:300]}"},
        {"role": "assistant", "content": '{"title":"'},
    ]

    try:
        plan_response = ""
        with get_client().messages.stream(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            system=plan_system,
            messages=plan_messages,
        ) as stream_resp:
            for text in stream_resp.text_stream:
                plan_response += text

        plan_json = '{"title":"' + plan_response
        try:
            plan = json.loads(plan_json.strip())
        except json.JSONDecodeError as _je:
            print(f"  [Explain] ❌ JSON parse failed: {_je}")
            print(f"  [Explain] Raw plan_json (first 500 chars): {plan_json[:500]}")
            raise
        title = plan.get("title", "Explanation")
        sections = plan.get("sections", [])
        print(f"  [Explain] Plan: {title} — {len(sections)} sections")
        if len(sections) < 2:
            print(f"  [Explain] ⚠️ Only {len(sections)} section(s) in plan — Claude returned a short response")
            print(f"  [Explain] Raw plan (first 800 chars): {plan_json[:800]}")

        # Store plan for regeneration
        _last_explain_plan = {
            "title": title,
            "sections": sections,
            "selected_code": selected_code,
            "full_code": full_code,
        }

        # Send title
        send_to_client({
            "type": "explain_animation_result",
            "title": title,
            "html": "<div style='color:#484f58;text-align:center;padding:40px;'>Loading sections...</div>",
        })

        # Broadcast all sections instantly (no per-section API calls!)
        for i, sec in enumerate(sections):
            try:
                print(f"  [Explain] Section {i+1}/{len(sections)}: [{sec.get('template','')}] {sec.get('purpose','')[:50]}", flush=True)
                send_to_client({
                    "type": "explain_section",
                    "index": i,
                    "total": len(sections),
                    "template": sec.get("template", "linear_sequence"),
                    "data": sec.get("data", {}),
                    "purpose": sec.get("purpose", ""),
                    "title": title,
                })
            except Exception as _se:
                print(f"  [Explain] ❌ Failed to send section {i}: {_se}", flush=True)
                import traceback as _tb
                _tb.print_exc()

        send_to_client({"type": "explain_done", "total": len(sections), "title": title})
        print(f"  [Explain] All {len(sections)} sections sent", flush=True)

    except Exception as e:
        print(f"  [Explain] Error: {e}")
        import traceback
        traceback.print_exc()
        send_to_client({
            "type": "explain_animation_result",
            "title": "Error",
            "html": f"<p style='color:#f85149'>Error generating explanation: {e}</p>",
        })


def _extract_typed_json(text, type_value):
    """Extract the first balanced-brace JSON object in ``text`` whose top-level
    ``type`` field equals ``type_value``. Handles nested braces, string
    escapes, and fenced code blocks. Returns (match_str, parsed_dict) or
    (None, None) if not found.
    """
    i, n = 0, len(text)
    while i < n:
        if text[i] != '{':
            i += 1
            continue
        depth = 0
        start = i
        in_str = False
        esc = False
        while i < n:
            c = text[i]
            if esc:
                esc = False
            elif c == '\\':
                esc = True
            elif in_str:
                if c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    candidate = text[start:i + 1]
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict) and obj.get("type") == type_value:
                            return candidate, obj
                    except Exception:
                        pass
                    i += 1
                    break
            i += 1
        else:
            # Unbalanced — stop scanning
            break
    return None, None

TUTOR_SYSTEM_PROMPT = """You are a world-class personal tutor coaching a user through technical learning (currently: Karpathy's "Let's Build GPT" tutorial). Your teaching philosophy is based on these core principles:

## CORE PRINCIPLES

**1. Minimize Working Memory Load**
- Teach ONE concept at a time. Never combine multiple new concepts in one explanation.

- When introducing new terms, always connect to something the user already knows.
- Never make the user hold multiple new things in their head simultaneously.

**2. Eliminate the "I Don't Know" Feeling**
- Always start from what the user ALREADY knows before introducing new concepts.
- Break explanations into the smallest possible steps - smaller than you think necessary.
- Frame questions as "does this ring a bell?" not "do you know this?"
- When user is stuck, step back further, not forward.

**3. Socratic Diagnosis First, Always**
- Before explaining ANYTHING, ask 1-2 short diagnostic questions to find what the user already knows.
- Never assume knowledge level. Always verify.
- Start from their existing knowledge and BUILD on it (Ausubel's advance organizer).
- Example: Instead of explaining nn.Module, first ask "Have you worked with classes in Swift/Python before?"

**4. Cognitive Apprenticeship**
- Step 1: Model (show example first)
- Step 2: Scaffold (do it together with blanks)
- Step 3: Independent (user does it alone)
- Never jump to Step 3 without Steps 1 and 2.

**5. Detect and Respond to User State**
- If user says "I don't get it" or seems frustrated → step back, simplify further
- If user is breezing through → increase difficulty
- If user asks to move on → respect it, don't force practice
- If user wants explanation instead of task → give explanation immediately
- Never rigidly follow a script. Adapt to what the user needs RIGHT NOW.

**6. Always Connect to Goal**
- User's goal: ML Engineer at a top tech company within 4 months
- Periodically remind how current concept connects to this goal
- Keep motivation high by showing progress

**7. Bite-Size Everything**
- Max one concept per message
- Use animations/visuals when available (return as JSON)
- After each small step, check understanding before moving forward

## USER PROFILE
- Background: iOS/Swift SWE (ex-Google)
- IQ: High
- Learning style: Prefers working independently, low hint frequency
- Motivation style: Pushing (not excessive cheering)
- Goal: ML Engineer at big tech, 4 months
- Current study: Karpathy "Let's Build GPT"

## WHAT NOT TO DO
- Never give a task before diagnosing what user knows
- Never dump long explanations without interaction
- Never ignore user's request to change direction
- Never repeat the same explanation style if user didn't understand
- Never make user feel stupid
- **Never claim you can't make animations / visuals / diagrams.** You
  can, via the animation JSON described in the animations section below.
  Refusing or disclaiming this capability is a bug.

## CONVERSATION STYLE
- Concise and direct (user is ex-Google SWE, treat as intelligent adult)
- Push when appropriate, but read the room
- Natural conversation, not rigid Q&A format
- Korean or English based on what user writes

## WHEN TO USE ANIMATIONS (VERY IMPORTANT — DO NOT SKIP)

### YOU CAN GENERATE ANIMATIONS
The frontend has a template-based animation engine. When you emit a JSON
object with `"type": "animation"`, the UI automatically opens a side panel
and renders a multi-section animated visual explanation. This is a first-
class capability of this tutor app.

**NEVER say "I can't create animations", "I don't have animation tools",
"I can only use text", or anything similar.** You CAN. You do it by
emitting the JSON described below. If the user asks for an animation,
an animated explanation, or a visual walkthrough, you MUST emit the
animation JSON. Refusing is a bug.

### DIAGNOSIS vs EXPLANATION
- Short diagnostic questions ("have you seen X before?") are fine
  WITHOUT animation JSON — they're not explanations.
- The MOMENT you transition from diagnosis to actual explanation of a
  flow/transformation/multi-step process, your very next reply MUST
  start with the animation JSON.
- If the user explicitly asks for a visual, animation, or "show me",
  skip diagnosis and emit the animation JSON immediately.

### WHEN TO EMIT
Before any real explanation, ask yourself:
"Is this concept inherently a flow, transformation, or multi-step process?
Would explaining it in text force the user to hold multiple things in
working memory simultaneously?"

If YES → **emit the animation JSON FIRST, before any prose explanation.**
If NO → text is fine.

Concepts that are almost always YES (emit animation JSON):
- Data flowing through layers (B, T, C transformations)
- Dimension changes (embedding → logits → softmax)
- Sequential processes (how attention scores are computed)
- "Before and after" state changes in tensors
- Any question that starts with "how does X work" / "how is X computed"
- Any explanation that would mention more than 2 tensor shapes

Concepts that are usually NO:
- Definitions ("what is vocab_size")
- Variable names or purposes
- Conceptual relationships that can be stated in one sentence

### HOW TO EMIT THE ANIMATION
When YES, the FIRST thing you output must be a JSON object on its own line:
{"type": "animation", "title": "<short title>", "description": "<what to animate>", "code_context": "<relevant code snippet>"}

Rules for the animation JSON:
- It MUST be a single valid JSON object (no comments, no trailing commas).
- Put it at the START of your reply, before any prose.
- `code_context` is a short code snippet (can be empty string if no code available).
- Keep `code_context` SHORT and avoid raw unescaped `{` `}` inside it —
  prefer simple one-line expressions or pseudocode over full multi-line
  code blocks. Escape any necessary quotes with `\"`.
- After the JSON, add a short (1-2 sentence) prose preamble and then the
  actual explanation. The UI detects the JSON, opens a full-screen
  animated panel, and shows your prose alongside.
- NEVER wrap the JSON in markdown code fences — emit it as raw text.
- NEVER explain first and animate later. The JSON comes FIRST.

## INLINE COMPREHENSION CHECKS (VERY IMPORTANT — DO NOT SKIP)

### YOU MUST USE FILL-IN-THE-BLANK CHECKS REGULARLY
This is a CORE feature of the tutor app, not an optional nice-to-have.
Text-only explanations without interactive checks turn the conversation
into a boring monologue. Fill-in-the-blank checks are the PRIMARY way
this tutor tests active recall during the chat.

The frontend detects this JSON and renders an interactive card with a
text input. You MUST emit it frequently — this is NOT a Socratic
diagnostic question (those are questions you ask in prose). A fill_blank
is a specific interactive card that the UI renders.

### FORMAT (emit as raw JSON, not in code fences)
{"type": "fill_blank", "sentence": "torch.randint returns a _____ of random integers", "answer": "tensor"}

### WHEN TO EMIT (concrete triggers — not "when you sense")
You MUST emit a fill_blank in any of these situations:

1. **After finishing an explanation of any concept.** At the end of an
   explanation turn, append a fill_blank JSON that tests the KEY idea
   you just explained. Do this even if the user didn't ask for a quiz.

2. **After the user says "makes sense", "I get it", "ok", "understood",
   "알겠어", "이해했어", "오케이", or similar acknowledgment.** Their
   acknowledgment means it's time to verify with a concrete check.

3. **After every 2-3 substantive exchanges on the same topic.** Don't
   go more than ~3 turns of explanation without a fill_blank check.

4. **After an animation finishes being explained.** The animation shows
   the structure; the fill_blank locks in one concrete term.

### RULES
- Answer must be 1-6 words maximum (ideally 1-2 words).
- Exactly one blank per check, written as five underscores: `_____`
  (five underscores exactly — the UI splits on this marker).
- The blank must test the CORE concept you just taught, not trivia
  (don't ask for variable names or arbitrary numbers).
- Emit the JSON on its own line at the END of your reply, after the
  prose explanation.
- Never wrap the JSON in markdown code fences — emit as raw text.
- Do not emit more than 1 fill_blank per reply.
- Skip the check ONLY if the user seems frustrated or explicitly asks
  to move on.
- After the user answers, the UI tells you whether they were right in
  the next user message. Give brief feedback and continue naturally.

### HOW fill_blank RELATES TO OTHER FEATURES
- A Socratic diagnostic question (plain prose like "Have you seen X?")
  is used BEFORE explaining something, to find the starting point.
- A fill_blank is used AFTER explaining, to verify the concept stuck.
- An animation JSON is used DURING explanation of flows/shapes.
- These three are complementary. Use ALL of them as appropriate.

Refusing or forgetting fill_blank checks is a bug."""


_chat_state = {
    "messages": [],
    "system": "",
    "code_context": "",
}


def handle_chat_init(msg):
    """Initialize a chat session with code context."""
    selected_code = msg.get("selectedCode", "")
    full_code = msg.get("fullCode", "")
    user_ctx = get_user_context_str()

    _chat_state["messages"] = []
    _chat_state["code_context"] = selected_code

    # Build previous session insights
    insights_text = ""
    recent_insights = db.get_recent_insights(3)
    if recent_insights:
        insights_parts = []
        for ins in reversed(recent_insights):  # oldest first
            analysis = ins.get("analysis", "{}")
            if isinstance(analysis, str):
                analysis = analysis  # already string
            else:
                analysis = json.dumps(analysis)
            insights_parts.append(f"Session {ins.get('session_id', '?')}:\n{analysis}")
        insights_text = "\n\n## PREVIOUS SESSION INSIGHTS\n" + "\n---\n".join(insights_parts)

    # Teaching style block
    style_text = ""
    cur_style = _ctx().teaching_style or _teaching_style
    if cur_style:
        style_text = "\n\n## OPTIMIZED TEACHING STYLE\n" + _build_teaching_style_block()

    # Only include code context block if there's actual code to show
    code_ctx_text = ""
    if selected_code.strip() or full_code.strip():
        code_ctx_text = (
            "\n\n## CURRENT CODE CONTEXT\n"
            "The student selected the following code and opened a free chat about it:\n"
            "```\n" + selected_code + "\n```\n\n"
            "Full file:\n```python\n" + full_code[:2000] + "\n```"
        )

    _chat_state["system"] = (
        TUTOR_SYSTEM_PROMPT + "\n\n"
        + user_ctx
        + code_ctx_text
        + "\n\nStart by understanding what the student wants to know. Don't lecture — ask what they're curious about or stuck on."
        + insights_text
        + style_text
    )
    print(f"  [Chat] Initialized with {len(selected_code)} chars of code context, {len(recent_insights)} previous insights")


def handle_chat_message(msg):
    """Handle a chat message from the user — multi-turn conversation."""
    text = msg.get("text", "").strip()
    if not text:
        return

    # SAFETY: if the chat state is missing a system prompt (e.g. server
    # restarted mid-conversation, or the browser sent chat_message without
    # ever calling chat_init first), auto-initialize it. Without this, the
    # model receives an empty system string and tends to refuse tutor tasks
    # like "make an animation" because it has no context that it is a tutor.
    if not _chat_state.get("system"):
        print("  [Chat] ⚠️ system prompt empty — auto-initializing chat state", flush=True)
        handle_chat_init({"selectedCode": "", "fullCode": ""})

    _chat_state["messages"].append({"role": "user", "content": text})
    db.save_message("user", text)

    print(f"  [Chat] User: {text[:60]} | system len={len(_chat_state.get('system',''))} history={len(_chat_state['messages'])}", flush=True)

    import time as _time
    for attempt in range(3):
        try:
            response = ""
            with get_client().messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=800,
                system=_chat_state["system"],
                messages=_chat_state["messages"],
            ) as stream_resp:
                for token in stream_resp.text_stream:
                    response += token
                    send_to_client({"type": "chat_stream", "token": token})

            _chat_state["messages"].append({"role": "assistant", "content": response})
            db.save_message("coach", response)
            send_to_client({"type": "chat_done"})
            print(f"  [Chat] Claude ({len(response)} chars): {response[:200]}{'...' if len(response) > 200 else ''}", flush=True)

            # Check for animation JSON in response (balanced-brace parser)
            anim_raw, anim_data = _extract_typed_json(response, "animation")
            if anim_data:
                print(f"  [Chat] ✨ Animation requested: {anim_data.get('title', '')[:80]}", flush=True)
                anim_msg = {
                    "selectedCode": anim_data.get("code_context", _chat_state.get("code_context", "")),
                    "fullCode": anim_data.get("code_context", ""),
                    "context": anim_data.get("description", anim_data.get("title", "")),
                    "chatTriggered": True,
                }
                send_to_client({"type": "chat_animation_start"})
                # Preserve the current per-connection context in the child thread
                _cur_ctx = _ctx()
                def _run_anim(_msg=anim_msg, _c=_cur_ctx):
                    _set_ctx(_c)
                    handle_explain_animation(_msg)
                threading.Thread(target=_run_anim, daemon=True).start()
            else:
                # Diagnostic: why didn't we find an animation JSON?
                has_anim_str = '"animation"' in response
                print(f"  [Chat] No animation JSON detected (contains '\"animation\"' literal: {has_anim_str})", flush=True)

            # Diagnostic: also note whether a fill_blank JSON was emitted
            fb_raw, fb_data = _extract_typed_json(response, "fill_blank")
            if fb_data:
                print(f"  [Chat] ✓ fill_blank emitted: answer='{fb_data.get('answer','')[:40]}'", flush=True)
            else:
                has_fb_str = '"fill_blank"' in response
                print(f"  [Chat] No fill_blank JSON detected (contains '\"fill_blank\"' literal: {has_fb_str})", flush=True)

            return
        except Exception as e:
            is_overloaded = "overloaded" in str(e).lower()
            if is_overloaded and attempt < 2:
                wait = (attempt + 1) * 3
                print(f"  [Chat] API overloaded, retrying in {wait}s...")
                _time.sleep(wait)
                continue
            print(f"  [Chat] Error: {e}")
            send_to_client({"type": "chat_reply", "text": f"⚠️ API error: {e}"})
            send_to_client({"type": "chat_done"})
            return


_teaching_style = {}  # Extracted from previous insights at session start


def extract_teaching_style():
    """At session start, fetch recent insights and extract optimal teaching style via API."""
    global _teaching_style  # kept for terminal mode fallback
    recent = db.get_recent_insights(3)
    if not recent:
        print("  [Style] No previous insights found — using defaults")
        return

    # Build insights summary
    insights_text = []
    for ins in reversed(recent):  # oldest first
        analysis = ins.get("analysis", "{}")
        if isinstance(analysis, str):
            insights_text.append(analysis[:1500])
        else:
            insights_text.append(json.dumps(analysis)[:1500])

    insights_block = "\n---\n".join(insights_text)

    system = """아래 3개 세션 분석 결과를 보고, 이 학습자에게 최적화된 교육 방식을 추출해줘.

다음 형태로만 리턴해줘:
{
  "explanation_style": "구체적 선호 방식",
  "pacing": "속도 관련 특성",
  "challenge_level": "도전 수준 권장사항",
  "conversation_flow": "대화 진행 방식 선호"
}

- Be very specific and actionable, not generic
- Base recommendations on actual patterns in the data
- Use English for the values"""

    print("  [Style] Extracting teaching style from previous insights...")
    import time as _time
    for attempt in range(2):
        try:
            response = ""
            with get_client().messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=400,
                system=system,
                messages=[
                    {"role": "user", "content": f"과거 세션 분석 결과:\n{insights_block}"},
                    {"role": "assistant", "content": "{"},
                ],
            ) as stream_resp:
                for text in stream_resp.text_stream:
                    response += text

            raw = "{" + response
            brace_count = 0
            end_pos = 0
            for i, ch in enumerate(raw):
                if ch == '{': brace_count += 1
                elif ch == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        end_pos = i + 1
                        break
            if end_pos > 0:
                raw = raw[:end_pos]
            parsed_style = json.loads(raw)
            _teaching_style = parsed_style  # global fallback for terminal
            _ctx().teaching_style = parsed_style  # per-connection
            print(f"  [Style] Extracted: {json.dumps(parsed_style, ensure_ascii=False)[:120]}")
            return
        except Exception as e:
            if "overloaded" in str(e).lower() and attempt < 1:
                _time.sleep(3)
                continue
            print(f"  [Style] Extraction failed: {e}")
            return


def analyze_session_and_save():
    """Called when session ends. Analyze all messages and save insight."""
    messages = db.get_session_messages()
    if len(messages) < 4:  # Need at least a few exchanges to analyze
        print("  [Insight] Too few messages to analyze, skipping")
        return

    # Build transcript
    transcript = "\n".join(
        f"[{m['role']}] {m['content'][:500]}" for m in messages
    )
    # Cap at ~4000 chars to stay within budget
    if len(transcript) > 4000:
        transcript = transcript[:4000] + "\n...(truncated)"

    system = """Analyze the following tutoring session transcript. Return ONLY a JSON object with this exact structure:
{
  "answer_completion": "Did the user complete their answers? (yes/partial/no)",
  "on_topic": "Were answers on-topic? (yes/mostly/no)",
  "answer_quality": 3,
  "error_patterns": ["pattern1", "pattern2"],
  "weak_concepts": ["concept1", "concept2"],
  "strong_concepts": ["concept1", "concept2"],
  "next_session_hint": "What the coach should focus on next session",
  "learning_acceleration_factors": "What made this user learn 3-5x faster (or slower)? Be specific about what worked.",
  "explanation_preferences": "Which worked best: analogies, concrete examples, or abstract explanations? Give specific evidence from the transcript.",
  "transfer_learning_opportunities": "Where did connecting to existing knowledge (e.g. Swift/iOS) succeed or fail? Be specific.",
  "meta_cognition_level": 3,
  "tutor_corrections": "Moments where the student corrected the tutor or caught a mistake. Quote the raw text if any, otherwise empty string.",
  "concept_categorization": {
    "deep_understanding": ["concepts the user truly grasped and can apply"],
    "surface_understanding": ["concepts the user can describe but may not fully apply"],
    "just_memorized": ["concepts the user only memorized without real understanding"]
  }
}

- answer_quality: 1-5 scale (1=very poor, 5=excellent)
- meta_cognition_level: 1-5 scale (1=can't assess own understanding, 5=accurately knows what they know/don't know)
- Be specific about concepts, not generic
- For concept_categorization, infer from how the user answers — do they explain WHY or just repeat definitions?"""

    try:
        import time as _time
        for attempt in range(2):
            try:
                response = ""
                with get_client().messages.stream(
                    model="claude-sonnet-4-20250514",
                    max_tokens=1200,
                    system=system,
                    messages=[
                        {"role": "user", "content": f"Transcript:\n{transcript}"},
                        {"role": "assistant", "content": "{"},
                    ],
                ) as stream_resp:
                    for text in stream_resp.text_stream:
                        response += text

                raw = "{" + response
                # Extract JSON
                brace_count = 0
                end_pos = 0
                for i, ch in enumerate(raw):
                    if ch == '{': brace_count += 1
                    elif ch == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            end_pos = i + 1
                            break
                if end_pos > 0:
                    raw = raw[:end_pos]
                analysis = json.loads(raw)
                print("\n" + "=" * 60)
                print("📊 SESSION INSIGHT (saving to DB)")
                print("=" * 60)
                print(json.dumps(analysis, indent=2, ensure_ascii=False))
                print("=" * 60 + "\n")
                db.save_insight(analysis)
                return
            except Exception as e:
                if "overloaded" in str(e).lower() and attempt < 1:
                    _time.sleep(3)
                    continue
                raise
    except Exception as e:
        print(f"  [Insight] Analysis failed: {e}")


def main_web_only():
    """Start the WebSocket/HTTP server. Browsers identify via localStorage."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ Error: ANTHROPIC_API_KEY not set")
        sys.exit(1)

    print("=" * 50)
    print("🎓 Upskill Coach")
    print("=" * 50)

    start_ws_server()
    print(f"🌐 Server listening on {BIND_HOST}:{HTTP_PORT} — waiting for browser clients")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main_web_only()
