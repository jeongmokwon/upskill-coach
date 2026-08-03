"""
PostgreSQL/SQLite database for tracking user interactions, practice results, and sessions.
Uses DATABASE_URL env var for PostgreSQL (Render), falls back to SQLite locally.
"""

import os
import time
import json
import uuid
from datetime import datetime, timedelta

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
            # The user's OWN observable definition of "it started"
            # (e.g. "sat at the laptop and typed code into an
            # IDE/Colab"). Elicited during discovery, persisted via
            # the [IGNITION_DEF:] marker; ignition judgments are
            # scored against THIS, per user, not a generic rule.
            ("ignition_marker", "TEXT DEFAULT ''"),
            # Position in the current sequence plan (exploration v2:
            # the sequence lives server-side as state; the LLM only
            # receives the CURRENT step as its assignment). Mutable
            # like phase; every move is an event.
            ("plan_cursor", "INTEGER DEFAULT 0"),
            # Onboarding state machine (P0-A). Timestamps, not
            # booleans — duration is data. started_at = first coach
            # send to this user; completed_at is set by CODE when the
            # five required fields are all non-empty (goal, path,
            # bite, ignition marker, schedule) — never asserted by
            # the LLM.
            ("onboarding_started_at", "TEXT"),
            ("onboarding_completed_at", "TEXT"),
            # What the coach committed to doing for this user, as
            # confirmed by them — onboarding deliverable #3 (brief §7
            # onboarding arc). Missing it was why pilot user #1 gave
            # ten turns of himself and got nothing back.
            ("agreed_offer", "TEXT DEFAULT ''"),
            # 'provisional' (inferred by the analysis pass) vs
            # 'confirmed' (the user said it). See set_ignition_marker.
            ("ignition_marker_status", "TEXT DEFAULT ''"),
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
                status TEXT NOT NULL DEFAULT 'pending',
                name TEXT DEFAULT '',
                email TEXT DEFAULT '',
                consent_checkins INTEGER DEFAULT 0,
                consent_support INTEGER DEFAULT 0
            )
        """)
        # Migrate: per-purpose consent columns (TFV round-2, 2026-07-25).
        # Carrier review requires a separate opt-in per messaging
        # purpose, and the signup form now collects name/email so the
        # form can be completed without SMS consent. Each ALTER in its
        # own transaction (same rationale as user_profiles above).
        for col, ddl in [
            ("name", "TEXT DEFAULT ''"),
            ("email", "TEXT DEFAULT ''"),
            ("consent_checkins", "INTEGER DEFAULT 0"),
            ("consent_support", "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.cursor().execute(f"ALTER TABLE sms_signups ADD COLUMN {col} {ddl}")
                conn.commit()
            except Exception:
                conn.rollback()
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
        # Full rendered LLM inputs (T2b). One row PER CALL — the
        # exact system prompt + messages array the API received,
        # byte-for-byte, plus the response. "Raw is sacred" applied
        # to the LLM's input side: templates dedupe (above), rendered
        # calls never do — each is a unique flight-recorder snapshot.
        # TEXT uuid PK avoids RETURNING/lastrowid dialect branching.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS llm_calls (
                call_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                trigger TEXT NOT NULL,
                model TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                prompt_versions_json TEXT NOT NULL DEFAULT '{}',
                response_text TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_llm_calls_user_ts ON llm_calls (user_id, ts)"
        )
        # LearnerState v1 snapshots (T5). Append-only annotations of a
        # user's day, written by the nightly job. Re-annotation (brief
        # §4.2) means the same (user, day) can have MANY rows — each
        # tagged with the schema/prompt/model that produced it; newest
        # row under the current versions wins at read time. Nothing is
        # ever updated or deleted.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS learner_state_snapshots (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                created_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                prompt_version TEXT NOT NULL,
                model TEXT NOT NULL,
                state_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                llm_call_id TEXT
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_lss_user_day ON learner_state_snapshots (user_id, day)"
        )
        # User notes (exploration P3, brief §7). Sparse falsifiable
        # conditional statements about one user. Append-only: editing
        # a note = new row with same note_id and version+1; the
        # latest version per note_id wins at read time. Notes are a
        # DERIVED layer — raw traces stay the ground truth.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS user_notes (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                note_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                claim TEXT NOT NULL,
                given_json TEXT NOT NULL DEFAULT '{}',
                when_json TEXT NOT NULL DEFAULT '[]',
                expect TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                confidence TEXT NOT NULL DEFAULT 'hypothesis',
                source TEXT NOT NULL DEFAULT 'operator'
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_notes_user ON user_notes (user_id, note_id)"
        )
        # Sequence plans (exploration v2). Append-only plan CONTENT;
        # the moving cursor lives on user_profiles (mutable state,
        # like phase) and every move is an event.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS sequence_plans (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'operator'
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_plans_user ON sequence_plans (user_id, version)"
        )
        # Learning paths (T8, brief §7) — the three-layer route:
        # direction / project + done-condition / bites. Append-only
        # versions; onboarding's [PATH:] marker writes v1.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS learning_paths (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                direction TEXT NOT NULL,
                project TEXT NOT NULL DEFAULT '',
                project_done_condition TEXT NOT NULL DEFAULT '',
                bites_done TEXT NOT NULL DEFAULT '[]',
                current_bite TEXT NOT NULL DEFAULT '',
                next_candidates TEXT NOT NULL DEFAULT '[]',
                changed_by TEXT NOT NULL DEFAULT 'llm_marker',
                decision_id TEXT
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_paths_user ON learning_paths (user_id, version)"
        )
        # Learning materials (offer-loop arc). One row per thing the
        # user studies from — an uploaded file, a shared link, or a
        # source they only named in conversation. The LLM digest is
        # the coach's reading; user_description/wants_json are the
        # user's OWN account from the Theo-led walkthrough, and the
        # user's words always outrank the digest. walkthrough_status
        # reaches 'validated' only when the coach produced a sample of
        # its offer (e.g. an insider-plausible question) and the user
        # confirmed it rings true — that validation is what the
        # material_walkthrough onboarding field will key on.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS user_materials (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                orig_filename TEXT NOT NULL DEFAULT '',
                extracted_text TEXT NOT NULL DEFAULT '',
                digest TEXT NOT NULL DEFAULT '',
                user_description TEXT NOT NULL DEFAULT '',
                wants_json TEXT NOT NULL DEFAULT '[]',
                walkthrough_status TEXT NOT NULL DEFAULT 'none',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_materials_user ON user_materials (user_id)"
        )
        # Magic-link tokens: possession of the link IS the login for
        # /my (uploads, later the screen session). One row per user;
        # regenerating replaces the token, which invalidates any
        # leaked link.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS user_tokens (
                user_id TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        # Migrate: learning_paths.path_kind (brief §7 "Learning
        # types"). The direction/project+done-condition/bite middle
        # layer is project-shaped and does not fit every learner;
        # path_kind records which framing applies — 'deliverable'
        # (project with a done-condition) / 'coverage' (a body of
        # material to reach a level over) / 'duration' (sustained
        # practice). Own transaction, same rationale as above.
        for col, ddl in [
            ("path_kind", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.cursor().execute(f"ALTER TABLE learning_paths ADD COLUMN {col} {ddl}")
                conn.commit()
            except Exception:
                conn.rollback()
        # User profile briefs (brief §7 "User profile brief").
        # Generated at onboarding completion by the same call that
        # produces notes + the sequence plan. Versioned + append-only
        # like every other derived per-user artifact: a new read of
        # the user is a new row, never an overwrite. Structured where
        # a machine branches on it (learning_types), free-form where
        # only the LLM consumes it (personality). wants_json holds
        # VERBATIM user quotes — raw is sacred applies to
        # self-description.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS user_profile_briefs (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                job TEXT NOT NULL DEFAULT '',
                learning_types_json TEXT NOT NULL DEFAULT '[]',
                materials_json TEXT NOT NULL DEFAULT '[]',
                wants_json TEXT NOT NULL DEFAULT '[]',
                personality TEXT NOT NULL DEFAULT '',
                rationale TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'p7',
                llm_call_id TEXT
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_briefs_user ON user_profile_briefs (user_id, version)"
        )
        # Per-user send schedule (storage here; the hourly tick that
        # consumes it is P0-C). windows_json: [{"start": "HH:MM",
        # "end": "HH:MM"}] in the user's local (PT) day.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS user_schedule (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                windows_json TEXT NOT NULL,
                raw_text TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'llm_marker'
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_sched_user ON user_schedule (user_id, version)"
        )
        # Availability grid snapshots (brief §7). A DERIVED projection
        # of user_schedule + the event log (see availability.py), kept
        # as append-only versions: the events stay the truth, any row
        # here is rebuildable by re-running recompute(). A new version
        # is written only when the grid actually changed.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS availability_snapshots (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                grid_json TEXT NOT NULL,
                sources_json TEXT NOT NULL DEFAULT '{}',
                method_version TEXT NOT NULL DEFAULT 'v1'
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_avail_user ON availability_snapshots (user_id, version)"
        )
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
            ("ignition_marker", "TEXT DEFAULT ''"),
            ("plan_cursor", "INTEGER DEFAULT 0"),
            ("onboarding_started_at", "TEXT"),
            ("onboarding_completed_at", "TEXT"),
            ("agreed_offer", "TEXT DEFAULT ''"),
            ("ignition_marker_status", "TEXT DEFAULT ''"),
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
        # TFV round-2 migration — see Postgres branch for rationale.
        for col, default in [
            ("name", "TEXT DEFAULT ''"),
            ("email", "TEXT DEFAULT ''"),
            ("consent_checkins", "INTEGER DEFAULT 0"),
            ("consent_support", "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE sms_signups ADD COLUMN {col} {default}")
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
                status TEXT NOT NULL DEFAULT 'pending',
                name TEXT DEFAULT '',
                email TEXT DEFAULT '',
                consent_checkins INTEGER DEFAULT 0,
                consent_support INTEGER DEFAULT 0
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

            CREATE TABLE IF NOT EXISTS llm_calls (
                call_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                trigger TEXT NOT NULL,
                model TEXT NOT NULL,
                system_prompt TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                prompt_versions_json TEXT NOT NULL DEFAULT '{}',
                response_text TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_llm_calls_user_ts ON llm_calls (user_id, ts);

            CREATE TABLE IF NOT EXISTS learner_state_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                day TEXT NOT NULL,
                created_at TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                prompt_version TEXT NOT NULL,
                model TEXT NOT NULL,
                state_json TEXT NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                llm_call_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_lss_user_day ON learner_state_snapshots (user_id, day);

            CREATE TABLE IF NOT EXISTS user_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                note_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                claim TEXT NOT NULL,
                given_json TEXT NOT NULL DEFAULT '{}',
                when_json TEXT NOT NULL DEFAULT '[]',
                expect TEXT NOT NULL DEFAULT '',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                confidence TEXT NOT NULL DEFAULT 'hypothesis',
                source TEXT NOT NULL DEFAULT 'operator'
            );

            CREATE INDEX IF NOT EXISTS idx_notes_user ON user_notes (user_id, note_id);

            CREATE TABLE IF NOT EXISTS sequence_plans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                steps_json TEXT NOT NULL,
                rationale TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'operator'
            );

            CREATE INDEX IF NOT EXISTS idx_plans_user ON sequence_plans (user_id, version);

            CREATE TABLE IF NOT EXISTS learning_paths (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                direction TEXT NOT NULL,
                project TEXT NOT NULL DEFAULT '',
                project_done_condition TEXT NOT NULL DEFAULT '',
                bites_done TEXT NOT NULL DEFAULT '[]',
                current_bite TEXT NOT NULL DEFAULT '',
                next_candidates TEXT NOT NULL DEFAULT '[]',
                changed_by TEXT NOT NULL DEFAULT 'llm_marker',
                decision_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_paths_user ON learning_paths (user_id, version);

            CREATE TABLE IF NOT EXISTS user_materials (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                orig_filename TEXT NOT NULL DEFAULT '',
                extracted_text TEXT NOT NULL DEFAULT '',
                digest TEXT NOT NULL DEFAULT '',
                user_description TEXT NOT NULL DEFAULT '',
                wants_json TEXT NOT NULL DEFAULT '[]',
                walkthrough_status TEXT NOT NULL DEFAULT 'none',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_materials_user ON user_materials (user_id);

            CREATE TABLE IF NOT EXISTS user_tokens (
                user_id TEXT PRIMARY KEY,
                token TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                windows_json TEXT NOT NULL,
                raw_text TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'llm_marker'
            );

            CREATE INDEX IF NOT EXISTS idx_sched_user ON user_schedule (user_id, version);

            CREATE TABLE IF NOT EXISTS availability_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                grid_json TEXT NOT NULL,
                sources_json TEXT NOT NULL DEFAULT '{}',
                method_version TEXT NOT NULL DEFAULT 'v1'
            );

            CREATE INDEX IF NOT EXISTS idx_avail_user ON availability_snapshots (user_id, version);

            CREATE TABLE IF NOT EXISTS user_profile_briefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                ts TEXT NOT NULL,
                job TEXT NOT NULL DEFAULT '',
                learning_types_json TEXT NOT NULL DEFAULT '[]',
                materials_json TEXT NOT NULL DEFAULT '[]',
                wants_json TEXT NOT NULL DEFAULT '[]',
                personality TEXT NOT NULL DEFAULT '',
                rationale TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'p7',
                llm_call_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_briefs_user ON user_profile_briefs (user_id, version);
        """)
        # learning_paths.path_kind migration — see Postgres branch for
        # rationale. Runs after the table exists.
        for col, default in [
            ("path_kind", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE learning_paths ADD COLUMN {col} {default}")
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
    """Save a coach or user message to the messages table.

    timestamp is written explicitly from Python (never left to the
    column's CURRENT_TIMESTAMP default): SQLite's default writes
    "YYYY-MM-DD HH:MM:SS" (space separator, UTC) while every reader
    that compares this column — get_recent_sms_messages(since=...),
    get_last_activity_time — works with datetime.now().isoformat()
    values, and SQLite compares TEXT lexically (' ' < 'T'), so
    default-stamped rows sort before any same-day isoformat value
    and vanish from the filters. Postgres casts both, hiding the
    bug in prod. Writing isoformat here keeps one format per D1.3.
    """
    sid = session_id or _sid()
    if not sid:
        return
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO messages (session_id, user_id, role, content, timestamp) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P})",
        (sid, _uid(), role, content, datetime.now().isoformat())
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


def get_recent_insights(limit=3, user_id=None):
    """Get most recent N insights. `user_id=None` falls back to the
    thread-local current user (legacy web-chat convention); pilot-path
    callers pass user_id explicitly (T10)."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM insights WHERE user_id = {_P} ORDER BY id DESC LIMIT {_P}",
        (user_id or _uid(), limit)
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

    timestamp is written explicitly (isoformat) rather than left to
    the column default — see save_message for why the SQLite default
    breaks the `timestamp > since` phase filter.
    """
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO messages "
        f"(session_id, user_id, role, content, channel, direction, timestamp) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
        (_sms_sid(user_id), user_id, role, content, "sms", direction,
         datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_recent_sms_messages(user_id, limit=20, since=None,
                            with_time=False):
    """Return last N SMS messages for `user_id`, oldest-first.

    Format matches Anthropic's messages array — [{role, content}, ...] —
    so it can be passed straight into a Claude call as conversation
    history.

    If `since` (ISO-8601 string) is provided, only messages with
    `timestamp > since` are returned. This lets the caller scope
    the LLM's visible history to the current phase, so old
    conversations from before a phase transition don't bleed into
    the current mode and cause the LLM to reconcile-then-hallucinate.

    `with_time=True` prefixes each turn with how long ago it was
    ("[3시간 전] ..."). Observed failure without it: the coach
    referenced a message sent four hours earlier as "어제" — history
    carries no clock, so elapsed time was guessed.
    """
    conn = get_conn()
    if since:
        cur = _execute(conn,
            f"SELECT role, content, timestamp FROM messages "
            f"WHERE session_id = {_P} AND channel = 'sms' "
            f"AND timestamp > {_P} "
            f"ORDER BY id DESC LIMIT {_P}",
            (_sms_sid(user_id), since, limit)
        )
    else:
        cur = _execute(conn,
            f"SELECT role, content, timestamp FROM messages "
            f"WHERE session_id = {_P} AND channel = 'sms' "
            f"ORDER BY id DESC LIMIT {_P}",
            (_sms_sid(user_id), limit)
        )
    rows = _fetchall(cur)
    conn.close()
    rows.reverse()  # oldest-first for the LLM
    if not with_time:
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    import os as _os
    tz = int(_os.environ.get("TZ_OFFSET_HOURS", "-8"))
    now = datetime.now()
    days_kr = ["월", "화", "수", "목", "금", "토", "일"]
    out = []
    for r in rows:
        label = ""
        ts = r.get("timestamp")
        if ts:
            try:
                # timestamps may be datetime (pg) or ISO text (sqlite)
                when = ts if isinstance(ts, datetime) else \
                    datetime.fromisoformat(str(ts).replace("Z", ""))
                mins = (now - when).total_seconds() / 60.0
                if mins < 90:
                    rel = f"{max(1, round(mins))}분 전"
                elif mins < 60 * 36:
                    rel = f"{round(mins / 60)}시간 전"
                else:
                    rel = f"{round(mins / 1440)}일 전"
                # Absolute stamp alongside the relative one: with only
                # "35시간 전" the model had to do calendar arithmetic to
                # place a turn, and got it wrong twice (calling a
                # 4-hour-old message "어제", and a Wednesday-night
                # exchange "어젯밤" on a Friday).
                local = when + timedelta(hours=tz)
                abs_kr = (f"{days_kr[local.weekday()]}요일 "
                          f"{local.strftime('%H:%M')}")
                label = f"[{abs_kr}, {rel}] "
            except Exception:
                label = ""
        out.append({"role": r["role"], "content": label + r["content"]})
    return out


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


def get_last_event(user_id, kind, payload_contains=None):
    """Most recent event of `kind` for a user → dict {ts, payload}
    or None.
    `payload_contains` does a plain substring match on the JSON-as-TEXT
    payload (e.g. '"slot": "morning"' or a session id) — dialect-neutral
    LIKE, no engine JSON functions (D1.3). Used by the infra sweep
    (T6) to answer "when did X last happen?" cheaply."""
    conn = get_conn()
    if payload_contains:
        cur = _execute(conn,
            f"SELECT ts, payload FROM events "
            f"WHERE user_id = {_P} AND kind = {_P} AND payload LIKE {_P} "
            f"ORDER BY id DESC LIMIT 1",
            (user_id, kind, f"%{payload_contains}%"))
    else:
        cur = _execute(conn,
            f"SELECT ts, payload FROM events "
            f"WHERE user_id = {_P} AND kind = {_P} "
            f"ORDER BY id DESC LIMIT 1",
            (user_id, kind))
    row = _fetchone(cur)
    conn.close()
    return row


def get_open_observe_sessions():
    """All currently-open observe sessions across users → list of
    dicts {session_id, user_id, started_at}. Infra sweep (T6) input."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT session_id, user_id, started_at FROM observe_sessions "
        f"WHERE ended_at IS NULL ORDER BY started_at",
        ())
    rows = _fetchall(cur)
    conn.close()
    return rows


def get_last_observation_ts(session_id):
    """Timestamp of the newest capture in an observe session, or None
    if the session has no captures yet."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT ts FROM observations WHERE session_id = {_P} "
        f"ORDER BY id DESC LIMIT 1",
        (session_id,))
    row = _fetchone(cur)
    conn.close()
    return row["ts"] if row else None


# ─── User notes (exploration P3) ─────────────────────────────────

def save_user_note(user_id, claim, given=None, when=None, expect="",
                   evidence=None, confidence="hypothesis",
                   source="operator", note_id=None):
    """Append a note (or a new version of an existing note_id).
    Returns (note_id, version). Emits a note_saved event — note
    changes are interventions in their own right and must be
    joinable to what the coach did next."""
    note_id = note_id or uuid.uuid4().hex[:8]
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT MAX(version) AS v FROM user_notes "
        f"WHERE user_id = {_P} AND note_id = {_P}",
        (user_id, note_id))
    row = _fetchone(cur)
    version = (row["v"] or 0) + 1 if row else 1
    _execute(conn,
        f"INSERT INTO user_notes (user_id, note_id, version, ts, claim, "
        f" given_json, when_json, expect, evidence_json, confidence, source) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
        (user_id, note_id, version, datetime.now().isoformat(), claim,
         json.dumps(given or {}, ensure_ascii=False),
         json.dumps(when or [], ensure_ascii=False),
         expect,
         json.dumps(evidence or [], ensure_ascii=False),
         confidence, source))
    conn.commit()
    conn.close()
    log_event(user_id, "note_saved",
              {"note_id": note_id, "version": version,
               "confidence": confidence, "claim": claim[:200]},
              source=source)
    return note_id, version


def get_user_notes(user_id, include_retired=False):
    """Latest version of each note for a user, oldest-first by first
    appearance. confidence='retired' filtered unless asked for."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM user_notes WHERE user_id = {_P} ORDER BY id",
        (user_id,))
    rows = _fetchall(cur)
    conn.close()
    latest = {}
    order = []
    for r in rows:
        if r["note_id"] not in latest:
            order.append(r["note_id"])
        latest[r["note_id"]] = r
    notes = [latest[nid] for nid in order]
    if not include_retired:
        notes = [n for n in notes if n["confidence"] != "retired"]
    return notes


# ─── User profile brief (brief §7 "User profile brief") ──────────
#
# Who this learner is, as read off their onboarding conversation:
# job/field, learning types (the fixed multi-label taxonomy that the
# offer and step selection branch on), what they learn FROM, what
# they want from Theo AS VERBATIM QUOTES, and a free-form
# personality read. Versioned + append-only; the latest version is
# the live one. Immediate use, no approval gate — nightly revisions
# to it will be proposal-gated later.

def save_user_profile_brief(user_id, job="", learning_types=None,
                            materials=None, wants=None, personality="",
                            rationale="", source="p7", llm_call_id=None):
    """Append a profile-brief version. wants: [{"quote", "meaning"}]
    where quote is the user's OWN words. Returns the new version
    number. Emits profile_brief_saved — a changed read of the user is
    an intervention-shaping event and must be joinable to what the
    coach did next."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT MAX(version) AS v FROM user_profile_briefs "
        f"WHERE user_id = {_P}", (user_id,))
    row = _fetchone(cur)
    version = (row["v"] or 0) + 1 if row else 1
    _execute(conn,
        f"INSERT INTO user_profile_briefs (user_id, version, ts, job, "
        f" learning_types_json, materials_json, wants_json, personality, "
        f" rationale, source, llm_call_id) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, "
        f"{_P}, {_P})",
        (user_id, version, datetime.now().isoformat(), job,
         json.dumps(learning_types or [], ensure_ascii=False),
         json.dumps(materials or [], ensure_ascii=False),
         json.dumps(wants or [], ensure_ascii=False),
         personality, rationale, source, llm_call_id))
    conn.commit()
    conn.close()
    log_event(user_id, "profile_brief_saved",
              {"version": version, "job": job,
               "learning_types": learning_types or [],
               "materials": materials or [],
               "wants": [w.get("quote", "") for w in (wants or [])],
               "llm_call_id": llm_call_id},
              source=source)
    print(f"  [BRIEF] v{version} saved for {user_id} "
          f"(types: {', '.join(learning_types or []) or '-'})", flush=True)
    return version


def get_user_profile_brief(user_id):
    """Latest brief for a user → row dict, or None if never
    generated."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM user_profile_briefs WHERE user_id = {_P} "
        f"ORDER BY version DESC LIMIT 1", (user_id,))
    row = _fetchone(cur)
    conn.close()
    return row


# ─── Onboarding state machine (P0-A) ─────────────────────────────
#
# Completion is a DERIVED predicate over five stored fields — code
# decides, the LLM only fills fields via markers. started_at = the
# first coach send to this user.

def set_agreed_bite(user_id, bite_text, source="llm_marker",
                    decision_id=None):
    """Save the agreed first bite WITHOUT a phase transition — under
    the onboarding state machine, the phase flips only when the full
    checklist completes (check_and_complete_onboarding), not on the
    bite alone. commit_first_bite (bite + forced transition) remains
    for the operator rescue endpoint."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET agreed_first_bite = {_P}, "
        f"agreed_at = {_P} WHERE user_id = {_P}",
        (bite_text, datetime.now().isoformat(), user_id))
    conn.commit()
    conn.close()
    print(f"  [DB] First bite saved for {user_id}: {bite_text!r}", flush=True)
    log_event(user_id, "bite_committed",
              {"bite": bite_text, "decision_id": decision_id}, source=source)


def set_agreed_offer(user_id, offer_text, source="analyze"):
    """Persist what the coach committed to doing for this user, as
    confirmed by them (brief §7 onboarding arc, deliverable 3).
    Same column+event pattern as agreed_goal; refinable."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET agreed_offer = {_P} WHERE user_id = {_P}",
        (offer_text, user_id))
    conn.commit()
    conn.close()
    print(f"  [DB] Agreed offer saved for {user_id}: {offer_text!r}",
          flush=True)
    log_event(user_id, "offer_set", {"offer": offer_text}, source=source)


# ─── Learning materials + magic-link tokens (offer-loop arc) ────────

MATERIAL_KINDS = ("file", "link", "named")
WALKTHROUGH_STATUSES = ("none", "in_progress", "validated")


def add_user_material(user_id, kind, title="", source_url="",
                      orig_filename="", extracted_text="", digest="",
                      source="my_page"):
    """Register one thing the user studies from. kind: 'file'
    (uploaded, extracted_text/digest to follow), 'link' (source_url;
    latent knowledge stands in for the digest), 'named' (only spoken
    of in conversation). Returns the material id. Emits
    material_added — a new material is the anchor of the walkthrough
    arc and must be joinable to what the coach did next."""
    if kind not in MATERIAL_KINDS:
        raise ValueError(f"unknown material kind: {kind!r}")
    ensure_user_profile_row(user_id)
    now = datetime.now().isoformat()
    conn = get_conn()
    cur = _execute(conn,
        f"INSERT INTO user_materials (user_id, kind, title, source_url, "
        f" orig_filename, extracted_text, digest, created_at, updated_at) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})"
        + (" RETURNING id" if DB_TYPE == "postgres" else ""),
        (user_id, kind, title, source_url, orig_filename,
         extracted_text, digest, now, now))
    if DB_TYPE == "postgres":
        material_id = _fetchone(cur)["id"]
    else:
        material_id = cur.lastrowid
    conn.commit()
    conn.close()
    log_event(user_id, "material_added",
              {"material_id": material_id, "kind": kind, "title": title,
               "source_url": source_url},
              source=source)
    return material_id


def get_user_materials(user_id):
    """All materials for a user, newest first. wants_json is decoded
    into 'wants'."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM user_materials WHERE user_id = {_P} "
        f"ORDER BY id DESC", (user_id,))
    rows = _fetchall(cur)
    conn.close()
    for r in rows:
        try:
            r["wants"] = json.loads(r.get("wants_json") or "[]")
        except Exception:
            r["wants"] = []
    return rows


def get_material(material_id):
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM user_materials WHERE id = {_P}", (material_id,))
    row = _fetchone(cur)
    conn.close()
    if row:
        try:
            row["wants"] = json.loads(row.get("wants_json") or "[]")
        except Exception:
            row["wants"] = []
    return row


def set_material_digest(material_id, digest, extracted_text=None):
    """Store the one-time LLM reading (and optionally the extracted
    text it was made from)."""
    conn = get_conn()
    if extracted_text is None:
        _execute(conn,
            f"UPDATE user_materials SET digest = {_P}, updated_at = {_P} "
            f"WHERE id = {_P}",
            (digest, datetime.now().isoformat(), material_id))
    else:
        _execute(conn,
            f"UPDATE user_materials SET digest = {_P}, "
            f" extracted_text = {_P}, updated_at = {_P} WHERE id = {_P}",
            (digest, extracted_text, datetime.now().isoformat(),
             material_id))
    conn.commit()
    conn.close()


def update_material_walkthrough(material_id, user_description=None,
                                wants=None, status=None,
                                source="analyze"):
    """Record what the Theo-led walkthrough produced: the user's own
    description, their (quote, meaning) wants, and the status. Partial
    updates are normal — the walkthrough lands across turns. 'wants'
    REPLACES the stored list (the analysis pass re-reads the whole
    conversation each time, so its extraction is already cumulative).
    status='validated' is reserved for coach-sample-confirmed-by-user;
    the checklist keys on it. Emits material_walkthrough_updated."""
    if status is not None and status not in WALKTHROUGH_STATUSES:
        raise ValueError(f"unknown walkthrough status: {status!r}")
    row = get_material(material_id)
    if not row:
        raise ValueError(f"no material {material_id}")
    sets, vals = ["updated_at = " + _P], [datetime.now().isoformat()]
    payload = {"material_id": material_id}
    if user_description is not None:
        sets.append("user_description = " + _P)
        vals.append(user_description)
        payload["user_description"] = user_description
    if wants is not None:
        sets.append("wants_json = " + _P)
        vals.append(json.dumps(wants, ensure_ascii=False))
        payload["wants"] = wants
    if status is not None:
        sets.append("walkthrough_status = " + _P)
        vals.append(status)
        payload["status"] = status
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_materials SET {', '.join(sets)} WHERE id = {_P}",
        (*vals, material_id))
    conn.commit()
    conn.close()
    log_event(row["user_id"], "material_walkthrough_updated", payload,
              source=source)


def has_validated_material(user_id):
    """True once any material's walkthrough reached 'validated' — the
    mechanical fill condition for the material_walkthrough onboarding
    field."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT COUNT(*) AS n FROM user_materials "
        f"WHERE user_id = {_P} AND walkthrough_status = {_P}",
        (user_id, "validated"))
    row = _fetchone(cur)
    conn.close()
    return bool(row and row["n"])


def ensure_user_token(user_id):
    """The user's magic-link token for /my, created on first ask.
    Possession of the link is the login; no accounts, no passwords."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT token FROM user_tokens WHERE user_id = {_P}", (user_id,))
    row = _fetchone(cur)
    if row:
        conn.close()
        return row["token"]
    token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    _execute(conn,
        f"INSERT INTO user_tokens (user_id, token, created_at) "
        f"VALUES ({_P}, {_P}, {_P})",
        (user_id, token, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    return token


def get_user_id_by_token(token):
    """→ user_id or None. The /my page's whole auth check."""
    if not token:
        return None
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT user_id FROM user_tokens WHERE token = {_P}", (token,))
    row = _fetchone(cur)
    conn.close()
    return row["user_id"] if row else None


def regenerate_user_token(user_id):
    """Replace the token — invalidates any leaked link. Returns the
    new token."""
    token = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    now = datetime.now().isoformat()
    conn = get_conn()
    cur = _execute(conn,
        f"UPDATE user_tokens SET token = {_P}, created_at = {_P} "
        f"WHERE user_id = {_P}", (token, now, user_id))
    if cur.rowcount == 0:
        _execute(conn,
            f"INSERT INTO user_tokens (user_id, token, created_at) "
            f"VALUES ({_P}, {_P}, {_P})", (user_id, token, now))
    conn.commit()
    conn.close()
    log_event(user_id, "token_regenerated", {}, source="admin")
    return token


def save_learning_path(user_id, direction, project="",
                       done_condition="", source="llm_marker",
                       path_kind=""):
    """Append a learning-path version (T8; [PATH:] writes v1).
    current_bite mirrors agreed_first_bite at write time so the path
    row is self-contained. path_kind ('deliverable' / 'coverage' /
    'duration', brief §7 "Learning types") records which framing the
    middle layer takes for this user; '' = not yet judged."""
    ensure_user_profile_row(user_id)
    prof = get_user_profile_by_id(user_id) or {}
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT MAX(version) AS v FROM learning_paths WHERE user_id = {_P}",
        (user_id,))
    row = _fetchone(cur)
    version = (row["v"] or 0) + 1 if row else 1
    _execute(conn,
        f"INSERT INTO learning_paths (user_id, version, ts, direction, "
        f" project, project_done_condition, current_bite, changed_by, "
        f" path_kind) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
        (user_id, version, datetime.now().isoformat(), direction,
         project, done_condition, prof.get("agreed_first_bite") or "",
         source, path_kind))
    conn.commit()
    conn.close()
    log_event(user_id, "path_set",
              {"version": version, "direction": direction,
               "project": project, "done_condition": done_condition,
               "path_kind": path_kind},
              source=source)
    return version


def get_current_path(user_id):
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM learning_paths WHERE user_id = {_P} "
        f"ORDER BY version DESC LIMIT 1", (user_id,))
    row = _fetchone(cur)
    conn.close()
    return row


def save_user_schedule(user_id, windows, raw_text="",
                       source="llm_marker"):
    """Append a schedule version. windows: [{"start": "HH:MM",
    "end": "HH:MM"}] in the user's local day. The hourly tick (P0-C)
    consumes the latest version."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT MAX(version) AS v FROM user_schedule WHERE user_id = {_P}",
        (user_id,))
    row = _fetchone(cur)
    version = (row["v"] or 0) + 1 if row else 1
    _execute(conn,
        f"INSERT INTO user_schedule (user_id, version, ts, windows_json, "
        f" raw_text, source) VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
        (user_id, version, datetime.now().isoformat(),
         json.dumps(windows, ensure_ascii=False), raw_text, source))
    conn.commit()
    conn.close()
    log_event(user_id, "schedule_set",
              {"version": version, "windows": windows,
               "raw": raw_text[:200]}, source=source)
    return version


def get_user_schedule(user_id):
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM user_schedule WHERE user_id = {_P} "
        f"ORDER BY version DESC LIMIT 1", (user_id,))
    row = _fetchone(cur)
    conn.close()
    return row


# ─── Availability grid snapshots (brief §7) ──────────────────────
#
# DERIVED: written only by availability.py's recompute path, never
# from a conversation and never by an LLM. Append-only versions —
# the caller decides whether the grid changed enough to warrant one.

def save_availability_snapshot(user_id, grid, sources, method_version,
                               changed_cells=None):
    """Append one availability snapshot. Returns the new version.
    Emits availability_updated — a changed grid is a finding about
    the user and must be joinable to what the coach did next."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT MAX(version) AS v FROM availability_snapshots "
        f"WHERE user_id = {_P}", (user_id,))
    row = _fetchone(cur)
    version = (row["v"] or 0) + 1 if row else 1
    _execute(conn,
        f"INSERT INTO availability_snapshots (user_id, version, ts, "
        f" grid_json, sources_json, method_version) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
        (user_id, version, datetime.now().isoformat(),
         json.dumps(grid, ensure_ascii=False),
         json.dumps(sources, ensure_ascii=False), method_version))
    conn.commit()
    conn.close()
    cell_count = sum(len(hours) for hours in (grid or {}).values())
    log_event(user_id, "availability_updated",
              {"version": version, "method_version": method_version,
               "cell_count": cell_count,
               "changed_cells": changed_cells or []},
              source="annotate")
    print(f"  [AVAIL] v{version} for {user_id} ({cell_count} cells)",
          flush=True)
    return version


def get_availability_snapshot(user_id):
    """Latest snapshot row, or None."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM availability_snapshots WHERE user_id = {_P} "
        f"ORDER BY version DESC LIMIT 1", (user_id,))
    row = _fetchone(cur)
    conn.close()
    return row


def get_availability_snapshots(user_id, limit=20):
    """Snapshot history, newest version first."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM availability_snapshots WHERE user_id = {_P} "
        f"ORDER BY version DESC LIMIT {_P}", (user_id, limit))
    rows = _fetchall(cur)
    conn.close()
    return rows


def mark_onboarding_started(user_id):
    """Idempotent: stamp onboarding_started_at at the first coach
    send to this user."""
    ensure_user_profile_row(user_id)
    prof = get_user_profile_by_id(user_id) or {}
    if prof.get("onboarding_started_at"):
        return prof["onboarding_started_at"]
    now = datetime.now().isoformat()
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET onboarding_started_at = {_P} "
        f"WHERE user_id = {_P}", (now, user_id))
    conn.commit()
    conn.close()
    log_event(user_id, "onboarding_started", {}, source="server")
    return now


# Order IS the onboarding arc (brief §7): discover the goal, widen it
# into a path, TELL THEM WHAT THE COACH WILL DO and get that
# confirmed, agree when to message, agree what "started" looks like,
# then land one concrete task. The prompt shows only the first
# unsettled item as the current focus.
# The order IS the arc. `bite` is deliberately NOT here: the first
# concrete task belongs to the first study session, which happens
# AFTER onboarding completes and the sequence plan exists. What
# onboarding needs in its place is the offer — what the coach will
# do for them.
ONBOARDING_FIELDS = ("goal", "path", "offer", "schedule",
                     "ignition_marker")


def get_onboarding_state(user_id):
    """→ {'started_at', 'completed_at', 'missing': [...], 'filled':
    [...]} — the checklist, computed mechanically from stored data."""
    prof = get_user_profile_by_id(user_id) or {}
    filled = []
    if (prof.get("agreed_goal") or "").strip():
        filled.append("goal")
    if get_current_path(user_id):
        filled.append("path")
    if (prof.get("ignition_marker") or "").strip():
        filled.append("ignition_marker")
    if get_user_schedule(user_id):
        filled.append("schedule")
    if (prof.get("agreed_offer") or "").strip():
        filled.append("offer")
    return {
        "started_at": prof.get("onboarding_started_at"),
        "completed_at": prof.get("onboarding_completed_at"),
        "filled": filled,
        "missing": [f for f in ONBOARDING_FIELDS if f not in filled],
    }


def check_and_complete_onboarding(user_id, force=False):
    """If all five fields are filled (or force=True, operator
    override/backfill) and not yet completed: stamp completed_at,
    transition discovery→first_bite, emit events. Returns True if
    completion happened on THIS call."""
    # Without a row the UPDATE below matches nothing, and we would log
    # a completion that never happened — the operator-backfill path hit
    # exactly that.
    ensure_user_profile_row(user_id)
    state = get_onboarding_state(user_id)
    if state["completed_at"]:
        return False
    if state["missing"] and not force:
        return False
    now = datetime.now().isoformat()
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET onboarding_completed_at = {_P} "
        f"WHERE user_id = {_P}", (now, user_id))
    conn.commit()
    conn.close()
    log_event(user_id, "onboarding_completed",
              {"forced": force, "missing_at_completion": state["missing"]},
              source="admin" if force else "server")
    phase = get_user_phase(user_id)["phase"]
    if phase == "discovery":
        conn = get_conn()
        _execute(conn,
            f"UPDATE user_profiles SET phase = 'first_bite' "
            f"WHERE user_id = {_P}", (user_id,))
        conn.commit()
        conn.close()
        log_event(user_id, "phase_transition",
                  {"to": "first_bite", "via": "onboarding_completed"},
                  source="server")
    print(f"  [DB] Onboarding completed for {user_id} (forced={force})",
          flush=True)
    return True


# ─── Sequence plans (exploration v2) ─────────────────────────────

def save_sequence_plan(user_id, steps, rationale="", source="operator"):
    """Append a new plan version and reset the cursor to step 0.
    steps: ordered list of {"tag", "intensity", "intent"} — intent is
    a one-line human/LLM-readable purpose ("open why question, his
    own words"). Returns the new version number."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT MAX(version) AS v FROM sequence_plans WHERE user_id = {_P}",
        (user_id,))
    row = _fetchone(cur)
    version = (row["v"] or 0) + 1 if row else 1
    _execute(conn,
        f"INSERT INTO sequence_plans (user_id, version, ts, steps_json, "
        f" rationale, source) VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
        (user_id, version, datetime.now().isoformat(),
         json.dumps(steps, ensure_ascii=False), rationale, source))
    _execute(conn,
        f"UPDATE user_profiles SET plan_cursor = 0 WHERE user_id = {_P}",
        (user_id,))
    conn.commit()
    conn.close()
    log_event(user_id, "sequence_plan_set",
              {"version": version, "steps": steps,
               "rationale": rationale[:300]}, source=source)
    print(f"  [PLAN] v{version} set for {user_id} ({len(steps)} steps)",
          flush=True)
    return version


def get_current_plan(user_id):
    """Latest plan + live cursor → {'version', 'steps', 'cursor',
    'rationale'} or None if the user has no plan."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM sequence_plans WHERE user_id = {_P} "
        f"ORDER BY version DESC LIMIT 1", (user_id,))
    row = _fetchone(cur)
    conn.close()
    if not row:
        return None
    prof = get_user_profile_by_id(user_id) or {}
    return {
        "version": row["version"],
        "steps": json.loads(row["steps_json"]),
        "cursor": prof.get("plan_cursor") or 0,
        "rationale": row["rationale"],
        "ts": row["ts"],
    }


def move_plan_cursor(user_id, new_index, reason, source="llm_marker"):
    """Move the cursor (an [ADVANCE] judgment, or an operator fix).
    Every move is an event — cursor motion is an intervention."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET plan_cursor = {_P} WHERE user_id = {_P}",
        (new_index, user_id))
    conn.commit()
    conn.close()
    log_event(user_id, "plan_cursor_moved",
              {"to": new_index, "reason": reason}, source=source)


# ─── LearnerState snapshots (T5) ─────────────────────────────────

def get_active_user_ids(start_iso, end_iso):
    """Users with ≥1 event in [start, end) — the nightly annotation
    job's definition of 'active that day'. Excludes '_unknown'
    (events not attributable to a learner)."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT DISTINCT user_id FROM events "
        f"WHERE ts >= {_P} AND ts < {_P}",
        (start_iso, end_iso))
    rows = _fetchall(cur)
    conn.close()
    return [r["user_id"] for r in rows if r["user_id"] != "_unknown"]


def get_events_with_ids(user_id, start_iso, end_iso, limit=1000):
    """A user's events in [start, end) INCLUDING row ids, oldest-first.
    The annotation job cites these ids as evidence — get_events()
    deliberately omits ids for display, so this is a separate reader."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT id, ts, kind, payload, source FROM events "
        f"WHERE user_id = {_P} AND ts >= {_P} AND ts < {_P} "
        f"ORDER BY id LIMIT {_P}",
        (user_id, start_iso, end_iso, limit))
    rows = _fetchall(cur)
    conn.close()
    return rows


def save_learner_state_snapshot(user_id, day, schema_version,
                                prompt_version, model, state,
                                evidence_ids, llm_call_id=None):
    """Append one LearnerState snapshot (T5). Plain INSERT, never an
    update: re-annotating a day adds a row, it can't erase one."""
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO learner_state_snapshots "
        f"(user_id, day, created_at, schema_version, prompt_version, "
        f" model, state_json, evidence_json, llm_call_id) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
        (user_id, day, datetime.now().isoformat(), schema_version,
         prompt_version, model,
         json.dumps(state, ensure_ascii=False),
         json.dumps(evidence_ids), llm_call_id))
    conn.commit()
    conn.close()
    print(f"  [T5] LearnerState snapshot saved: {user_id} {day}", flush=True)


def get_learner_state_snapshots(user_id=None, day=None, limit=50):
    """Snapshots newest-first, optionally filtered by user and/or day."""
    conds, params = [], []
    if user_id:
        conds.append(f"user_id = {_P}")
        params.append(user_id)
    if day:
        conds.append(f"day = {_P}")
        params.append(day)
    where = f"WHERE {' AND '.join(conds)} " if conds else ""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT id, user_id, day, created_at, schema_version, "
        f"prompt_version, model, state_json, evidence_json, llm_call_id "
        f"FROM learner_state_snapshots {where}"
        f"ORDER BY id DESC LIMIT {_P}",
        (*params, limit))
    rows = _fetchall(cur)
    conn.close()
    return rows


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


def save_llm_call(user_id, trigger, model, system_prompt, messages,
                  prompt_versions=None, response_text=""):
    """Record one LLM call verbatim: the exact rendered system prompt
    and messages array the API received, plus the response. Returns
    call_id, or None on failure. NEVER raises — recording must not
    break the send path."""
    call_id = uuid.uuid4().hex[:12]
    try:
        conn = get_conn()
        _execute(conn,
            f"INSERT INTO llm_calls "
            f"(call_id, user_id, ts, trigger, model, system_prompt, "
            f" messages_json, prompt_versions_json, response_text) "
            f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
            (call_id, user_id, datetime.now().isoformat(), trigger, model,
             system_prompt,
             json.dumps(messages, ensure_ascii=False),
             json.dumps(prompt_versions or {}, ensure_ascii=False),
             response_text)
        )
        conn.commit()
        conn.close()
        return call_id
    except Exception as e:
        print(f"[LLM_CALLS] ⚠️ save failed ({trigger}): {e}", flush=True)
        return None


def get_llm_call(call_id):
    """Retrieve one recorded call by id, messages parsed back to a
    list. None if not found."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM llm_calls WHERE call_id = {_P}", (call_id,))
    row = _fetchone(cur)
    conn.close()
    if row:
        row["messages"] = json.loads(row["messages_json"])
        row["prompt_versions"] = json.loads(row["prompt_versions_json"])
    return row


def get_llm_calls(user_id, limit=20):
    """Recent calls for a user, newest first (summaries incl. sizes)."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT call_id, ts, trigger, model FROM llm_calls "
        f"WHERE user_id = {_P} ORDER BY ts DESC LIMIT {_P}",
        (user_id, limit))
    rows = _fetchall(cur)
    conn.close()
    return rows


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
        "ignition_marker": prof.get("ignition_marker") or "",
        "ignition_marker_status": prof.get("ignition_marker_status") or "",
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


def set_ignition_marker(user_id, marker_text, source="llm_marker",
                        status="confirmed", basis="stated",
                        confidence="high"):
    """Persist the observable definition of ignition for this user.

    Unlike the agreement fields, this one may be DERIVED from what the
    user said about their material and workflow (operator decision
    2026-07-30): it is the instrument their sessions get judged with,
    not a promise they made. `status` records which it is —
    'provisional' (inferred, awaiting a passing confirmation) or
    'confirmed' (stated or agreed). Refinable any number of times; a
    later confirmation simply overwrites with status confirmed."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET ignition_marker = {_P}, "
        f"ignition_marker_status = {_P} WHERE user_id = {_P}",
        (marker_text, status, user_id)
    )
    conn.commit()
    conn.close()
    print(f"  [DB] Ignition marker ({status}) saved for {user_id}: "
          f"{marker_text!r}", flush=True)
    log_event(user_id, "ignition_def_set",
              {"marker": marker_text, "status": status, "basis": basis,
               "confidence": confidence}, source=source)


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


def commit_first_bite(user_id, bite_text, source="llm_marker", decision_id=None):
    """Save the agreed-upon first bite and transition to Phase 1.
    decision_id (T3) joins this transition to the policy decision
    that allowed it."""
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
              {"to": "first_bite", "bite": bite_text,
               "decision_id": decision_id}, source=source)


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


def save_sms_signup(phone, name="", email="", consent_checkins=False,
                    consent_support=False):
    """Record a web opt-in consent (phone already normalized E.164).
    Each row is one consent submission with per-purpose flags — carrier
    verification requires a separate opt-in per messaging purpose.
    Duplicate phones allowed — each submission is its own consent
    record with its own timestamp."""
    conn = get_conn()
    cur = _execute(conn,
        f"INSERT INTO sms_signups (phone, consented_at, name, email, "
        f"consent_checkins, consent_support) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
        (phone, datetime.now().isoformat(), name, email,
         int(consent_checkins), int(consent_support))
    )
    conn.commit()
    conn.close()
    print(f"  [DB] SMS signup consent recorded for {phone} "
          f"(checkins={int(consent_checkins)}, support={int(consent_support)})",
          flush=True)


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
