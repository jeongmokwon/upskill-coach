"""
Upskill Coach — web-only learning coach.

- Per-connection chat with Claude (Anthropic API)
- Animated explanation side panel
- Session insight extraction → personalized teaching style

Usage:
    source venv/bin/activate
    python coach.py
"""

import os
import sys
import time
import json
import asyncio
import threading
import anthropic
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
BIND_HOST = os.environ.get("BIND_HOST", "localhost")  # "0.0.0.0" on Render


def _ensure_inter_font_installed():
    """Best-effort runtime install of Inter into fontconfig's user dir.

    The render.yaml buildCommand also tries this, but Render's build $HOME
    can differ from runtime $HOME — when that happens, fc-cache populates
    the wrong user's cache and Pango at runtime never sees Inter (verified
    by `WARNING Font Inter not in [...]` log lines coming out of Manim).

    Doing it here, at runtime, uses the actual runtime $HOME so the cache
    lands where Pango will read it. Idempotent: skips quickly if Inter is
    already on the cache list.
    """
    import shutil
    import subprocess

    project_dir = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(project_dir, "fonts", "inter")
    if not os.path.isdir(src_dir):
        print("[BOOT][font] fonts/inter/ not in project dir — skipping install", flush=True)
        return

    # Quick check: if fc-list already knows about Inter, we're done.
    try:
        check = subprocess.run(
            ["fc-list", ":family"], capture_output=True, text=True, timeout=10,
        )
        if "Inter" in check.stdout:
            print("[BOOT][font] Inter already registered with fontconfig ✓", flush=True)
            return
    except FileNotFoundError:
        print("[BOOT][font] fc-list not on PATH — fontconfig may be missing; "
              "Manim font fallbacks will be used", flush=True)
        return
    except Exception as e:
        print(f"[BOOT][font] fc-list probe failed: {e} — proceeding with install", flush=True)

    target_dir = os.path.expanduser("~/.local/share/fonts")
    try:
        os.makedirs(target_dir, exist_ok=True)
        copied = 0
        for name in os.listdir(src_dir):
            if name.lower().endswith(".otf"):
                shutil.copy2(os.path.join(src_dir, name),
                             os.path.join(target_dir, name))
                copied += 1
        print(f"[BOOT][font] copied {copied} OTF files to {target_dir}", flush=True)
        # Refresh fontconfig cache so Pango finds the new fonts.
        result = subprocess.run(
            ["fc-cache", "-fv", target_dir],
            capture_output=True, text=True, timeout=30,
        )
        ok_marker = "Inter" in subprocess.run(
            ["fc-list", ":family"], capture_output=True, text=True, timeout=10,
        ).stdout
        print(f"[BOOT][font] fc-cache returncode={result.returncode}; "
              f"Inter visible to fontconfig now: {ok_marker}", flush=True)
        if not ok_marker:
            # Surface a sample of fc-cache output so we can diagnose
            print(f"[BOOT][font] fc-cache stdout (first 400): {result.stdout[:400]}", flush=True)
            print(f"[BOOT][font] fc-cache stderr (first 400): {result.stderr[:400]}", flush=True)
    except Exception as e:
        print(f"[BOOT][font] runtime install failed: {e}", flush=True)


# Run at import time so it's done before any Manim subprocess runs.
_ensure_inter_font_installed()

def get_client():
    global client
    if client is None:
        client = anthropic.Anthropic()
    return client

# ─── WebSocket + HTTP Server ─────────────────────────────────────────
ws_clients = set()
ws_loop = None


# ─── Per-connection session context (multi-user support) ─────────────

class ClientCtx:
    """Per-WebSocket-connection session state."""
    __slots__ = ("ws", "user_id", "user_profile", "study_topic",
                 "section_id", "followups_stopped", "db_session_id",
                 "teaching_style", "apprentice")

    def __init__(self, ws):
        self.ws = ws
        self.user_id = ""
        self.user_profile = {}
        self.study_topic = ""
        self.section_id = ""
        self.followups_stopped = False
        self.db_session_id = ""
        self.teaching_style = {}
        # Apprenticeship mode state — separate from legacy chat flow
        self.apprentice = {
            "topic": "",
            "diagnostic_log": [],  # [{question, answer, observation}]
            "user_state": None,    # filled after diagnostic
            "lesson_plan": None,   # fixed once created
            "messages": [],        # generator conversation history
        }


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
                    elif msg_type == "apprentice_start":
                        _spawn(handle_apprentice_start, (msg,), websocket)
                    elif msg_type == "apprentice_diagnostic":
                        _spawn(handle_apprentice_diagnostic, (msg,), websocket)
                    elif msg_type == "apprentice_chat":
                        _spawn(handle_apprentice_chat, (msg,), websocket)
                    elif msg_type == "apprentice_practice_submit":
                        _spawn(handle_apprentice_practice_submit, (msg,), websocket)
                    elif msg_type == "apprentice_continue":
                        _spawn(handle_apprentice_continue, (msg,), websocket)
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


# ─── Admin (read-only) ────────────────────────────────────────────────
#
# Three routes — all gated by HTTP Basic Auth backed by the
# ADMIN_PASSWORD env var. If the env var is unset the routes return 503
# (admin disabled). Username is ignored; password must match.
#
# WARNING: Basic Auth over plain HTTP is fine for localhost dev. Do NOT
# expose this to the public internet without putting it behind TLS or
# a stronger auth layer.

import base64 as _b64
import html as _html
from collections import Counter as _Counter
from urllib.parse import quote as _urlquote


def _admin_auth_check(request):
    """Returns None if the request is authorized, otherwise an aiohttp
    Response that the caller must return to abort the handler."""
    pw = os.environ.get("ADMIN_PASSWORD", "")
    if not pw:
        return web.Response(
            text="Admin disabled. Set ADMIN_PASSWORD env var to enable.",
            status=503,
        )
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Upskill Admin"'},
            text="Authorization required",
        )
    try:
        decoded = _b64.b64decode(auth[6:].strip()).decode("utf-8", errors="replace")
        _, _, password = decoded.partition(":")
    except Exception:
        password = ""
    if password != pw:
        return web.Response(
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Upskill Admin"'},
            text="Invalid credentials",
        )
    return None


def _admin_db_conn():
    """Open a read-only DB connection via the cross-DB helper in
    db.py. Routes through db.get_conn() so it works the same on
    SQLite (local dev) and PostgreSQL (Render via DATABASE_URL)."""
    return db.get_conn()


def _admin_html_page(title: str, body: str) -> str:
    """Wrap admin page body in a minimal dark-themed HTML shell."""
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8">
<title>{_html.escape(title)} — Admin</title>
<style>
  body {{ margin:0; padding:24px; background:#0d1117; color:#e6edf3;
         font-family:"SF Mono","Fira Code",monospace; font-size:13px;
         line-height:1.5; }}
  h1 {{ font-size:18px; color:#58a6ff; margin:0 0 8px; }}
  h2 {{ font-size:14px; color:#f0883e; margin:24px 0 8px; }}
  a {{ color:#58a6ff; text-decoration:none; }}
  a:hover {{ text-decoration:underline; }}
  .crumbs {{ font-size:12px; color:#8b949e; margin-bottom:16px; }}
  table {{ width:100%; border-collapse:collapse; margin-bottom:16px;
          font-size:12px; }}
  th, td {{ padding:8px 12px; text-align:left;
           border-bottom:1px solid #21262d; vertical-align:top; }}
  th {{ color:#8b949e; font-weight:600; background:#161b22;
        position:sticky; top:0; }}
  tr:hover td {{ background:#161b22; }}
  .meta {{ color:#8b949e; }}
  .right {{ text-align:right; }}
  .muted {{ color:#484f58; }}
  .empty {{ color:#484f58; font-style:italic; padding:8px 0; }}
  .tag {{ display:inline-block; padding:2px 8px; background:#21262d;
         border-radius:4px; font-size:11px; color:#e6edf3;
         margin:2px 4px 2px 0; }}
  .tag.weak {{ background:#f8514922; color:#f85149;
               border:1px solid #f8514944; }}
  .tag.strong {{ background:#3fb95022; color:#3fb950;
                 border:1px solid #3fb95044; }}
  .tag.deep {{ background:#58a6ff22; color:#58a6ff;
               border:1px solid #58a6ff44; }}
  .tag.surface {{ background:#bc8cff22; color:#bc8cff;
                  border:1px solid #bc8cff44; }}
  .tag.memorized {{ background:#f0883e22; color:#f0883e;
                    border:1px solid #f0883e44; }}
  .panel {{ background:#161b22; border:1px solid #21262d;
           border-radius:6px; padding:16px; margin-bottom:16px; }}
  .msg {{ padding:8px 12px; margin:6px 0; border-radius:4px;
         border-left:3px solid #30363d; background:#161b22; }}
  .msg.user {{ border-left-color:#1f6feb; background:#1f6feb15; }}
  .msg.coach {{ border-left-color:#58a6ff; }}
  .msg-role {{ font-size:11px; color:#8b949e;
              text-transform:uppercase; letter-spacing:0.5px;
              margin-bottom:4px; }}
  pre {{ white-space:pre-wrap; word-wrap:break-word; margin:0;
        font-family:inherit; font-size:12px; }}
  .warn {{ background:#f0883e22; border:1px solid #f0883e44;
          color:#f0883e; padding:10px 14px; border-radius:6px;
          margin-bottom:16px; font-size:12px; }}
  .kv {{ display:grid; grid-template-columns:140px 1fr; gap:6px 16px;
        font-size:12px; }}
  .kv .k {{ color:#8b949e; }}
</style></head>
<body>
<div class="warn">⚠️ Read-only admin. Do NOT deploy publicly without proper auth (Basic Auth + ADMIN_PASSWORD is a dev-only gate).</div>
{body}
</body></html>"""


def _admin_format_pct(num, denom):
    if not denom:
        return "—"
    return f"{int(round(100 * num / denom))}% ({num}/{denom})"


async def _admin_users_handler(request):
    """List all users with quick stats. Click → user detail."""
    blk = _admin_auth_check(request)
    if blk:
        return blk
    conn = _admin_db_conn()
    try:
        cur = db._execute(conn, """
            SELECT
                up.user_id, up.user_name, up.studying, up.hint_preference,
                up.difficulty, up.created_at,
                (SELECT COUNT(*) FROM sessions s WHERE s.user_id = up.user_id) AS n_sessions,
                (SELECT COUNT(*) FROM messages m WHERE m.user_id = up.user_id) AS n_messages,
                (SELECT MAX(m.timestamp) FROM messages m WHERE m.user_id = up.user_id) AS last_activity,
                (SELECT COUNT(*) FROM interactions i
                   WHERE i.user_id = up.user_id AND i.interaction_type = 'practice'
                ) AS n_practice,
                (SELECT COUNT(*) FROM interactions i
                   WHERE i.user_id = up.user_id AND i.interaction_type = 'practice'
                     AND i.is_correct = 1
                ) AS n_correct,
                (SELECT COUNT(*) FROM insights ins WHERE ins.user_id = up.user_id) AS n_insights
            FROM user_profiles up
            ORDER BY (last_activity IS NULL), last_activity DESC
        """)
        rows = db._fetchall(cur)
    finally:
        conn.close()

    if not rows:
        body = "<h1>Users</h1><div class='empty'>No users yet.</div>"
        return web.Response(text=_admin_html_page("Users", body), content_type="text/html")

    parts = ["<h1>Users</h1>",
             "<table><thead><tr>",
             "<th>User</th><th>Studying</th><th>Hint pref</th>",
             "<th class='right'>Sessions</th><th class='right'>Messages</th>",
             "<th class='right'>Practice ✓</th><th class='right'>Insights</th>",
             "<th>Last activity</th>",
             "</tr></thead><tbody>"]
    for r in rows:
        link = f"/admin/user/{_urlquote(r['user_id'])}"
        parts.append(
            "<tr>"
            f"<td><a href='{link}'>{_html.escape(r['user_name'] or r['user_id'])}</a>"
            f"<div class='meta'>{_html.escape(r['user_id'])}</div></td>"
            f"<td>{_html.escape(r['studying'] or '')}</td>"
            f"<td>{_html.escape(r['hint_preference'] or '')}</td>"
            f"<td class='right'>{r['n_sessions']}</td>"
            f"<td class='right'>{r['n_messages']}</td>"
            f"<td class='right'>{_admin_format_pct(r['n_correct'], r['n_practice'])}</td>"
            f"<td class='right'>{r['n_insights']}</td>"
            f"<td class='meta'>{_html.escape(r['last_activity'] or '—')}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    return web.Response(text=_admin_html_page("Users", "".join(parts)), content_type="text/html")


async def _admin_user_handler(request):
    """Per-user detail: profile, sessions, accuracy, sticking points, insights."""
    blk = _admin_auth_check(request)
    if blk:
        return blk
    user_id = request.match_info.get("user_id", "")
    if not user_id:
        return web.Response(text="missing user_id", status=400)

    P = db._P
    conn = _admin_db_conn()
    try:
        cur = db._execute(conn, f"SELECT * FROM user_profiles WHERE user_id = {P}", (user_id,))
        prof = db._fetchone(cur)
        if not prof:
            body = (
                "<div class='crumbs'><a href='/admin'>← Users</a></div>"
                f"<h1>User not found</h1><div class='empty'>{_html.escape(user_id)}</div>"
            )
            return web.Response(text=_admin_html_page("User not found", body),
                                content_type="text/html", status=404)

        cur = db._execute(conn, f"""
            SELECT s.session_id, s.start_time, s.end_time, s.study_topic,
                   (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.session_id) AS n_msgs,
                   (SELECT 1 FROM insights i WHERE i.session_id = s.session_id LIMIT 1) AS has_insight
            FROM sessions s
            WHERE s.user_id = {P}
            ORDER BY s.start_time DESC
        """, (user_id,))
        sessions = db._fetchall(cur)

        cur = db._execute(conn, f"""
            SELECT timestamp, practice_question, user_answer, is_correct,
                   time_taken_seconds, study_topic, session_id
            FROM interactions
            WHERE user_id = {P} AND interaction_type = 'practice'
            ORDER BY id DESC
        """, (user_id,))
        practice = db._fetchall(cur)

        cur = db._execute(conn, f"""
            SELECT session_id, analysis, created_at
            FROM insights
            WHERE user_id = {P}
            ORDER BY id DESC
        """, (user_id,))
        insights = db._fetchall(cur)
    finally:
        conn.close()

    # ─── Aggregate weak/strong concepts across all insights ───
    weak_counter = _Counter()
    strong_counter = _Counter()
    for ins in insights:
        try:
            data = json.loads(ins["analysis"]) if ins["analysis"] else {}
        except Exception:
            data = {}
        for w in data.get("weak_concepts", []) or []:
            weak_counter[str(w)] += 1
        for s in data.get("strong_concepts", []) or []:
            strong_counter[str(s)] += 1

    n_practice = len(practice)
    n_correct = sum(1 for p in practice if p["is_correct"])

    parts = [
        "<div class='crumbs'><a href='/admin'>← Users</a></div>",
        f"<h1>{_html.escape(prof['user_name'] or prof['user_id'])}</h1>",
        "<div class='panel'><div class='kv'>",
        f"<div class='k'>user_id</div><div>{_html.escape(prof['user_id'])}</div>",
        f"<div class='k'>studying</div><div>{_html.escape(prof['studying'] or '—')}</div>",
        f"<div class='k'>goal</div><div>{_html.escape(prof['goal'] or '—')}</div>",
        f"<div class='k'>background</div><div>{_html.escape(prof['background'] or '—')}</div>",
        f"<div class='k'>hint_preference</div><div>{_html.escape(prof['hint_preference'] or '—')}</div>",
        f"<div class='k'>difficulty</div><div>{prof['difficulty']}</div>",
        f"<div class='k'>condition</div><div>{prof['user_condition']}</div>",
        f"<div class='k'>created_at</div><div class='meta'>{_html.escape(prof['created_at'])}</div>",
        "</div></div>",
    ]

    # ─── Practice accuracy ───
    parts.append(f"<h2>Practice accuracy — {_admin_format_pct(n_correct, n_practice)}</h2>")
    if practice:
        parts.append("<table><thead><tr>"
                     "<th>When</th><th>Question</th><th>Answer</th>"
                     "<th class='right'>Result</th><th class='right'>Time</th>"
                     "</tr></thead><tbody>")
        for p in practice[:50]:
            ok = "✓" if p["is_correct"] else "✗"
            ok_color = "#3fb950" if p["is_correct"] else "#f85149"
            t = p["time_taken_seconds"]
            t_str = f"{t:.1f}s" if t else "—"
            parts.append(
                "<tr>"
                f"<td class='meta'>{_html.escape(p['timestamp'] or '')}</td>"
                f"<td>{_html.escape((p['practice_question'] or '')[:160])}</td>"
                f"<td>{_html.escape(p['user_answer'] or '')}</td>"
                f"<td class='right' style='color:{ok_color};font-weight:bold'>{ok}</td>"
                f"<td class='right meta'>{t_str}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
        if len(practice) > 50:
            parts.append(f"<div class='meta'>(showing 50 of {len(practice)})</div>")
    else:
        parts.append("<div class='empty'>No practice attempts yet.</div>")

    # ─── Sticking points ───
    parts.append("<h2>Recurring weak concepts (sticking points)</h2>")
    if weak_counter:
        parts.append("<div>")
        for concept, cnt in weak_counter.most_common(20):
            parts.append(
                f"<span class='tag weak'>{_html.escape(concept)}"
                f"{'  ×' + str(cnt) if cnt > 1 else ''}</span>"
            )
        parts.append("</div>")
    else:
        parts.append("<div class='empty'>None recorded yet.</div>")

    parts.append("<h2>Recurring strong concepts</h2>")
    if strong_counter:
        parts.append("<div>")
        for concept, cnt in strong_counter.most_common(20):
            parts.append(
                f"<span class='tag strong'>{_html.escape(concept)}"
                f"{'  ×' + str(cnt) if cnt > 1 else ''}</span>"
            )
        parts.append("</div>")
    else:
        parts.append("<div class='empty'>None recorded yet.</div>")

    # ─── Sessions list ───
    parts.append(f"<h2>Sessions ({len(sessions)})</h2>")
    if sessions:
        parts.append("<table><thead><tr>"
                     "<th>Session</th><th>Started</th><th>Ended</th>"
                     "<th>Topic</th><th class='right'>Msgs</th>"
                     "<th class='right'>Insight</th>"
                     "</tr></thead><tbody>")
        for s in sessions:
            slink = f"/admin/session/{_urlquote(s['session_id'])}"
            ended = s['end_time'] or "<span class='muted'>(active/orphan)</span>"
            insight_mark = "✓" if s['has_insight'] else "<span class='muted'>—</span>"
            parts.append(
                "<tr>"
                f"<td><a href='{slink}'>{_html.escape(s['session_id'])}</a></td>"
                f"<td class='meta'>{_html.escape(s['start_time'] or '')}</td>"
                f"<td class='meta'>{ended}</td>"
                f"<td>{_html.escape(s['study_topic'] or '')}</td>"
                f"<td class='right'>{s['n_msgs']}</td>"
                f"<td class='right'>{insight_mark}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")
    else:
        parts.append("<div class='empty'>No sessions yet.</div>")

    # ─── Recent insights (full bodies) ───
    parts.append("<h2>Recent insights (most recent 3)</h2>")
    if insights:
        for ins in insights[:3]:
            try:
                data = json.loads(ins["analysis"]) if ins["analysis"] else {}
            except Exception:
                data = {"_parse_error": "could not parse analysis JSON"}
            slink = f"/admin/session/{_urlquote(ins['session_id'])}"
            parts.append(
                "<div class='panel'>"
                f"<div class='meta'>session <a href='{slink}'>{_html.escape(ins['session_id'])}</a> · {_html.escape(ins['created_at'])}</div>"
                f"<pre style='margin-top:8px'>{_html.escape(json.dumps(data, indent=2, ensure_ascii=False))}</pre>"
                "</div>"
            )
    else:
        parts.append("<div class='empty'>No insights yet.</div>")

    title = f"User {prof['user_name'] or prof['user_id']}"
    return web.Response(text=_admin_html_page(title, "".join(parts)), content_type="text/html")


async def _admin_session_handler(request):
    """Show a session's transcript + its insight (if any)."""
    blk = _admin_auth_check(request)
    if blk:
        return blk
    sid = request.match_info.get("session_id", "")
    if not sid:
        return web.Response(text="missing session_id", status=400)

    P = db._P
    conn = _admin_db_conn()
    try:
        cur = db._execute(conn, f"SELECT * FROM sessions WHERE session_id = {P}", (sid,))
        session = db._fetchone(cur)
        if not session:
            body = (
                "<div class='crumbs'><a href='/admin'>← Users</a></div>"
                f"<h1>Session not found</h1><div class='empty'>{_html.escape(sid)}</div>"
            )
            return web.Response(text=_admin_html_page("Session not found", body),
                                content_type="text/html", status=404)
        cur = db._execute(conn,
            f"SELECT role, content, timestamp FROM messages WHERE session_id = {P} ORDER BY id",
            (sid,))
        msgs = db._fetchall(cur)
        cur = db._execute(conn,
            f"SELECT analysis, created_at FROM insights WHERE session_id = {P} ORDER BY id DESC LIMIT 1",
            (sid,))
        insight = db._fetchone(cur)
    finally:
        conn.close()

    user_link = f"/admin/user/{_urlquote(session['user_id'])}"
    parts = [
        f"<div class='crumbs'><a href='/admin'>← Users</a> · "
        f"<a href='{user_link}'>{_html.escape(session['user_id'])}</a></div>",
        f"<h1>Session {_html.escape(sid)}</h1>",
        "<div class='panel'><div class='kv'>",
        f"<div class='k'>topic</div><div>{_html.escape(session['study_topic'] or '—')}</div>",
        f"<div class='k'>start</div><div class='meta'>{_html.escape(session['start_time'] or '')}</div>",
        f"<div class='k'>end</div><div class='meta'>{_html.escape(session['end_time'] or '(active/orphan)')}</div>",
        f"<div class='k'>messages</div><div>{len(msgs)}</div>",
        "</div></div>",
    ]

    parts.append(f"<h2>Transcript ({len(msgs)} messages)</h2>")
    if msgs:
        for m in msgs:
            role = m["role"] or "?"
            cls = "user" if role == "user" else "coach"
            parts.append(
                f"<div class='msg {cls}'>"
                f"<div class='msg-role'>{_html.escape(role)} <span class='meta'>· {_html.escape(m['timestamp'] or '')}</span></div>"
                f"<pre>{_html.escape(m['content'] or '')}</pre>"
                "</div>"
            )
    else:
        parts.append("<div class='empty'>No messages.</div>")

    parts.append("<h2>Insight</h2>")
    if insight:
        try:
            data = json.loads(insight["analysis"]) if insight["analysis"] else {}
        except Exception:
            data = {"_parse_error": "could not parse analysis JSON"}
        parts.append(
            "<div class='panel'>"
            f"<div class='meta'>analyzed at {_html.escape(insight['created_at'])}</div>"
            f"<pre style='margin-top:8px'>{_html.escape(json.dumps(data, indent=2, ensure_ascii=False))}</pre>"
            "</div>"
        )
    else:
        parts.append("<div class='empty'>No insight saved for this session.</div>")

    return web.Response(text=_admin_html_page(f"Session {sid}", "".join(parts)),
                        content_type="text/html")


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
        # Admin routes — registered BEFORE the static catch-all so they
        # take precedence. Auth is enforced inside each handler via
        # ADMIN_PASSWORD env var (returns 503 if unset, 401 otherwise).
        app.router.add_get("/admin", _admin_users_handler)
        app.router.add_get("/admin/user/{user_id}", _admin_user_handler)
        app.router.add_get("/admin/session/{session_id}", _admin_session_handler)
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

        # Drain any prior unfinished sessions for this user. These are
        # sessions where the WS-disconnect handler never cleanly ran
        # (process kill, OS sleep, daemon thread killed before save).
        # Runs in background so the user gets connected immediately.
        _cleanup_orphan_sessions_async(profile["user_id"])

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

    # Drain orphan sessions for this user (in case the user was already
    # known under a different session_id and had unfinished sessions).
    _cleanup_orphan_sessions_async(uid)

    # Start DB session
    db.start_session(study_topic=studying)
    _ctx().db_session_id = db.get_session_id()
    db.touch_activity()
    # First session — no insights yet, skip API call

    print(f"  [Server] Onboarded: {uid} — studying: {studying}")

    # Send state to client
    send_to_client({"type": "connected", "study_context": studying})
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


def _default_manim_python():
    """Best-effort location of a Python interpreter with `manim` installed."""
    candidates = [
        os.environ.get("MANIM_PYTHON", ""),
        "/Users/jeongmokwon/Desktop/manim-venv/bin/python",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return sys.executable  # fallback; will fail at import if manim absent


def _extract_manim_to_json(manim_code: str, class_name: str):
    """Run animation_extractor/extract.py in a subprocess to convert a Manim
    Scene into our JSON timeline. Returns the parsed dict on success, or
    None on any failure (with diagnostics printed to the server log)."""
    import subprocess
    import tempfile

    manim_python = _default_manim_python()
    extract_script = os.path.join(PROJECT_DIR, "animation_extractor", "extract.py")
    if not os.path.exists(extract_script):
        print(f"  [Manim] ❌ extract.py not found at {extract_script}", flush=True)
        return None

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(manim_code)
        tmp_path = f.name

    keep_tmp = False
    try:
        result = subprocess.run(
            [manim_python, extract_script, tmp_path, class_name],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            keep_tmp = True
            print(
                f"  [Manim] extract failed (code {result.returncode})\n"
                f"  [Manim]   scene file kept at: {tmp_path}\n"
                f"  [Manim]   repro: {manim_python} {extract_script} {tmp_path} {class_name}\n"
                f"  [Manim] ─── stderr ───\n{result.stderr}\n"
                f"  [Manim] ─── stdout (first 800) ───\n{result.stdout[:800]}",
                flush=True,
            )
            return None
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as e:
            keep_tmp = True
            print(f"  [Manim] extract output not JSON: {e}", flush=True)
            print(f"  [Manim]   scene file kept at: {tmp_path}", flush=True)
            print(f"  [Manim]   stdout (first 800): {result.stdout[:800]}", flush=True)
            print(f"  [Manim]   stderr (first 800): {result.stderr[:800]}", flush=True)
            return None
    except subprocess.TimeoutExpired:
        keep_tmp = True
        print("  [Manim] extract timed out (60s)", flush=True)
        print(f"  [Manim]   scene file kept at: {tmp_path}", flush=True)
        return None
    except Exception as e:
        keep_tmp = True
        print(f"  [Manim] extract exception: {e}", flush=True)
        print(f"  [Manim]   scene file kept at: {tmp_path}", flush=True)
        return None
    finally:
        if not keep_tmp:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


def _try_parse_json(text: str):
    """Tolerant JSON parser — handles stray prose around a JSON object."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    import re as _re
    m = _re.search(r'\{[\s\S]*\}', text)
    if m:
        try:
            return json.loads(m.group())
        except Exception:
            pass
    return None


# ─── Typography helper prelude ─────────────────────────────────────
#
# Eight Text() factories with hardcoded font="Inter" + a fixed font_size
# per visual role. The runtime injects this prelude AHEAD of the
# LLM-generated Manim code so the Scene's construct() can call e.g.
# Title("Foo") and get back a properly sized Text("Foo", font="Inter",
# font_size=28, weight=BOLD) — without trusting the LLM to remember
# the right kwargs.
#
# Both server-side Pango (when measuring widths for .next_to() etc.)
# and the browser SVG renderer use Inter, so these sizes translate
# 1:1 across the rendering boundary. No clamp, no injector.
#
# Helpers SET font + font_size directly on kw, overriding any value
# the LLM may have passed — so the size discipline holds regardless
# of LLM compliance. weight has setdefault so callers can still
# upgrade Caption to bold etc. when they really want.
_HELPER_PRELUDE = '''\
# === Typography helpers (injected by the upskill-coach runtime). ===
# Use these factories instead of raw Text() so font sizes are
# deterministic and server/browser metrics stay matched.
def Title(s, **kw):
    """Animation title — large, bold."""
    kw['font'] = 'Inter'; kw['font_size'] = 28
    kw.setdefault('weight', BOLD)
    return Text(s, **kw)

def Subtitle(s, **kw):
    """Section heading."""
    kw['font'] = 'Inter'; kw['font_size'] = 22
    return Text(s, **kw)

def Caption(s, **kw):
    """Bottom-of-frame summary."""
    kw['font'] = 'Inter'; kw['font_size'] = 18
    return Text(s, **kw)

def AxisLabel(s, **kw):
    """Brace labels (e.g. \"T = 4 (sequence length)\")."""
    kw['font'] = 'Inter'; kw['font_size'] = 16
    return Text(s, **kw)

def CellDigit(s, **kw):
    """Numbers shown inside matrix cells."""
    kw['font'] = 'Inter'; kw['font_size'] = 18
    return Text(s, **kw)

def RowLabel(s, **kw):
    """Row identifiers (e.g. \"batch 0\")."""
    kw['font'] = 'Inter'; kw['font_size'] = 14
    return Text(s, **kw)

def ColLabel(s, **kw):
    """Column identifiers."""
    kw['font'] = 'Inter'; kw['font_size'] = 14
    return Text(s, **kw)

def CodeText(s, **kw):
    """Inline code-like text (variable names, short snippets)."""
    kw['font'] = 'Inter'; kw['font_size'] = 16
    return Text(s, **kw)
# === end helpers ===

'''


def _inject_typography_helpers(manim_code: str) -> str:
    """Prepend the helper definitions right after the LLM's manim
    `import *` line so Title/Subtitle/etc. (and BOLD) are in scope
    when the Scene's construct() runs.

    If we don't see a `from manim import *`, prepend our own at the
    very top — the LLM's later `from manim import *` (if any) is
    idempotent.
    """
    import re as _re
    m = _re.search(r'^\s*from\s+manim\s+import\s+\*\s*$', manim_code, _re.MULTILINE)
    if m:
        insert_at = m.end()
        return manim_code[:insert_at] + '\n\n' + _HELPER_PRELUDE + manim_code[insert_at:]
    return 'from manim import *\n\n' + _HELPER_PRELUDE + manim_code


def handle_explain_animation(msg):
    """Generate a Manim scene for the chat topic, extract it to a JSON
    timeline, and ship the timeline to the browser for live playback.

    Replaces the legacy 12-template orchestrator. Kept the same message
    signature so the existing `{"type": "animation", ...}` trigger from
    chat_message continues to work unchanged.
    """
    selected_code = msg.get("selectedCode", "")
    full_code = msg.get("fullCode", "")
    context = msg.get("context", "")
    title_hint = msg.get("title", "")

    print(f"  [Manim] scene request: {title_hint or context[:60]}", flush=True)

    # Import lazily so coach.py still starts when the package is absent.
    try:
        from animation_extractor.manim_prompt import build_manim_system_prompt
    except Exception as e:
        print(f"  [Manim] prompt module import failed: {e}", flush=True)
        send_to_client({
            "type": "animation_error",
            "message": f"manim_prompt import failed: {e}",
        })
        return

    user_ctx = get_user_context_str()
    system = build_manim_system_prompt(extra_context=user_ctx)

    task_parts = [
        f"Title hint: {title_hint or 'an animated explanation'}",
        f"Context: {context[:600]}",
    ]
    if selected_code.strip():
        task_parts.append(f"Selected code:\n```\n{selected_code[:1500]}\n```")
    if full_code.strip() and full_code.strip() != selected_code.strip():
        task_parts.append(f"Full file:\n```python\n{full_code[:1500]}\n```")
    task_parts.append(
        "Produce a SINGLE Manim Scene that teaches ONE key concept from the "
        "above. Return the JSON format described in OUTPUT FORMAT."
    )
    task = "\n\n".join(task_parts)

    try:
        response = get_client().messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=6000,
            system=system,
            messages=[{"role": "user", "content": task}],
        )
        raw = response.content[0].text

        parsed = _try_parse_json(raw)
        if not parsed:
            print(f"  [Manim] could not parse Claude response as JSON", flush=True)
            print(f"  [Manim] raw (first 400): {raw[:400]}", flush=True)
            send_to_client({
                "type": "animation_error",
                "message": "Failed to parse Manim response JSON",
            })
            return

        class_name = parsed.get("class_name") or ""
        manim_code = parsed.get("manim_code") or ""
        if not class_name or not manim_code:
            print(f"  [Manim] response missing class_name/manim_code: keys={list(parsed.keys())}",
                  flush=True)
            send_to_client({
                "type": "animation_error",
                "message": "Incomplete Manim response",
            })
            return

        print(f"  [Manim] generated {class_name}: {len(manim_code)} chars — extracting…",
              flush=True)

        # Diagnostic: dump the LLM's actual Manim source so we can see
        # what it emitted (helper usage vs raw Text(), absolute font_size
        # vs .scale()-based shrinks, etc.). Truncated to keep the log
        # readable. Remove this once we've validated LLM compliance.
        _preview = manim_code[:1500]
        print(f"  [Manim] code preview (first 1500 chars):\n{_preview}\n  [Manim] /preview\n",
              flush=True)

        # Prepend the typography helper prelude so Title/Subtitle/...
        # are in scope inside the Scene's construct(). Layout sizes
        # are deterministic regardless of what font_size the LLM may
        # have tried to put on a raw Text() call.
        manim_code = _inject_typography_helpers(manim_code)

        timeline = _extract_manim_to_json(manim_code, class_name)
        if timeline is None:
            send_to_client({
                "type": "animation_error",
                "message": "Failed to extract Manim scene",
            })
            return

        n_mobj = len(timeline.get("mobjects", {}))
        n_ev = len(timeline.get("timeline", []))
        dur = timeline.get("total_duration_ms", 0)
        print(
            f"  [Manim] timeline: {n_mobj} mobjects, {n_ev} events, {dur}ms",
            flush=True,
        )

        send_to_client({
            "type": "animation_timeline",
            "title": title_hint or class_name,
            "timeline": timeline,
        })
        return
    except Exception as e:
        print(f"  [Manim] error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        send_to_client({
            "type": "animation_error",
            "message": str(e),
        })
        return


# ─── Legacy 12-template explanation (deprecated — kept for rollback safety) ───
def _legacy_handle_explain_animation(msg):
    """Template-based explanation: single orchestrator call classifies + extracts data, browser renders."""
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


def _sanitize_json_candidate(s: str) -> str:
    """Walk a JSON candidate and escape raw \n/\r/\t that appear INSIDE
    string values (where strict JSON requires them escaped). No-op for
    already-valid JSON. The model occasionally emits multi-line strings
    in animation/fill_blank JSON; without this, json.loads() rejects
    them with a JSONDecodeError.
    """
    out = []
    in_str = False
    esc = False
    for c in s:
        if esc:
            out.append(c)
            esc = False
            continue
        if c == '\\':
            out.append(c)
            esc = True
            continue
        if c == '"':
            out.append(c)
            in_str = not in_str
            continue
        if in_str:
            if c == '\n':
                out.append('\\n'); continue
            if c == '\r':
                out.append('\\r'); continue
            if c == '\t':
                out.append('\\t'); continue
        out.append(c)
    return ''.join(out)


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
                    obj = None
                    try:
                        obj = json.loads(candidate)
                    except Exception:
                        # Retry after sanitizing raw control chars inside strings
                        try:
                            obj = json.loads(_sanitize_json_candidate(candidate))
                        except Exception:
                            obj = None
                    if isinstance(obj, dict) and obj.get("type") == type_value:
                        return candidate, obj
                    i += 1
                    break
            i += 1
        else:
            # Unbalanced — stop scanning
            break
    return None, None

TUTOR_SYSTEM_PROMPT = """You are a world-class personal tutor coaching a user through technical learning. The specific topic the user is studying is injected further down in the user context — do not assume any particular subject a priori. Your teaching philosophy is based on these core principles:

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
- Match the language the user writes in (the user may write in any language;
  mirror their language without commenting on the choice)

## WHEN TO USE ANIMATIONS (VERY IMPORTANT — DO NOT SKIP)

### YOU CAN GENERATE ANIMATIONS
The frontend has a Manim-backed animation engine. When you emit a JSON
object with `"type": "animation"`, the server writes a Manim scene,
extracts it to a JSON timeline, and ships it to the side panel for live
SVG playback. This is a first-class capability of this tutor app.

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

### ONE CONCEPT PER ANIMATION (VERY IMPORTANT)
Each animation teaches ONE atomic concept (~8 seconds of Manim).
Do NOT try to cram multiple ideas into one animation.

When a topic has multiple sub-concepts (e.g. "how does idx turn into
(B,T,C)?" decomposes into: (1) what (B,T) means, (2) what the embedding
table looks like, (3) the lookup op, (4) stacking into (B,T,C)):

1. Decide the sequence yourself before emitting anything.
2. In this turn, emit a `description` that targets ONLY the first
   sub-concept. Write prose that teaches that single piece alongside it.
3. End the turn with a check-in question (e.g. "Make sense so far?",
   "Any questions before we move on?", or its equivalent in the user's
   language) — a prose question (NOT a fill_blank) that invites the
   user to confirm or ask follow-ups.
4. On the user's next acknowledgment, emit ANOTHER animation JSON for
   the second sub-concept. Repeat until the decomposition is done.
5. After the final sub-concept, use a fill_blank to lock in the whole
   chain.

Do NOT emit multiple animation JSON objects in one reply. Exactly one
per turn, at the start.

### PROSE SCOPE MUST MATCH ANIMATION SCOPE (key rule)
The prose you write must talk about ONLY what the current animation
visualizes. Everything else is deferred to the next turn.

Forbidden:
- Foreshadowing concepts you'll cover in the next turn ("later we'll
  add positional embedding…" — don't; cover that when you cover it)
- Stating shape/dimension numbers in prose that the animation does
  not show
- Jumping from concrete to abstract ("so generalized this is (B,T,C)")
  — abstraction belongs in its own turn
- Introducing more than one new term per turn (e.g. "embedding table"
  and "lookup" are two distinct terms — pick the one the animation
  is highlighting)

Rule of thumb: if the animation shows only "X → Y", the prose says
only that X → Y. Never describe in words what the learner cannot see
on the screen.

### CHECK-IN QUESTION IS MANDATORY
Every coach turn (including ones with an animation) must end with a
check-in question. Use a phrasing natural in the user's language —
e.g. in English:
- "Make sense so far?"
- "Any questions before we move on?"
- "Want me to dig deeper anywhere here?"

(Use the equivalent in whatever language the user is writing in.)

A turn without a check-in question is incomplete. Letting the user
push forward with a single "ok" / acknowledgment moves at the
coach's pace, not the learner's.

The check-in question does NOT replace fill_blank — the check-in
goes at the end of every turn; fill_blank goes at the end of a
concept block.

### NO STAGE SKIPPING
Do not omit intermediate operations that actually exist in the code
or algorithm. Example: in BigramLanguageModel, after tok_emb the
next step is (pos_emb addition →) lm_head, not lm_head directly.
Do not collapse the shape chain into something simpler "for clarity"
— follow the real forward order. Skip a stage only if the user
explicitly asks you to.

### HANDLING MULTI-STAGE PROCESS REQUESTS
When the user signals they want to understand a multi-step process
end-to-end (signals: "the whole thing", "end-to-end", "flow",
"overview", "from start to finish", or any question about a named
function/pipeline/algorithm's overall behavior — and the same in
other languages):

1. Do NOT emit animation JSON in this turn.
2. Instead, present a decomposition plan in prose:
   - Numbered list of N stages
   - Each stage must be small enough to be ONE animation
   - Use the actual code/concept terms for stage names (not generic
     "step 1, 2, 3")
3. Ask which stage to start from ("Start with #1? Or jump to a
   specific stage?").
4. Only after the user confirms, emit the animation for the chosen
   stage.

Example (when the user asks about BigramLanguageModel forward pass):
> "Here's how I'll break this down:
>  1. idx (B,T) — the input shape
>  2. tok_emb lookup → (B,T,C)
>  3. add pos_emb → (B,T,C)
>  4. lm_head → (B,T,vocab_size)
>  5. loss computation
>  Start with #1?"

Never compress a multi-stage process into a single animation. When
the user asks about "the whole thing", they want a map, not a
3-second compressed video.

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

2. **After any user acknowledgment** — e.g. "makes sense", "I get
   it", "ok", "understood", "got it", or the equivalent in whatever
   language the user is writing in. Their acknowledgment means it's
   time to verify with a concrete check.

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

    # If the user has been idle longer than IDLE_THRESHOLD_MINUTES,
    # treat that as the natural end of the prior learning session:
    # close + analyze it (in background) and start a new DB session
    # for this incoming message. Done before we save the message so
    # save_message lands in the new session's row.
    try:
        _rotate_session_if_idle()
    except Exception as _re:
        print(f"  [Session] _rotate_session_if_idle raised: {_re}", flush=True)

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


# ═══════════════════════════════════════════════════════════════════
# APPRENTICESHIP MODE — new architecture (eval → generator → panels)
# ═══════════════════════════════════════════════════════════════════

APPRENTICE_MODEL = "claude-sonnet-4-20250514"
NUM_DIAGNOSTIC_QUESTIONS = 3

# MVP: skip diagnostic, use a fixed beginner profile so we can focus on the teaching flow.
# Revive the diagnostic phase later once the teaching UX is stable.
APPRENTICE_SKIP_DIAGNOSTIC = True
HARDCODED_USER_STATE = {
    "tier": "lower",
    "tier_reasoning": "Hardcoded for MVP testing — treat as absolute beginner with strong motivation.",
    "dominant_error_patterns": [],
    "current_emotional_states": [
        {"state": "B007", "intensity": "mid"}
    ],
    "summary_for_generator": (
        "This learner is a complete beginner — knows essentially nothing about ML yet. "
        "However they are motivated and willing to put in serious effort. "
        "Use maximum scaffolding, strict one-concept-per-turn (P018), errorless learning (P007), "
        "heavy inline completion prompts (P019), and P022 barrier reduction. "
        "For T002 practice, give only the single most important substep at a time."
    ),
}

_ontology_cache = None


def _load_ontology():
    """Load and cache ontology.json from project dir."""
    global _ontology_cache
    if _ontology_cache is None:
        path = os.path.join(PROJECT_DIR, "ontology.json")
        with open(path) as f:
            _ontology_cache = json.load(f)
        print(f"  [Ontology] loaded: {len(_ontology_cache.get('user_states', []))} states, "
              f"{len(_ontology_cache.get('pedagogical_principles', []))} principles, "
              f"{len(_ontology_cache.get('panels', []))} panels", flush=True)
    return _ontology_cache


def _parse_json_response(text):
    """Parse JSON from LLM response, tolerant to surrounding text."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        import re as _re
        m = _re.search(r'\{[\s\S]*\}', text)
        if m:
            return json.loads(m.group())
        raise


def _call_apprentice_llm(system_prompt, user_message="proceed", max_tokens=2048):
    """Single-shot LLM call for eval/generator, returns parsed JSON."""
    response = get_client().messages.create(
        model=APPRENTICE_MODEL,
        max_tokens=max_tokens,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
    )
    return _parse_json_response(response.content[0].text)


# ─── Eval agent prompts ────────────────────────────────────────────

def _eval_question_prompt(ontology, topic, history):
    return f"""You are a diagnostic evaluator for a learning coach. The user wants to learn: {topic}

Your job: generate ONE short-answer diagnostic question that assesses the learner's current level.

These questions follow the "short-answer diagnostic" principle:
- Answerable in 1-20 words
- Wrong answers are informative (see error_taxonomy)
- Probes: pattern recognition, working memory, transfer ability, attention to detail
- Start foundational; escalate only if earlier answers show strong signal

You have asked {len(history)} diagnostic question(s) so far of {NUM_DIAGNOSTIC_QUESTIONS} total.

Previous Q&A:
{json.dumps(history, indent=2, ensure_ascii=False) if history else "(none yet)"}

Reference — error_taxonomy (what wrong answers reveal):
{json.dumps(ontology["error_taxonomy"], indent=2, ensure_ascii=False)}

Reference — diagnostic_cues:
{json.dumps(ontology["diagnostic_cues"], indent=2, ensure_ascii=False)}

Output ONLY a JSON object:
{{
  "question": "the diagnostic question in the user's language",
  "example_shown": "optional example shown before the question, or null",
  "ideal_answer": "what a correct answer would look like",
  "tests_for": "what this question is probing"
}}"""


def _eval_observe_prompt(ontology, topic, question_obj, user_answer):
    return f"""You are a diagnostic evaluator analyzing a single user answer.

Topic: {topic}

Question asked:
{json.dumps(question_obj, indent=2, ensure_ascii=False)}

User's answer: "{user_answer}"

Reference — error_taxonomy:
{json.dumps(ontology["error_taxonomy"], indent=2, ensure_ascii=False)}

Reference — diagnostic_cues:
{json.dumps(ontology["diagnostic_cues"], indent=2, ensure_ascii=False)}

Reference — user_states:
{json.dumps(ontology["user_states"], indent=2, ensure_ascii=False)}

Output ONLY a JSON object:
{{
  "error_type": "E001-E007 or null if correct",
  "error_reasoning": "why you chose this error_type",
  "cue_ratings": {{
    "D001_relevance": "high | mid | low",
    "D002_orthographic": "high | mid | low",
    "D003_completeness": "high | mid | low"
  }},
  "observed_states": [
    {{ "state": "B0XX", "intensity": "low | mid | high" }}
  ],
  "notes": "brief observation"
}}"""


def _eval_conclude_prompt(ontology, topic, diagnostic_log):
    return f"""You are a diagnostic evaluator. The diagnostic phase is complete.

Topic: {topic}

Diagnostic log:
{json.dumps(diagnostic_log, indent=2, ensure_ascii=False)}

Synthesize the learner's profile.

Tier definitions:
- upper: pattern recognition + transfer + attention to detail all strong
- upper-mid: mostly correct with minor syntactic or completeness issues
- mid: can reproduce but struggles to modify; some orthographic slips
- lower-mid: incomplete answers, fragments, low attention to detail
- lower: single elements, no structure, minimal engagement
- lowest: avoidance, irrelevant, or complete disconnect

Output ONLY a JSON object:
{{
  "tier": "upper | upper-mid | mid | lower-mid | lower | lowest",
  "tier_reasoning": "1-2 sentences",
  "dominant_error_patterns": ["E0XX", ...],
  "current_emotional_states": [
    {{ "state": "B0XX", "intensity": "low | mid | high" }}
  ],
  "summary_for_generator": "3-5 sentence narrative in the voice of a tutor briefing another tutor"
}}"""


# ─── Generator agent prompt ────────────────────────────────────────

def _generator_system_prompt(ontology, topic, user_state, lesson_plan):
    plan_section = (
        json.dumps(lesson_plan, indent=2, ensure_ascii=False)
        if lesson_plan
        else "(no lesson plan yet — create one in your next turn if T002 applies)"
    )

    return f"""You are an expert learning coach. Your job is to teach the user about: {topic}

CRITICAL — TOPIC vs DIAGNOSTIC CONTEXT:
- The user's ACTUAL learning goal is: "{topic}". This is what you teach.
- The diagnostic phase may have touched on DIFFERENT sub-topics (e.g., specific prerequisites) only to assess the learner's level.
- DO NOT teach the diagnostic sub-topics as the main lesson. DO NOT continue asking diagnostic-style questions.
- DO NOT ask more short-answer assessment questions. The diagnostic phase is COMPLETE.
- Your entire teaching arc must be about "{topic}".

═══════════════════════════════════════════════════════════
LEARNER PROFILE (from diagnostic phase — used to adapt your teaching style, NOT to choose what to teach)
═══════════════════════════════════════════════════════════

{json.dumps(user_state, indent=2, ensure_ascii=False)}

Adapt your teaching STYLE (not your TOPIC) based on this profile:
- If tier is lower/lower-mid: maximize scaffolding, strict P018, errorless learning (P007), heavy P019 inline prompts, P022 barrier reduction. For T002 practice: only the single most important substep.
- If tier is mid: standard scaffolding, still err on errorless, frequent check-ins. For T002 practice: 1-2 key substeps.
- If tier is upper-mid: standard approach, can use socratic questioning. For T002 practice: most substeps.
- If tier is upper: socratic questioning (P020), desirable difficulty (P016), less scaffolding. For T002 practice: all substeps.

═══════════════════════════════════════════════════════════
CURRENT LESSON PLAN (fixed once created this session)
═══════════════════════════════════════════════════════════

{plan_section}

═══════════════════════════════════════════════════════════
OUTPUT FORMAT (always this exact JSON)
═══════════════════════════════════════════════════════════

{{
  "message": "short chat message — keep it EMPTY or brief; the cell carries the lesson.",
  "chat_mode": "minimized | expanded",
  "await_user": false,
  "panels": [
    {{
      "type": "panel_apprentice_demo",
      "action": "open | update",
      "content": {{
        "title": "string",
        "language": "string",
        "substeps": [
          {{
            "substep_id": "s1",
            "label": "short label",
            "pass_1": {{ "big_display": "string", "caption": "string" }},
            "pass_2": {{
              "blocks": [
                {{ "type": "comment", "text": "# planning comment" }},
                {{ "type": "code", "text": "actual_code()" }},
                {{ "type": "narrative", "text": "why this matters, context, gotchas" }}
              ]
            }}
          }}
        ],
        "focused_substep_id": "s1"
      }}
    }}
  ],
  "lesson_plan": {{
    "topic": "string",
    "substeps": [
      {{ "substep_id": "s1", "label": "short label", "key_idea": "what this substep accomplishes" }}
    ],
    "practice_substep_ids": ["s1"]
  }},
  "meta": {{
    "principle_used": "P0XX",
    "pattern": "T0XX or null",
    "pattern_step": "step name or null"
  }}
}}

Include "lesson_plan" ONLY on the plan turn (T002 step="plan"). Otherwise omit.
Once a lesson_plan exists above, DO NOT modify it — it is fixed for the session.

chat_mode guidance:
- "minimized" (default): short message in collapsed chat handle
- "expanded": use only when the user asks a question or needs a longer explanation that cannot fit next to the panels

═══════════════════════════════════════════════════════════
TEACHING PRINCIPLES
═══════════════════════════════════════════════════════════

{json.dumps(ontology["pedagogical_principles"], indent=2, ensure_ascii=False)}

═══════════════════════════════════════════════════════════
AVAILABLE PANELS
═══════════════════════════════════════════════════════════

{json.dumps(ontology["panels"], indent=2, ensure_ascii=False)}

═══════════════════════════════════════════════════════════
TEACHING PATTERNS
═══════════════════════════════════════════════════════════

{json.dumps(ontology["teaching_patterns"], indent=2, ensure_ascii=False)}

═══════════════════════════════════════════════════════════
RULES
═══════════════════════════════════════════════════════════

- Respond in the same language the user writes in
- Apply ONE concept per turn/cell (P018)
- Start concrete, move to abstract (P011)
- panel_animation is NOT AVAILABLE in this build. Do not emit panel_animation.
- panel_apprentice_practice is NOT AVAILABLE in this build (deferred).

═══════════════════════════════════════════════════════════
T002 FLOW — plan, then one-cell-per-turn
═══════════════════════════════════════════════════════════

TURN 1 (plan):
  - Execute T002 step="plan". Emit the "lesson_plan" JSON field with substeps decomposed for the topic.
  - Chat message: brief 1-liner like "Here's our plan — starting with the first step."
  - NO panel updates in this turn. focused_substep_id is not yet set.

TURN 2 (first cell) and every subsequent TURN (next cell):
  - Emit ONE panel_apprentice_demo update.
  - action: "open" on TURN 2, "update" thereafter.
  - substeps: the full list of cells emitted so far, PLUS the new one. Always include prior cells so the
    frontend can reconcile and keep them rendered with their pass_1 + pass_2 content intact.
  - focused_substep_id: the NEW (most recently added) substep_id.
  - Chat message empty or a single short sentence.
  - await_user: false while cells remain in the lesson_plan; true only when the walkthrough is complete.

═══════════════════════════════════════════════════════════
CELL STRUCTURE (each substep cell deepens INSIDE itself)
═══════════════════════════════════════════════════════════

Each cell carries BOTH layers in the same update:

  pass_1 — BIG PICTURE (rendered first)
    - big_display: a concrete artifact shown at large font. The emotional hook — a real string, a short
      list, a formula, a numeric example. Keep it simple. Empty string is allowed if label + caption
      alone make the point.
    - caption: one plain-language sentence saying what this step is about.

  pass_2 — INTERLEAVED COMMENTS + CODE + NARRATIVE (types in under a divider)
    - blocks: an ORDERED list of {{ type, text }} items. You (the coach) choose the order and count.
    - Three block types, each with a distinct role:
        * type: "comment"   — # planning comments an engineer would write BEFORE the next code block.
                              Example: "# read the entire text into one long string\\n# this is our dataset"
                              Each line must start with "#". Multiple lines allowed.
        * type: "code"      — real Python (or target language) that IMPLEMENTS the preceding comment block.
                              No "#" prefix. Actual, runnable code.
                              Example: "with open('input.txt', 'r') as f:\\n    text = f.read()"
        * type: "narrative" — the coach speaking TO the learner — context, reasoning, why this matters,
                              gotchas, analogies. Prose style. NOT code, NOT # comments.
                              Example: "This becomes the dataset the model learns from. The patterns in
                              these characters are what it will try to mimic."

    - Typical patterns (but NOT prescribed — coach decides):
        comment → code → narrative
        narrative → comment → code → comment → code
        comment → code → narrative → code → comment → code
      Pick the flow that best teaches THIS substep. Let the pedagogy lead the structure.

    - Total blocks per cell: 3-7 is typical. More if the substep genuinely needs it.

    - Block guidance:
        * Comments state INTENT ("# ..."); code DELIVERS it. Keep them close — a comment block should
          usually be followed by the code that implements it.
        * Narrative explains WHY or REFRAMES — use it when the code alone won't land the concept,
          or when the learner needs context before/after seeing the code.
        * Never write comments that just re-describe the next line. Comments should teach the plan;
          code should execute the plan.

  NO blanks, NO questions in this build. Those are deferred to a future pass.

═══════════════════════════════════════════════════════════
CELL DESIGN GUIDANCE
═══════════════════════════════════════════════════════════

  - Each cell's big_display should be as CONCRETE as possible. Prefer an actual example the learner can
    read over an abstract description.
      GOOD: big_display = 'text = "First Citizen: Before we proceed any further, hear me speak."'
      BAD : big_display = 'The raw text data'
  - Captions: plain language, under ~15 words ideal.
  - CONSISTENCY: caption and big_display must match. If the caption says "the complete works of
    Shakespeare" the big_display MUST end with "..." to signal truncation. When big_display is a
    snippet of something larger, end with "..." and phrase the caption accordingly ("Here's a snippet
    of the training text").
  - RESPECT THE SOURCE. If the topic references a specific source (tutorial, textbook, paper, lecture),
    your artifacts and pseudo-code MUST match what that source actually uses — not a generic
    reinvention. Example: "Karpathy's Let's Build GPT" uses slicing like
    `x = data[i:i+block_size]; y = data[i+1:i+block_size+1]`, NOT `for i in range: ...` Python loops.
    If the exact source conventions are uncertain, pick idiomatic library code over naive loops.
  - Pseudo-code lines should carry WHY, not just WHAT. "# for each training pair" is weak.
    "# for each training pair (x[i], y[i]), increment N[x[i], y[i]] — this accumulates the bigram
    counts" is strong.

EXAMPLE CELL (topic: Karpathy's Bigram Language Model, beginner learner):
{{
  "substep_id": "s2",
  "label": "Build character vocabulary",
  "pass_1": {{
    "big_display": "[' ', '!', '$', '&', ',', '-', '.', ':', ';', '?', 'A', 'B', 'C', ..., 'z']",
    "caption": "We collect every unique character in the text — this is our vocabulary."
  }},
  "pass_2": {{
    "blocks": [
      {{ "type": "comment", "text": "# take the set of characters that appear in text\\n# sort them so indexing is stable across runs" }},
      {{ "type": "code", "text": "chars = sorted(list(set(text)))\\nvocab_size = len(chars)" }},
      {{ "type": "narrative", "text": "The position of each character in this sorted list becomes its integer token id — the model will always see 'a' as the same number." }},
      {{ "type": "comment", "text": "# build two lookup tables: char→int for encoding, int→char for decoding" }},
      {{ "type": "code", "text": "stoi = {{ch: i for i, ch in enumerate(chars)}}\\nitos = {{i: ch for i, ch in enumerate(chars)}}" }}
    ]
  }}
}}

═══════════════════════════════════════════════════════════
OUTPUT RULES
═══════════════════════════════════════════════════════════

- DO NOT narrate cells in chat. Caption + pseudo_code carry the lesson. Chat stays empty or one short
  acknowledgment.
- DO NOT emit panel_animation or panel_apprentice_practice updates.
- EACH turn after plan delivers exactly ONE new cell, with ALL prior cells still present in the
  substeps array (so the frontend can render stable state).
- ALWAYS output valid JSON — no plain text.
"""


# ─── WS handlers ───────────────────────────────────────────────────

def handle_apprentice_start(msg):
    """Kick off a new apprenticeship session.

    If APPRENTICE_SKIP_DIAGNOSTIC is True, uses a hardcoded beginner user_state
    and jumps straight to the teaching phase. Otherwise runs the diagnostic flow.
    """
    topic = (msg.get("topic") or "").strip()
    if not topic:
        send_to_client({"type": "apprentice_error", "message": "topic is required"})
        return

    ctx = _ctx()
    if not ctx:
        return

    ctx.apprentice["topic"] = topic
    ctx.apprentice["diagnostic_log"] = []
    ctx.apprentice["user_state"] = None
    ctx.apprentice["lesson_plan"] = None
    ctx.apprentice["messages"] = []

    if APPRENTICE_SKIP_DIAGNOSTIC:
        # Bypass diagnostic — hardcoded beginner profile
        ctx.apprentice["user_state"] = dict(HARDCODED_USER_STATE)
        print(f"  [Apprentice] start: topic='{topic}' (diagnostic SKIPPED, tier=lower hardcoded)",
              flush=True)

        send_to_client({
            "type": "apprentice_greeting",
            "message": f"Great — let's start on {topic}. I'll sketch a short plan tailored to your level first.",
        })

        # Also notify client that diagnostic is "done" so chat auto-expands
        send_to_client({
            "type": "apprentice_diagnostic_done",
            "user_state": ctx.apprentice["user_state"],
        })

        # Prime generator with T002 plan directive
        priming = (
            f"[system] My learning goal is: {topic}. "
            f"No diagnostic was run — assume I am a complete beginner with strong motivation. "
            f"Please execute T002 step=\"plan\" NOW. "
            f"Decompose {topic} into 4-8 substeps appropriate for tier=lower (small, gentle steps). "
            f"Output the lesson_plan field in your JSON. "
            f"In the chat message say one short line like 'Here's our plan — starting with the first step.'. "
            f"Do NOT emit panel updates in this turn. The first substep cell will be delivered in the next turn."
        )
        # Plan turn: explicitly strip panels in case the generator ignores the instruction
        _run_generator_turn(ctx, priming, strip_panels=True)

        # After plan is created, auto-trigger the first cell (model step)
        _request_first_cell(ctx)
        return

    # Full flow: run diagnostic
    print(f"  [Apprentice] start: topic='{topic}'", flush=True)
    send_to_client({
        "type": "apprentice_greeting",
        "message": f"Great — let's get started on {topic}. First, a few short questions to figure out your current level.",
    })
    _ask_next_diagnostic(ctx)


def _ask_next_diagnostic(ctx):
    """Generate and send the next diagnostic question (or conclude)."""
    ontology = _load_ontology()
    topic = ctx.apprentice["topic"]
    log = ctx.apprentice["diagnostic_log"]

    if len(log) >= NUM_DIAGNOSTIC_QUESTIONS:
        _conclude_diagnostic(ctx)
        return

    try:
        q = _call_apprentice_llm(_eval_question_prompt(ontology, topic, log))
    except Exception as e:
        print(f"  [Apprentice] eval question gen failed: {e}", flush=True)
        send_to_client({"type": "apprentice_error", "message": "Failed to generate a diagnostic question. Please try again."})
        return

    # Store the current question on ctx so we know what the next answer is for
    ctx.apprentice["_current_question"] = q

    send_to_client({
        "type": "apprentice_diagnostic_question",
        "q_index": len(log),
        "total": NUM_DIAGNOSTIC_QUESTIONS,
        "question": q.get("question", ""),
        "example_shown": q.get("example_shown"),
        "tests_for": q.get("tests_for", ""),
    })


def handle_apprentice_diagnostic(msg):
    """Receive an answer to a diagnostic question, observe, then ask next or conclude."""
    answer = (msg.get("answer") or "").strip()
    ctx = _ctx()
    if not ctx:
        return

    q = ctx.apprentice.get("_current_question")
    if not q:
        send_to_client({"type": "apprentice_error", "message": "no active diagnostic question"})
        return

    ontology = _load_ontology()
    topic = ctx.apprentice["topic"]

    try:
        observation = _call_apprentice_llm(_eval_observe_prompt(ontology, topic, q, answer))
    except Exception as e:
        print(f"  [Apprentice] eval observe failed: {e}", flush=True)
        observation = {"error_type": None, "notes": f"(observation failed: {e})"}

    ctx.apprentice["diagnostic_log"].append({
        "question": q,
        "answer": answer,
        "observation": observation,
    })

    print(f"  [Apprentice] diag Q{len(ctx.apprentice['diagnostic_log'])}: "
          f"error_type={observation.get('error_type')} notes={observation.get('notes', '')[:80]}",
          flush=True)

    _ask_next_diagnostic(ctx)


def _conclude_diagnostic(ctx):
    """Eval synthesizes user_state, send to client, ready for teaching."""
    ontology = _load_ontology()
    topic = ctx.apprentice["topic"]

    try:
        user_state = _call_apprentice_llm(
            _eval_conclude_prompt(ontology, topic, ctx.apprentice["diagnostic_log"])
        )
    except Exception as e:
        print(f"  [Apprentice] eval conclude failed: {e}", flush=True)
        user_state = {
            "tier": "mid",
            "tier_reasoning": "diagnostic conclusion failed; defaulting to mid",
            "dominant_error_patterns": [],
            "current_emotional_states": [],
            "summary_for_generator": "Diagnostic failed. Treat as mid-tier learner with unknown specifics.",
        }

    ctx.apprentice["user_state"] = user_state
    print(f"  [Apprentice] diagnostic done: tier={user_state.get('tier')}", flush=True)

    send_to_client({
        "type": "apprentice_diagnostic_done",
        "user_state": user_state,
    })

    # Auto-start teaching with a strong priming message that forces T002 plan on turn 1
    priming = (
        f"[system] Diagnostic is complete. My learning goal is: {topic}. "
        f"Please execute T002 step=\"plan\" NOW. "
        f"Decompose {topic} into substeps appropriate for my tier. "
        f"Output the lesson_plan field in your JSON response. "
        f"Do not ask any more diagnostic questions. Do not start teaching content in this turn — "
        f"only produce the lesson plan and a brief acknowledgment message."
    )
    _run_generator_turn(ctx, priming)


def handle_apprentice_chat(msg):
    """User sends a chat message during the teaching phase."""
    text = (msg.get("message") or "").strip()
    if not text:
        return
    ctx = _ctx()
    if not ctx:
        return
    if not ctx.apprentice.get("user_state"):
        send_to_client({"type": "apprentice_error", "message": "diagnostic not complete"})
        return
    _run_generator_turn(ctx, text)


def handle_apprentice_practice_submit(msg):
    """User submits a practice attempt for a substep."""
    substep_id = msg.get("substep_id", "")
    code = msg.get("code", "")
    ctx = _ctx()
    if not ctx:
        return

    # Pass the submission to generator as a structured user turn
    submission_msg = (
        f"[practice_submit] substep_id={substep_id}\n"
        f"```\n{code}\n```\n"
        f"Please review and give red-pen feedback. Then decide if the user is ready for the next step."
    )
    _run_generator_turn(ctx, submission_msg)


def _request_first_cell(ctx):
    """After the plan turn, ask the generator to deliver the FIRST substep cell."""
    priming = (
        "[system] The lesson plan is set. Now deliver the FIRST substep cell.\n"
        "- Emit a SINGLE panel_apprentice_demo update with action:\"open\", containing ONE substep "
        "in the substeps array — the first substep from the lesson_plan.\n"
        "- The substep must carry BOTH pass_1 AND pass_2 content:\n"
        "    pass_1: { big_display, caption }\n"
        "    pass_2: { pseudo_code }   # 3-6 lines of # comments\n"
        "- focused_substep_id must equal the substep_id of the cell you just emitted.\n"
        "- Keep the chat message empty or a single short sentence.\n"
        "- Set await_user: false. The frontend will automatically ask for the next cell when this one "
        "finishes rendering."
    )
    _run_generator_turn(ctx, priming)


def handle_apprentice_continue(msg):
    """Frontend finished rendering the current cell and wants the next one.

    The current substep's blank answer (if any) is included so the generator knows
    how the learner did.
    """
    ctx = _ctx()
    if not ctx:
        return

    substep_id = msg.get("substep_id", "")
    user_answer = msg.get("user_answer", "")
    answer_correct = msg.get("answer_correct", None)

    priming_parts = [
        "[system] The learner finished rendering the current cell. Deliver the NEXT substep cell.",
    ]
    if substep_id:
        priming_parts.append(f"Cell just completed: {substep_id}.")
    if user_answer:
        status = "correct" if answer_correct else "incorrect" if answer_correct is False else "(no check)"
        priming_parts.append(f"Their blank answer: '{user_answer}' ({status}).")
    priming_parts.append(
        "Emit a SINGLE panel_apprentice_demo update with action:\"update\". "
        "The substeps array must contain ALL substeps emitted so far (including prior cells) PLUS the "
        "next one from the lesson_plan. Each substep MUST carry both pass_1 and pass_2 content. "
        "Set focused_substep_id to the NEW substep_id. Chat message empty or one short sentence. "
        "Set await_user: false. If the lesson_plan is exhausted, set await_user: true and message "
        "the learner that the walkthrough is complete."
    )
    priming = " ".join(priming_parts)
    _run_generator_turn(ctx, priming)


def _run_generator_turn(ctx, user_message, strip_panels=False):
    """One generator turn: build prompt, call, parse, dispatch panel updates.

    strip_panels=True clears the `panels` field before dispatch — useful for
    the plan turn, where the model is told not to emit panels but sometimes does.
    """
    ontology = _load_ontology()
    topic = ctx.apprentice["topic"]
    user_state = ctx.apprentice.get("user_state")
    lesson_plan = ctx.apprentice.get("lesson_plan")
    history = ctx.apprentice["messages"]

    history.append({"role": "user", "content": user_message})

    system = _generator_system_prompt(ontology, topic, user_state, lesson_plan)

    try:
        response = get_client().messages.create(
            model=APPRENTICE_MODEL,
            max_tokens=20000,  # large enough for detailed animation scenes (~40-80 timeline steps)
            system=system,
            messages=history,
        )
        raw = response.content[0].text
    except Exception as e:
        print(f"  [Apprentice] generator call failed: {e}", flush=True)
        send_to_client({"type": "apprentice_error", "message": f"generator error: {e}"})
        history.pop()  # drop the user message we just added so retry works
        return

    history.append({"role": "assistant", "content": raw})

    try:
        parsed = _parse_json_response(raw)
    except Exception as e:
        print(f"  [Apprentice] generator response not JSON: {e}; raw head: {raw[:200]}", flush=True)
        send_to_client({
            "type": "apprentice_teach",
            "message": raw,  # fall back to raw text
            "chat_mode": "expanded",
            "panels": [],
            "meta": {},
        })
        return

    # Capture lesson_plan on first creation only (fixed after)
    if parsed.get("lesson_plan") and ctx.apprentice.get("lesson_plan") is None:
        ctx.apprentice["lesson_plan"] = parsed["lesson_plan"]
        print(f"  [Apprentice] lesson_plan created: "
              f"{len(parsed['lesson_plan'].get('substeps', []))} substeps, "
              f"practice on {parsed['lesson_plan'].get('practice_substep_ids', [])}",
              flush=True)

    meta = parsed.get("meta", {}) or {}
    panels_out = parsed.get("panels", []) or []
    if strip_panels and panels_out:
        print(f"  [Apprentice] stripping {len(panels_out)} unsolicited panel(s) from plan turn: "
              f"{[p.get('type') for p in panels_out]}", flush=True)
        panels_out = []

    print(f"  [Apprentice] turn: principle={meta.get('principle_used')} "
          f"pattern={meta.get('pattern')} step={meta.get('pattern_step')} "
          f"panels={[p.get('type') for p in panels_out]}",
          flush=True)

    send_to_client({
        "type": "apprentice_teach",
        "message": parsed.get("message", ""),
        "chat_mode": parsed.get("chat_mode", "minimized"),
        "await_user": parsed.get("await_user", False),
        "panels": panels_out,
        "meta": meta,
        "lesson_plan": ctx.apprentice.get("lesson_plan"),
    })


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

    system = """Given the analysis of this learner's 3 most recent sessions, extract the teaching style that works best for them.

Return ONLY this JSON shape:
{
  "explanation_style": "<specific preferred mode>",
  "pacing": "<speed-related trait>",
  "challenge_level": "<recommended challenge level>",
  "conversation_flow": "<preferred conversational flow>"
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
                    {"role": "user", "content": f"Past session analysis results:\n{insights_block}"},
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


def analyze_session_and_save(session_id: str | None = None):
    """Analyze a session's transcript and persist an insight row.

    `session_id` lets the caller analyze a specific session (e.g. an
    orphaned prior session, or the session being rotated out by the
    idle-timeout helper). If omitted, falls back to the current
    thread-local session via db._sid().
    """
    messages = db.get_session_messages(session_id)
    if len(messages) < 4:  # Need at least a few exchanges to analyze
        sid_label = session_id or 'current'
        print(f"  [Insight] Too few messages to analyze ({len(messages)}) for session {sid_label}, skipping")
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
                print(f"📊 SESSION INSIGHT (saving to DB) — session {session_id or 'current'}")
                print("=" * 60)
                print(json.dumps(analysis, indent=2, ensure_ascii=False))
                print("=" * 60 + "\n")
                db.save_insight(analysis, session_id=session_id)
                return
            except Exception as e:
                if "overloaded" in str(e).lower() and attempt < 1:
                    _time.sleep(3)
                    continue
                raise
    except Exception as e:
        print(f"  [Insight] Analysis failed: {e}")


# ─── Session lifecycle: idle rotation + orphan cleanup ────────────
#
# A "session" in this app means "one focused learning unit", not "one
# WebSocket connection lifetime". Two helpers below realize that:
#
#   1. _rotate_session_if_idle(): on every chat_message we check the
#      gap since the last message in the current session. If it
#      exceeds IDLE_THRESHOLD_MINUTES, we close+analyze the prior
#      session and open a fresh one for the incoming message. This is
#      what makes a 3-hour "I closed my laptop and came back" gap
#      register as two sessions instead of one.
#
#   2. _cleanup_orphan_sessions_async(): on connect, drain any of the
#      user's prior sessions that still have end_time IS NULL. Those
#      are sessions where the WS-disconnect handler never ran cleanly
#      (process kill, OS sleep, daemon thread killed). Without this
#      net, those sessions silently never get analyzed.

IDLE_THRESHOLD_MINUTES = 20


def _parse_db_timestamp(ts_str):
    """Parse a DB timestamp string into datetime. Tolerant of either
    isoformat (sessions/insights) or "YYYY-MM-DD HH:MM:SS" (messages)."""
    if not ts_str:
        return None
    from datetime import datetime as _dt
    for fmt in (None,):  # try fromisoformat first (handles both forms in py3.11+)
        try:
            return _dt.fromisoformat(ts_str)
        except Exception:
            pass
    try:
        return _dt.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _rotate_session_if_idle():
    """Called at the top of handle_chat_message. If the gap since the
    last message in the current session exceeds IDLE_THRESHOLD_MINUTES,
    end+analyze the prior session and start a new one for the user.

    Done synchronously enough that the incoming message lands in the
    NEW session — but the analyzer itself runs in a background thread
    so the user doesn't wait on Claude.
    """
    ctx = _ctx()
    if not ctx or not ctx.user_id:
        return
    prior_sid = ctx.db_session_id or db.get_session_id()
    if not prior_sid:
        return

    last_ts_str = db.get_last_activity_time(prior_sid)
    if not last_ts_str:
        return  # no messages yet → nothing to rotate

    from datetime import datetime as _dt
    last_ts = _parse_db_timestamp(last_ts_str)
    if not last_ts:
        return
    gap_min = (_dt.now() - last_ts).total_seconds() / 60.0
    if gap_min < IDLE_THRESHOLD_MINUTES:
        return

    print(f"  [Session] Idle {gap_min:.1f}min > {IDLE_THRESHOLD_MINUTES}min — rotating "
          f"session {prior_sid} → new session", flush=True)

    # Close the prior session row immediately so it shows up as ended
    # in queries, and so the orphan-cleanup pass on next connect won't
    # pick it up.
    try:
        db.end_session(session_id=prior_sid)
    except Exception as e:
        print(f"  [Session] end_session({prior_sid}) failed: {e}", flush=True)

    # Analyze in background — don't block the user's incoming message.
    captured_uid = ctx.user_id
    def _analyze_in_bg(_uid=captured_uid, _sid=prior_sid):
        try:
            db.set_thread_user(_uid, _sid)
            analyze_session_and_save(session_id=_sid)
        except Exception as e:
            print(f"  [Session] background analyze of {_sid} failed: {e}", flush=True)
    threading.Thread(target=_analyze_in_bg, daemon=True).start()

    # Start a new DB session for the incoming message. set_thread_user
    # rebinds db's per-thread session id; start_session creates the row
    # and updates user_state.
    db.set_thread_user(ctx.user_id, None)
    db.start_session(study_topic=ctx.study_topic or "")
    new_sid = db.get_session_id()
    ctx.db_session_id = new_sid
    print(f"  [Session] New session started for {ctx.user_id}: {new_sid}", flush=True)
    # Note: we deliberately keep _chat_state.messages (LLM short-term
    # context). Analytics splits, conversational continuity stays.


def _cleanup_orphan_sessions_async(user_id):
    """Find any prior sessions for `user_id` that were never cleanly
    ended (end_time IS NULL) and run analyze + end on each, in a
    background thread.

    The orphan list is SNAPSHOTTED here (in the calling thread) before
    the background work begins, so that any new session created right
    after this call by start_session() will not be picked up. The
    background thread then iterates that fixed snapshot — even if more
    sessions become open later, they're not in scope for this drain.

    Idempotent — safe to call repeatedly; sessions already analyzed
    become end_time-stamped and won't reappear in future snapshots.
    """
    if not user_id:
        return
    try:
        orphans = db.get_open_sessions_for_user(user_id)
    except Exception as e:
        print(f"  [Orphan] lookup failed for {user_id}: {e}", flush=True)
        return
    if not orphans:
        return
    snapshot = [(o["session_id"], o.get("n_msgs", 0)) for o in orphans]

    def _do(_uid=user_id, _items=snapshot):
        print(f"  [Orphan] Found {len(_items)} unfinished session(s) for {_uid} — draining", flush=True)
        for sid, n in _items:
            try:
                db.set_thread_user(_uid, sid)
                # analyze_session_and_save bails internally if msgs < 4
                analyze_session_and_save(session_id=sid)
            except Exception as e:
                print(f"  [Orphan] analyze {sid} (n_msgs={n}) failed: {e}", flush=True)
            try:
                db.end_session(session_id=sid)
            except Exception as e:
                print(f"  [Orphan] end {sid} failed: {e}", flush=True)
        print(f"  [Orphan] Drain complete for {_uid}", flush=True)

    threading.Thread(target=_do, daemon=True).start()


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
