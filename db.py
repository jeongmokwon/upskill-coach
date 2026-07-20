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

        # Migrate: user_profiles phase-tracking columns (Phase 0/1 flow).
        # Phase 0 = 'discovery' — LLM co-discovers goal + first bite with
        # user over ~3 days. Phase 1 = 'first_bite' — LLM nudges toward
        # doing that specific bite in the evening window. phase_started_at
        # is NULL until the first discovery interaction fires; the timer
        # starts then, not on migration.
        for col, ddl in [
            ("phase", "TEXT DEFAULT 'discovery'"),
            ("phase_started_at", "TEXT"),
            ("agreed_first_bite", "TEXT DEFAULT ''"),
            ("agreed_at", "TEXT"),
            # The goal chain agreed during discovery ("career change to
            # ML → build one small ML project myself"). Without this
            # persisted, the goal lives only in SMS history, gets
            # truncated past HISTORY_LIMIT, and the LLM falls back to
            # the stale web-onboarding `goal` field — observed to
            # produce goal hallucination mid-conversation.
            ("agreed_goal", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.cursor().execute(f"ALTER TABLE user_profiles ADD COLUMN {col} {ddl}")
                conn.commit()
            except Exception:
                conn.rollback()

        # Migrate: messages.channel + messages.direction (added with SMS
        # tutor). channel='web' is the historical row type; 'sms' rows are
        # written by the SMS slot handlers. direction is only meaningful
        # for SMS ('in' = user→us, 'out' = us→user); web rows leave it ''.
        # Each ALTER in its own transaction so a duplicate-column error on
        # one column does not poison the other.
        for col, ddl in [
            ("channel", "TEXT DEFAULT 'web'"),
            ("direction", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.cursor().execute(f"ALTER TABLE messages ADD COLUMN {col} {ddl}")
                conn.commit()
            except Exception:
                conn.rollback()

        # Screen-observer tables. Deliberately SEPARATE from the web
        # `sessions` table: the web analyzer + orphan-cleanup pass walk
        # open rows in `sessions` and would try to analyze observer
        # sessions (which have no chat messages). Isolation keeps both
        # lifecycles from interfering.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS observe_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT
            )
        """)
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS observations (
                id SERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                summary TEXT NOT NULL
            )
        """)
        # SMS pilot signups (web opt-in form /sms-signup). Rows are
        # consent records: phone + timestamp of the checked-box
        # submission. status='pending' until the founder activates the
        # user manually — signup alone never triggers messages.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS sms_signups (
                id SERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                consented_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
        """)
        # Unified append-only event log (WEEK1_ORDER T1, brief §4.1).
        # Dialect discipline per D1.3: this table is touched only by
        # INSERT and SELECT; payload is JSON serialized to TEXT in
        # Python (no engine JSON functions anywhere).
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS events (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                schema_version INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'server'
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events (user_id, ts)"
        )
        # Prompt version registry (WEEK1_ORDER T2, brief §4.3). A row
        # per distinct prompt-template content ever observed in use;
        # content-hash is the identity (same trick as git). Register-
        # on-read: the running system records what actually ran.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS prompt_versions (
                hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                first_seen TEXT NOT NULL
            )
        """)
        conn.commit()
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
            # Phase 0/1 flow — see Postgres branch for rationale.
            ("phase", "TEXT DEFAULT 'discovery'"),
            ("phase_started_at", "TEXT"),
            ("agreed_first_bite", "TEXT DEFAULT ''"),
            ("agreed_at", "TEXT"),
            ("agreed_goal", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE user_profiles ADD COLUMN {col} {default}")
            except Exception:
                pass
        # SMS tutor migration — see Postgres branch above for rationale.
        for col, default in [
            ("channel", "TEXT DEFAULT 'web'"),
            ("direction", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {default}")
            except Exception:
                pass
        # Screen-observer tables — see Postgres branch for isolation
        # rationale (kept separate from web `sessions`).
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS observe_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT
            );

            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                summary TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sms_signups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL,
                consented_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                schema_version INTEGER NOT NULL DEFAULT 1,
                source TEXT NOT NULL DEFAULT 'server'
            );

            CREATE INDEX IF NOT EXISTS idx_events_user_ts ON events (user_id, ts);

            CREATE TABLE IF NOT EXISTS prompt_versions (
                hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                content TEXT NOT NULL,
                first_seen TEXT NOT NULL
            );
        """)

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


# ─── SMS tutor helpers ───────────────────────────────────────────
#
# SMS conversations don't follow the web "session per study sitting"
# model — they're an ambient, ongoing thread. We store them in the same
# `messages` table (channel='sms') under a stable synthetic session_id
# `sms-<user_id>` so they're easy to fetch as one rolling thread without
# adding a second table. No row is needed in `sessions` for this — the
# schema has no FK, and SMS history is logically separate from study
# sessions anyway.

def _sms_sid(user_id):
    return f"sms-{user_id}"


def save_sms_message(user_id, role, content, direction):
    """Append one SMS message to the rolling thread for `user_id`.

    role: 'user' or 'assistant' (matches Anthropic API shape so the
          thread can be fed straight back into Claude)
    direction: 'in' (user → us) or 'out' (us → user)
    """
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO messages "
        f"(session_id, user_id, role, content, channel, direction) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
        (_sms_sid(user_id), user_id, role, content, "sms", direction)
    )
    conn.commit()
    conn.close()


def get_recent_sms_messages(user_id, limit=20, since=None):
    """Return last N SMS messages for `user_id`, oldest-first.

    Format matches Anthropic's messages array — [{role, content}, ...] —
    so it can be passed straight into a Claude call as conversation
    history.

    If `since` (ISO-8601 string) is provided, only messages with
    `timestamp > since` are returned. This lets the caller scope
    the LLM's visible history to the current phase, so old
    conversations from before a phase transition don't bleed into
    the current mode and cause the LLM to reconcile-then-hallucinate.
    """
    conn = get_conn()
    if since:
        cur = _execute(conn,
            f"SELECT role, content FROM messages "
            f"WHERE session_id = {_P} AND channel = 'sms' "
            f"AND timestamp > {_P} "
            f"ORDER BY id DESC LIMIT {_P}",
            (_sms_sid(user_id), since, limit)
        )
    else:
        cur = _execute(conn,
            f"SELECT role, content FROM messages "
            f"WHERE session_id = {_P} AND channel = 'sms' "
            f"ORDER BY id DESC LIMIT {_P}",
            (_sms_sid(user_id), limit)
        )
    rows = _fetchall(cur)
    conn.close()
    rows.reverse()  # oldest-first for the LLM
    return [{"role": r["role"], "content": r["content"]} for r in rows]


def get_today_sessions_for_user(user_id, tz_offset_hours=-8):
    """Web (channel='web') sessions started today, in the user's local TZ.

    Used by the 9pm evening slot to say "you covered X today, do one
    more small step". tz_offset_hours defaults to PT (-8 PST, -7 PDT —
    DST drift is acceptable for an MVP single user; we'll only be off
    near the date boundary).

    Returns list of dicts (session_id, study_topic, start_time,
    end_time), oldest-first.
    """
    from datetime import datetime, timedelta, timezone
    tz = timezone(timedelta(hours=tz_offset_hours))
    now_local = datetime.now(tz)
    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    # Sessions store start_time as naive ISO (datetime.now().isoformat() in
    # start_session). Compare lex on the local-naive ISO — close enough
    # for one-user MVP and avoids needing to bulk-rewrite the timestamp
    # storage convention.
    threshold_iso = today_start_local.replace(tzinfo=None).isoformat()
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT session_id, study_topic, start_time, end_time "
        f"FROM sessions WHERE user_id = {_P} AND start_time >= {_P} "
        f"ORDER BY start_time ASC",
        (user_id, threshold_iso)
    )
    rows = _fetchall(cur)
    conn.close()
    return rows


# ─── Unified append-only event log (WEEK1_ORDER T1) ──────────────
#
# Brief §4.1: "Nothing that happens in the system may be unrecorded."
# One timeline per user. Append-only by convention — this module
# exports INSERT and SELECT helpers only; there is no update/delete
# path. Dialect-neutral SQL only (D1.3).

EVENTS_SCHEMA_VERSION = 1


def log_event(user_id, kind, payload=None, source="server"):
    """Append one event to the unified log. NEVER raises — event
    logging must not be able to break the main flow. Failures are
    printed (and thus visible in Render logs) but swallowed.

    user_id may be None/'' for events not yet attributable to a user
    (recorded under '_unknown' rather than dropped)."""
    try:
        conn = get_conn()
        _execute(conn,
            f"INSERT INTO events (user_id, ts, kind, payload, schema_version, source) "
            f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
            (user_id or "_unknown", datetime.now().isoformat(), kind,
             json.dumps(payload or {}, ensure_ascii=False),
             EVENTS_SCHEMA_VERSION, source)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[EVENTS] ⚠️ log_event({kind}) failed: {e}", flush=True)


def register_prompt_version(name, content):
    """Content-hash a prompt template; record it if unseen. Returns
    the hash either way. NEVER raises outward — a registry hiccup
    must not block message sending.

    Dialect discipline (D1.3): SELECT-then-INSERT, no upsert. A
    concurrent duplicate INSERT hits the PK and is swallowed — the
    row already exists, which is the outcome we wanted.
    """
    import hashlib
    h = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    try:
        conn = get_conn()
        cur = _execute(conn,
            f"SELECT hash FROM prompt_versions WHERE hash = {_P}", (h,))
        exists = _fetchone(cur)
        if not exists:
            try:
                _execute(conn,
                    f"INSERT INTO prompt_versions (hash, name, content, first_seen) "
                    f"VALUES ({_P}, {_P}, {_P}, {_P})",
                    (h, name, content, datetime.now().isoformat()))
                conn.commit()
                print(f"  [PROMPTS] New version registered: {name}@{h}", flush=True)
            except Exception:
                conn.rollback()  # concurrent insert — already there
        conn.close()
        if not exists:
            # "Prompt version changed" is an event class the brief
            # names explicitly (§4.1).
            log_event("_system", "prompt_version_registered",
                      {"name": name, "hash": h}, source="system")
    except Exception as e:
        print(f"[PROMPTS] ⚠️ register failed ({name}): {e}", flush=True)
    return h


def get_prompt_version(h):
    """Retrieve the exact prompt template text by hash, or None."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT hash, name, content, first_seen FROM prompt_versions "
        f"WHERE hash = {_P}", (h,))
    row = _fetchone(cur)
    conn.close()
    return row


def get_events(user_id, limit=200, since=None):
    """Read a user's timeline, oldest-first. `since` = ISO ts filter."""
    conn = get_conn()
    if since:
        cur = _execute(conn,
            f"SELECT ts, kind, payload, source FROM events "
            f"WHERE user_id = {_P} AND ts > {_P} ORDER BY id DESC LIMIT {_P}",
            (user_id, since, limit))
    else:
        cur = _execute(conn,
            f"SELECT ts, kind, payload, source FROM events "
            f"WHERE user_id = {_P} ORDER BY id DESC LIMIT {_P}",
            (user_id, limit))
    rows = _fetchall(cur)
    conn.close()
    rows.reverse()
    return rows


# ─── Phase 0/1 flow helpers ──────────────────────────────────────
#
# The SMS companion runs a two-phase micro-experiment:
#   Phase 0 (discovery) — LLM co-discovers with the user, over up to
#     3 days, a rough goal + starting position + one concrete 15-min
#     "first bite" they'll attempt in the evening window.
#   Phase 1 (first_bite) — LLM shifts to nudging the user to actually
#     do that specific bite when their evening window opens.
#
# Phase transition is triggered by the LLM emitting a [COMMIT: "..."]
# marker in its response when it detects user agreement. The server
# parses the marker, saves the bite text, transitions phase, and
# strips the marker before sending to the user.

def get_user_phase(user_id):
    """Return {'phase', 'phase_started_at', 'agreed_first_bite',
    'agreed_at', 'agreed_goal'} for user_id. Missing user → default
    discovery state."""
    prof = get_user_profile_by_id(user_id) or {}
    return {
        "phase": prof.get("phase") or "discovery",
        "phase_started_at": prof.get("phase_started_at"),
        "agreed_first_bite": prof.get("agreed_first_bite") or "",
        "agreed_at": prof.get("agreed_at"),
        "agreed_goal": prof.get("agreed_goal") or "",
    }


def ensure_user_profile_row(user_id):
    """Create a minimal user_profiles row if none exists. Idempotent.

    Root-cause guard: every phase-state writer below uses UPDATE,
    and UPDATE against a missing row is a silent 0-row no-op — the
    endpoint reports success while nothing persists. Observed in
    prod: the SMS tutor user never completed web onboarding on this
    database, so no row existed and goal/phase/timer writes all
    evaporated for days. Callers can't be trusted to know whether
    onboarding ever ran, so every writer calls this first.
    """
    if not user_id:
        return
    if get_user_profile_by_id(user_id):
        return
    now = datetime.now().isoformat()
    conn = get_conn()
    if DB_TYPE == "postgres":
        _execute(conn, """
            INSERT INTO user_profiles (user_id, user_name, created_at, updated_at)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id) DO NOTHING
        """, (user_id, user_id, now, now))
    else:
        _execute(conn, """
            INSERT OR IGNORE INTO user_profiles (user_id, user_name, created_at, updated_at)
            VALUES (?, ?, ?, ?)
        """, (user_id, user_id, now, now))
    conn.commit()
    conn.close()
    print(f"  [DB] Created minimal profile row for {user_id}", flush=True)


def set_agreed_goal(user_id, goal_text, source="llm_marker"):
    """Persist the goal chain agreed during discovery conversation.
    Callable any number of times — later agreements refine earlier.
    Emits the goal_set event here (single source of truth) so every
    caller path — marker parse, admin rescue — is covered once."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET agreed_goal = {_P} WHERE user_id = {_P}",
        (goal_text, user_id)
    )
    conn.commit()
    conn.close()
    print(f"  [DB] Agreed goal saved for {user_id}: {goal_text!r}", flush=True)
    log_event(user_id, "goal_set", {"goal": goal_text}, source=source)


def ensure_phase_timer_started(user_id):
    """Idempotent: if user is in discovery and timer NULL, stamp it now.
    Returns the phase_started_at (existing or freshly set)."""
    ensure_user_profile_row(user_id)
    state = get_user_phase(user_id)
    if state["phase"] != "discovery":
        return state["phase_started_at"]
    if state["phase_started_at"]:
        return state["phase_started_at"]
    now = datetime.now().isoformat()
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET phase_started_at = {_P} WHERE user_id = {_P}",
        (now, user_id)
    )
    conn.commit()
    conn.close()
    print(f"  [DB] Phase 0 timer started for {user_id} at {now}", flush=True)
    log_event(user_id, "phase_timer_started", {"phase_started_at": now}, source="cron")
    return now


def days_in_discovery(user_id):
    """Whole days elapsed since phase_started_at. 0 on the same day.
    Returns 0 if timer not yet started."""
    state = get_user_phase(user_id)
    if not state["phase_started_at"]:
        return 0
    try:
        started = datetime.fromisoformat(state["phase_started_at"])
    except Exception:
        return 0
    return (datetime.now() - started).days


def commit_first_bite(user_id, bite_text, source="llm_marker"):
    """Save the agreed-upon first bite and transition to Phase 1."""
    ensure_user_profile_row(user_id)
    now = datetime.now().isoformat()
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET "
        f"phase = 'first_bite', "
        f"agreed_first_bite = {_P}, "
        f"agreed_at = {_P} "
        f"WHERE user_id = {_P}",
        (bite_text, now, user_id)
    )
    conn.commit()
    conn.close()
    print(f"  [DB] Phase transition first_bite for {user_id}: {bite_text!r}", flush=True)
    log_event(user_id, "phase_transition",
              {"to": "first_bite", "bite": bite_text}, source=source)


def reset_phase_state(user_id, source="admin"):
    """Rescue: reset the user to a fresh Phase 0 with the timer
    starting NOW. Old SMS history remains in the DB but is filtered
    out of LLM context by the `since=phase_started_at` scope in
    get_recent_sms_messages(). Idempotent."""
    ensure_user_profile_row(user_id)
    now = datetime.now().isoformat()
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET "
        f"phase = 'discovery', "
        f"phase_started_at = {_P}, "
        f"agreed_first_bite = '', "
        f"agreed_at = NULL, "
        f"agreed_goal = '' "
        f"WHERE user_id = {_P}",
        (now, user_id)
    )
    conn.commit()
    conn.close()
    print(f"  [DB] Phase state reset for {user_id} at {now}", flush=True)
    log_event(user_id, "phase_reset", {"phase_started_at": now}, source=source)
    return now


# ─── Screen observer ─────────────────────────────────────────────
#
# Local agent (observer.py) runs on the user's laptop: declares a
# session, uploads periodic screenshots. Server summarizes each
# screenshot to TEXT via a small vision model and stores only the
# text here — images are never persisted (Render disk is ephemeral
# anyway, and text is what the companion brain consumes).

def start_observe_session(user_id):
    """Open a new observe session. Closes any dangling open sessions
    for this user first (crashed agent, closed laptop lid, etc.)."""
    now = datetime.now().isoformat()
    conn = get_conn()
    _execute(conn,
        f"UPDATE observe_sessions SET ended_at = {_P} "
        f"WHERE user_id = {_P} AND ended_at IS NULL",
        (now, user_id)
    )
    sid = str(uuid.uuid4())[:8]
    _execute(conn,
        f"INSERT INTO observe_sessions (session_id, user_id, started_at) "
        f"VALUES ({_P}, {_P}, {_P})",
        (sid, user_id, now)
    )
    conn.commit()
    conn.close()
    print(f"  [DB] Observe session started: {sid}", flush=True)
    return sid


def end_observe_session(session_id):
    now = datetime.now().isoformat()
    conn = get_conn()
    _execute(conn,
        f"UPDATE observe_sessions SET ended_at = {_P} WHERE session_id = {_P}",
        (now, session_id)
    )
    conn.commit()
    conn.close()
    print(f"  [DB] Observe session ended: {session_id}", flush=True)


def save_observation(session_id, user_id, summary):
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO observations (session_id, user_id, ts, summary) "
        f"VALUES ({_P}, {_P}, {_P}, {_P})",
        (session_id, user_id, datetime.now().isoformat(), summary)
    )
    conn.commit()
    conn.close()


def save_sms_signup(phone):
    """Record a web opt-in consent (phone already normalized E.164).
    Returns the row id. Duplicate phones allowed — each submission is
    its own consent record with its own timestamp."""
    conn = get_conn()
    cur = _execute(conn,
        f"INSERT INTO sms_signups (phone, consented_at) VALUES ({_P}, {_P})",
        (phone, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()
    print(f"  [DB] SMS signup consent recorded for {phone}", flush=True)


def get_open_observe_session(user_id):
    """Most recent open observe session for user, or None. Used to
    decide whether an on-demand capture request is worth making."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT session_id, started_at FROM observe_sessions "
        f"WHERE user_id = {_P} AND ended_at IS NULL "
        f"ORDER BY started_at DESC LIMIT 1",
        (user_id,)
    )
    row = _fetchone(cur)
    conn.close()
    return row


def get_recent_observations(user_id, minutes=30, limit=5):
    """Last N observations within the past `minutes`, oldest-first.
    Empty list when no agent is running — callers render that as
    'no live screen context'."""
    from datetime import timedelta
    threshold = (datetime.now() - timedelta(minutes=minutes)).isoformat()
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT ts, summary FROM observations "
        f"WHERE user_id = {_P} AND ts > {_P} "
        f"ORDER BY id DESC LIMIT {_P}",
        (user_id, threshold, limit)
    )
    rows = _fetchall(cur)
    conn.close()
    rows.reverse()
    return rows


# Initialize on import
try:
    init_db()
    print("[DB] init_db() OK", flush=True)
except Exception as e:
    print(f"[DB] ❌ init_db() failed: {e}", flush=True)
    import traceback
    traceback.print_exc()
