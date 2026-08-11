"""
Theo — web-only learning coach.

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


async def _app_handler(request):
    """Serve the Theo chat app (index.html) at /app.

    The app lived at / until 2026-07; it moved so / can serve the
    public landing page carriers review for toll-free verification
    (see _landing_handler). The app's WebSocket connects to the
    absolute path /ws, so it works unchanged from /app."""
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


# ─── SMS endpoints ────────────────────────────────────────────────────
#
# Two routes wire the SMS tutor (sms.py) to the outside world:
#
#   POST /sms/inbound    Twilio webhook for incoming texts. Verified
#                        via X-Twilio-Signature.
#
#   POST /sms/cron-tick  Triggered by Render Cron Jobs (which run
#                        `curl -X POST -H 'X-Cron-Secret: $CRON_SECRET'
#                        https://upskill-coach-dmmu.onrender.com/sms/cron-tick?slot=X`).
#                        Verified via shared-secret header.
#
# Both run their LLM/Twilio work in a background thread so we can
# return the HTTP response promptly (Twilio retries if we take too
# long; Render Cron doesn't care but the principle is the same).

async def _sms_inbound_handler(request):
    """Twilio webhook → sms.handle_inbound() in a thread."""
    import sms

    # Twilio POSTs form-encoded. Parse before verifying so we have
    # both params and signature.
    form = await request.post()
    params = {k: form[k] for k in form}
    signature = request.headers.get("X-Twilio-Signature", "")

    # Reconstruct the public URL Twilio used. Behind Render's
    # Cloudflare proxy the scheme is https even though aiohttp may
    # see http. Honor X-Forwarded-Proto when present.
    scheme = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host") or request.host
    url = f"{scheme}://{host}{request.path_qs}"

    if not sms.verify_twilio_signature(url, params, signature):
        print(f"[SMS] ❌ inbound signature failed (url={url}, sig={signature[:12]}...)", flush=True)
        return web.Response(status=403, text="bad signature")

    from_number = params.get("From", "")
    body = params.get("Body", "")
    print(f"[SMS] inbound from={from_number} body={body[:80]!r}", flush=True)

    # Run the LLM + Twilio send in a thread so we can ack quickly.
    asyncio.get_event_loop().run_in_executor(
        None, sms.handle_inbound, from_number, body
    )

    # Empty TwiML response — we send our reply out-of-band via the
    # REST API rather than returning <Message> inline. Lets us send
    # multiple SMS bubbles cleanly.
    return web.Response(
        text='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
        content_type="application/xml",
    )


async def _sms_cron_tick_handler(request):
    """Render Cron Job → sms.handle_cron_tick(slot).

    Accepts the shared secret via either:
      - HTTP header `X-Cron-Secret: <value>`  (preferred)
      - URL query param `?secret=<value>`     (fallback)

    Why both: Render's image-runtime Docker Command field tokenizes
    by whitespace WITHOUT respecting quote grouping. That makes the
    `-H "X-Cron-Secret: hex"` form impossible to pass — the space in
    the header value breaks into multiple argv tokens. Putting the
    secret in the URL has no spaces, so the cron command stays a
    single clean curl invocation with no quoting at all. Trade-off:
    the secret appears in Render's cron logs (which is the same
    place it's already set as an env var, so net new exposure is ~0).
    """
    import sms

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        print(f"[SMS] ❌ cron-tick auth failed (provided={provided[:8]}...)", flush=True)
        return web.Response(status=403, text="bad secret")

    slot = request.query.get("slot", "").strip().lower()
    if slot not in sms.SLOTS:
        return web.Response(status=400, text=f"slot must be one of {sms.SLOTS}")

    # Optional single-user targeting for MANUAL triggers. Since the
    # M2 fan-out, a bare trigger fires for the whole roster — which
    # is what the scheduled crons want and exactly what an operator
    # poking one user does not: observed need the same day the
    # fan-out shipped ("남편한테까지 가는 거 아닌가?"). With
    # user_id, only that user's slot runs.
    target = (request.query.get("user_id") or "").strip()
    if slot == "nudge" and not target:
        return web.Response(
            status=400,
            text="nudge is per-user by definition — pass user_id\n")
    if target:
        import db
        phone = sms._phone_for(target)
        if not phone:
            return web.Response(status=404,
                                text=f"no phone bound for {target}")
        print(f"[SMS] cron-tick slot={slot} → {target} only", flush=True)
        asyncio.get_event_loop().run_in_executor(
            None, sms._cron_tick_for_user, target, phone, slot)
        return web.json_response({"ok": True, "slot": slot,
                                  "user_id": target})

    print(f"[SMS] cron-tick slot={slot} (all active users)", flush=True)
    # Run the LLM + send in a thread — same reasoning as inbound.
    asyncio.get_event_loop().run_in_executor(
        None, sms.handle_cron_tick, slot
    )

    return web.json_response({"ok": True, "slot": slot})


async def _sms_schedule_tick_handler(request):
    """Hourly Render cron → sms.handle_schedule_tick() (P0-C).

    One fixed cron (`0 * * * *`) drives every per-user agreed send
    window: the handler compares each window's start hour against
    the user's current local hour and fires at most one send, with
    morning/evening semantics decided by the window's start hour.
    Same secret auth as /sms/cron-tick (header or ?secret=).
    """
    import sms

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        print(f"[SMS] ❌ schedule-tick auth failed (provided={provided[:8]}...)", flush=True)
        return web.Response(status=403, text="bad secret")

    print("[SMS] schedule-tick", flush=True)

    # Run the LLM + send in a thread — same reasoning as cron-tick.
    asyncio.get_event_loop().run_in_executor(
        None, sms.handle_schedule_tick
    )

    return web.json_response({"ok": True})


async def _schedule_debug_handler(request):
    """GET /schedule?secret=...[&user_id=X] — the latest agreed
    windows + raw text + which windows already fired today (the same
    dedup rule the tick uses). P0-C observation surface, same shape
    as /onboarding."""
    import sms

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = (request.query.get("user_id", "").strip()
               or os.environ.get("TUTOR_USER_ID", "").strip())
    if not user_id:
        return web.Response(status=400, text="user_id required")

    status = sms.schedule_status(user_id)
    parts = [f"# schedule — {user_id}"]
    if not status["schedule"]:
        parts.append("(no schedule rows — fixed crons serve this user)")
        return web.Response(text="\n".join(parts),
                            content_type="text/plain")
    s = status["schedule"]
    parts += [f"v{s['version']} at {s['ts']} (source: {s['source']})",
              f"raw: {s['raw_text']}", ""]
    for w in status["windows"]:
        state = (f"fired today at {w['last_fired']}" if w["fired_today"]
                 else "not fired today")
        parts.append(f"- {w['window']} → {w['slot']} — {state}")
    return web.Response(text="\n".join(parts), content_type="text/plain")


async def _availability_handler(request):
    """GET /availability?secret=...[&user_id=X] — the derived day×hour
    grid (brief §7), plus the self-report vs observed disagreement
    line, which is the finding worth reading. Live recompute, so the
    view never lags the events; the stored snapshot's version is
    shown alongside.

    Read-only: nothing here feeds send scheduling (see availability.py).
    """
    import availability

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = (request.query.get("user_id", "").strip()
               or os.environ.get("TUTOR_USER_ID", "").strip())
    if not user_id:
        return web.Response(status=400, text="user_id required")

    return web.Response(text=availability.render(user_id),
                        content_type="text/plain")


async def _sms_reset_and_fire_handler(request):
    """One-shot rescue endpoint: reset the tutor user's phase state
    (fresh Phase 0 with timer starting NOW, cutting off old SMS
    history from LLM context), then immediately trigger the evening
    slot to send a clean discovery message.

    Same secret auth as cron-tick. Only intended for use when the
    conversation has been polluted and needs a hard reset — for
    example when redesign migration left stale SMS history that
    was bleeding into the new phase's LLM context.
    """
    import sms
    import db

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = os.environ.get("TUTOR_USER_ID", "").strip()
    if not user_id:
        return web.Response(status=500, text="TUTOR_USER_ID not set")

    reset_at = db.reset_phase_state(user_id, source="admin")
    print(f"[SMS] rescue: phase reset for {user_id} at {reset_at}", flush=True)

    asyncio.get_event_loop().run_in_executor(
        None, sms.handle_cron_tick, "evening"
    )
    return web.json_response({"ok": True, "reset_at": reset_at, "fired": "evening"})


async def _sms_set_goal_handler(request):
    """Admin rescue: directly set the agreed goal without LLM
    cooperation. Used when the goal was agreed in conversation but
    never persisted (pre-agreed_goal-column history), so the LLM
    keeps falling back to the stale onboarding goal.

    POST /sms/set-goal?secret=...&goal=<url-encoded text>
    """
    import db

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = os.environ.get("TUTOR_USER_ID", "").strip()
    if not user_id:
        return web.Response(status=500, text="TUTOR_USER_ID not set")

    goal = request.query.get("goal", "").strip()
    if not goal:
        return web.Response(status=400, text="goal param required")

    db.set_agreed_goal(user_id, goal, source="admin")
    return web.json_response({"ok": True, "user_id": user_id, "agreed_goal": goal})


async def _sms_set_ignition_handler(request):
    """Admin rescue: directly set the user's ignition marker (their
    observable definition of "it started"). The designed path is the
    onboarding conversation ([IGNITION_DEF:] marker); this covers
    users who predate that flow, until re-onboarding.

    POST /sms/set-ignition?secret=...&marker=<url-encoded text>[&user_id=X]
    """
    import db

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = (request.query.get("user_id", "").strip()
               or os.environ.get("TUTOR_USER_ID", "").strip())
    if not user_id:
        return web.Response(status=500, text="user_id unresolved")

    marker = request.query.get("marker", "").strip()
    if not marker:
        return web.Response(status=400, text="marker param required")

    db.set_ignition_marker(user_id, marker, source="admin")
    return web.json_response({"ok": True, "user_id": user_id,
                              "ignition_marker": marker})


async def _sms_status_handler(request):
    """Diagnosis: report the tutor user's current phase state so we
    can see server-side truth without shelling into the DB.

    GET /sms/status?secret=...
    """
    import db

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = os.environ.get("TUTOR_USER_ID", "").strip()
    if not user_id:
        return web.Response(status=500, text="TUTOR_USER_ID not set")

    state = db.get_user_phase(user_id)
    state["user_id"] = user_id
    state["days_in_discovery"] = db.days_in_discovery(user_id)
    return web.json_response(state)


async def _debug_timeline_handler(request):
    """Human-readable per-user event timeline — WEEK1_ORDER T1's
    acceptance surface. Plain text, newest last.

    GET /debug/timeline?secret=...[&user_id=X][&limit=200]
    """
    import db

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = (request.query.get("user_id", "").strip()
               or os.environ.get("TUTOR_USER_ID", "").strip())
    if not user_id:
        return web.Response(status=400, text="user_id required")
    try:
        limit = min(int(request.query.get("limit", "200")), 1000)
    except ValueError:
        limit = 200
    # full=1 skips the scannability cap — transcript.py reads this
    # endpoint programmatically and the cap was costing it message
    # tails (7 unrestorable messages in one weekend pull).
    full = request.query.get("full", "").strip() in ("1", "true")

    rows = db.get_events(user_id, limit=limit)
    lines = [f"# timeline for {user_id} — {len(rows)} events (oldest first)"]
    for r in rows:
        payload = r.get("payload") or "{}"
        # keep each line scannable; full payloads live in the table
        if not full and len(payload) > 300:
            payload = payload[:300] + "…"
        lines.append(f"{r['ts']}  [{r['source']}] {r['kind']}  {payload}")
    return web.Response(text="\n".join(lines) + "\n", content_type="text/plain")


async def _debug_prompt_handler(request):
    """Retrieve the exact prompt template text behind a version hash
    — T2's acceptance surface. Take any sms_out event's
    prompt_versions hash and get back the template that produced it.

    GET /debug/prompt?secret=...&hash=abc123def456
    """
    import db

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    h = request.query.get("hash", "").strip()
    if not h:
        return web.Response(status=400, text="hash required")

    row = db.get_prompt_version(h)
    if not row:
        return web.Response(status=404, text=f"no prompt version {h}")
    header = (f"# {row['name']} @ {row['hash']}\n"
              f"# first seen: {row['first_seen']}\n"
              f"# {'─' * 60}\n")
    return web.Response(text=header + row["content"], content_type="text/plain")


async def _debug_llm_call_handler(request):
    """Retrieve a full recorded LLM call — the exact rendered input
    the API received + the raw response (T2b's acceptance surface).
    Take an llm_call_id from any sms_out event in /debug/timeline.

    GET /debug/llm-call?secret=...&id=<call_id>
    """
    import db

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    call_id = request.query.get("id", "").strip()
    if not call_id:
        return web.Response(status=400, text="id required")

    row = db.get_llm_call(call_id)
    if not row:
        return web.Response(status=404, text=f"no llm call {call_id}")

    parts = [
        f"# llm_call {row['call_id']} — user={row['user_id']} ts={row['ts']}",
        f"# trigger={row['trigger']} model={row['model']}",
        f"# prompt_versions={row['prompt_versions_json']}",
        "", "═" * 30 + " SYSTEM PROMPT (as sent) " + "═" * 30, "",
        row["system_prompt"],
        "", "═" * 30 + " MESSAGES (as sent) " + "═" * 30, "",
        json.dumps(row["messages"], ensure_ascii=False, indent=2),
        "", "═" * 30 + " RESPONSE (raw, pre-marker-strip) " + "═" * 20, "",
        row["response_text"], "",
    ]
    return web.Response(text="\n".join(parts), content_type="text/plain")


async def _annotate_run_handler(request):
    """T5 nightly annotation trigger — Render cron hits this after
    the day ends (same curl-image pattern as the SMS slots), and the
    founder can re-run any historical day on demand (re-annotation:
    appends new rows, never overwrites).

    POST /annotate/run?secret=...[&day=YYYY-MM-DD][&user_id=X]
    day defaults to yesterday (PT). user_id defaults to all users
    active that day.
    """
    import annotate

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    day = request.query.get("day", "").strip() or None
    user_id = request.query.get("user_id", "").strip() or None

    def _run():
        if user_id:
            state = annotate.annotate_day(user_id, day)
            return {user_id: "ok" if state else "skipped"}
        return annotate.annotate_all(day)

    # LLM calls take seconds-to-minutes; keep them off the event loop.
    results = await asyncio.get_event_loop().run_in_executor(None, _run)
    return web.json_response({"day": day or "(yesterday PT)",
                              "results": results})


async def _debug_learner_state_handler(request):
    """Human-readable LearnerState snapshots — T5's acceptance
    surface, next to /debug/timeline.

    GET /debug/learner-state?secret=...[&user_id=X][&day=YYYY-MM-DD][&limit=10]
    """
    import db

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    rows = db.get_learner_state_snapshots(
        user_id=request.query.get("user_id", "").strip() or None,
        day=request.query.get("day", "").strip() or None,
        limit=int(request.query.get("limit", "10")),
    )
    if not rows:
        return web.Response(text="(no snapshots)", content_type="text/plain")

    parts = []
    for r in rows:
        parts += [
            f"# snapshot {r['id']} — user={r['user_id']} day={r['day']} "
            f"created={r['created_at']}",
            f"# schema=v{r['schema_version']} "
            f"prompt={r['prompt_version']} model={r['model']} "
            f"llm_call={r['llm_call_id']}",
            f"# evidence event ids: {r['evidence_json']}",
        ]
        try:
            parts.append(json.dumps(json.loads(r["state_json"]),
                                    ensure_ascii=False, indent=2))
        except Exception:
            parts.append(str(r["state_json"]))
        parts.append("")
    return web.Response(text="\n".join(parts), content_type="text/plain")


async def _notes_handler(request):
    """User-notes governance until the operator dashboard exists.

    GET  /notes?secret=...[&user_id=X]           — view (incl. retired)
    POST /notes?secret=...  body: JSON           — add or revise
         {user_id?, note_id? (revise), claim, given?, when?, expect?,
          evidence?, confidence?}
    Retiring = POST with note_id + confidence="retired".
    """
    import db
    import notes as notes_mod

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    default_user = os.environ.get("TUTOR_USER_ID", "").strip()

    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="JSON body required")
        user_id = (body.get("user_id") or default_user).strip()
        if not (user_id and body.get("claim")):
            return web.Response(status=400, text="user_id and claim required")
        note_id, version = db.save_user_note(
            user_id, body["claim"],
            given=body.get("given"), when=body.get("when"),
            expect=body.get("expect", ""),
            evidence=body.get("evidence"),
            confidence=body.get("confidence", "hypothesis"),
            source="operator", note_id=body.get("note_id"))
        return web.json_response({"ok": True, "note_id": note_id,
                                  "version": version})

    user_id = request.query.get("user_id", "").strip() or default_user
    rows = db.get_user_notes(user_id, include_retired=True)
    parts = [f"# user notes — {user_id} ({len(rows)} notes)", ""]
    for n in rows:
        parts += [f"[{n['note_id']} v{n['version']}] "
                  f"({n['confidence']}, {n['source']}, {n['ts'][:10]})",
                  f"  {n['claim']}",
                  f"  given={n['given_json']} when={n['when_json']} "
                  f"expect={n['expect']}",
                  f"  evidence={n['evidence_json']}", ""]
    parts += ["— rendered prompt block —", "",
              notes_mod.render_notes_block(user_id) or "(empty)"]
    return web.Response(text="\n".join(parts), content_type="text/plain")


async def _window_open_handler(request):
    """Operator override for the WhatsApp free-form window.

    Twilio's sandbox swallows the `join <code>` message, so a user can
    reopen their 24h window without us ever seeing an inbound. This
    records the reopening (timestamped now, so it ages out like a real
    message) instead of faking a user turn in the conversation.

    POST /sms/window-open?secret=...[&user_id=X][&note=...]
    """
    import sms

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = (request.query.get("user_id", "").strip()
               or os.environ.get("TUTOR_USER_ID", "").strip())
    if not user_id:
        return web.Response(status=400, text="user_id required")

    sms.mark_whatsapp_window_open(user_id, request.query.get("note", ""))
    return web.json_response({"ok": True, "user_id": user_id,
                              "window_closed_now":
                                  sms.whatsapp_window_closed(user_id)})


async def _analyze_turn_handler(request):
    """Run the per-turn analysis pass on demand — back-extraction over
    a conversation that predates the analysis call, or a re-run after
    a failure. Idempotent (unchanged values are skipped).

    POST /analyze/turn?secret=...[&user_id=X]
    """
    import analyze_turn

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = (request.query.get("user_id", "").strip()
               or os.environ.get("TUTOR_USER_ID", "").strip())
    if not user_id:
        return web.Response(status=400, text="user_id required")

    result = await asyncio.get_event_loop().run_in_executor(
        None, analyze_turn.analyze_history, user_id)
    if result is None:
        return web.json_response(
            {"ok": False, "hint": "no conversation, or see "
                                  "/debug/timeline"}, status=500)
    return web.json_response({"ok": True, **result})


async def _plan_generate_handler(request):
    """Run initial plan generation now (P0-B) — for reruns after a
    p7_generation_failed, or manual triggering during rehearsal.

    POST /plan/generate?secret=...[&user_id=X]
    """
    import genplan

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = (request.query.get("user_id", "").strip()
               or os.environ.get("TUTOR_USER_ID", "").strip())
    if not user_id:
        return web.Response(status=400, text="user_id required")

    result = await asyncio.get_event_loop().run_in_executor(
        None, genplan.generate, user_id)
    if result is None:
        return web.json_response(
            {"ok": False,
             "hint": "see p7_generation_failed in /debug/timeline"},
            status=500)
    return web.json_response({"ok": True, **result})


async def _onboarding_handler(request):
    """Onboarding state machine — observation + operator override.

    GET  /onboarding?secret=...[&user_id=X]         — state + checklist
    POST /onboarding?secret=...&action=complete[&force=1][&user_id=X]
         — run the completion check now; force=1 is the operator
           override/backfill (e.g. the founder, who predates the flow)
    """
    import db

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = (request.query.get("user_id", "").strip()
               or os.environ.get("TUTOR_USER_ID", "").strip())
    if not user_id:
        return web.Response(status=400, text="user_id required")

    if request.method == "POST":
        if request.query.get("action") != "complete":
            return web.Response(status=400, text="action=complete required")
        force = request.query.get("force", "").strip() in ("1", "true")
        changed = db.check_and_complete_onboarding(user_id, force=force)
        return web.json_response({"ok": True, "completed_now": changed,
                                  "state": db.get_onboarding_state(user_id)})

    state = db.get_onboarding_state(user_id)
    path = db.get_current_path(user_id)
    sched = db.get_user_schedule(user_id)
    parts = [f"# onboarding — {user_id}",
             f"started_at:   {state['started_at']}",
             f"completed_at: {state['completed_at']}",
             f"filled:  {', '.join(state['filled']) or '(none)'}",
             f"missing: {', '.join(state['missing']) or '(none)'}", ""]
    if path:
        parts += [f"path v{path['version']}: {path['direction']} | "
                  f"{path['project']} | {path['project_done_condition']}"]
    if sched:
        parts += [f"schedule v{sched['version']}: {sched['windows_json']} "
                  f"(raw: {sched['raw_text']})"]
    return web.Response(text="\n".join(parts), content_type="text/plain")


async def _plan_handler(request):
    """Sequence-plan governance (exploration v2) + the profile brief.

    GET  /plan?secret=...[&user_id=X]     — profile brief, path kind,
                                            current plan + cursor
    POST /plan?secret=...  body: JSON     — set a new plan version
         {user_id?, steps: [{tag, intensity, intent}], rationale?}
    """
    import db

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    default_user = os.environ.get("TUTOR_USER_ID", "").strip()

    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400, text="JSON body required")
        user_id = (body.get("user_id") or default_user).strip()
        steps = body.get("steps")
        if not (user_id and isinstance(steps, list) and steps):
            return web.Response(status=400,
                                text="user_id and non-empty steps required")
        version = db.save_sequence_plan(
            user_id, steps, rationale=body.get("rationale", ""),
            source="operator")
        return web.json_response({"ok": True, "version": version})

    user_id = request.query.get("user_id", "").strip() or default_user
    parts = []

    # Profile brief first — the plan only makes sense against who the
    # user is (brief §7 "User profile brief").
    brief = db.get_user_profile_brief(user_id)
    path = db.get_current_path(user_id)
    if brief:
        wants = json.loads(brief["wants_json"] or "[]")
        parts += [f"# profile brief — {user_id} v{brief['version']} "
                  f"({brief['source']}, {brief['ts'][:16]})",
                  f"job:             {brief['job'] or '(not stated)'}",
                  f"learning types:  "
                  f"{', '.join(json.loads(brief['learning_types_json'] or '[]')) or '(none)'}",
                  f"materials:       "
                  f"{', '.join(json.loads(brief['materials_json'] or '[]')) or '(none)'}",
                  f"path kind:       "
                  f"{(path or {}).get('path_kind') or '(not set)'}",
                  "wants (their own words):"]
        for w in wants:
            parts.append(f"  · \"{w.get('quote', '')}\"  → "
                         f"{w.get('meaning', '')}")
        if not wants:
            parts.append("  (none recorded)")
        parts += [f"personality:     {brief['personality'] or '(none)'}",
                  f"rationale:       {brief['rationale'] or '(none)'}",
                  ""]
    else:
        parts += [f"# profile brief — {user_id}: (none generated — see "
                  f"POST /plan/generate)", ""]

    plan = db.get_current_plan(user_id)
    if not plan:
        parts.append(f"(no plan for {user_id})")
        return web.Response(text="\n".join(parts),
                            content_type="text/plain")
    parts += [f"# sequence plan — {user_id} v{plan['version']} "
              f"(cursor at step {plan['cursor'] + 1} of {len(plan['steps'])})",
              f"rationale: {plan['rationale']}", ""]
    for i, s in enumerate(plan["steps"]):
        mark = "→" if i == plan["cursor"] else " "
        parts.append(f" {mark} {i + 1}. {s['tag']}@{s.get('intensity', 2)}"
                     f" — {s.get('intent', '')}")
    return web.Response(text="\n".join(parts), content_type="text/plain")


async def _debug_trace_handler(request):
    """Step-language trace of a user's recent days — exploration P1.
    The same rendering feeds the planner (block C), the founder's
    morning review, and the nightly scorer.

    GET /debug/trace?secret=...[&user_id=X][&days=3][&verbose=1]
    """
    import trace as trace_mod

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = (request.query.get("user_id", "").strip()
               or os.environ.get("TUTOR_USER_ID", "").strip())
    if not user_id:
        return web.Response(status=400, text="user_id required")
    days = int(request.query.get("days", "3"))
    verbose = request.query.get("verbose", "").strip() in ("1", "true")

    text = trace_mod.render_trace(user_id, days=days, verbose=verbose)
    header = (f"# trace — user={user_id} days={days} "
              f"verbose={verbose}\n\n")
    return web.Response(text=header + text, content_type="text/plain")


async def _sms_set_bite_handler(request):
    """Admin rescue: manually commit the first bite, transitioning
    discovery → first_bite. Used when the [COMMIT:] marker never
    fired during a conversation where agreement clearly happened.

    POST /sms/set-bite?secret=...&bite=<url-encoded text>
    """
    import db

    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")

    user_id = os.environ.get("TUTOR_USER_ID", "").strip()
    if not user_id:
        return web.Response(status=500, text="TUTOR_USER_ID not set")

    bite = request.query.get("bite", "").strip()
    if not bite:
        return web.Response(status=400, text="bite param required")

    db.commit_first_bite(user_id, bite, source="admin")
    return web.json_response(
        {"ok": True, "user_id": user_id, "phase": "first_bite", "agreed_first_bite": bite}
    )


# ─── Screen observer endpoints ────────────────────────────────────────
#
# The local agent (observer.py, runs on the user's laptop) talks to
# these three routes. Auth = same shared-secret pattern as the other
# SMS admin endpoints; identity = TUTOR_USER_ID (single-user MVP).
# The web app's session/login flow is untouched — observer sessions
# live in their own tables (see db.py isolation note).

def _observer_auth(request):
    """Shared-secret check. Returns user_id or None."""
    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (
        request.headers.get("X-Cron-Secret", "").strip()
        or request.query.get("secret", "").strip()
    )
    if not expected or provided != expected:
        return None
    return os.environ.get("TUTOR_USER_ID", "").strip() or None


async def _observe_start_handler(request):
    """POST /observe/start — open an observe session, return its id."""
    import db
    user_id = _observer_auth(request)
    if not user_id:
        return web.Response(status=403, text="bad secret")
    sid = db.start_observe_session(user_id)
    db.log_event(user_id, "observe_start", {"session_id": sid}, source="observer")
    return web.json_response({"ok": True, "session_id": sid})


async def _observe_capture_handler(request):
    """POST /observe/capture?session_id= — body is JPEG/PNG bytes.

    Summarizes via vision model and stores text. Synchronous within
    an executor thread: the agent uploads every ~60s, so a couple of
    seconds of processing latency is fine, and returning the summary
    lets the agent print it for local debugging.
    """
    import db
    import observe as observe_mod

    user_id = _observer_auth(request)
    if not user_id:
        return web.Response(status=403, text="bad secret")

    session_id = request.query.get("session_id", "").strip()
    if not session_id:
        return web.Response(status=400, text="session_id required")

    image_bytes = await request.read()
    if not image_bytes or len(image_bytes) < 100:
        return web.Response(status=400, text="empty body")
    if len(image_bytes) > 4_000_000:
        return web.Response(status=413, text="image too large — agent should downscale")

    media_type = request.headers.get("Content-Type", "image/jpeg")
    if media_type not in ("image/jpeg", "image/png"):
        media_type = "image/jpeg"

    # forced=1 → on-demand capture (user just texted asking the tutor
    # to look) → deep vision tier with verbatim code transcription.
    deep = request.query.get("forced", "") == "1"

    loop = asyncio.get_event_loop()
    try:
        summary = await loop.run_in_executor(
            None,
            lambda: observe_mod.summarize_screenshot(image_bytes, media_type, deep=deep),
        )
    except Exception as e:
        print(f"[OBS] ❌ vision call failed: {e}", flush=True)
        return web.Response(status=502, text=f"vision failed: {e}")

    db.save_observation(session_id, user_id, summary)
    db.log_event(user_id, "observation",
                 {"session_id": session_id, "deep": deep, "summary": summary},
                 source="observer")
    print(f"[OBS] {session_id}: {summary[:100]}", flush=True)
    return web.json_response({"ok": True, "summary": summary})


async def _observe_end_handler(request):
    """POST /observe/end?session_id= — close the session."""
    import db
    user_id = _observer_auth(request)
    if not user_id:
        return web.Response(status=403, text="bad secret")
    session_id = request.query.get("session_id", "").strip()
    if not session_id:
        return web.Response(status=400, text="session_id required")
    db.end_observe_session(session_id)
    db.log_event(user_id, "observe_end", {"session_id": session_id}, source="observer")
    return web.json_response({"ok": True, "session_id": session_id})


async def _observe_poll_handler(request):
    """GET /observe/poll — long-poll from the local agent.

    Holds the connection up to ~20s. Returns {"capture": true} the
    moment an on-demand capture request is pending (dropped by the
    inbound WhatsApp handler when the user texts mid-session), else
    {"capture": false} at timeout and the agent immediately re-polls.
    This doubles as the agent's sleep between timer captures, so
    on-demand latency is sub-second on the signaling side.
    """
    import observe as observe_mod

    user_id = _observer_auth(request)
    if not user_id:
        return web.Response(status=403, text="bad secret")

    observe_mod.record_poll(user_id)  # liveness heartbeat (T6)
    deadline = asyncio.get_event_loop().time() + 20
    while asyncio.get_event_loop().time() < deadline:
        if observe_mod.consume_capture_request(user_id):
            return web.json_response({"capture": True})
        await asyncio.sleep(0.5)
    return web.json_response({"capture": False})


# ─── Privacy + Terms (A2P 10DLC compliance) ─────────────────────────
#
# Twilio's A2P 10DLC Campaign vetting requires public URLs for the
# Campaign's privacy policy and terms. Their reviewers actually fetch
# these URLs and look for specific phrases:
#
#   - non-sharing statement for mobile numbers
#   - message frequency note
#   - "Message and data rates may apply"
#   - STOP / HELP keyword behavior
#
# Pages are inline HTML so they don't depend on the static handler's
# file-on-disk path resolution. Content is honest — Theo is a
# single-owner personal learning experiment, no third-party sharing.

_PAGE_CSS = """
:root {
  --pine: #123f2b;        /* deep brand green — headings, footer, dark UI   */
  --green: #1b6e47;       /* primary actions, links                        */
  --green-hover: #14563a;
  --tint: #eef5f0;        /* soft green wash for bands and cards           */
  --ink: #14201a;
  --muted: #5a6b61;
  --line: #e3eae5;
}
* { box-sizing: border-box; }
html { scroll-behavior: smooth; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Inter', 'Segoe UI',
       sans-serif; margin: 0; color: var(--ink); line-height: 1.65;
       font-size: 16.5px; -webkit-font-smoothing: antialiased;
       background: #fff; }
a { color: var(--green); }
h1, h2, h3 { line-height: 1.22; letter-spacing: -0.015em; }
p { margin: 10px 0; }
ul { padding-left: 22px; }
li { margin: 5px 0; }
.meta { color: var(--muted); font-size: 13.5px; margin-bottom: 24px; }

/* ── Header ─────────────────────────────────────────────────────── */
.header { border-bottom: 1px solid var(--line); background: #fff; }
.header-in { max-width: 1360px; margin: 0 auto; padding: 16px 28px;
             display: flex; align-items: center;
             justify-content: space-between; gap: 18px; flex-wrap: wrap; }
.logo { display: flex; align-items: center; gap: 10px;
        text-decoration: none; color: var(--pine); font-weight: 800;
        font-size: 21px; letter-spacing: -0.02em; }
.logo svg { display: block; }
.nav-links { display: flex; gap: 22px; align-items: center;
             flex-wrap: wrap; font-size: 15px; }
.nav-links a { color: #3c4a42; text-decoration: none; font-weight: 500; }
.nav-links a:hover { color: var(--green); }
.btn { display: inline-block; padding: 12px 24px; border-radius: 999px;
       background: var(--green); color: #fff !important; font-weight: 600;
       font-size: 15.5px; text-decoration: none; }
.btn:hover { background: var(--green-hover); }
.btn-ghost { background: transparent; color: var(--pine) !important;
             border: 1.5px solid var(--pine); }
.btn-ghost:hover { background: var(--tint); }
.nav-links .btn { padding: 9px 18px; font-size: 14.5px; }

/* ── Page containers ────────────────────────────────────────────── */
main.doc { max-width: 780px; margin: 0 auto; padding: 34px 28px 60px; }
main.doc h1 { font-size: 32px; margin: 6px 0 8px; }
main.doc h2 { font-size: 20px; margin: 30px 0 4px; }
main.doc h3 { font-size: 16.5px; margin: 22px 0 2px; }
main.wide { max-width: 1360px; margin: 0 auto; padding: 0 28px 64px; }

/* ── Hero ───────────────────────────────────────────────────────── */
.hero { display: grid; grid-template-columns: 0.9fr 1.1fr; gap: 56px;
        align-items: center; padding: 64px 0 44px; }
.eyebrow { color: var(--green); font-weight: 700; font-size: 13.5px;
           text-transform: uppercase; letter-spacing: 0.09em;
           margin-bottom: 14px; }
.hero h1 { font-size: clamp(34px, 4.6vw, 52px); font-weight: 800;
           margin: 0 0 16px; color: var(--pine); }
.hero .lead { font-size: 18.5px; color: #35443b; margin: 0 0 26px; }
.hero-ctas { display: flex; gap: 12px; flex-wrap: wrap; }
.hero-note { font-size: 13.5px; color: var(--muted); margin-top: 16px; }

/* ── Phone mockup ───────────────────────────────────────────────── */
.hero-visual { position: relative; padding: 12px 6px; }
.phone { width: min(340px, 100%); margin: 0 auto; background: #fff;
         border: 1px solid var(--line); border-radius: 34px;
         box-shadow: 0 24px 60px rgba(18, 63, 43, 0.16);
         padding: 18px 14px 22px; }
.phone-top { text-align: center; padding-bottom: 10px;
             border-bottom: 1px solid var(--line); margin-bottom: 12px; }
.phone-top .avatar { width: 34px; height: 34px; border-radius: 50%;
                     background: var(--pine); color: #fff; font-weight: 700;
                     font-size: 15px; display: inline-flex;
                     align-items: center; justify-content: center; }
.phone-top .name { font-size: 12.5px; color: var(--muted); margin-top: 4px; }
.thread { display: flex; flex-direction: column; gap: 8px; }
.tstamp { text-align: center; font-size: 11px; color: #9aa79f;
          margin: 4px 0; }
.msg { max-width: 86%; padding: 9px 13px; border-radius: 18px;
       font-size: 13.5px; line-height: 1.45; }
.msg.in { background: #f0f2f0; color: #1c2620; align-self: flex-start;
          border-bottom-left-radius: 6px; }
.msg.out { background: var(--green); color: #fff; align-self: flex-end;
           border-bottom-right-radius: 6px; }
.hero-photo { position: relative; }
.hero-photo img { width: 100%; display: block; border-radius: 22px;
                  box-shadow: 0 24px 60px rgba(18, 63, 43, 0.22); }
.pthread { position: absolute; top: 8%; left: -5%; width: 48%;
           display: flex; flex-direction: column; gap: 9px; }
.pchip { background: rgba(255,255,255,0.92); color: var(--pine);
         font-weight: 700; font-size: 11.5px; padding: 6px 12px;
         border-radius: 999px; align-self: flex-start;
         box-shadow: 0 8px 22px rgba(0,0,0,0.18); }
.pbubble { border-radius: 16px; padding: 10px 13px; font-size: 12.8px;
           line-height: 1.45; box-shadow: 0 10px 26px rgba(0,0,0,0.2); }
.pbubble.in { background: rgba(255,255,255,0.97); color: #1c2620;
              border-bottom-left-radius: 6px; align-self: flex-start; }
.pbubble.out { background: var(--green); color: #fff;
               border-bottom-right-radius: 6px; align-self: flex-end; }

/* ── Sections ───────────────────────────────────────────────────── */
.section { padding: 42px 0; }
.section > h2 { font-size: clamp(26px, 3vw, 32px); font-weight: 800;
                color: var(--pine); margin: 0 0 6px; }
.section .sub { color: var(--muted); margin: 0 0 26px; font-size: 17px; }
.section .centered { text-align: center; max-width: 640px;
                     margin-left: auto; margin-right: auto; }
.cards { display: grid; grid-template-columns: repeat(auto-fit,
         minmax(230px, 1fr)); gap: 18px; }
.card { border: 1px solid var(--line); border-radius: 14px;
        padding: 22px; background: #fff; }
.card h3 { margin: 0 0 6px; font-size: 17px; color: var(--pine); }
.card p { margin: 0; font-size: 14.5px; color: #425148; }
.band { background: var(--tint); border-radius: 18px; padding: 30px 32px; }
.band h2 { margin-top: 0; color: var(--pine); }

/* ── How-it-works: large alternating numbered rows ──────────────── */
.hiw-row { display: grid; grid-template-columns: 0.9fr 1.1fr; gap: 56px;
           align-items: center; padding: 56px 0; }
.hiw-row.flip { grid-template-columns: 1.1fr 0.9fr; }
.hiw-row.flip .hiw-copy { order: 2; }
.hiw-row.flip .hiw-visual { order: 1; }
.hiw-num { width: 52px; height: 52px; border-radius: 50%;
           background: var(--tint); color: var(--pine); font-weight: 700;
           font-size: 22px; display: flex; align-items: center;
           justify-content: center; margin-bottom: 22px; }
.hiw-eyebrow { color: var(--green); font-weight: 700; font-size: 14px;
               margin-bottom: 6px; }
.hiw-copy h3 { font-size: clamp(28px, 3.4vw, 40px); font-weight: 800;
               color: var(--pine); margin: 0 0 14px; }
.hiw-copy p { color: #35443b; font-size: 16.5px; }
.hiw-note { font-size: 13.5px; color: var(--muted); margin-top: 26px; }
.hiw-visual { background: linear-gradient(160deg, #f2f7f3, #e7f0ea);
              border-radius: 20px; padding: 34px 30px; }
.mock { max-width: 400px; margin: 0 auto; background: #fff;
        border: 1px solid var(--line); border-radius: 16px;
        padding: 18px 18px 20px;
        box-shadow: 0 18px 44px rgba(18, 63, 43, 0.14); }
.mock .mhead { font-size: 12px; font-weight: 700; color: var(--muted);
               text-transform: uppercase; letter-spacing: 0.07em;
               margin-bottom: 12px; }
.mock .row { display: flex; justify-content: space-between; gap: 12px;
             padding: 10px 0; border-bottom: 1px solid var(--line);
             font-size: 14px; }
.mock .row:last-child { border-bottom: 0; }
.mock .row .k { color: var(--muted); }
.mock .row .v { font-weight: 600; color: var(--ink); text-align: right; }
.mock .ok { color: var(--green); font-weight: 700; }
.mock .thread { margin-top: 2px; }

@media (max-width: 820px) {
  .hiw-row, .hiw-row.flip { grid-template-columns: 1fr; gap: 26px;
                            padding: 36px 0; }
  .hiw-row.flip .hiw-copy { order: 1; }
  .hiw-row.flip .hiw-visual { order: 2; }
}

/* ── Doc-page building blocks (legal, about, faq) ───────────────── */
.sms-box { margin-top: 28px; padding: 20px 22px; border: 1px solid var(--line);
           border-radius: 12px; background: var(--tint); font-size: 15px; }
.sms-box h2 { margin-top: 0 !important; }

/* ── Footer (near-black, Arist-style columns + bottom bar) ──────── */
.footer { background: #0b100d; color: #b9c6be; margin-top: 72px; }
.footer a { color: #e6ece8; text-decoration: none; }
.footer a:hover { text-decoration: underline; }
.footer-in { max-width: 1360px; margin: 0 auto; padding: 56px 28px 40px;
             display: grid; gap: 36px;
             grid-template-columns: 1.4fr 1fr 1fr 1.2fr;
             font-size: 14.5px; }
.footer h4 { margin: 0 0 12px; font-size: 16px; font-weight: 600;
             color: #fff; }
.footer ul { list-style: none; padding: 0; margin: 0; }
.footer li { margin: 9px 0; }
.footer .flogo { display: flex; align-items: center; gap: 9px;
                 color: #fff; font-weight: 800; font-size: 20px;
                 margin-bottom: 12px; }
.footer .fdesc { font-size: 13.5px; color: #93a49a; max-width: 260px; }
.footer-bar { border-top: 1px solid rgba(255,255,255,0.12); }
.footer-bar-in { max-width: 1360px; margin: 0 auto; padding: 20px 28px;
                 display: flex; justify-content: space-between;
                 align-items: center; gap: 14px; flex-wrap: wrap;
                 font-size: 13px; color: #93a49a; }
.footer-bar-in .flinks { display: flex; gap: 22px; flex-wrap: wrap; }
.footer-bar-in a { color: #b9c6be; }
@media (max-width: 820px) {
  .footer-in { grid-template-columns: 1fr 1fr; }
}

@media (max-width: 820px) {
  .hero { grid-template-columns: 1fr; padding-top: 36px; gap: 36px; }
  .chip-1 { left: 0; } .chip-2 { right: 0; }
}
"""

_SITE_ORIGIN = "https://www.learningtheo.com"

_SITE_DESC = ("Theo is a learning coach in your text messages — coaching "
              "check-ins that get you to actually start studying, and "
              "step-by-step help when you're stuck. By Green Gables "
              "Studio LLC.")

# Inline SVG logo mark (matches site_assets/favicon.svg).
_LOGO_SVG = """<svg width="30" height="30" viewBox="0 0 100 100"
  xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
  <rect width="100" height="100" rx="24" fill="#123f2b"/>
  <path d="M30 26h28a12 12 0 0 1 12 12v36H42a12 12 0 0 1-12-12V26z"
        fill="none" stroke="#fff" stroke-width="7" stroke-linejoin="round"/>
  <path d="M30 62h40" stroke="#fff" stroke-width="7" stroke-linecap="round"/>
</svg>"""

_SITE_NAV = f"""
<header class="header"><div class="header-in">
  <a class="logo" href="/">{_LOGO_SVG} Theo</a>
  <nav class="nav-links">
    <a href="/#how-it-works">How it works</a>
    <a href="/faq">FAQ</a>
    <a href="/about">About</a>
    <a href="/contact">Contact</a>
    <a class="btn" href="/sms-signup">Get started</a>
  </nav>
</div></header>
"""

_SITE_FOOTER = f"""
<footer class="footer">
  <div class="footer-in">
    <div>
      <div class="flogo">{_LOGO_SVG} Theo</div>
      <p class="fdesc">A learning coach in your text messages. Built and
      operated by Green Gables Studio LLC, an independent software studio
      in Mountain View, California.</p>
    </div>
    <div>
      <h4>Product</h4>
      <ul>
        <li><a href="/#how-it-works">How it works</a></li>
        <li><a href="/#why">Why it works</a></li>
        <li><a href="/sms-signup">Sign up</a></li>
        <li><a href="/faq">FAQ</a></li>
      </ul>
    </div>
    <div>
      <h4>Company</h4>
      <ul>
        <li><a href="/about">About</a></li>
        <li><a href="/contact">Contact</a></li>
      </ul>
    </div>
    <div>
      <h4>Contact</h4>
      <ul>
        <li>Green Gables Studio LLC</li>
        <li>2605 Miller Avenue, Unit 3401<br>Mountain View, CA 94040</li>
        <li><a href="tel:+16469063961">+1 (646) 906-3961</a></li>
        <li><a href="mailto:jeongmo.kwon@learningtheo.com">jeongmo.kwon@learningtheo.com</a></li>
      </ul>
    </div>
  </div>
  <div class="footer-bar"><div class="footer-bar-in">
    <div>© 2026 Green Gables Studio LLC. All rights reserved.</div>
    <div class="flinks">
      <a href="/privacy">Privacy Policy</a>
      <a href="/terms">Terms of Service</a>
      <a href="/sms-signup">SMS Consent</a>
    </div>
  </div></div>
</footer>
"""


def _site_page(title, body_html, desc=None, path="/", wide=False):
    """Shared page chrome for all public pages: nav header, meta/OG
    tags, favicon, footer with full business details. Static HTML,
    readable without JavaScript — carriers review these pages."""
    desc = desc or _SITE_DESC
    main_class = "wide" if wide else "doc"
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{desc}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:type" content="website">
<meta property="og:url" content="{_SITE_ORIGIN}{path}">
<meta property="og:image" content="{_SITE_ORIGIN}/site_assets/theo-og.png">
<meta name="twitter:card" content="summary_large_image">
<link rel="icon" type="image/svg+xml" href="/site_assets/favicon.svg">
<style>{_PAGE_CSS}</style>
</head><body>
{_SITE_NAV}
<main class="{main_class}">
{body_html}
</main>
{_SITE_FOOTER}
</body></html>"""


_LEGAL_PATHS = {"Privacy Policy": "/privacy",
                "Terms and Conditions": "/terms",
                "SMS Signup": "/sms-signup",
                "Screen Sharing Consent": "/screen-consent"}


def _legal_page(title, body_html):
    """Back-compat wrapper: legal/compliance pages get the same site
    chrome; their body content is unchanged."""
    return _site_page(f"{title} — Theo", body_html,
                      path=_LEGAL_PATHS.get(title, "/"))


# ─── Public landing page (/) ─────────────────────────────────────────
#
# The business-facing homepage carriers open during Toll-Free
# Verification review (rejection 30489: "website must be established
# and active" — the reviewer saw only the app shell). Static inline
# HTML, fully readable without JavaScript. The SMS-program wording
# here must stay consistent with /privacy, /terms, and /sms-signup
# and with the TFV submission (up to 4 msgs/day, HELP/STOP, data
# rates, web-form opt-in).

# Registered business address shown on the landing page. Must match
# the Twilio Business Profile (TFV round-2: business details on the
# website must match the verification submission).
_BUSINESS_ADDRESS = "2605 Miller Avenue, Unit 3401, Mountain View, CA 94040"

_BUSINESS_BLOCK = f"""
<div class="sms-box">
  <h2>Business &amp; contact information</h2>
  <p><strong>Legal name:</strong> Green Gables Studio LLC</p>
  <p><strong>Product / DBA:</strong> Theo</p>
  <p><strong>Address:</strong> {_BUSINESS_ADDRESS}</p>
  <p><strong>Phone:</strong> <a href="tel:+16469063961">+1 (646) 906-3961</a></p>
  <p><strong>Email:</strong>
  <a href="mailto:jeongmo.kwon@learningtheo.com">jeongmo.kwon@learningtheo.com</a></p>
  <p><strong>Website:</strong> <a href="/">www.learningtheo.com</a></p>
</div>
"""


async def _landing_handler(request):
    # Legacy fallback: early clients opened their WebSocket against /
    # instead of /ws, so the upgrade check stays with the root route.
    if request.headers.get("Upgrade", "").lower() == "websocket":
        print(f"[WS] Upgrade on / from {request.remote}", flush=True)
        return await ws_handler(request)

    body = f"""
<section class="hero">
  <div>
    <div class="eyebrow">Your AI learning coach</div>
    <h1>A learning coach in your text messages</h1>
    <p class="lead">You don't fail at learning because the material is
    too hard. You fail because sitting down is hard. Theo texts you at
    the moments you chose, gets you started with a step small enough to
    actually take, and helps you through it — right in your Messages
    app.</p>
    <div class="hero-ctas">
      <a class="btn" href="/sms-signup">Get started</a>
      <a class="btn btn-ghost" href="#how-it-works">See how it works</a>
    </div>
    <p class="hero-note">US phone numbers · Free to join · Consent
    required before any message is sent</p>
  </div>
  <div class="hero-visual">
    <div class="hero-photo">
      <img src="/site_assets/hero-home.jpg"
           alt="A Theo member at home in the evening, starting a study
           session from a text check-in">
      <div class="pthread">
        <div class="pchip">Evening check-in · 7:50 PM</div>
        <div class="pbubble in">Hi, it's Theo — your evening study
        check-in. Yesterday we agreed on a 15-minute session on your
        Python course. Ready to start now, or reply LATER to push it
        to tonight. Reply HELP for help or STOP to cancel.</div>
        <div class="pbubble out">ok, starting now</div>
      </div>
    </div>
  </div>
</section>

<div id="how-it-works">
<section class="hiw-row">
  <div class="hiw-copy">
    <div class="hiw-num">1</div>
    <div class="hiw-eyebrow">Sign up</div>
    <h3>Tell Theo what you're learning</h3>
    <p>Sign up with your goal — an online course, YouTube tutorials, a
    textbook, your own project — and agree on when Theo may text you.
    Text coaching is strictly opt-in: each message type has its own
    consent checkbox, and nothing is sent before you've said yes.</p>
    <p class="hiw-note">Every member is personally onboarded and gets
    hands-on attention from day one.</p>
  </div>
  <div class="hiw-visual">
    <div class="mock">
      <div class="mhead">Your coaching setup</div>
      <div class="row"><span class="k">Learning</span>
        <span class="v">Python for data analysis</span></div>
      <div class="row"><span class="k">This week's goal</span>
        <span class="v">Finish course section 3</span></div>
      <div class="row"><span class="k">Check-in window</span>
        <span class="v">Evenings, around 7:50 PM</span></div>
      <div class="row"><span class="k">Coaching check-ins</span>
        <span class="v ok">Consented ✓</span></div>
      <div class="row"><span class="k">Study support</span>
        <span class="v ok">Consented ✓</span></div>
    </div>
  </div>
</section>

<section class="hiw-row flip">
  <div class="hiw-copy">
    <div class="hiw-num">2</div>
    <div class="hiw-eyebrow">Coaching check-ins</div>
    <h3>Theo texts. You start.</h3>
    <p>At the times you agreed on, Theo checks in — while you're on the
    couch, walking home, anywhere your phone is. Not a nagging
    reminder: a conversation that ends with a first step small enough
    to start right now. That moment of starting is the whole
    point.</p>
    <p class="hiw-note">Up to 4 messages per day total; the actual
    rhythm follows your replies and your schedule.</p>
  </div>
  <div class="hiw-visual">
    <div class="mock">
      <div class="mhead">Theo · Messages</div>
      <div class="thread">
        <div class="tstamp">7:50 PM</div>
        <div class="msg in">Hi, it's Theo — your evening study check-in.
        Yesterday we agreed on a 15-minute session on your Python
        course. Ready to start now, or reply LATER to push it to
        tonight. Reply HELP for help or STOP to cancel.</div>
        <div class="msg out">ok, starting now</div>
      </div>
    </div>
  </div>
</section>

<section class="hiw-row">
  <div class="hiw-copy">
    <div class="hiw-num">3</div>
    <div class="hiw-eyebrow">Study support</div>
    <h3>Stuck? Text back.</h3>
    <p>While you study, Theo stays one text away. Hit an error, lose
    the thread, don't know what a sentence means — text it to Theo and
    get an answer that walks you through it step by step, using your
    own materials rather than replacing them.</p>
    <p class="hiw-note">Theo carries no course catalog — it coaches you
    through whatever you're already learning with.</p>
  </div>
  <div class="hiw-visual">
    <div class="mock">
      <div class="mhead">Theo · Messages</div>
      <div class="thread">
        <div class="tstamp">8:14 PM</div>
        <div class="msg out">getting a NameError in cell 2</div>
        <div class="msg in">Hi, it's Theo. That NameError usually means
        the variable isn't defined yet in this session. Try re-running
        the first cell, then tell me what output you see. Reply HELP
        for help or STOP to cancel.</div>
      </div>
    </div>
  </div>
</section>
</div>

<section class="section" id="why">
  <div class="centered">
    <h2>Why a coach in your texts?</h2>
    <p class="sub">Theo is built on a simple observation from months of
    hands-on coaching experiments: adults don't need more content —
    they need to start.</p>
  </div>
  <div class="cards">
    <div class="card">
      <h3>Starting is the real problem</h3>
      <p>Courses, tutorials, and textbooks are everywhere. What's rare
      is the nudge that turns "I should study tonight" into an open
      laptop. Theo is built entirely around that moment.</p>
    </div>
    <div class="card">
      <h3>No new app to open</h3>
      <p>Opening yet another app is itself a hurdle. Texts arrive where
      your attention already lives — no install, no login, no
      streak guilt.</p>
    </div>
    <div class="card">
      <h3>Grounded in behavioral science</h3>
      <p>Theo's coaching draws on established research — implementation
      intentions, self-determination theory, motivational
      interviewing — not badges and leaderboards.</p>
    </div>
  </div>
</section>

<section class="section">
  <div class="band" style="text-align:center;">
    <h2>Ready to actually start?</h2>
    <p style="max-width:520px; margin:8px auto 20px;">Tell Theo what
    you're learning, and let the check-ins do the rest.</p>
    <a class="btn" href="/sms-signup">Get started</a>
  </div>
</section>
"""
    return web.Response(text=_site_page("Theo — A learning coach in your text messages",
                                        body, path="/", wide=True),
                        content_type="text/html")


async def _about_handler(request):
    body = f"""
<h1>About</h1>
<div class="meta">Green Gables Studio LLC · Mountain View, California</div>

<p>Green Gables Studio LLC is an independent software studio that
designs and operates <strong>Theo</strong>, an AI learning coach
delivered by text message. The studio builds the whole service
in-house: the coaching program, the SMS service, and this
website.</p>

<h2>Why we are building Theo</h2>
<p>Theo comes out of a simple observation from our founder's own
years of self-directed learning: adults who study on their own rarely
fail because the material is too hard. They fail because sitting down
and starting is hard. Courses, tutorials, and textbooks are everywhere
— what's missing is the thing a good human coach provides: someone who
knows how <em>you</em> work, notices when you stall, and makes the
next step small enough to actually take.</p>

<p>Before writing a line of product code, our founder — Jeongmo Kwon,
a former iOS app developer and startup founder — spent months running
the coaching loop manually as a self-experiment: daily coaching
messages, real study sessions, and careful notes on what actually got
a tired adult to open the laptop and begin. Theo is that experiment,
productized: an AI coach that layers on top of whatever you are
already using to learn, rather than another content library.</p>

<h2>How Theo coaches</h2>
<ul>
  <li><strong>Start-first coaching.</strong> Theo's job begins before
      the study session: check-ins, tiny first steps, and
      conversations that lower the cost of starting.</li>
  <li><strong>Your materials, not ours.</strong> Theo carries no
      course catalog. It coaches you through your own course,
      tutorial, textbook, or project.</li>
  <li><strong>Grounded in behavioral science.</strong> The coaching
      approach draws on established research — self-determination
      theory, implementation intentions, and motivational
      interviewing — rather than streaks and badges.</li>
  <li><strong>Personal by design.</strong> Theo adapts its coaching
      to how each learner actually starts, stalls, and recovers.</li>
</ul>

<h2>Joining Theo</h2>
<p>Theo is deliberately high-touch: every member is personally
onboarded, and coaching is reviewed by a person, not left to run on
autopilot. Members join through our
<a href="/sms-signup">signup form</a> (US phone numbers);
text-message coaching is optional and requires explicit per-purpose
consent. Theo is currently free to use.</p>

{_BUSINESS_BLOCK}
"""
    return web.Response(
        text=_site_page("About — Theo", body,
                        desc=("Green Gables Studio LLC is the independent "
                              "software studio behind Theo, an AI learning "
                              "coach grounded in behavioral science."),
                        path="/about"),
        content_type="text/html")


async def _faq_handler(request):
    body = """
<h1>Frequently asked questions</h1>
<div class="meta">Theo · Green Gables Studio LLC</div>

<h2>About Theo</h2>

<h3>What is Theo?</h3>
<p>Theo is a personal AI learning coach that lives in your text
messages. It checks in at the times you agreed on to help you actually
start studying, and answers by text when you're stuck mid-session.</p>

<h3>Is Theo a course?</h3>
<p>No. Theo has no content library and doesn't replace your
materials. You bring whatever you are learning with — an online
course, YouTube tutorials, a textbook, your own project — and Theo
coaches you through it.</p>

<h3>Who can join?</h3>
<p>Anyone with a US mobile number. Leave your details on the
<a href="/sms-signup">signup form</a> and we'll reach out to get you
set up — every member is personally onboarded.</p>

<h3>How much does it cost?</h3>
<p>Theo is currently free to use, and consent to text messages is not
a condition of any purchase.</p>

<h3>What do I need to use Theo?</h3>
<p>A US mobile number that can receive text messages, and your own
opt-in on the signup form. That's it — there is no app to install.</p>

<h2>The SMS program</h2>

<h3>How do I sign up for text messages?</h3>
<p>On the <a href="/sms-signup">signup form</a>, each of the two
message programs — coaching check-ins and study support — has its own
separate, optional checkbox. You only receive the message types you
checked, and only after you submit the form. No messages are ever
sent without your explicit consent.</p>

<h3>Do I have to check the consent boxes to sign up?</h3>
<p>No. Both checkboxes are optional, and consent is not a condition
of signing up or of any purchase. You can sign up without them —
text coaching simply doesn't start until you've opted in.</p>

<h3>How many messages will I get?</h3>
<p>Up to 4 messages per day total across all Theo messages. The
actual number varies with your replies and your study schedule.</p>

<h3>Does it cost anything to receive messages?</h3>
<p>Theo doesn't charge for messages, but standard message and data
rates from your mobile carrier may apply.</p>

<h3>How do I stop messages?</h3>
<p>Reply <strong>STOP</strong> at any time. You'll get a single
confirmation and then no further messages. Reply
<strong>HELP</strong> at any time for help and contact
information.</p>

<h3>What happens to my information?</h3>
<p>We collect only what the signup form asks for, use it only to run
the service, and never sell, rent, or share mobile information with
third parties for marketing. Details are in our
<a href="/privacy">Privacy Policy</a> and
<a href="/terms">Terms of Service</a>.</p>
"""
    return web.Response(
        text=_site_page("FAQ — Theo", body,
                        desc=("Common questions about Theo, the AI learning "
                              "coach by Green Gables Studio LLC, and its "
                              "opt-in SMS coaching program."),
                        path="/faq"),
        content_type="text/html")


async def _contact_handler(request):
    body = f"""
<h1>Contact</h1>
<div class="meta">Green Gables Studio LLC</div>

<p>Questions about Theo or the SMS program? We'd love to hear from
you.</p>

<ul>
  <li><strong>Email:</strong>
      <a href="mailto:jeongmo.kwon@learningtheo.com">jeongmo.kwon@learningtheo.com</a>
      — we typically respond within 1&ndash;2 business days.</li>
  <li><strong>Phone:</strong> <a href="tel:+16469063961">+1 (646)
      906-3961</a></li>
  <li><strong>Mail:</strong> Green Gables Studio LLC,
      {_BUSINESS_ADDRESS}</li>
  <li><strong>SMS participants:</strong> reply <strong>HELP</strong>
      to any Theo message for help, or <strong>STOP</strong> to cancel
      at any time.</li>
</ul>

<p>Interested in joining Theo? Leave your details on the
<a href="/sms-signup">signup form</a>.</p>

{_BUSINESS_BLOCK}
"""
    return web.Response(
        text=_site_page("Contact — Theo", body,
                        desc=("Contact Green Gables Studio LLC, the studio "
                              "behind Theo — email, mailing address, and "
                              "SMS help."),
                        path="/contact"),
        content_type="text/html")


async def _privacy_handler(request):
    body = """
<h1>Privacy Policy</h1>
<div class="meta">Last updated: August 2026</div>

<p>Theo is an AI learning-coach service built and operated by Green
Gables Studio LLC. This policy describes how phone numbers, message
content, and study data are handled.</p>

<h2>What we collect</h2>
<ul>
  <li>Contact details submitted through our
      <a href="/sms-signup">signup form</a>: name, email address,
      and mobile phone number, together with the text-message consent
      choices made on that form.</li>
  <li>SMS messages exchanged with members, stored for the purpose of
      providing learning context to subsequent messages.</li>
  <li>Study materials and study-session data a member chooses to
      share (see "Study sessions and shared materials" below).</li>
  <li>Service usage data tied to the member's account.</li>
</ul>

<h2>How we use it</h2>
<p>Contact details and message content are used solely to operate the
service: delivering educational content, study check-ins, and coaching
conversations to members, and contacting members about their
enrollment. They are not used for marketing or advertising of any
kind. Text messages are sent only for the purposes a member has
separately consented to on the signup form.</p>

<h2>Service improvement and research</h2>
<p>We use de-identified and aggregated data — including message
content and service usage patterns with personal identifiers
removed — to improve Theo's coaching quality, evaluate which coaching
approaches work, and train and refine the models and prompts that
power the service. De-identified data cannot reasonably be linked
back to you. This use never includes selling your data or sharing it
for third parties' own purposes, and you may request deletion of your
data at any time.</p>

<h2>Study sessions and shared materials</h2>
<p>When you choose to share your screen during a study session,
captured frames are processed transiently to generate text
observations and are not stored. Documents and links you share are
stored as extracted text to power your coaching. You can end a
session at any time, and sharing is always initiated by you. Before
your first session we present a dedicated
<a href="/screen-consent">Screen Sharing Consent</a> describing
exactly what is captured and kept; your acceptance is recorded with
its date and document version.</p>

<h2>Sharing</h2>
<p><strong>We do not sell, rent, or share mobile information with third
parties or affiliates for marketing or promotional purposes.</strong>
We share data only with service providers that process it on our
behalf to run the service — such as our communications provider
(Twilio) for delivering messages, our model provider (Anthropic) for
generating message content, and our hosting and email providers —
under obligations of confidentiality. We never share data for third
parties' own purposes.</p>

<h2>Business transfers</h2>
<p>If Theo is involved in a merger, acquisition, or sale of assets,
your information may be transferred as part of that transaction,
subject to the commitments in this policy.</p>

<h2>Message frequency</h2>
<p>Message frequency varies and is capped at the limit the user agrees
to during onboarding (currently up to 4 messages per day). Users may
reduce this limit or pause messaging at any time by replying
<strong>STOP</strong>.</p>

<h2>STOP and HELP</h2>
<ul>
  <li>Reply <strong>STOP</strong> at any time to unsubscribe and stop
      receiving messages.</li>
  <li>Reply <strong>HELP</strong> at any time for assistance and contact
      information.</li>
  <li>Message and data rates may apply.</li>
</ul>

<h2>Retention</h2>
<p>Message history is retained for the duration the user maintains an
active account, and is deleted on request.</p>

<h2>Contact</h2>
<p>For privacy questions, contact
<a href="mailto:jeongmo.kwon@learningtheo.com">jeongmo.kwon@learningtheo.com</a>.</p>
"""
    return web.Response(text=_legal_page("Privacy Policy", body),
                        content_type="text/html")


async def _terms_handler(request):
    body = """
<h1>Terms and Conditions</h1>
<div class="meta">Last updated: August 2026</div>

<p>Theo is an early-stage AI learning-coach service operated
by Green Gables Studio LLC. By using the SMS service, you agree to the
following.</p>

<h2>What you'll receive</h2>
<p>You will receive SMS messages from Theo intended to deliver
educational content, study prompts, and motivational nudges related to
the topic you have chosen to study. Messages are generated by an AI
model and may occasionally contain mistakes.</p>

<h2>Message frequency</h2>
<p>Up to 4 messages per day. The actual frequency varies based on your
schedule and replies. You can lower or pause the frequency at any time
by replying STOP.</p>

<h2>Opt-in</h2>
<p>Consent to receive text messages is collected through our
<a href="/sms-signup">web signup form</a>, which offers a separate,
optional checkbox for each message type (coaching check-ins and
study support). You will only receive the message types
you have checked. Providing your phone number alone does not
constitute consent, and consent is not a condition of signing
up.</p>

<h2>Opt-out and help</h2>
<ul>
  <li>Reply <strong>STOP</strong> at any time to unsubscribe. You will
      receive a single confirmation message and then no further
      messages.</li>
  <li>Reply <strong>HELP</strong> at any time to receive a help reply
      with contact information.</li>
  <li>Message and data rates may apply.</li>
</ul>

<h2>Data use</h2>
<p>By using Theo, you agree that de-identified and aggregated service
data may be used to improve the service and develop the coaching
models behind it, as described in our
<a href="/privacy">Privacy Policy</a>.</p>

<h2>Disclaimer</h2>
<p>Theo is provided as-is for personal educational use. The
service makes no warranty as to the accuracy of generated content and
should not be used as a substitute for professional instruction in
fields where accuracy is critical.</p>

<h2>Privacy</h2>
<p>See our <a href="/privacy">Privacy Policy</a> for how your phone
number and messages are handled.</p>
"""
    return web.Response(text=_legal_page("Terms and Conditions", body),
                        content_type="text/html")


# ─── SMS opt-in signup form ──────────────────────────────────────────
#
# The compliant web opt-in form for the SMS coaching pilot. Serves two
# purposes: (1) the real consent-capture entry point for pilot users,
# (2) the public "opt-in policy proof" URL for Twilio's Toll-Free
# Verification review. Built to the carrier checklist AND the TFV
# round-2 rejection feedback (2026-07-25):
#   - one NOT-pre-checked checkbox PER messaging purpose (coaching
#     check-ins vs two-way learning support), each describing the
#     message type it covers;
#   - SMS consent is OPTIONAL: email is the primary signup field, the
#     submit button is always enabled, and the form states explicitly
#     that signup works without either box checked;
#   - frequency, data-rates, HELP/STOP, ToS/Privacy links kept.
# Submissions are consent records only — status stays 'pending' until
# the founder activates the user; the form never triggers messages by
# itself.

_FIELD_STYLE = ("margin-top:6px; padding:10px 12px; width:100%; "
                "max-width:340px; font-size:16px; border:1px solid #ccc; "
                "border-radius:8px;")


async def _sms_signup_page_handler(request):
    body = f"""
<h1>Sign up for Theo</h1>
<div class="meta">Green Gables Studio LLC</div>

<p>Leave your details to sign up. The text-message consent boxes
below are optional — you can sign up without checking either, and we
will contact you by email to get you set up.</p>

<form method="POST" action="/sms-signup" style="margin-top:20px">
  <label for="name" style="font-weight:600">Full Name</label><br>
  <input type="text" id="name" name="name"
         placeholder="Type your full name" autocomplete="name"
         style="{_FIELD_STYLE}">

  <div style="margin-top:14px">
  <label for="email" style="font-weight:600">Email *</label><br>
  <input type="email" id="email" name="email" required
         placeholder="Enter your email" autocomplete="email"
         style="{_FIELD_STYLE}">
  </div>

  <div style="margin-top:14px">
  <label for="phone" style="font-weight:600">Mobile Phone Number *</label><br>
  <input type="tel" id="phone" name="phone" required
         placeholder="(555) 123-4567" autocomplete="tel"
         style="{_FIELD_STYLE}">
  <div style="font-size:13px; color:#666; margin-top:4px">US mobile
  numbers only.</div>
  </div>

  <div style="margin-top:18px; display:flex; gap:10px; align-items:flex-start;">
    <input type="checkbox" id="consent_checkins" name="consent_checkins"
           value="yes" style="margin-top:4px; width:16px; height:16px;">
    <label for="consent_checkins">I consent to receive <strong>coaching
    check-in</strong> text messages from Theo (Green Gables Studio LLC)
    at the phone number provided — scheduled check-ins and reminders
    about my study plan. Up to 4 messages per day total across all Theo
    messages; actual frequency varies with my replies. Message and data
    rates may apply. Reply HELP for help or STOP to cancel at any
    time.</label>
  </div>

  <div style="margin-top:12px; display:flex; gap:10px; align-items:flex-start;">
    <input type="checkbox" id="consent_support" name="consent_support"
           value="yes" style="margin-top:4px; width:16px; height:16px;">
    <label for="consent_support">I consent to receive <strong>study
    support</strong> text messages from Theo (Green Gables Studio LLC)
    at the phone number provided — replies that help me step by step
    when I text Theo during a study session. Up to 4 messages per day
    total across all Theo messages; actual frequency varies with my
    replies. Message and data rates may apply. Reply HELP for help or
    STOP to cancel at any time.</label>
  </div>

  <p style="margin-top:16px"><strong>Consent is optional:</strong> each
  checkbox above is a separate, optional consent for that message type
  only, and you can complete this signup without checking either.
  Consent is not required to use Theo and is not a condition of any
  purchase.</p>

  <p>By signing up, you agree to our
  <a href="/terms">Terms of Service</a> and
  <a href="/privacy">Privacy Policy</a>.</p>

  <button type="submit"
          style="margin-top:12px; padding:12px 28px; font-size:16px;
                 border:none; border-radius:999px; background:#1b6e47;
                 color:#fff; cursor:pointer;">
    Sign me up</button>
</form>
"""
    return web.Response(text=_legal_page("SMS Signup", body),
                        content_type="text/html")


def _normalize_us_phone(raw):
    """'(555) 123-4567' / '5551234567' / '15551234567' → '+15551234567'.
    Returns None if it doesn't parse as a US number."""
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return None


def _signup_error(msg):
    body = f"""
<h1>Hmm — something didn't look right</h1>
<p>{msg}</p>
<p><a href="/sms-signup">← Back to signup</a></p>
"""
    return web.Response(status=400, text=_legal_page("SMS Signup", body),
                        content_type="text/html")



# ─── /my — the user's private space (magic-link auth) ────────────────
#
# Possession of the link IS the login: /my?k=<token> (PR 2 of the
# walkthrough arc). Here the user shows Theo what they study from —
# a file upload or a link — so the Theo-led walkthrough has something
# real to anchor on. The conversation itself stays on SMS.

def _my_auth(request):
    """→ (user_id, token) or (None, None)."""
    import db
    token = (request.query.get("k") or "").strip()
    return db.get_user_id_by_token(token), token


_MY_STATUS_LABEL = {"none": "shared — walkthrough not started",
                    "in_progress": "walkthrough in progress",
                    "validated": "walked through ✓"}


SCREEN_CONSENT_VERSION = "2026-08-05"


async def _screen_consent_page_handler(request):
    body = f"""
<h1>Screen Sharing Consent</h1>
<div class="meta">Version {SCREEN_CONSENT_VERSION} · Green Gables
Studio LLC</div>

<p>Theo offers optional <b>study sessions</b>: you share your screen
from your laptop so Theo can look at your study material with you
while you talk. This document describes exactly what that involves.
It is shown to you before your first session, and your acceptance is
recorded with its date and this document's version.</p>

<h2>What is captured, and when</h2>
<ul>
<li>Sessions only ever start when <b>you</b> click "화면 공유 시작"
and choose what to share in your browser's picker. Theo can never
open a session by itself.</li>
<li>During a session, still frames of the shared screen are captured
at meaningful moments — when you switch windows, when you stop
scrolling, when you stay on one spot for a while, and when you send
a chat message. This is <b>not continuous video</b>. No audio, no
camera, and no keystrokes are captured.</li>
<li>A visible indicator ("Theo가 보는 중") is shown for the whole
session, and your browser shows its own sharing indicator too.</li>
</ul>

<h2>What happens to the frames</h2>
<ul>
<li>Each captured frame is sent, over an encrypted connection, to
our AI model provider (Anthropic) to produce a short <b>written
observation</b> of what is on screen — for example, which section of
your document you are reading.</li>
<li>The frame image is <b>discarded immediately after it is read</b>.
It is never written to disk and never stored. What remains is the
written observation only.</li>
</ul>

<h2>What we keep</h2>
<ul>
<li>The written observations from each session (text).</li>
<li>Session records: when a session started and ended, and how many
frames were captured.</li>
<li>Your chat messages with Theo during the session, as part of your
ongoing conversation history.</li>
<li>Materials you upload separately are kept as extracted text, as
described in our <a href="/privacy">Privacy Policy</a>.</li>
</ul>

<h2>Your control</h2>
<ul>
<li>End a session at any time with the 세션 종료 button; closing the
tab or laptop also ends it within about a minute.</li>
<li>You can simply never start a session — every other part of Theo
works without screen sharing.</li>
<li>You may request deletion of your session observations and any
other data at any time by replying to any Theo message or emailing
<a href="mailto:jeongmo.kwon@learningtheo.com">jeongmo.kwon@learningtheo.com</a>.</li>
</ul>

<p class="meta">This consent supplements our
<a href="/privacy">Privacy Policy</a> and
<a href="/terms">Terms</a>. If this document materially changes, you
will be asked to review and accept the new version before your next
session.</p>
"""
    return web.Response(text=_legal_page("Screen Sharing Consent", body),
                        content_type="text/html")


async def _session_consent_handler(request):
    """Record the just-in-time acceptance (version-stamped)."""
    import db
    user_id, _tok = _my_auth(request)
    if not user_id:
        return web.Response(status=404, text="Not found")
    db.record_consent(user_id, "screen_share", SCREEN_CONSENT_VERSION)
    return web.json_response({"ok": True})


async def _signups_handler(request):
    """Operator-only: the activation inbox — pending signups with the
    exact command to activate each.

    GET /debug/signups?secret=...
    """
    import db
    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (request.headers.get("X-Cron-Secret", "").strip()
                or request.query.get("secret", "").strip())
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")
    rows = db.get_pending_signups()
    if not rows:
        return web.Response(text="(no pending signups)\n",
                            content_type="text/plain")
    lines = []
    for r in rows:
        lines.append(
            f"#{r['id']} {r.get('name') or '(no name)'} "
            f"{r['phone']} {r.get('email') or '(no email)'} "
            f"sms_consent={'Y' if r.get('consent_checkins') else 'N'} "
            f"at {r['consented_at']}\n"
            f"  activate: POST /debug/activate?secret=...&signup_id="
            f"{r['id']}&user_id=<choose-a-slug>")
    return web.Response(text="\n".join(lines) + "\n",
                        content_type="text/plain")


async def _activate_handler(request):
    """Operator-only: promote a pending signup to a live user (M3).
    One click does everything a new user needs:

      profile row + phone binding + email + magic-link token +
      welcome email (their /my link) + signup marked active +
      user_activated event

    From that moment the cron fan-out serves them: the next evening
    slot opens their onboarding conversation.

    POST /debug/activate?secret=..&signup_id=3&user_id=grace1
    """
    import db
    import emailer
    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (request.headers.get("X-Cron-Secret", "").strip()
                or request.query.get("secret", "").strip())
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")
    sid = (request.query.get("signup_id") or "").strip()
    user_id = (request.query.get("user_id") or "").strip()
    if not sid.isdigit() or not user_id.isalnum():
        return web.Response(status=400,
                            text="signup_id + alphanumeric user_id required")
    row = db.get_sms_signup(int(sid))
    if not row:
        return web.Response(status=404, text="no such signup")
    if row["status"] != "pending":
        return web.Response(status=409,
                            text=f"signup #{sid} is {row['status']}, not pending")
    if not row.get("consent_checkins"):
        # The signup form allows submitting without SMS consent; a
        # user without the checked box may NOT be texted. Refusing
        # here keeps the carrier promise structural, not procedural.
        return web.Response(status=412,
                            text="no SMS consent on this signup — cannot "
                                 "activate for texting")
    if db.get_user_profile_by_id(user_id):
        return web.Response(status=409, text=f"user {user_id} exists")
    try:
        db.ensure_user_profile_row(user_id)
        db.set_user_phone(user_id, row["phone"], source="activation")
        if (row.get("name") or "").strip():
            db.set_user_name(user_id, row["name"], source="activation")
    except ValueError as e:
        return web.Response(status=409, text=str(e))
    email_line = ""
    if (row.get("email") or "").strip():
        ok, detail = emailer.send_welcome(user_id, row["email"].strip())
        email_line = (f"welcome email: sent ({detail})" if ok
                      else f"welcome email FAILED: {detail}")
    db.set_signup_status(int(sid), "active")
    db.log_event(user_id, "user_activated",
                 {"signup_id": int(sid), "phone": row["phone"],
                  "name": row.get("name") or "",
                  "email_sent": bool(email_line.startswith("welcome email: sent"))},
                 source="operator")
    return web.Response(
        text=(f"activated {user_id}: {row['phone']}\n{email_line}\n"
              f"the next evening cron opens their onboarding.\n"))


async def _reset_user_handler(request):
    """Operator-only, DESTRUCTIVE: wipe a user back to birth (keeps
    phone + email + magic token; everything else gone, consent
    included). Requires confirm=<user_id> typed again.

    POST /debug/reset-user?secret=..&user_id=jeongmo&confirm=jeongmo
    """
    import db
    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (request.headers.get("X-Cron-Secret", "").strip()
                or request.query.get("secret", "").strip())
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")
    user_id = (request.query.get("user_id") or "").strip()
    confirm = (request.query.get("confirm") or "").strip()
    if not user_id or confirm != user_id:
        return web.Response(
            status=400,
            text="confirm=<user_id> must be repeated exactly — this "
                 "wipes the user's entire history\n")
    counts = db.reset_user(user_id)
    wiped = {k: v for k, v in counts.items()
             if isinstance(v, int) and v}
    return web.Response(
        text=f"reset {user_id} — kept phone/email/token; wiped: "
             f"{json.dumps(wiped)}\n")


async def _bind_phone_handler(request):
    """Operator-only: bind a phone number to a user (CRON_SECRET).
    The multi-user backfill path — and the guard against silent
    rebinding lives in db.set_user_phone.

    POST /debug/bind-phone?secret=..&user_id=chrisyu2&phone=%2B1555...
    """
    import db
    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (request.headers.get("X-Cron-Secret", "").strip()
                or request.query.get("secret", "").strip())
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")
    user_id = (request.query.get("user_id") or "").strip()
    phone = (request.query.get("phone") or "").strip()
    if not user_id or not phone.startswith("+"):
        return web.Response(status=400,
                            text="user_id + phone (E.164, URL-encode the +) required")
    try:
        db.set_user_phone(user_id, phone)
    except ValueError as e:
        return web.Response(status=409, text=str(e))
    return web.Response(text=f"bound {phone} -> {user_id}\n")


async def _my_token_handler(request):
    """Operator-only: mint/fetch a user's magic link (CRON_SECRET
    auth, same convention as the other debug endpoints). This is how
    the husband's link gets generated after deploy.

    GET /debug/my-link?secret=...&user_id=chrisyu2
    """
    import db
    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (request.headers.get("X-Cron-Secret", "").strip()
                or request.query.get("secret", "").strip())
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")
    user_id = (request.query.get("user_id") or "").strip()
    if not user_id:
        return web.Response(status=400, text="user_id required")
    token = db.ensure_user_token(user_id)
    return web.Response(
        text=f"https://www.learningtheo.com/my?k={token}\n",
        content_type="text/plain")


async def _email_my_link_handler(request):
    """Operator-only: have Theo email the user their /my link — the
    real product flow (signup → email → coach points at it), being
    validated on pilot #1 instead of hand-delivering the URL.

    POST /debug/email-my-link?secret=...&user_id=chrisyu2&email=a@b.c
    (email optional once stored on the profile)
    """
    import db
    import emailer
    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (request.headers.get("X-Cron-Secret", "").strip()
                or request.query.get("secret", "").strip())
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")
    user_id = (request.query.get("user_id") or "").strip()
    if not user_id:
        return web.Response(status=400, text="user_id required")
    email = (request.query.get("email") or "").strip()
    if not email:
        prof = db.get_user_profile_by_id(user_id) or {}
        email = (prof.get("email") or "").strip()
    if "@" not in email:
        return web.Response(status=400,
                            text="email required (none on profile)")
    ok, detail = emailer.send_my_link(user_id, email)
    return web.Response(
        status=200 if ok else 502,
        text=(f"sent to {email} (resend id {detail})\n" if ok
              else f"FAILED: {detail}\n"))


# ── screen co-viewing session endpoints (PR A: perception only) ──────

async def _session_start_handler(request):
    import db
    user_id, token = _my_auth(request)
    if not user_id:
        return web.Response(status=404, text="Not found")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not db.has_consent(user_id, "screen_share",
                          SCREEN_CONSENT_VERSION):
        return web.json_response(
            {"consent_required": True,
             "version": SCREEN_CONSENT_VERSION}, status=428)
    declared = (str(body.get("source") or "")).strip()[:300]
    sid = db.start_screen_session(user_id, declared_source=declared)
    # The coach acknowledges presence the moment the session opens —
    # a server template, not a model call: instant, and pinned to the
    # understated register ("오 보인다—" enthusiasm reads as creepy
    # when the subject is your screen). It still joins the one
    # thread, marked template so analysis knows no model chose it.
    greeting = "세션 시작했네. 보고 있을게."
    db.save_sms_message(user_id, "assistant", greeting, "out",
                        channel="web")
    db.log_event(user_id, "web_out",
                 {"text": greeting, "session_id": sid,
                  "steps": [{"tag": "connect", "intensity": 1}],
                  "template": "session_greeting"}, source="web")
    return web.json_response({"session_id": sid, "greeting": greeting})


async def _session_heartbeat_handler(request):
    import db
    user_id, _tok = _my_auth(request)
    if not user_id:
        return web.Response(status=404, text="Not found")
    body = await request.json()
    ssn = db.get_screen_session((body.get("session_id") or "").strip())
    if not ssn or ssn["user_id"] != user_id:
        return web.Response(status=404, text="no session")
    db.touch_screen_session(ssn["session_id"])
    return web.json_response({"ok": True})


async def _session_frame_handler(request):
    """One captured frame. The bytes live in this handler and the
    eyes call, and nowhere else — never written to disk."""
    import asyncio

    import db
    import eyes
    user_id, _tok = _my_auth(request)
    if not user_id:
        return web.Response(status=404, text="Not found")
    body = await request.json()
    ssn = db.get_screen_session((body.get("session_id") or "").strip())
    if not ssn or ssn["user_id"] != user_id or ssn["ended_at"]:
        return web.Response(status=404, text="no session")
    event = (body.get("event") or "unknown").strip()[:32]
    try:
        jpeg = _b64.b64decode(body.get("jpeg_b64") or "")
    except Exception:
        return web.Response(status=400, text="bad frame")
    if not jpeg or len(jpeg) > 4 * 1024 * 1024:
        return web.Response(status=400, text="bad frame size")
    db.touch_screen_session(ssn["session_id"])
    asyncio.get_event_loop().run_in_executor(
        None, eyes.read_frame, user_id, ssn["session_id"], jpeg, event,
        ssn.get("declared_source") or "")
    return web.json_response({"ok": True})


async def _session_message_handler(request):
    """One web-chat turn. The message may carry the current frame
    (grabbed at send time — ~1s freshness). The heavy work runs in a
    thread so the event loop stays free; the HTTP response carries
    the coach's reply."""
    import asyncio

    import db
    import sms as sms_mod
    user_id, _tok = _my_auth(request)
    if not user_id:
        return web.Response(status=404, text="Not found")
    body = await request.json()
    ssn = db.get_screen_session((body.get("session_id") or "").strip())
    if not ssn or ssn["user_id"] != user_id or ssn["ended_at"]:
        return web.Response(status=404, text="no session")
    text = (str(body.get("text") or "")).strip()[:2000]
    if not text:
        return web.Response(status=400, text="empty message")
    jpeg = None
    if body.get("jpeg_b64"):
        try:
            jpeg = _b64.b64decode(body["jpeg_b64"])
            if len(jpeg) > 4 * 1024 * 1024:
                jpeg = None
        except Exception:
            jpeg = None
    db.touch_screen_session(ssn["session_id"])
    reply = await asyncio.get_event_loop().run_in_executor(
        None, sms_mod.generate_web_reply, user_id, ssn["session_id"],
        text, jpeg)
    if not reply:
        return web.json_response(
            {"reply": "…잠깐 말이 엉켰다. 다시 한 번만 보내줄래?"})
    return web.json_response({"reply": reply})


async def _session_stream_handler(request):
    """Streaming web-chat turn. First tokens reach the client in
    ~1-2s; the full turn still takes what it takes, but arrives at
    reading speed instead of as an 8-second silence.

    Markers ([STEP:]/[EXPECT:]/[IGNITION:]) stream at the END of the
    model's output and must never reach the user's eyes: a sliding
    HOLDBACK of 160 chars is kept unflushed, so trailing markers stay
    server-side; finish_web_turn then strips them from the stored
    text and the final SSE event carries the clean full text (the
    client swaps its accumulated text for it).
    """
    import asyncio
    import queue as queue_mod

    import db
    import sms as sms_mod
    user_id, _tok = _my_auth(request)
    if not user_id:
        return web.Response(status=404, text="Not found")
    body = await request.json()
    ssn = db.get_screen_session((body.get("session_id") or "").strip())
    if not ssn or ssn["user_id"] != user_id or ssn["ended_at"]:
        return web.Response(status=404, text="no session")
    text = (str(body.get("text") or "")).strip()[:2000]
    if not text:
        return web.Response(status=400, text="empty message")
    jpeg = None
    if body.get("jpeg_b64"):
        try:
            jpeg = _b64.b64decode(body["jpeg_b64"])
            if len(jpeg) > 4 * 1024 * 1024:
                jpeg = None
        except Exception:
            jpeg = None
    db.touch_screen_session(ssn["session_id"])

    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache"})
    await resp.prepare(request)
    loop = asyncio.get_event_loop()
    q = queue_mod.Queue()
    HOLDBACK = 160

    def produce():
        try:
            system_prompt, history, versions = sms_mod.build_web_turn(
                user_id, ssn["session_id"], text, jpeg)
            client = anthropic.Anthropic()
            full, flushed = "", 0
            with client.messages.stream(
                    model=sms_mod.MODEL, max_tokens=600,
                    system=system_prompt, messages=history) as stream:
                for delta in stream.text_stream:
                    full += delta
                    safe = len(full) - HOLDBACK
                    if safe > flushed:
                        q.put(("delta", full[flushed:safe]))
                        flushed = safe
            final = sms_mod.finish_web_turn(
                user_id, ssn["session_id"], full, system_prompt,
                history, versions)
            q.put(("done", final or "(빈 응답)"))
        except Exception as e:
            print(f"[SESSION] ⚠️ stream turn failed: {e}", flush=True)
            q.put(("done", "…잠깐 말이 엉켰다. 다시 한 번만 보내줄래?"))

    fut = loop.run_in_executor(None, produce)
    try:
        while True:
            kind, payload = await loop.run_in_executor(None, q.get)
            await resp.write(
                f"event: {kind}\ndata: {json.dumps(payload)}\n\n"
                .encode())
            if kind == "done":
                break
    finally:
        await fut
    await resp.write_eof()
    return resp


async def _material_admin_handler(request):
    """Operator-only material correction (CRON_SECRET). First use:
    a live chat test's banter was mis-extracted into walkthrough
    wants on the founder's own account — pipeline noise must be
    erasable without SQL access.

    POST /debug/material?secret=..&id=1&action=clear_walkthrough
    """
    import db
    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (request.headers.get("X-Cron-Secret", "").strip()
                or request.query.get("secret", "").strip())
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")
    mid = request.query.get("id", "").strip()
    action = request.query.get("action", "").strip()
    m = db.get_material(int(mid)) if mid.isdigit() else None
    if not m:
        return web.Response(status=404, text="no material")
    if action == "clear_walkthrough":
        db.update_material_walkthrough(
            int(mid), user_description="", wants=[], status="none",
            source="admin")
        return web.Response(text=f"cleared walkthrough on material {mid} "
                                 f"({m.get('title')})\n")
    return web.Response(status=400, text="unknown action")


async def _prompt_preview_handler(request):
    """Operator-only (CRON_SECRET): assemble the EXACT prompt a
    scheduled send would put in front of the model for a real user,
    without sending, recording, or logging anything. Read-only —
    in drill preview mode no prediction is written (predictions
    never render into the prompt, so the preview is byte-identical
    to a real send's assembly).

    GET /debug/prompt-preview?secret=..&user_id=chrisyu2&slot=morning
    """
    import db
    import drill
    import sms
    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (request.headers.get("X-Cron-Secret", "").strip()
                or request.query.get("secret", "").strip())
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")
    user_id = request.query.get("user_id", "").strip()
    slot = request.query.get("slot", "morning").strip()
    if not user_id:
        return web.Response(status=400, text="user_id required")

    drill_ctx = None
    if slot in ("morning", "evening", "nudge"):
        try:
            drill_ctx = drill.prepare_scheduled_question(
                user_id, record=False)
        except Exception as e:
            return web.Response(
                status=500, text=f"drill prepare failed: {e}")
    if drill_ctx:
        trigger = f"cron_{slot}_drill"
        system_prompt, versions = sms._build_drill_prompt(
            user_id, drill_ctx)
    else:
        trigger = f"cron_{slot}"
        system_prompt, versions = sms._build_system_prompt(
            slot, user_id)
    if system_prompt is None:
        return web.Response(text=f"(slot {slot}: no prompt for this "
                                 f"user's state — the send would "
                                 f"skip)")

    phase_state = db.get_user_phase(user_id)
    history = db.get_recent_sms_messages(
        user_id, limit=sms.HISTORY_LIMIT,
        since=phase_state["phase_started_at"], with_time=True)
    if not history:
        history = [sms._server_turn(
            f"The scheduled {slot} send is firing and there is no "
            f"prior thread with this user — this is your first "
            f"message to them. Write it.")]
    elif history[-1]["role"] == "assistant":
        history.append(sms._server_turn(
            f"The scheduled {slot} send is firing. The user has not "
            f"written since the last turn above. Write the next "
            f"message."))

    bar = "═" * 60
    out = (f"# prompt preview — user: {user_id}, trigger: {trigger}, "
           f"model: {sms.MODEL}\n"
           f"# prompt versions: "
           f"{json.dumps(versions, ensure_ascii=False)}\n\n"
           f"{bar}\nSYSTEM PROMPT\n{bar}\n\n{system_prompt}\n\n"
           f"{bar}\nMESSAGES\n{bar}\n\n"
           + json.dumps(history, ensure_ascii=False, indent=1))
    return web.Response(text=out, content_type="text/plain",
                        charset="utf-8")


async def _track_admin_handler(request):
    """Operator-only track edits (CRON_SECRET). First use: renaming
    the imported track — '회사 PDF' was an operator placeholder and
    the name renders into every drill prompt.

    POST /debug/track?secret=..&id=1&action=rename&name=<url-encoded>
    """
    import db
    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (request.headers.get("X-Cron-Secret", "").strip()
                or request.query.get("secret", "").strip())
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")
    tid = request.query.get("id", "").strip()
    action = request.query.get("action", "").strip()
    if not tid.isdigit():
        return web.Response(status=400, text="numeric id required")
    if action == "rename":
        name = request.query.get("name", "").strip()
        if not name:
            return web.Response(status=400, text="name required")
        old = db.rename_track(int(tid), name)
        if old is None:
            return web.Response(status=404, text="no track")
        return web.Response(text=f"track {tid}: '{old}' → '{name}'\n")
    return web.Response(status=400, text="unknown action")


async def _ledger_import_handler(request):
    """Operator-only v3 ledger backfill (CRON_SECRET). The rehearsal
    replayed a user's history offline and produced the ledgers as
    review documents; once approved, this pushes them into production
    so v3 starts warm instead of re-deriving months of signal.

    POST /debug/import-ledgers?secret=..
    body: {"user_id": .., "track": {"name", "mode", "authority",
           "exam_date", "performance_stage"},
           "items": [..], "attempts": [..], "taught": [..],
           "person_notes": [..]}
    Refuses a duplicate track name for the user — re-running an
    import must not double every ledger row.
    """
    import db
    expected = os.environ.get("CRON_SECRET", "").strip()
    provided = (request.headers.get("X-Cron-Secret", "").strip()
                or request.query.get("secret", "").strip())
    if not expected or provided != expected:
        return web.Response(status=403, text="bad secret")
    body = await request.json()
    user_id = (body.get("user_id") or "").strip()
    track = body.get("track") or {}
    name = (track.get("name") or "").strip()
    if not user_id or not name:
        return web.Response(status=400,
                            text="user_id and track.name required")
    if any(t["name"] == name for t in db.get_tracks(user_id)):
        return web.Response(
            status=409, text=f"track '{name}' already exists for "
                             f"{user_id} — import refused")
    track_id = db.create_track(
        user_id, name, mode=track.get("mode", "drill"),
        authority=track.get("authority", "file_wins"),
        exam_date=track.get("exam_date", ""),
        performance_stage=track.get("performance_stage", ""),
        source="backfill")
    counts = {"items": 0, "attempts": 0, "taught": 0,
              "person_notes": 0}
    skipped = []
    for it in body.get("items") or []:
        try:
            db.add_knowledge_item(
                track_id, user_id, stem=it.get("stem", ""),
                anchor_type=it.get("anchor_type", "file_chunk"),
                anchor_quote=it.get("anchor_quote", ""),
                section_hint=it.get("section_hint", ""),
                elements=it.get("elements"),
                kind=it.get("kind", ""),
                est_difficulty=it.get("est_difficulty", 2),
                source="backfill")
            counts["items"] += 1
        except ValueError as e:
            skipped.append(f"item '{it.get('stem', '')[:40]}': {e}")
    for a in body.get("attempts") or []:
        db.record_attempt(
            track_id, user_id, verdict=a.get("verdict", ""),
            question=a.get("question", ""),
            answer_verbatim=a.get("answer_verbatim", ""),
            elements=a.get("elements"),
            source=a.get("source", "drill"),
            self_confidence=a.get("self_confidence", ""),
            confidence_marker=a.get("confidence_marker", ""),
            note=a.get("note", ""), ts=a.get("ts"))
        counts["attempts"] += 1
    for t in body.get("taught") or []:
        db.add_taught(track_id, user_id, quote=t.get("quote", ""),
                      teaching=t.get("teaching", ""),
                      kind=t.get("kind", ""),
                      conflict_flag=t.get("conflict_flag", ""),
                      ts=t.get("ts"))
        counts["taught"] += 1
    for p in body.get("person_notes") or []:
        db.add_person_note(user_id, p.get("observation", ""),
                           evidence=p.get("evidence", ""),
                           confidence=p.get("confidence", "low"),
                           ts=p.get("ts"))
        counts["person_notes"] += 1
    db.log_event(user_id, "ledgers_imported",
                 {"track_id": track_id, **counts,
                  "skipped": len(skipped)}, source="admin")
    return web.json_response({"ok": True, "track_id": track_id,
                              "counts": counts, "skipped": skipped})


async def _session_stop_handler(request):
    import db
    user_id, _tok = _my_auth(request)
    if not user_id:
        return web.Response(status=404, text="Not found")
    body = await request.json()
    sid = (body.get("session_id") or "").strip()
    ssn = db.get_screen_session(sid)
    if not ssn or ssn["user_id"] != user_id:
        return web.Response(status=404, text="no session")
    closed = db.end_screen_session(sid, reason="user")
    closing = ""
    if closed:
        closing = "오늘 세션은 여기까지 기록해뒀어."
        db.save_sms_message(user_id, "assistant", closing, "out",
                            channel="web")
        db.log_event(user_id, "web_out",
                     {"text": closing, "session_id": sid,
                      "steps": [{"tag": "release", "intensity": 1}],
                      "template": "session_closing"}, source="web")
    return web.json_response({"ok": True, "closing": closing})


async def _my_page_handler(request):
    import db
    user_id, token = _my_auth(request)
    if not user_id:
        return web.Response(status=404, text="Not found")
    consented = db.has_consent(user_id, "screen_share",
                               SCREEN_CONSENT_VERSION)
    rows = db.get_user_materials(user_id)
    items = ""
    for m in rows:
        what = (f'<a href="{m["source_url"]}" rel="noopener">{m["title"]}</a>'
                if m["kind"] == "link" else m["title"])
        state = _MY_STATUS_LABEL.get(m["walkthrough_status"],
                                     m["walkthrough_status"])
        note = ("" if m["kind"] != "file" else
                (" · Theo has read it" if m["digest"]
                 else " · Theo is reading it…"))
        items += f"<li>{what} <span class='meta'>— {state}{note}</span></li>\n"
    saved = ""
    if request.query.get("ok"):
        saved = ("<p style='color:#2a7d2a; font-weight:600'>Got it — "
                 "Theo is reading it now and will text you when "
                 "it's done.</p>")
    err = ""
    if request.query.get("err"):
        err = (f"<p style='color:#b00020; font-weight:600'>"
               f"{request.query.get('err')}</p>")
    body = f"""
<h1>Your learning space</h1>
<div class="meta">Private page — anyone with this link can see it,
so don't share the address.</div>
{saved}{err}
<h2 style="margin-top:26px">What you're learning from</h2>
<p>Show Theo the thing you actually study from — the file you made,
or the link you keep coming back to. Theo reads it once, then talks
it through with you over text.</p>
<ul>{items or "<li class='meta'>(nothing shared yet)</li>"}</ul>

<h3 style="margin-top:22px">Share a file</h3>
<form method="POST" action="/my/upload?k={token}"
      enctype="multipart/form-data">
  <input type="file" name="file" accept=".pdf,.docx" required>
  <div style="font-size:13px; color:#666; margin-top:4px">PDF or
  DOCX, up to 20MB. We keep the text, not the file itself.</div>
  <button class="btn" type="submit" style="margin-top:10px">Upload</button>
</form>

<h3 style="margin-top:22px">Or share a link</h3>
<form method="POST" action="/my/link?k={token}">
  <input type="url" name="url" required placeholder="https://…"
         style="{_FIELD_STYLE}">
  <input type="text" name="title" placeholder="What is it? (optional)"
         style="{_FIELD_STYLE}">
  <button class="btn" type="submit" style="margin-top:10px">Add link</button>
</form>

<h2 style="margin-top:30px">Study together — share your screen</h2>
<p>Start a session and open what you're studying. Theo watches with
you — it captures a frame only when something meaningful happens
(you switch, you settle, you linger), reads it once, and <b>deletes
the image immediately</b>. Only the written observation is kept.</p>
<div id="ssn-controls">
  <input type="text" id="ssn-source" placeholder="오늘 뭘로 공부해? (선택)"
         style="{_FIELD_STYLE}; max-width:420px">
  <button class="btn" id="ssn-start" style="margin-top:10px">화면 공유 시작</button>
</div>
<div id="ssn-consent" style="display:none; margin-top:12px; padding:16px 18px;
     border:1px solid #ccc; border-radius:10px; background:#fafafa">
  <div style="font-weight:700">시작 전에 한 가지만 — Theo가 화면을 어떻게 보는지</div>
  <ul style="font-size:13.5px; line-height:1.7; margin:10px 0; padding-left:18px">
    <li>세션은 <b>네가 시작할 때만</b> 열리고, 보는 동안 표시등이 항상 떠 있어요.</li>
    <li>연속 녹화가 아니라 <b>의미 있는 순간의 정지 화면</b>만 캡처돼요 (창 전환,
    스크롤 멈춤 등). 소리·카메라·키보드는 안 봐요.</li>
    <li>캡처된 화면은 AI가 <b>읽는 즉시 삭제</b>되고, 글로 된 관찰만 남아요.</li>
    <li>언제든 종료할 수 있고, 삭제 요청도 언제든 가능해요.</li>
  </ul>
  <div class="meta">자세한 내용:
  <a href="/screen-consent" target="_blank" rel="noopener">Screen Sharing
  Consent</a> (동의하면 이 문서 버전과 시각이 기록됩니다)</div>
  <button class="btn" id="ssn-agree" style="margin-top:12px">동의하고 시작</button>
</div>
<div id="ssn-live" style="display:none; margin-top:12px; padding:14px 18px;
     border:2px solid #e8590c; border-radius:10px; background:#fff4ec">
  <div style="font-weight:700; color:#e8590c">● Theo가 보는 중</div>
  <div id="ssn-status" class="meta" style="margin-top:6px">시작 중…</div>
  <div class="meta" style="margin-top:6px">이 창은 옆에 작게 띄워둬도 돼요.
  화면 원본은 읽는 즉시 삭제됩니다.</div>
  <div id="chat-log" style="margin-top:12px; max-height:46vh; overflow-y:auto;
       display:flex; flex-direction:column; gap:8px"></div>
  <div style="display:flex; gap:8px; margin-top:10px">
    <input type="text" id="chat-in" placeholder="Theo에게 말하기…"
           style="flex:1; padding:10px 12px; border:1px solid #ccc;
                  border-radius:8px; font-size:14px">
    <button class="btn" id="chat-send">전송</button>
  </div>
  <button class="btn" id="ssn-stop" style="margin-top:10px">세션 종료</button>
</div>
<script>
(function () {{
  var K = new URLSearchParams(location.search).get("k");
  var CONSENTED = {str(consented).lower()};
  var sid = null, stream = null, video, small, sctx, prev = null;
  var lastUpload = 0, lastActivity = 0, lastBig = 0, dwelled = false;
  var settleQuiet = 0, hadScroll = false, timers = [];
  var MIN_GAP = 15000, MIN_GAP_SWITCH = 5000;
  var ACT = 0.030, QUIET = 0.010, BIG = 0.25;

  function status(t) {{ document.getElementById("ssn-status").textContent = t; }}

  function post(path, body) {{
    return fetch(path + "?k=" + K, {{method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(body)}});
  }}

  function grabAndSend(event) {{
    var now = Date.now();
    var gap = event === "context_switch" ? MIN_GAP_SWITCH : MIN_GAP;
    if (now - lastUpload < gap) return;
    lastUpload = now;
    var c = document.createElement("canvas");
    c.width = video.videoWidth; c.height = video.videoHeight;
    c.getContext("2d").drawImage(video, 0, 0);
    c.toBlob(function (blob) {{
      if (!blob) return;
      var r = new FileReader();
      r.onload = function () {{
        post("/session/frame", {{session_id: sid, event: event,
          jpeg_b64: r.result.split(",")[1]}});
        status("마지막 관찰 전송: " + new Date().toLocaleTimeString()
               + " (" + event + ")");
      }};
      r.readAsDataURL(blob);
    }}, "image/jpeg", 0.85);
  }}

  function tick() {{
    if (!stream) return;
    sctx.drawImage(video, 0, 0, 64, 36);
    var d = 0, img;
    try {{ img = sctx.getImageData(0, 0, 64, 36).data; }} catch (e) {{ return; }}
    if (prev) {{
      var sum = 0;
      for (var i = 0; i < img.length; i += 16) sum += Math.abs(img[i] - prev[i]);
      d = sum / (img.length / 16) / 255;
    }}
    prev = new Uint8ClampedArray(img);
    var now = Date.now();
    if (d > BIG && now - lastBig > 3000) {{
      lastBig = now; lastActivity = now; dwelled = false; hadScroll = false;
      grabAndSend("context_switch");
    }} else if (d > ACT) {{
      lastActivity = now; hadScroll = true; settleQuiet = 0; dwelled = false;
    }} else if (d < QUIET) {{
      if (hadScroll) {{
        settleQuiet++;
        if (settleQuiet >= 3) {{ hadScroll = false; settleQuiet = 0;
          grabAndSend("scroll_settle"); }}
      }}
      if (!dwelled && lastActivity && now - lastActivity > 60000) {{
        dwelled = true; grabAndSend("dwell");
      }}
    }}
  }}

  function cleanup() {{
    timers.forEach(clearInterval); timers = [];
    if (stream) {{ stream.getTracks().forEach(function (t) {{ t.stop(); }}); stream = null; }}
    if (sid) {{
      var endedSid = sid; sid = null;
      post("/session/stop", {{session_id: endedSid}})
        .then(function (r) {{ return r.json(); }})
        .then(function (j) {{ if (j.closing) bubble("theo", j.closing); }})
        .catch(function () {{}});
    }}
    status("세션 종료됨 — 대화 기록은 그대로 남아요");
    document.getElementById("chat-in").disabled = true;
    document.getElementById("chat-send").disabled = true;
    document.getElementById("ssn-stop").style.display = "none";
    document.getElementById("ssn-controls").style.display = "block";
  }}

  function beginShare() {{
    navigator.mediaDevices.getDisplayMedia({{video: {{frameRate: 5}}}})
    .then(function (st) {{
      stream = st;
      video = document.createElement("video");
      video.srcObject = st; video.muted = true; video.play();
      small = document.createElement("canvas");
      small.width = 64; small.height = 36;
      sctx = small.getContext("2d", {{willReadFrequently: true}});
      st.getVideoTracks()[0].onended = cleanup;
      return post("/session/start",
        {{source: document.getElementById("ssn-source").value}})
        .then(function (r) {{ return r.json(); }});
    }})
    .then(function (j) {{
      // declared source rides on start via form field
      sid = j.session_id;
      document.getElementById("ssn-controls").style.display = "none";
      document.getElementById("ssn-live").style.display = "block";
      status("공유 시작됨");
      if (j.greeting) bubble("theo", j.greeting);
      lastActivity = Date.now();
      timers.push(setInterval(tick, 500));
      timers.push(setInterval(function () {{
        post("/session/heartbeat", {{session_id: sid}});
      }}, 20000));
      setTimeout(function () {{ grabAndSend("start"); }}, 1200);
    }})
    .catch(function (e) {{
      var st = document.getElementById("ssn-status");
      if (st) st.textContent = "공유가 시작되지 않았어요: " + e.message;
    }});
  }}

  document.getElementById("ssn-start").onclick = function () {{
    if (!CONSENTED) {{
      document.getElementById("ssn-consent").style.display = "block";
      return;
    }}
    beginShare();
  }};

  document.getElementById("ssn-agree").onclick = function () {{
    post("/session/consent", {{}}).then(function () {{
      CONSENTED = true;
      document.getElementById("ssn-consent").style.display = "none";
      beginShare();
    }});
  }};
  function bubble(role, text) {{
    var d = document.createElement("div");
    d.style.cssText = "padding:8px 12px; border-radius:12px; font-size:14px;"
      + "line-height:1.55; max-width:88%; white-space:pre-wrap;"
      + (role === "me"
         ? "align-self:flex-end; background:#e8590c; color:#fff"
         : "align-self:flex-start; background:#f1f3f5; color:#222");
    d.textContent = text;
    document.getElementById("chat-log").appendChild(d);
    d.scrollIntoView({{block: "end"}});
    return d;
  }}

  function currentFrameB64(cb) {{
    if (!video || !video.videoWidth) return cb(null);
    var c = document.createElement("canvas");
    c.width = video.videoWidth; c.height = video.videoHeight;
    c.getContext("2d").drawImage(video, 0, 0);
    c.toBlob(function (blob) {{
      if (!blob) return cb(null);
      var r = new FileReader();
      r.onload = function () {{ cb(r.result.split(",")[1]); }};
      r.readAsDataURL(blob);
    }}, "image/jpeg", 0.85);
  }}

  var chatBusy = false;
  function setBusy(b) {{
    chatBusy = b;
    document.getElementById("chat-in").disabled = b;
    document.getElementById("chat-send").disabled = b;
    document.getElementById("chat-in").placeholder =
      b ? "Theo가 생각 중…" : "Theo에게 말하기…";
  }}
  function sendChat() {{
    var inp = document.getElementById("chat-in");
    var t = inp.value.trim();
    if (!t || !sid || chatBusy) return;
    inp.value = "";
    bubble("me", t);
    var thinking = bubble("theo", "…");
    setBusy(true);
    var timer = setTimeout(function () {{
      thinking.textContent = "(응답이 늦네요 — 잠시 후 다시 보내주세요)";
      setBusy(false);
    }}, 75000);
    currentFrameB64(function (b64) {{
      fetch("/session/message/stream?k=" + K, {{method: "POST",
        headers: {{"Content-Type": "application/json"}},
        body: JSON.stringify({{session_id: sid, text: t, jpeg_b64: b64}})}})
      .then(function (r) {{
        if (!r.ok || !r.body) throw new Error("stream " + r.status);
        var reader = r.body.getReader();
        var dec = new TextDecoder();
        var buf = "", acc = "";
        function pump() {{
          return reader.read().then(function (x) {{
            if (x.done) return;
            buf += dec.decode(x.value, {{stream: true}});
            var events = buf.split("\\n\\n");
            buf = events.pop();
            events.forEach(function (ev) {{
              var kind = (ev.match(/^event: (.+)$/m) || [])[1];
              var data = (ev.match(/^data: (.+)$/m) || [])[1];
              if (!kind || data === undefined) return;
              var payload = JSON.parse(data);
              if (kind === "delta") {{
                acc += payload;
                thinking.textContent = acc;
              }} else if (kind === "done") {{
                thinking.textContent = payload;
                clearTimeout(timer);
                setBusy(false);
              }}
              thinking.scrollIntoView({{block: "end"}});
            }});
            return pump();
          }});
        }}
        return pump();
      }})
      .catch(function () {{
        clearTimeout(timer);
        thinking.textContent = "(전송 실패 — 다시 보내줄래?)";
        setBusy(false);
      }});
    }});
  }}
  document.getElementById("chat-send").onclick = sendChat;
  document.getElementById("chat-in").addEventListener("keydown",
    function (e) {{ if (e.key === "Enter") sendChat(); }});

  document.getElementById("ssn-stop").onclick = cleanup;
  window.addEventListener("beforeunload", function () {{
    if (sid && navigator.sendBeacon) {{
      navigator.sendBeacon("/session/stop?k=" + K,
        new Blob([JSON.stringify({{session_id: sid}})],
                 {{type: "application/json"}}));
    }}
  }});
}})();
</script>
"""
    return web.Response(text=_site_page("Your learning space", body,
                                        path="/my"),
                        content_type="text/html")


def _my_redirect(token, ok=False, err=""):
    from urllib.parse import quote
    loc = f"/my?k={token}" + ("&ok=1" if ok else "")
    if err:
        loc += f"&err={quote(err)}"
    raise web.HTTPFound(loc)


async def _my_upload_handler(request):
    import asyncio

    import materials
    user_id, token = _my_auth(request)
    if not user_id:
        return web.Response(status=404, text="Not found")
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "file":
        _my_redirect(token, err="No file arrived — try again?")
    filename = field.filename or ""
    data = b""
    while True:
        chunk = await field.read_chunk()
        if not chunk:
            break
        data += chunk
        if len(data) > materials.MAX_FILE_BYTES:
            _my_redirect(token, err="File is larger than 20MB.")
    material_id, err = materials.register_upload(user_id, filename, data)
    if err:
        _my_redirect(token, err=err)
    # The one-time read runs in the background so the page answers
    # instantly; the list shows "Theo is reading it…" until it lands.
    asyncio.get_event_loop().run_in_executor(
        None, materials.digest_material, material_id)
    _my_redirect(token, ok=True)


async def _my_link_handler(request):
    import materials
    user_id, token = _my_auth(request)
    if not user_id:
        return web.Response(status=404, text="Not found")
    form = await request.post()
    _mid, err = materials.register_link(user_id, form.get("url"),
                                        form.get("title") or "")
    if err:
        _my_redirect(token, err=err)
    _my_redirect(token, ok=True)


async def _sms_signup_submit_handler(request):
    import db

    form = await request.post()
    name = (form.get("name") or "").strip()[:200]
    email = (form.get("email") or "").strip()[:200]
    consent_checkins = form.get("consent_checkins") == "yes"
    consent_support = form.get("consent_support") == "yes"
    any_consent = consent_checkins or consent_support

    # Email and phone are both required signup fields; the SMS consent
    # checkboxes stay optional (TFV round-2: consent must not gate
    # completing the signup — providing a phone number is contact info,
    # not consent, and no message is sent without a checked box).
    if not email or "@" not in email:
        return _signup_error("Please enter a valid email address.")

    phone_raw = (form.get("phone") or "").strip()
    phone = _normalize_us_phone(phone_raw) if phone_raw else None
    if not phone:
        return _signup_error(
            "Please enter a valid US mobile number, e.g. (555) 123-4567.")

    if any_consent:
        # A consent record row is written ONLY when a box was checked;
        # email-only signups carry no SMS consent to record.
        db.save_sms_signup(phone, name=name, email=email,
                           consent_checkins=consent_checkins,
                           consent_support=consent_support)
    db.log_event(phone or email,
                 "signup_consent" if any_consent else "signup_submitted",
                 {"phone": phone or "", "name": name, "email": email,
                  "consent_checkins": consent_checkins,
                  "consent_support": consent_support},
                 source="web")

    if any_consent:
        body = """
<h1>You're all set 🎉</h1>
<p>Thanks — your signup is recorded. Every member is personally
onboarded, so you'll receive a welcome text from Theo shortly to get
you set up.</p>
<p>You can reply STOP at any time to cancel, or HELP for assistance.
Message and data rates may apply.</p>
<p><a href="/">← Home</a></p>
"""
    else:
        body = """
<h1>You're all set 🎉</h1>
<p>Thanks — your signup is recorded. Every member is personally
onboarded, so we'll reach out to you by email to get you set up.</p>
<p><a href="/">← Home</a></p>
"""
    return web.Response(text=_legal_page("SMS Signup", body),
                        content_type="text/html")


@web.middleware
async def _log_middleware(request, handler):
    """Log every incoming request for debugging."""
    upgrade = request.headers.get("Upgrade", "")
    print(f"[REQ] {request.method} {request.path} from={request.remote} upgrade={upgrade}", flush=True)
    return await handler(request)


async def _infra_sweep_loop():
    """T6 watchdog: run infra.sweep() every 2 minutes for the life of
    the server. sweep() swallows its own errors; this loop only guards
    against import-time surprises so it can never die quietly."""
    import infra
    while True:
        try:
            infra.sweep()
        except Exception as e:
            print(f"[INFRA] ⚠️ sweep crashed: {e}", flush=True)
        await asyncio.sleep(120)


def start_ws_server():
    """Start combined WebSocket + HTTP server on a single port using aiohttp."""
    global ws_loop
    ws_loop = asyncio.new_event_loop()

    async def _run():
        app = web.Application(middlewares=[_log_middleware])
        app.router.add_get("/health", _health_handler)
        app.router.add_get("/ws", ws_handler)
        # / is the public landing page (TFV-reviewed business website);
        # the chat app moved to /app.
        app.router.add_get("/", _landing_handler)
        app.router.add_get("/app", _app_handler)
        # Admin routes — registered BEFORE the static catch-all so they
        # take precedence. Auth is enforced inside each handler via
        # ADMIN_PASSWORD env var (returns 503 if unset, 401 otherwise).
        app.router.add_get("/admin", _admin_users_handler)
        app.router.add_get("/admin/user/{user_id}", _admin_user_handler)
        app.router.add_get("/admin/session/{session_id}", _admin_session_handler)
        # SMS — POST endpoints, registered before the static catch-all.
        app.router.add_post("/sms/inbound", _sms_inbound_handler)
        app.router.add_post("/sms/cron-tick", _sms_cron_tick_handler)
        app.router.add_post("/sms/schedule-tick", _sms_schedule_tick_handler)
        app.router.add_get("/schedule", _schedule_debug_handler)
        app.router.add_get("/availability", _availability_handler)
        app.router.add_post("/sms/reset-and-fire", _sms_reset_and_fire_handler)
        app.router.add_post("/sms/set-goal", _sms_set_goal_handler)
        app.router.add_post("/sms/set-ignition", _sms_set_ignition_handler)
        app.router.add_get("/sms/status", _sms_status_handler)
        app.router.add_get("/debug/timeline", _debug_timeline_handler)
        app.router.add_get("/debug/prompt", _debug_prompt_handler)
        app.router.add_get("/debug/llm-call", _debug_llm_call_handler)
        app.router.add_get("/debug/learner-state", _debug_learner_state_handler)
        app.router.add_get("/debug/trace", _debug_trace_handler)
        app.router.add_get("/my", _my_page_handler)
        app.router.add_get("/debug/my-link", _my_token_handler)
        app.router.add_post("/debug/bind-phone", _bind_phone_handler)
        app.router.add_post("/debug/reset-user", _reset_user_handler)
        app.router.add_get("/debug/signups", _signups_handler)
        app.router.add_post("/debug/activate", _activate_handler)
        app.router.add_post("/debug/email-my-link", _email_my_link_handler)
        app.router.add_post("/my/upload", _my_upload_handler)
        app.router.add_post("/my/link", _my_link_handler)
        app.router.add_post("/session/start", _session_start_handler)
        app.router.add_post("/session/heartbeat", _session_heartbeat_handler)
        app.router.add_post("/session/frame", _session_frame_handler)
        app.router.add_post("/session/stop", _session_stop_handler)
        app.router.add_post("/session/message", _session_message_handler)
        app.router.add_get("/screen-consent", _screen_consent_page_handler)
        app.router.add_post("/session/consent", _session_consent_handler)
        app.router.add_post("/session/message/stream", _session_stream_handler)
        app.router.add_post("/debug/material", _material_admin_handler)
        app.router.add_post("/debug/import-ledgers", _ledger_import_handler)
        app.router.add_post("/debug/track", _track_admin_handler)
        app.router.add_get("/debug/prompt-preview", _prompt_preview_handler)
        app.router.add_get("/notes", _notes_handler)
        app.router.add_post("/notes", _notes_handler)
        app.router.add_get("/plan", _plan_handler)
        app.router.add_post("/plan", _plan_handler)
        app.router.add_get("/onboarding", _onboarding_handler)
        app.router.add_post("/onboarding", _onboarding_handler)
        app.router.add_post("/plan/generate", _plan_generate_handler)
        app.router.add_post("/analyze/turn", _analyze_turn_handler)
        app.router.add_post("/sms/window-open", _window_open_handler)
        app.router.add_post("/annotate/run", _annotate_run_handler)
        app.router.add_post("/sms/set-bite", _sms_set_bite_handler)
        # Screen observer — local agent (observer.py) endpoints.
        app.router.add_post("/observe/start", _observe_start_handler)
        app.router.add_post("/observe/capture", _observe_capture_handler)
        app.router.add_post("/observe/end", _observe_end_handler)
        app.router.add_get("/observe/poll", _observe_poll_handler)
        # Public legal pages — required by Twilio A2P 10DLC Campaign
        # vetting. Reviewers fetch these URLs and grep for the
        # compliance phrases.
        app.router.add_get("/privacy", _privacy_handler)
        app.router.add_get("/terms", _terms_handler)
        # Public site pages (business credibility for TFV + recruits).
        app.router.add_get("/about", _about_handler)
        app.router.add_get("/faq", _faq_handler)
        app.router.add_get("/contact", _contact_handler)
        # SMS pilot opt-in form (also the TFV opt-in policy proof URL).
        app.router.add_get("/sms-signup", _sms_signup_page_handler)
        app.router.add_post("/sms-signup", _sms_signup_submit_handler)
        app.router.add_get("/{path:.*}", _static_handler)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, BIND_HOST, HTTP_PORT)
        await site.start()
        print(f"🌐 Browser UI: http://{BIND_HOST}:{HTTP_PORT}", flush=True)
        asyncio.ensure_future(_infra_sweep_loop())
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
        # Onboarding bypass (2026-07-21): first-time visitors land
        # directly on the home screen — big "Theo" + tagline — with a
        # default profile created silently so chat works immediately.
        # Rationale: the public URL is what Twilio/carrier reviewers
        # open; a form-and-quiz gate reads as friction, not product.
        # The onboarding flow (form, quiz, handle_onboarding_submit)
        # is kept intact below — nothing triggers it anymore, but
        # pilot onboarding may re-enable a variant of it.
        uid = db.create_user_profile(
            "anonymous", goal="Learn and grow", background="",
            studying="", hint_preference="hints", difficulty=3,
            user_condition=3, user_id=session_id,
        )
        db.set_user_id(uid)
        ctx = ws_sessions.get(websocket)
        if ctx:
            ctx.user_id = uid
            ctx.user_profile = {
                "user_id": uid, "user_name": "anonymous",
                "goal": "Learn and grow", "background": "",
                "studying": "", "hint_preference": "hints",
                "difficulty": 3, "user_condition": 3,
            }
            ctx.study_topic = ""
            _set_ctx(ctx)
        db.start_session(study_topic="")
        if ctx:
            ctx.db_session_id = db.get_session_id()
        db.touch_activity()
        print(f"  [Server] New visitor {session_id} — onboarding "
              f"bypassed, default profile created", flush=True)
        asyncio.run_coroutine_threadsafe(
            websocket.send_str(json.dumps({"type": "connected",
                                           "study_context": ""})),
            ws_loop,
        )
        asyncio.run_coroutine_threadsafe(
            websocket.send_str(json.dumps({"type": "show_code_editor"})),
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
        # Surface the per-Text [serialize] / DEBUG_UC diagnostic lines from
        # extract.py's stderr even on success — without this they're
        # silently dropped (subprocess captures stderr but we only print
        # it on failure). Filter out Manim's WARNING noise.
        if result.stderr:
            for _line in result.stderr.splitlines():
                if "[serialize]" in _line or "DEBUG_UC" in _line:
                    print(f"  [Manim] {_line}", flush=True)
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
# Each helper attaches a `_uc_font_size` marker on the returned Text
# mobject so the extractor can read OUR intended size directly,
# bypassing Manim's `font_size` property (which is computed as
# height/initial_height * _font_size and drifts when .next_to() /
# .move_to() / VGroup membership perturbs the height).
import sys as _sys
def _uc_make(text, fs, weight=None, **kw):
    kw['font'] = 'Inter'
    kw['font_size'] = fs
    if weight is not None:
        kw.setdefault('weight', weight)
    t = Text(text, **kw)
    try:
        t._uc_font_size = fs
    except Exception:
        pass
    print(f"DEBUG_UC: helper made Text(font_size={fs}) for {text[:40]!r}", file=_sys.stderr, flush=True)
    return t

def Title(s, **kw):
    """Animation title — large, bold, auto-positioned at top.

    Manim's built-in `Title` class auto-positions itself to the top
    edge. The LLM relies on that convention and rarely calls
    .to_edge(UP) explicitly. Our helper has to do the same or the
    title lands at the default origin (0,0,0) and overlaps the rest
    of the scene. The LLM can still override by calling
    .to_edge() / .move_to() / .shift() afterwards.
    """
    t = _uc_make(s, 28, weight=BOLD, **kw)
    try:
        t.to_edge(UP, buff=0.5)
    except Exception:
        pass
    return t

def Subtitle(s, **kw):
    """Section heading."""
    return _uc_make(s, 22, **kw)

def Caption(s, **kw):
    """Bottom-of-frame summary."""
    return _uc_make(s, 18, **kw)

def AxisLabel(s, **kw):
    """Brace labels (e.g. \"T = 4 (sequence length)\")."""
    return _uc_make(s, 16, **kw)

def CellDigit(s, **kw):
    """Numbers shown inside matrix cells."""
    return _uc_make(s, 18, **kw)

def RowLabel(s, **kw):
    """Row identifiers (e.g. \"batch 0\")."""
    return _uc_make(s, 14, **kw)

def ColLabel(s, **kw):
    """Column identifiers."""
    return _uc_make(s, 14, **kw)

def CodeText(s, **kw):
    """Inline code-like text (variable names, short snippets)."""
    return _uc_make(s, 16, **kw)
# === end helpers ===

'''


def _inject_typography_helpers(manim_code: str) -> str:
    """Inject typography helpers so Title/Subtitle/etc. point at our
    factory functions inside the Scene's construct().

    Prepending alone wasn't enough: prod logs showed the LLM defines
    its OWN `def Title(text, **kwargs): return Text(text, font="Inter",
    font_size=28, ...)` at the top of the script (it copies the names
    from the prompt's helper catalog). Python resolves later `def`
    statements as overwrites at module level — so the LLM's def
    silently shadowed ours, the `_uc_font_size` marker never got
    stamped (logged as `_uc_marker=None` for every Text), and we
    fell back to Manim's `font_size` property (which drifts on
    Render — see height/initial_height divergence in the [serialize]
    log for any text containing `(`, `=`, `,`, `→`).

    Strategy:
      1. Ensure `from manim import *` is at the top so Text / BOLD
         are available inside the helpers' bodies.
      2. APPEND the helper prelude AFTER the LLM's code. Python's
         module-level lookup uses the latest binding, so when
         construct() finally runs and looks up `Title`, it finds OUR
         def (the appended one) — regardless of whether the LLM
         defined its own `def Title` earlier in the file.
    """
    import re as _re
    has_import = bool(_re.search(r'^\s*from\s+manim\s+import\s+\*\s*$',
                                 manim_code, _re.MULTILINE))
    head = '' if has_import else 'from manim import *\n\n'
    # Trailing newline + blank line to keep the appended block visually
    # separate from whatever LLM code ends with (often a class def with
    # no trailing newline).
    return head + manim_code + '\n\n' + _HELPER_PRELUDE


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

        # Diagnostic: dump the LLM's full Manim source so we can see
        # what it emitted (helper usage vs raw Text(), absolute
        # font_size vs .scale()-based shrinks, etc.).
        print(f"  [Manim] code BEGIN (LLM raw)\n{manim_code}\n  [Manim] code END\n",
              flush=True)

        # Prepend the typography helper prelude so Title/Subtitle/...
        # are in scope inside the Scene's construct(). Layout sizes
        # are deterministic regardless of what font_size the LLM may
        # have tried to put on a raw Text() call.
        manim_code = _inject_typography_helpers(manim_code)

        # Diagnostic: also dump POST-injection code so we can see
        # exactly where the helper prelude landed relative to the
        # LLM's `from manim import *` line. If LLM has a SECOND
        # `from manim import *` after our prelude, Manim's own classes
        # would re-shadow our defs — that explains why Title works
        # (we override before LLM imports) but Subtitle/Caption don't
        # (LLM re-imports manim AFTER our def). First 1500 chars.
        print(f"  [Manim] code POST-INJECT (first 1500)\n{manim_code[:1500]}\n  [Manim] /post\n",
              flush=True)

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
- Example: Instead of explaining nn.Module, first ask "Have you worked with classes in Python before?"

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

**6. Always Connect to Goal, if specified.**
- Periodically remind how current concept connects to this goal
- Keep motivation high by showing progress

**7. Bite-Size Everything**
- Max one concept per message
- Use animations/visuals when available (return as JSON)
- After each small step, check understanding before moving forward

## USER PROFILE
''

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
- Concise and direct (treat as intelligent adult)
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

CONCRETE LIMIT: an animation should reveal 1-2 teaching steps,
absolute maximum 3. Counting "steps" = how many distinct things
the learner has to track (an axis label, a matrix, an arrow showing
a transformation, a result tensor — that's already 4 things, too
many). If your scene has 4+ things appearing in sequence, you're
trying to fit two animations' worth of content into one — split it.

A scene with too many steps is the #1 source of layout drift /
overlap problems. Fewer things on screen = each thing has more
room and the learner has more attention budget.

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
   - Numbered list of stages — DECOMPOSE AS FINELY AS POSSIBLE.
     There is NO upper bound on stage count and a finer split is
     ALWAYS preferable to a coarse one. 8-15 stages for a complex
     topic is normal and good. The constraint is the OPPOSITE
     direction: each stage must fit in 1-2 (max 3) reveal steps
     of a single animation (see ONE CONCEPT PER ANIMATION). When
     in doubt, split a stage into two — never merge two stages
     into one.
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
    print("🎓 Theo")
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
