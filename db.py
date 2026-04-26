"""
PostgreSQL/SQLite database for tracking user interactions, practice results, and sessions.
Uses DATABASE_URL env var for PostgreSQL (Render), falls back to SQLite locally.
"""

import os
import time
import json
import uuid
from datetime import datetime

# ─── Connection setup ────────────────────────────────────────────────

DATABASE_URL = os.environ.get("DATABASE_URL")

if DATABASE_URL:
    # PostgreSQL (Render)
    import psycopg2
    import psycopg2.extras

    def get_conn():
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return conn

    def _fetchone(cursor):
        cols = [desc[0] for desc in cursor.description] if cursor.description else []
        row = cursor.fetchone()
        return dict(zip(cols, row)) if row else None

    def _fetchall(cursor):
        cols = [desc[0] for desc in cursor.description] if cursor.description else []
        return [dict(zip(cols, r)) for r in cursor.fetchall()]

    DB_TYPE = "postgres"
else:
    # SQLite (local development)
    import sqlite3

    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upskill_coach.db")

    def get_conn():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _fetchone(cursor):
        row = cursor.fetchone()
        return dict(row) if row else None

    def _fetchall(cursor):
        return [dict(r) for r in cursor.fetchall()]

    DB_TYPE = "sqlite"


import threading as _threading

USER_ID = "jeongmo"  # default fallback, overridden by onboarding

# ─── Thread-local user/session context (multi-user support) ──────
_tls = _threading.local()


def set_thread_user(user_id, session_id=None):
    """Set per-thread user_id and session_id (for multi-user server mode)."""
    _tls.user_id = user_id
    if session_id is not None:
        _tls.session_id = session_id


def _uid():
    """Get current user_id: thread-local first, then global fallback."""
    return getattr(_tls, 'user_id', None) or USER_ID


def _sid():
    """Get current session_id: thread-local first, then global fallback."""
    return getattr(_tls, 'session_id', None) or _current_session_id


def _execute(conn, sql, params=None):
    """Execute SQL, handling syntax differences between SQLite and PostgreSQL."""
    cur = conn.cursor()
    cur.execute(sql, params or ())
    return cur


def init_db():
    """Create tables if they don't exist."""
    conn = get_conn()

    if DB_TYPE == "postgres":
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                study_topic TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT
            );

            CREATE TABLE IF NOT EXISTS user_state (
                user_id TEXT PRIMARY KEY,
                last_session_start_time TEXT,
                last_session_end_time TEXT,
                current_session_id TEXT
            );

            CREATE TABLE IF NOT EXISTS interactions (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                interaction_type TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'user',
                study_topic TEXT,
                tutorial_section TEXT,
                question_text TEXT,
                answer_text TEXT,
                practice_question TEXT,
                user_answer TEXT,
                is_correct INTEGER,
                time_taken_seconds REAL,
                practice_requested INTEGER,
                skipped INTEGER DEFAULT 0,
                difficulty TEXT,
                extra_json TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS insights (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                analysis TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                user_name TEXT NOT NULL,
                goal TEXT DEFAULT '',
                background TEXT DEFAULT '',
                studying TEXT DEFAULT '',
                hint_preference TEXT DEFAULT 'hints',
                difficulty INTEGER DEFAULT 3,
                user_condition INTEGER DEFAULT 3,
                email TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        # IMPORTANT: commit the CREATE TABLEs BEFORE attempting any ALTER
        # migration. Otherwise, if an ALTER raises (e.g. column already
        # exists on a fresh DB), psycopg2 marks the transaction as aborted
        # and the final commit becomes a rollback, wiping out the tables
        # we just created.
        conn.commit()

        # Migrate: add email column if missing (separate transaction so a
        # failure here does not poison the CREATE TABLE commit above).
        try:
            conn.cursor().execute("ALTER TABLE user_profiles ADD COLUMN email TEXT DEFAULT ''")
            conn.commit()
        except Exception:
            conn.rollback()  # clear aborted-transaction state
    else:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                study_topic TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT
            );

            CREATE TABLE IF NOT EXISTS user_state (
                user_id TEXT PRIMARY KEY,
                last_session_start_time TEXT,
                last_session_end_time TEXT,
                current_session_id TEXT
            );

            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                interaction_type TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'user',
                study_topic TEXT,
                tutorial_section TEXT,
                question_text TEXT,
                answer_text TEXT,
                practice_question TEXT,
                user_answer TEXT,
                is_correct INTEGER,
                time_taken_seconds REAL,
                practice_requested INTEGER,
                skipped INTEGER DEFAULT 0,
                difficulty TEXT,
                extra_json TEXT
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS insights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                analysis JSON,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_profiles (
                user_id TEXT PRIMARY KEY,
                user_name TEXT NOT NULL,
                goal TEXT DEFAULT '',
                background TEXT DEFAULT '',
                studying TEXT DEFAULT '',
                hint_preference TEXT DEFAULT 'hints',
                difficulty INTEGER DEFAULT 3,
                user_condition INTEGER DEFAULT 3,
                email TEXT DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
        """)
        # Migrate: add columns if missing on existing SQLite DBs
        for col, default in [
            ("difficulty", "INTEGER DEFAULT 3"),
            ("user_condition", "INTEGER DEFAULT 3"),
            ("studying", "TEXT DEFAULT ''"),
            ("hint_preference", "TEXT DEFAULT 'hints'"),
            ("email", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE user_profiles ADD COLUMN {col} {default}")
            except Exception:
                pass

    conn.commit()
    conn.close()


def set_user_id(uid):
    """Set the active user ID (global + thread-local)."""
    global USER_ID
    USER_ID = uid
    _tls.user_id = uid


def get_user_profile(user_name):
    """Look up a user profile by name (case-insensitive). Returns dict or None."""
    conn = get_conn()
    cur = _execute(conn,
        "SELECT * FROM user_profiles WHERE LOWER(user_name) = LOWER(%s)" if DB_TYPE == "postgres"
        else "SELECT * FROM user_profiles WHERE LOWER(user_name) = LOWER(?)",
        (user_name,)
    )
    result = _fetchone(cur)
    conn.close()
    return result


def get_user_profile_by_id(user_id):
    """Look up a user profile by user_id. Returns dict or None."""
    conn = get_conn()
    _p = "%s" if DB_TYPE == "postgres" else "?"
    cur = _execute(conn, f"SELECT * FROM user_profiles WHERE user_id = {_p}", (user_id,))
    result = _fetchone(cur)
    conn.close()
    return result


def create_user_profile(user_name, goal="", background="", studying="", hint_preference="hints", difficulty=3, user_condition=3, user_id=None):
    """Create a new user profile. Returns the user_id."""
    uid = user_id or user_name.lower().replace(" ", "_")
    now = datetime.now().isoformat()
    conn = get_conn()
    if DB_TYPE == "postgres":
        _execute(conn, """
            INSERT INTO user_profiles
            (user_id, user_name, goal, background, studying, hint_preference, difficulty, user_condition, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                user_name=EXCLUDED.user_name, goal=EXCLUDED.goal, background=EXCLUDED.background,
                studying=EXCLUDED.studying, hint_preference=EXCLUDED.hint_preference,
                difficulty=EXCLUDED.difficulty, user_condition=EXCLUDED.user_condition,
                updated_at=EXCLUDED.updated_at
        """, (uid, user_name, goal, background, studying, hint_preference, difficulty, user_condition, now, now))
    else:
        _execute(conn, """
            INSERT OR REPLACE INTO user_profiles
            (user_id, user_name, goal, background, studying, hint_preference, difficulty, user_condition, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (uid, user_name, goal, background, studying, hint_preference, difficulty, user_condition, now, now))
    conn.commit()
    conn.close()
    return uid


def update_user_profile(user_id, **kwargs):
    """Update specific fields of a user profile."""
    allowed = {"goal", "background", "user_name", "studying", "hint_preference", "difficulty", "user_condition", "email"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = datetime.now().isoformat()
    if DB_TYPE == "postgres":
        set_clause = ", ".join(f"{k} = %s" for k in updates)
        conn = get_conn()
        _execute(conn,
            f"UPDATE user_profiles SET {set_clause} WHERE user_id = %s",
            list(updates.values()) + [user_id]
        )
    else:
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        conn = get_conn()
        _execute(conn,
            f"UPDATE user_profiles SET {set_clause} WHERE user_id = ?",
            list(updates.values()) + [user_id]
        )
    conn.commit()
    conn.close()


# ─── Session management ───────────────────────────────────────────

_current_session_id = None

# Placeholder for parameterized queries
_P = "%s" if DATABASE_URL else "?"


def start_session(study_topic=""):
    """Start a new session. Migrate previous session times."""
    global _current_session_id
    _current_session_id = str(uuid.uuid4())[:8]
    _tls.session_id = _current_session_id
    now = datetime.now().isoformat()

    conn = get_conn()

    # Migrate: move current → last
    cur = _execute(conn,
        f"SELECT current_session_id FROM user_state WHERE user_id = {_P}", (_uid(),)
    )
    row = _fetchone(cur)

    if row and row["current_session_id"]:
        prev_sid = row["current_session_id"]
        cur2 = _execute(conn,
            f"SELECT start_time, end_time FROM sessions WHERE session_id = {_P}", (prev_sid,)
        )
        prev_session = _fetchone(cur2)
        if prev_session:
            _execute(conn, f"""
                UPDATE user_state SET
                    last_session_start_time = {_P},
                    last_session_end_time = {_P},
                    current_session_id = {_P}
                WHERE user_id = {_P}
            """, (prev_session["start_time"], prev_session["end_time"] or now,
                  _current_session_id, _uid()))
    else:
        if DB_TYPE == "postgres":
            _execute(conn, f"""
                INSERT INTO user_state (user_id, current_session_id)
                VALUES ({_P}, {_P})
                ON CONFLICT (user_id) DO UPDATE SET current_session_id = EXCLUDED.current_session_id
            """, (_uid(), _current_session_id))
        else:
            _execute(conn, """
                INSERT OR REPLACE INTO user_state (user_id, current_session_id)
                VALUES (?, ?)
            """, (_uid(), _current_session_id))

    # Create new session row
    _execute(conn, f"""
        INSERT INTO sessions (session_id, user_id, study_topic, start_time)
        VALUES ({_P}, {_P}, {_P}, {_P})
    """, (_current_session_id, _uid(), study_topic, now))

    conn.commit()
    conn.close()
    print(f"  [DB] Session started: {_current_session_id}")
    return _current_session_id


def end_session(session_id=None):
    """End a session by setting its end_time.

    If `session_id` is omitted, ends the current thread's session and
    clears the thread-local + global trackers (the original behavior).
    If provided, ends only that specific session row — useful for
    closing orphan sessions or rotating to a new session at idle.
    """
    global _current_session_id
    sid = session_id or _sid()
    if not sid:
        return

    now = datetime.now().isoformat()
    conn = get_conn()
    _execute(conn,
        f"UPDATE sessions SET end_time = {_P} WHERE session_id = {_P}",
        (now, sid)
    )
    conn.commit()
    conn.close()
    print(f"  [DB] Session ended: {sid}")
    # Only clear thread-local trackers if we ended the *current* session
    if session_id is None or session_id == _sid():
        _current_session_id = None
        _tls.session_id = None


def get_session_id():
    return _sid()


# ─── Idle / pause detection ───────────────────────────────────────

IDLE_THRESHOLD_SECONDS = 300  # 5 minutes = likely a break

_last_activity_time = None


def touch_activity():
    """Record that user did something. Detect pause/resume gaps."""
    global _last_activity_time
    now = datetime.now()

    if _last_activity_time and _sid():
        gap = (now - _last_activity_time).total_seconds()
        if gap >= IDLE_THRESHOLD_SECONDS:
            conn = get_conn()
            _execute(conn, f"""
                INSERT INTO interactions
                (user_id, session_id, timestamp, interaction_type, source,
                 study_topic, extra_json)
                VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})
            """, (_uid(), _sid(), _last_activity_time.isoformat(),
                  "session_pause", "system", None,
                  json.dumps({"idle_seconds": round(gap)})))
            _execute(conn, f"""
                INSERT INTO interactions
                (user_id, session_id, timestamp, interaction_type, source,
                 study_topic, extra_json)
                VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})
            """, (_uid(), _sid(), now.isoformat(),
                  "session_resume", "system", None,
                  json.dumps({"idle_seconds": round(gap)})))
            conn.commit()
            conn.close()
            print(f"  [DB] Detected break: {gap/60:.0f} min idle → logged pause/resume")

    _last_activity_time = now


def mark_pending_followups_skipped():
    """Mark any unanswered followups as skipped."""
    if not _sid():
        return
    conn = get_conn()
    cur = _execute(conn, f"""
        SELECT f.id, f.practice_question FROM interactions f
        WHERE f.session_id = {_P}
          AND f.interaction_type = 'followup'
          AND f.skipped = 0
          AND f.user_answer IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM interactions a
              WHERE a.session_id = f.session_id
                AND a.interaction_type = 'followup_answer'
                AND a.practice_question = f.practice_question
                AND a.id > f.id
          )
    """, (_sid(),))
    rows = _fetchall(cur)

    if rows:
        ids = [r["id"] for r in rows]
        if DB_TYPE == "postgres":
            placeholders = ','.join(['%s'] * len(ids))
        else:
            placeholders = ','.join(['?'] * len(ids))
        _execute(conn,
            f"UPDATE interactions SET skipped = 1 WHERE id IN ({placeholders})",
            ids
        )
        conn.commit()
        print(f"  [DB] Marked {len(ids)} unanswered followup(s) as skipped")
    conn.close()


# ─── Interaction logging ──────────────────────────────────────────

def log_question(question_text, answer_text, tutorial_section=None, study_topic=None):
    """Log a Q&A interaction (terminal question → Claude answer)."""
    conn = get_conn()
    _execute(conn, f"""
        INSERT INTO interactions
        (user_id, session_id, timestamp, interaction_type, study_topic,
         tutorial_section, question_text, answer_text)
        VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})
    """, (_uid(), _sid() or "", datetime.now().isoformat(),
          "question", study_topic, tutorial_section, question_text, answer_text))
    conn.commit()
    conn.close()


def log_practice(practice_question, user_answer, is_correct, time_taken_seconds,
                 study_topic=None, tutorial_section=None, code_context=None,
                 answer_text=None, difficulty=None, practice_topic=None):
    """Log a practice question attempt."""
    extra = {}
    if code_context:
        extra["code_context"] = code_context
    if practice_topic:
        extra["practice_topic"] = practice_topic
    extra = extra or None

    conn = get_conn()
    _execute(conn, f"""
        INSERT INTO interactions
        (user_id, session_id, timestamp, interaction_type, study_topic,
         tutorial_section, practice_question, user_answer, is_correct,
         time_taken_seconds, answer_text, difficulty, extra_json)
        VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})
    """, (_uid(), _sid() or "", datetime.now().isoformat(),
          "practice", study_topic, tutorial_section, practice_question,
          user_answer, 1 if is_correct else 0, time_taken_seconds,
          answer_text, difficulty,
          json.dumps(extra) if extra else None))
    conn.commit()
    conn.close()


def get_session_interactions(session_id=None):
    """Get all interactions for a session as a list of dicts."""
    sid = session_id or _sid()
    if not sid:
        return []
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM interactions WHERE session_id = {_P} ORDER BY id",
        (sid,)
    )
    result = _fetchall(cur)
    conn.close()
    return result


def get_all_user_interactions():
    """Get ALL interactions for the user across all sessions, ordered by id."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM interactions WHERE user_id = {_P} ORDER BY id",
        (_uid(),)
    )
    result = _fetchall(cur)
    conn.close()
    return result


def get_topic_history(topic):
    """Get the most recent graded interactions for a topic across ALL sessions."""
    conn = get_conn()
    like_pattern = f'%"practice_topic": "{topic}"%'
    cur = _execute(conn, f"""
        SELECT is_correct, difficulty, extra_json FROM interactions
        WHERE user_id = {_P}
          AND interaction_type IN ('practice', 'followup_answer')
          AND user_answer IS NOT NULL
          AND extra_json LIKE {_P}
        ORDER BY id DESC
        LIMIT 5
    """, (_uid(), like_pattern))
    result = _fetchall(cur)
    conn.close()
    return result


def log_followup(practice_question, weak_concepts, study_topic=None,
                 tutorial_section=None, difficulty=None):
    """Log a system-generated follow-up question."""
    extra = {"weak_concepts": weak_concepts} if weak_concepts else None

    conn = get_conn()
    _execute(conn, f"""
        INSERT INTO interactions
        (user_id, session_id, timestamp, interaction_type, source, study_topic,
         tutorial_section, practice_question, difficulty, extra_json)
        VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})
    """, (_uid(), _sid() or "", datetime.now().isoformat(),
          "followup", "system", study_topic, tutorial_section,
          practice_question, difficulty,
          json.dumps(extra) if extra else None))
    conn.commit()
    conn.close()


def log_followup_answer(practice_question, user_answer, is_correct, time_taken_seconds,
                        answer_text=None, study_topic=None, tutorial_section=None,
                        difficulty=None):
    """Log user's answer to a system-generated follow-up."""
    conn = get_conn()
    _execute(conn, f"""
        INSERT INTO interactions
        (user_id, session_id, timestamp, interaction_type, source, study_topic,
         tutorial_section, practice_question, user_answer, is_correct,
         time_taken_seconds, answer_text, difficulty)
        VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})
    """, (_uid(), _sid() or "", datetime.now().isoformat(),
          "followup_answer", "user", study_topic, tutorial_section,
          practice_question, user_answer, 1 if is_correct else 0,
          time_taken_seconds, answer_text, difficulty))
    conn.commit()
    conn.close()


def log_practice_requested(code_context, study_topic=None, tutorial_section=None):
    """Log that a user requested practice questions."""
    extra = {"code_context": code_context} if code_context else None

    conn = get_conn()
    _execute(conn, f"""
        INSERT INTO interactions
        (user_id, session_id, timestamp, interaction_type, study_topic,
         tutorial_section, practice_requested, extra_json)
        VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})
    """, (_uid(), _sid() or "", datetime.now().isoformat(),
          "practice_request", study_topic, tutorial_section, 1,
          json.dumps(extra) if extra else None))
    conn.commit()
    conn.close()


# ─── Messages & Insights ─────────────────────────────────────────

def save_message(role, content, session_id=None):
    """Save a coach or user message to the messages table."""
    sid = session_id or _sid()
    if not sid:
        return
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO messages (session_id, user_id, role, content) VALUES ({_P}, {_P}, {_P}, {_P})",
        (sid, _uid(), role, content)
    )
    conn.commit()
    conn.close()


def get_session_messages(session_id=None):
    """Get all messages for a session."""
    sid = session_id or _sid()
    if not sid:
        return []
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM messages WHERE session_id = {_P} ORDER BY id",
        (sid,)
    )
    result = _fetchall(cur)
    conn.close()
    return result


def save_insight(analysis, session_id=None):
    """Save a session analysis insight."""
    sid = session_id or _sid()
    if not sid:
        return
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO insights (user_id, session_id, analysis) VALUES ({_P}, {_P}, {_P})",
        (_uid(), sid, json.dumps(analysis) if isinstance(analysis, dict) else analysis)
    )
    conn.commit()
    conn.close()
    print(f"  [DB] Insight saved for session {sid}")


def get_recent_insights(limit=3):
    """Get most recent N insights for the current user."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM insights WHERE user_id = {_P} ORDER BY id DESC LIMIT {_P}",
        (_uid(), limit)
    )
    result = _fetchall(cur)
    conn.close()
    return result


def get_last_activity_time(session_id=None):
    """Get the timestamp of the last message in a session."""
    sid = session_id or _sid()
    if not sid:
        return None
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT MAX(timestamp) as last_ts FROM messages WHERE session_id = {_P}",
        (sid,)
    )
    row = _fetchone(cur)
    conn.close()
    return row["last_ts"] if row else None


def get_open_sessions_for_user(user_id, exclude_session_id=None):
    """Return sessions whose `end_time IS NULL` for a given user.

    Used by the orphan-cleanup pass on connect: when a prior WebSocket
    session was never cleanly closed (process kill, OS sleep, etc), the
    session row stays open and its analyzer never ran. On the next
    connect we walk these and drain them through the analyzer.

    Returns a list of dicts with keys: session_id, start_time, n_msgs.
    Sessions are returned oldest-first so the analyzer processes them
    in chronological order.
    """
    if not user_id:
        return []
    conn = get_conn()
    cur = _execute(conn, f"""
        SELECT s.session_id, s.start_time,
               (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.session_id) AS n_msgs
        FROM sessions s
        WHERE s.user_id = {_P}
          AND s.end_time IS NULL
          {('AND s.session_id != ' + _P) if exclude_session_id else ''}
        ORDER BY s.start_time ASC
    """, (user_id, exclude_session_id) if exclude_session_id else (user_id,))
    rows = _fetchall(cur)
    conn.close()
    return rows


# Initialize on import
try:
    init_db()
    print("[DB] init_db() OK", flush=True)
except Exception as e:
    print(f"[DB] ❌ init_db() failed: {e}", flush=True)
    import traceback
    traceback.print_exc()
