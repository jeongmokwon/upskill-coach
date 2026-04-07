"""
SQLite database for tracking user interactions, practice results, and sessions.
"""

import sqlite3
import os
import time
import json
import uuid
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "upskill_coach.db")
USER_ID = "jeongmo"  # default, overridden by onboarding


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_conn()
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
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
    """)
    # Migrate: add columns if missing on existing DBs
    try:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN difficulty INTEGER DEFAULT 3")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN user_condition INTEGER DEFAULT 3")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN studying TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE user_profiles ADD COLUMN hint_preference TEXT DEFAULT 'hints'")
    except Exception:
        pass
    conn.commit()
    conn.close()


def set_user_id(uid):
    """Set the active user ID for this session."""
    global USER_ID
    USER_ID = uid


def get_user_profile(user_name):
    """Look up a user profile by name (case-insensitive). Returns dict or None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM user_profiles WHERE LOWER(user_name) = LOWER(?)",
        (user_name,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def create_user_profile(user_name, goal="", background="", studying="", hint_preference="hints", difficulty=3, user_condition=3):
    """Create a new user profile. Returns the user_id."""
    uid = user_name.lower().replace(" ", "_")
    now = datetime.now().isoformat()
    conn = get_conn()
    conn.execute("""
        INSERT OR REPLACE INTO user_profiles
        (user_id, user_name, goal, background, studying, hint_preference, difficulty, user_condition, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (uid, user_name, goal, background, studying, hint_preference, difficulty, user_condition, now, now))
    conn.commit()
    conn.close()
    return uid


def update_user_profile(user_id, **kwargs):
    """Update specific fields of a user profile."""
    allowed = {"goal", "background", "user_name", "studying", "hint_preference", "difficulty", "user_condition"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return
    updates["updated_at"] = datetime.now().isoformat()
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    conn = get_conn()
    conn.execute(
        f"UPDATE user_profiles SET {set_clause} WHERE user_id = ?",
        list(updates.values()) + [user_id]
    )
    conn.commit()
    conn.close()


# ─── Session management ───────────────────────────────────────────

_current_session_id = None


def start_session(study_topic=""):
    """Start a new session. Migrate previous session times."""
    global _current_session_id
    _current_session_id = str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()

    conn = get_conn()

    # Migrate: move current → last
    row = conn.execute(
        "SELECT current_session_id FROM user_state WHERE user_id = ?", (USER_ID,)
    ).fetchone()

    if row and row["current_session_id"]:
        prev_sid = row["current_session_id"]
        prev_session = conn.execute(
            "SELECT start_time, end_time FROM sessions WHERE session_id = ?", (prev_sid,)
        ).fetchone()
        if prev_session:
            conn.execute("""
                UPDATE user_state SET
                    last_session_start_time = ?,
                    last_session_end_time = ?,
                    current_session_id = ?
                WHERE user_id = ?
            """, (prev_session["start_time"], prev_session["end_time"] or now,
                  _current_session_id, USER_ID))
    else:
        conn.execute("""
            INSERT OR REPLACE INTO user_state (user_id, current_session_id)
            VALUES (?, ?)
        """, (USER_ID, _current_session_id))

    # Create new session row
    conn.execute("""
        INSERT INTO sessions (session_id, user_id, study_topic, start_time)
        VALUES (?, ?, ?, ?)
    """, (_current_session_id, USER_ID, study_topic, now))

    conn.commit()
    conn.close()
    print(f"  [DB] Session started: {_current_session_id}")
    return _current_session_id


def end_session():
    """End the current session."""
    global _current_session_id
    if not _current_session_id:
        return

    now = datetime.now().isoformat()
    conn = get_conn()
    conn.execute(
        "UPDATE sessions SET end_time = ? WHERE session_id = ?",
        (now, _current_session_id)
    )
    conn.commit()
    conn.close()
    print(f"  [DB] Session ended: {_current_session_id}")
    _current_session_id = None


def get_session_id():
    return _current_session_id


# ─── Idle / pause detection ───────────────────────────────────────

IDLE_THRESHOLD_SECONDS = 300  # 5 minutes = likely a break

_last_activity_time = None


def touch_activity():
    """Record that user did something. Detect pause/resume gaps."""
    global _last_activity_time
    now = datetime.now()

    if _last_activity_time and _current_session_id:
        gap = (now - _last_activity_time).total_seconds()
        if gap >= IDLE_THRESHOLD_SECONDS:
            # Log a pause at the old time and resume now
            conn = get_conn()
            conn.execute("""
                INSERT INTO interactions
                (user_id, session_id, timestamp, interaction_type, source,
                 study_topic, extra_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (USER_ID, _current_session_id, _last_activity_time.isoformat(),
                  "session_pause", "system", None,
                  json.dumps({"idle_seconds": round(gap)})))
            conn.execute("""
                INSERT INTO interactions
                (user_id, session_id, timestamp, interaction_type, source,
                 study_topic, extra_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (USER_ID, _current_session_id, now.isoformat(),
                  "session_resume", "system", None,
                  json.dumps({"idle_seconds": round(gap)})))
            conn.commit()
            conn.close()
            print(f"  [DB] Detected break: {gap/60:.0f} min idle → logged pause/resume")

    _last_activity_time = now


def mark_pending_followups_skipped():
    """Mark any unanswered followups as skipped.
    A followup is 'pending' if it has no matching followup_answer after it."""
    if not _current_session_id:
        return
    conn = get_conn()
    # Find followup IDs that were never answered
    rows = conn.execute("""
        SELECT f.id, f.practice_question FROM interactions f
        WHERE f.session_id = ?
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
    """, (_current_session_id,)).fetchall()

    if rows:
        ids = [r["id"] for r in rows]
        conn.execute(
            f"UPDATE interactions SET skipped = 1 WHERE id IN ({','.join('?' * len(ids))})",
            ids
        )
        conn.commit()
        print(f"  [DB] Marked {len(ids)} unanswered followup(s) as skipped")
    conn.close()


# ─── Interaction logging ──────────────────────────────────────────

def log_question(question_text, answer_text, tutorial_section=None, study_topic=None):
    """Log a Q&A interaction (terminal question → Claude answer)."""
    conn = get_conn()
    conn.execute("""
        INSERT INTO interactions
        (user_id, session_id, timestamp, interaction_type, study_topic,
         tutorial_section, question_text, answer_text)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (USER_ID, _current_session_id or "", datetime.now().isoformat(),
          "question", study_topic, tutorial_section, question_text, answer_text))
    conn.commit()
    conn.close()


def log_practice(practice_question, user_answer, is_correct, time_taken_seconds,
                 study_topic=None, tutorial_section=None, code_context=None,
                 answer_text=None, difficulty=None, practice_topic=None):
    """Log a practice question attempt."""
    import json as _json
    extra = {}
    if code_context:
        extra["code_context"] = code_context
    if practice_topic:
        extra["practice_topic"] = practice_topic
    extra = extra or None

    conn = get_conn()
    conn.execute("""
        INSERT INTO interactions
        (user_id, session_id, timestamp, interaction_type, study_topic,
         tutorial_section, practice_question, user_answer, is_correct,
         time_taken_seconds, answer_text, difficulty, extra_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (USER_ID, _current_session_id or "", datetime.now().isoformat(),
          "practice", study_topic, tutorial_section, practice_question,
          user_answer, 1 if is_correct else 0, time_taken_seconds,
          answer_text, difficulty,
          _json.dumps(extra) if extra else None))
    conn.commit()
    conn.close()


def get_session_interactions(session_id=None):
    """Get all interactions for a session as a list of dicts."""
    sid = session_id or _current_session_id
    if not sid:
        return []
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM interactions WHERE session_id = ? ORDER BY id",
        (sid,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_user_interactions():
    """Get ALL interactions for the user across all sessions, ordered by id."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM interactions WHERE user_id = ? ORDER BY id",
        (USER_ID,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_topic_history(topic):
    """Get the most recent graded interactions for a topic across ALL sessions.
    Returns list of dicts with is_correct and difficulty, most recent first."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT is_correct, difficulty, extra_json FROM interactions
        WHERE user_id = ?
          AND interaction_type IN ('practice', 'followup_answer')
          AND user_answer IS NOT NULL
          AND extra_json LIKE ?
        ORDER BY id DESC
        LIMIT 5
    """, (USER_ID, f'%"practice_topic": "{topic}"%')).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def log_followup(practice_question, weak_concepts, study_topic=None,
                 tutorial_section=None, difficulty=None):
    """Log a system-generated follow-up question."""
    import json as _json
    extra = {"weak_concepts": weak_concepts} if weak_concepts else None

    conn = get_conn()
    conn.execute("""
        INSERT INTO interactions
        (user_id, session_id, timestamp, interaction_type, source, study_topic,
         tutorial_section, practice_question, difficulty, extra_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (USER_ID, _current_session_id or "", datetime.now().isoformat(),
          "followup", "system", study_topic, tutorial_section,
          practice_question, difficulty,
          _json.dumps(extra) if extra else None))
    conn.commit()
    conn.close()


def log_followup_answer(practice_question, user_answer, is_correct, time_taken_seconds,
                        answer_text=None, study_topic=None, tutorial_section=None,
                        difficulty=None):
    """Log user's answer to a system-generated follow-up."""
    conn = get_conn()
    conn.execute("""
        INSERT INTO interactions
        (user_id, session_id, timestamp, interaction_type, source, study_topic,
         tutorial_section, practice_question, user_answer, is_correct,
         time_taken_seconds, answer_text, difficulty)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (USER_ID, _current_session_id or "", datetime.now().isoformat(),
          "followup_answer", "user", study_topic, tutorial_section,
          practice_question, user_answer, 1 if is_correct else 0,
          time_taken_seconds, answer_text, difficulty))
    conn.commit()
    conn.close()


def log_practice_requested(code_context, study_topic=None, tutorial_section=None):
    """Log that a user requested practice questions."""
    import json as _json
    extra = {"code_context": code_context} if code_context else None

    conn = get_conn()
    conn.execute("""
        INSERT INTO interactions
        (user_id, session_id, timestamp, interaction_type, study_topic,
         tutorial_section, practice_requested, extra_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (USER_ID, _current_session_id or "", datetime.now().isoformat(),
          "practice_request", study_topic, tutorial_section, 1,
          _json.dumps(extra) if extra else None))
    conn.commit()
    conn.close()


# ─── Messages & Insights ─────────────────────────────────────────

def save_message(role, content, session_id=None):
    """Save a coach or user message to the messages table."""
    sid = session_id or _current_session_id
    if not sid:
        return
    conn = get_conn()
    conn.execute(
        "INSERT INTO messages (session_id, user_id, role, content) VALUES (?, ?, ?, ?)",
        (sid, USER_ID, role, content)
    )
    conn.commit()
    conn.close()


def get_session_messages(session_id=None):
    """Get all messages for a session."""
    sid = session_id or _current_session_id
    if not sid:
        return []
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id = ? ORDER BY id",
        (sid,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def save_insight(analysis, session_id=None):
    """Save a session analysis insight."""
    sid = session_id or _current_session_id
    if not sid:
        return
    conn = get_conn()
    conn.execute(
        "INSERT INTO insights (user_id, session_id, analysis) VALUES (?, ?, ?)",
        (USER_ID, sid, json.dumps(analysis) if isinstance(analysis, dict) else analysis)
    )
    conn.commit()
    conn.close()
    print(f"  [DB] Insight saved for session {sid}")


def get_recent_insights(limit=3):
    """Get most recent N insights for the current user."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM insights WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (USER_ID, limit)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_last_activity_time(session_id=None):
    """Get the timestamp of the last message in a session."""
    sid = session_id or _current_session_id
    if not sid:
        return None
    conn = get_conn()
    row = conn.execute(
        "SELECT MAX(timestamp) as last_ts FROM messages WHERE session_id = ?",
        (sid,)
    ).fetchone()
    conn.close()
    return row["last_ts"] if row else None


# Initialize on import
init_db()
