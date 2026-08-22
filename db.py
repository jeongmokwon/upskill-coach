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

USER_ID = (os.environ.get("TUTOR_USER_ID", "").strip()
           or "_unset")  # legacy web-session fallback; real paths
                         # set the thread-local via set_user_id

# Life-track config columns, shared by both schema branches (additive
# only — legacy drill tracks keep every one of these empty). The
# track "생성" experiment (2026-08-18): a track is config data, not
# code — role + part_type (which machine) + surfacing (when the
# conductor may raise it) + donts. status gains values beyond
# active: 'held' (판정: 지금은 안 만든다 — T7 주식 case) and
# 'retired' (대화로 은퇴).
_TRACK_CONFIG_COLS = [
    ("role", "TEXT DEFAULT ''"),           # one-line mission
    ("part_type", "TEXT DEFAULT ''"),      # tracks_ops.PART_TYPES key
    ("surfacing", "TEXT DEFAULT '{}'"),    # JSON {kind, ...params}
    ("donts", "TEXT DEFAULT '[]'"),        # JSON list of strings
    ("cost_lane", "TEXT DEFAULT ''"),      # haiku | sonnet
    ("profile_facts", "TEXT DEFAULT '[]'"),  # JSON: facts to elicit
]

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
            ("email", "TEXT DEFAULT ''"),
            ("phone", "TEXT DEFAULT ''"),
            ("status", "TEXT DEFAULT 'active'"),
            # 'provisional' (inferred by the analysis pass) vs
            # 'confirmed' (the user said it). See set_ignition_marker.
            ("ignition_marker_status", "TEXT DEFAULT ''"),
            # Checklist v2 (2026-08-06): when the fixed
            # expectation-setting message was delivered, and the
            # settled answer to "do they study from a material?"
            # ('has_material' / 'no_material' / '' = not aligned yet).
            ("expectation_sent_at", "TEXT DEFAULT ''"),
            ("material_status", "TEXT DEFAULT ''"),
            # User-requested silence window ("주말 동안 보내지
            # 마"): ISO timestamp; proactive sends are gated
            # until it passes. Observed live: the coach AGREED
            # to pause in words while the cron kept firing — a
            # promise needs state or it is theater.
            ("paused_until", "TEXT DEFAULT ''"),
            # How much this user does NOT want small talk, [0,1].
            # A living judgment: the analysis pass re-reports it as
            # conversation accumulates, so an early misread does not
            # stick. NULL = not yet judged. Enforced in the prompt at
            # SMALLTALK_AVERSION_THRESHOLD (sms.py) — some pilot
            # users visibly bounced off chit-chat openers.
            ("smalltalk_aversion", "REAL"),
            # Life-track conversation gate: ISO timestamp when the
            # track-generation lane was enabled for this user. Empty
            # = the lane is invisible (the husband's prompts and
            # inbound path are byte-identical to before).
            ("tracks_enabled", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.cursor().execute(f"ALTER TABLE user_profiles ADD COLUMN {col} {ddl}")
                conn.commit()
            except Exception:
                conn.rollback()

        # Checklist-v2 backfill, once: users already mid-onboarding
        # never got (and should not retroactively get) the fixed
        # expectation message — stamp them done. A registered material
        # settles the alignment question by existing.
        try:
            cur = conn.cursor()
            cur.execute(
                "UPDATE user_profiles SET expectation_sent_at = %s "
                "WHERE onboarding_started_at IS NOT NULL "
                "AND onboarding_started_at != '' "
                "AND (expectation_sent_at IS NULL OR expectation_sent_at = '')",
                (datetime.now().isoformat(),))
            cur.execute(
                "UPDATE user_profiles SET material_status = 'has_material' "
                "WHERE (material_status IS NULL OR material_status = '') "
                "AND user_id IN (SELECT DISTINCT user_id FROM user_materials)")
            conn.commit()
        except Exception:
            conn.rollback()

        # (Operator-seeded pilot values for the first two users were
        # removed 2026-08-20 — the values live in the production DB;
        # per-user data does not belong in source.)

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
        # material_understanding onboarding field will key on.
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
        # Standing interaction preferences ("앞으로는 영어로") — the
        # relationship contract. User-stated rules about HOW to talk
        # used to live only in scrollable history and leaked back
        # (Korean returning after an explicit English request, the
        # same greeting recurring). Append-only; latest row per key
        # wins; every prompt renders the current set.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '',
                ts TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'analyze'
            )
        """)
        # ─── v3 ledgers (C-layer; design doc C_DESIGN_V3) ───
        # tracks: one user runs several relationships with the coach
        # (the PDF drill now; a bar-exam track or a companion track
        # only when the USER tells the coach directly — never from
        # operator hearsay).
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS tracks (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'drill',
                authority TEXT NOT NULL DEFAULT 'file_wins',
                exam_date TEXT DEFAULT '',
                performance_stage TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            )
        """)
        # reminders v0: a promise Theo makes ("토요일 9시에 알려줄게")
        # becomes a ROW, and a cron tick fires it through send_nudge —
        # the first machinery where a conversational promise is kept
        # automatically. fire_at is UTC ISO; recur '' = one-shot,
        # 'weekdays' advances to the next Mon-Fri at the same LA-local
        # time (DST-safe by construction).
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                fire_at TEXT NOT NULL,
                recur TEXT NOT NULL DEFAULT '',
                instruction TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                last_fired_at TEXT DEFAULT '',
                source TEXT NOT NULL DEFAULT 'operator'
            )
        """)
        # research requests: a user's explicit "find out X" becomes a
        # row (extracted by analyze with a verbatim evidence quote);
        # the research hop (research.py, web-search-enabled call)
        # fills findings and delivers via send_nudge. The ledger IS
        # the dedupe: analyze re-reads the whole transcript every
        # turn, so an ask must not re-fire once recorded.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS research_requests (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                question TEXT NOT NULL,
                evidence_quote TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                finished_at TEXT DEFAULT '',
                findings TEXT DEFAULT '',
                llm_call_id TEXT DEFAULT ''
            )
        """)
        # 문제 은행: every item carries a verbatim anchor from its
        # source — an item that cannot quote its origin does not
        # exist (the Rule 102(d)(1)/GS2 fabrications were exactly
        # unanchored claims reaching the user).
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS knowledge_items (
                id SERIAL PRIMARY KEY,
                track_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                anchor_type TEXT NOT NULL,
                anchor_quote TEXT NOT NULL DEFAULT '',
                section_hint TEXT DEFAULT '',
                stem TEXT NOT NULL,
                elements_json TEXT NOT NULL DEFAULT '[]',
                kind TEXT DEFAULT '',
                est_difficulty INTEGER DEFAULT 2,
                status TEXT NOT NULL DEFAULT 'untested',
                source TEXT NOT NULL DEFAULT 'extraction',
                created_at TEXT NOT NULL
            )
        """)
        # 오답노트: per-element grading + the confidence read from
        # the user's own hedging language ("I am not sure about the
        # third one" hedged exactly where he was wrong).
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS attempts (
                id SERIAL PRIMARY KEY,
                item_id INTEGER,
                track_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'drill',
                question TEXT NOT NULL DEFAULT '',
                answer_verbatim TEXT NOT NULL DEFAULT '',
                elements_json TEXT NOT NULL DEFAULT '[]',
                verdict TEXT NOT NULL DEFAULT '',
                self_confidence TEXT DEFAULT '',
                confidence_marker TEXT DEFAULT '',
                note TEXT DEFAULT ''
            )
        """)
        # 가르쳐준 것 장부: deliberate teachings/corrections only —
        # a wrong drill answer is 오답노트 material, not truth.
        # Truth order: user correction > file > model knowledge.
        # Conflicts with file/canon become conversation items, never
        # silent absorption.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS taught_ledger (
                id SERIAL PRIMARY KEY,
                track_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                quote TEXT NOT NULL,
                teaching TEXT NOT NULL,
                kind TEXT DEFAULT '',
                conflict_flag TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        # 사람 노트: condition→response style observations with
        # evidence and confidence. NEVER whole-person grades — a
        # verdict in the prompt bends the coach's tone downward.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS person_notes (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                observation TEXT NOT NULL,
                evidence TEXT DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'low',
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        # 예측 장부: recorded BEFORE the answer, then immutable —
        # only the scoring fields are written once when the actual
        # arrives. The grader never sees these rows (isolation, same
        # discipline as the eval judge). KPIs live here: 적중률 and
        # 파악 속도.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id SERIAL PRIMARY KEY,
                item_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                predicted_verdict TEXT NOT NULL,
                predicted_difficulty INTEGER,
                reason TEXT DEFAULT '',
                scored_at TEXT DEFAULT '',
                actual_verdict TEXT DEFAULT '',
                hit INTEGER
            )
        """)
        # Screen co-viewing sessions (PR A of the session build).
        # One row per user-initiated screen share on /my. Raw frames
        # are NEVER stored — they live in memory for the seconds the
        # eyes call needs, then vanish; observations (text) are the
        # only persistent trace. last_seen is the heartbeat: a session
        # whose heartbeat is >60s old is treated as dead (tab closed,
        # laptop slept) without needing a reaper process.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS screen_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                declared_source TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                ended_at TEXT,
                frames INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_ssn_user ON screen_sessions (user_id, started_at)"
        )
        # Consent records — the compliance artifact for sensitive
        # features (screen sharing first). One row per (user, doc,
        # version) acceptance, timestamped. Append-only: a new doc
        # version requires a fresh row.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS user_consents (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                doc TEXT NOT NULL,
                version TEXT NOT NULL,
                ts TEXT NOT NULL
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_consents_user ON user_consents (user_id, doc)"
        )
        # Track items — the one generic ledger behind every life-track
        # part (capture-list entries, cadence last-done stamps, owed
        # replies, expected deliveries). The payload is JSON on
        # purpose: item shapes differ per track and keep evolving, so
        # the schema stays out of their way — the per-part minimum
        # contract lives in code (tracks_ops.validate_item_payload),
        # not in columns.
        conn.cursor().execute("""
            CREATE TABLE IF NOT EXISTS track_items (
                id SERIAL PRIMARY KEY,
                track_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT '',
                resolved_at TEXT DEFAULT ''
            )
        """)
        conn.cursor().execute(
            "CREATE INDEX IF NOT EXISTS idx_track_items ON track_items (track_id, status)"
        )
        # Commit BEFORE the ALTER-with-rollback migrations below.
        # Those loops rollback when a column already exists (which is
        # every boot after the first), and an uncommitted CREATE TABLE
        # above would be rolled back with them — observed in
        # production: user_materials/user_tokens silently vanished on
        # every boot and /debug/my-link 500ed. CREATE TABLE IF NOT
        # EXISTS makes the commit idempotent.
        conn.commit()
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
        # Life-track config columns (additive only — the husband's
        # drill track keeps these empty and never reads them). The
        # rehearsal of 2026-08-18 derived this shape: a track is a
        # role + a part (which machine runs it) + a surfacing rule
        # (when the conductor may raise it) + donts. JSON columns so
        # config evolves in conversation without migrations.
        for col, ddl in _TRACK_CONFIG_COLS:
            try:
                conn.cursor().execute(f"ALTER TABLE tracks ADD COLUMN {col} {ddl}")
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
            ("email", "TEXT DEFAULT ''"),
            ("phone", "TEXT DEFAULT ''"),
            ("status", "TEXT DEFAULT 'active'"),
            ("ignition_marker_status", "TEXT DEFAULT ''"),
            # Checklist v2 — see Postgres branch for rationale.
            ("expectation_sent_at", "TEXT DEFAULT ''"),
            ("material_status", "TEXT DEFAULT ''"),
            # User-requested silence window ("주말 동안 보내지
            # 마"): ISO timestamp; proactive sends are gated
            # until it passes. Observed live: the coach AGREED
            # to pause in words while the cron kept firing — a
            # promise needs state or it is theater.
            ("paused_until", "TEXT DEFAULT ''"),
            # Small-talk aversion — see Postgres branch for rationale.
            ("smalltalk_aversion", "REAL"),
            # Life-track gate — see Postgres branch for rationale.
            ("tracks_enabled", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE user_profiles ADD COLUMN {col} {default}")
            except Exception:
                pass
        # Checklist-v2 backfill — see Postgres branch for rationale.
        try:
            conn.execute(
                "UPDATE user_profiles SET expectation_sent_at = ? "
                "WHERE onboarding_started_at IS NOT NULL "
                "AND onboarding_started_at != '' "
                "AND (expectation_sent_at IS NULL OR expectation_sent_at = '')",
                (datetime.now().isoformat(),))
            conn.execute(
                "UPDATE user_profiles SET material_status = 'has_material' "
                "WHERE (material_status IS NULL OR material_status = '') "
                "AND user_id IN (SELECT DISTINCT user_id FROM user_materials)")
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

            CREATE TABLE IF NOT EXISTS user_preferences (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                evidence TEXT NOT NULL DEFAULT '',
                ts TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'analyze'
            );

            CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'drill',
                authority TEXT NOT NULL DEFAULT 'file_wins',
                exam_date TEXT DEFAULT '',
                performance_stage TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                fire_at TEXT NOT NULL,
                recur TEXT NOT NULL DEFAULT '',
                instruction TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                last_fired_at TEXT DEFAULT '',
                source TEXT NOT NULL DEFAULT 'operator'
            );

            CREATE TABLE IF NOT EXISTS research_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                question TEXT NOT NULL,
                evidence_quote TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                finished_at TEXT DEFAULT '',
                findings TEXT DEFAULT '',
                llm_call_id TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS knowledge_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                anchor_type TEXT NOT NULL,
                anchor_quote TEXT NOT NULL DEFAULT '',
                section_hint TEXT DEFAULT '',
                stem TEXT NOT NULL,
                elements_json TEXT NOT NULL DEFAULT '[]',
                kind TEXT DEFAULT '',
                est_difficulty INTEGER DEFAULT 2,
                status TEXT NOT NULL DEFAULT 'untested',
                source TEXT NOT NULL DEFAULT 'extraction',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER,
                track_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'drill',
                question TEXT NOT NULL DEFAULT '',
                answer_verbatim TEXT NOT NULL DEFAULT '',
                elements_json TEXT NOT NULL DEFAULT '[]',
                verdict TEXT NOT NULL DEFAULT '',
                self_confidence TEXT DEFAULT '',
                confidence_marker TEXT DEFAULT '',
                note TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS taught_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                quote TEXT NOT NULL,
                teaching TEXT NOT NULL,
                kind TEXT DEFAULT '',
                conflict_flag TEXT DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS person_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                observation TEXT NOT NULL,
                evidence TEXT DEFAULT '',
                confidence TEXT NOT NULL DEFAULT 'low',
                status TEXT NOT NULL DEFAULT 'active'
            );

            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                predicted_verdict TEXT NOT NULL,
                predicted_difficulty INTEGER,
                reason TEXT DEFAULT '',
                scored_at TEXT DEFAULT '',
                actual_verdict TEXT DEFAULT '',
                hit INTEGER
            );


            CREATE TABLE IF NOT EXISTS screen_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                declared_source TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                ended_at TEXT,
                frames INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS user_consents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                doc TEXT NOT NULL,
                version TEXT NOT NULL,
                ts TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_consents_user ON user_consents (user_id, doc);

            CREATE INDEX IF NOT EXISTS idx_ssn_user ON screen_sessions (user_id, started_at);

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

            CREATE TABLE IF NOT EXISTS track_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                track_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT '',
                resolved_at TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_track_items ON track_items (track_id, status);
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
        # Life-track config columns — see Postgres branch for rationale.
        for col, default in _TRACK_CONFIG_COLS:
            try:
                conn.execute(f"ALTER TABLE tracks ADD COLUMN {col} {default}")
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


def save_sms_message(user_id, role, content, direction, channel="sms"):
    """Append one message to the rolling thread for `user_id`.

    channel: 'sms' or 'web' (the in-session chat). One thread, one
    memory — the coach is the same person on both; the channel tag
    exists so style rules and delivery paths can differ.

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
        (_sms_sid(user_id), user_id, role, content, channel, direction,
         datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def get_last_user_message_id(user_id):
    """Row id of the user's newest inbound — the freshness marker the
    burst-folding logic compares against."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT MAX(id) AS m FROM messages "
        f"WHERE user_id = {_P} AND role = 'user'", (user_id,))
    row = _fetchone(cur)
    conn.close()
    return row["m"] if row else None


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
    ("[Wed 22:48, 3h ago] ..."). Observed failure without it: the
    coach referenced a message sent four hours earlier as
    "yesterday" — history carries no clock, so elapsed time was
    guessed.
    """
    conn = get_conn()
    if since:
        cur = _execute(conn,
            f"SELECT role, content, timestamp FROM messages "
            f"WHERE session_id = {_P} AND channel IN ('sms','web') "
            f"AND timestamp > {_P} "
            f"ORDER BY id DESC LIMIT {_P}",
            (_sms_sid(user_id), since, limit)
        )
    else:
        cur = _execute(conn,
            f"SELECT role, content, timestamp FROM messages "
            f"WHERE session_id = {_P} AND channel IN ('sms','web') "
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
    days_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
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
                    rel = f"{max(1, round(mins))}m ago"
                elif mins < 60 * 36:
                    rel = f"{round(mins / 60)}h ago"
                else:
                    rel = f"{round(mins / 1440)}d ago"
                # Absolute stamp alongside the relative one: with only
                # "35h ago" the model had to do calendar arithmetic to
                # place a turn, and got it wrong twice (calling a
                # 4-hour-old message "yesterday", and a Wednesday-
                # night exchange "last night" on a Friday).
                local = when + timedelta(hours=tz)
                abs_en = (f"{days_en[local.weekday()]} "
                          f"{local.strftime('%H:%M')}")
                label = f"[{abs_en}, {rel}] "
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


_USER_SCOPED_TABLES = (
    "messages", "events", "observations", "screen_sessions",
    "user_materials", "user_notes", "sequence_plans", "learning_paths",
    "user_schedule", "user_preferences", "user_profile_briefs",
    "availability_snapshots", "tracks", "knowledge_items", "attempts",
    "taught_ledger", "person_notes", "predictions",
    "insights", "llm_calls", "user_consents", "user_state", "sessions",
)


def reset_user(user_id):
    """Wipe a user back to birth: every row in every user-scoped
    table is deleted, then a minimal profile row is recreated keeping
    ONLY phone, email, and their magic-link token (the /my link they
    already have keeps working; consent is deliberately wiped so the
    JIT flow runs again). Returns {table: rows_deleted}.

    Destructive by design — the operator endpoint requires an
    explicit confirmation. Built for pilot #0's own account so the
    founder can walk the real new-user flow end to end."""
    prof = get_user_profile_by_id(user_id) or {}
    keep_phone = (prof.get("phone") or "").strip()
    keep_email = (prof.get("email") or "").strip()
    conn = get_conn()
    counts = {}
    for t in _USER_SCOPED_TABLES:
        try:
            cur = _execute(conn,
                f"DELETE FROM {t} WHERE user_id = {_P}", (user_id,))
            counts[t] = cur.rowcount
        except Exception as e:
            conn.rollback()
            counts[t] = f"skipped ({e})"
    _execute(conn, f"DELETE FROM user_profiles WHERE user_id = {_P}",
             (user_id,))
    conn.commit()
    conn.close()
    ensure_user_profile_row(user_id)
    if keep_phone:
        conn = get_conn()
        _execute(conn,
            f"UPDATE user_profiles SET phone = {_P}, email = {_P} "
            f"WHERE user_id = {_P}",
            (keep_phone, keep_email, user_id))
        conn.commit()
        conn.close()
    log_event(user_id, "user_reset",
              {"kept": {"phone": bool(keep_phone),
                        "email": bool(keep_email)},
               "deleted": {k: v for k, v in counts.items()
                           if isinstance(v, int) and v}},
              source="admin")
    return counts


def set_user_phone(user_id, phone, source="operator"):
    """Bind a phone (E.164) to a user — THE identity edge for inbound
    routing and outbound sends. Refuses a number already bound to a
    different user: a silent re-bind would reroute someone's whole
    conversation."""
    phone = (phone or "").strip()
    existing = get_user_by_phone(phone)
    if existing and existing != user_id:
        raise ValueError(f"phone {phone} already bound to {existing}")
    ensure_user_profile_row(user_id)
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET phone = {_P} WHERE user_id = {_P}",
        (phone, user_id))
    conn.commit()
    conn.close()
    log_event(user_id, "phone_bound", {"phone": phone}, source=source)


def list_users():
    """Every user row's operator-relevant identity fields, newest
    first — the /debug/users roster."""
    conn = get_conn()
    cur = _execute(conn,
        "SELECT user_id, user_name, phone, email, status, "
        "tracks_enabled, created_at FROM user_profiles "
        "ORDER BY created_at DESC")
    rows = _fetchall(cur); conn.close()
    return rows


def get_user_by_phone(phone):
    """→ user_id or None. DB is the source of truth for routing; the
    TUTOR_USER_* env pair remains a fallback in sms.py so nothing
    breaks mid-migration."""
    phone = (phone or "").strip()
    if not phone:
        return None
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT user_id FROM user_profiles WHERE phone = {_P}",
        (phone,))
    row = _fetchone(cur)
    conn.close()
    return row["user_id"] if row else None


def get_active_users():
    """Users the coach serves: a bound phone and status 'active'.
    → [{'user_id', 'phone'}] ordered by user_id for stable cron
    iteration."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT user_id, phone FROM user_profiles "
        f"WHERE phone != '' AND status = 'active' ORDER BY user_id")
    rows = _fetchall(cur)
    conn.close()
    return rows


def set_user_status(user_id, status, source="operator"):
    """'active' | 'paused' | 'stopped'. Paused users keep all their
    data; the cron fan-out simply skips them. Stopped = the user
    texted STOP (carrier opt-out): every send path refuses them at
    the send_sms choke point until they text START."""
    if status not in ("active", "paused", "stopped"):
        raise ValueError(f"unknown status {status!r}")
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET status = {_P} WHERE user_id = {_P}",
        (status, user_id))
    conn.commit()
    conn.close()
    log_event(user_id, "status_changed", {"status": status},
              source=source)


def set_user_name(user_id, name, source="operator"):
    ensure_user_profile_row(user_id)
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET user_name = {_P} WHERE user_id = {_P}",
        (name.strip(), user_id))
    conn.commit()
    conn.close()


def set_user_email(user_id, email, source="operator"):
    ensure_user_profile_row(user_id)
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET email = {_P} WHERE user_id = {_P}",
        (email, user_id))
    conn.commit()
    conn.close()


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


def set_smalltalk_aversion(user_id, value, source="analyze"):
    """Persist the analysis pass's read of how much this user does
    NOT want small talk, [0,1]. A living judgment, not an agreement:
    re-reports overwrite earlier reads so a misjudged first
    impression cannot stick. Re-reports within 0.05 of the stored
    value are skipped — the model restates its read most turns and
    the event log must not churn. Returns True when it wrote."""
    value = max(0.0, min(1.0, float(value)))
    ensure_user_profile_row(user_id)
    prof = get_user_profile_by_id(user_id) or {}
    prev = prof.get("smalltalk_aversion")
    if prev is not None and abs(float(prev) - value) <= 0.05:
        return False
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET smalltalk_aversion = {_P} "
        f"WHERE user_id = {_P}", (value, user_id))
    conn.commit()
    conn.close()
    print(f"  [DB] smalltalk_aversion for {user_id}: {value}",
          flush=True)
    log_event(user_id, "smalltalk_judged", {"value": value},
              source=source)
    return True


def set_expectation_sent(user_id, source="sms"):
    """Stamp the expectation-setting delivery (checklist v2 item 1).
    Idempotent: an existing stamp is kept."""
    ensure_user_profile_row(user_id)
    prof = get_user_profile_by_id(user_id) or {}
    if (prof.get("expectation_sent_at") or "").strip():
        return
    now = datetime.now().isoformat()
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET expectation_sent_at = {_P} "
        f"WHERE user_id = {_P}", (now, user_id))
    conn.commit()
    conn.close()
    log_event(user_id, "expectation_sent", {}, source=source)


def set_material_status(user_id, status, source="analyze"):
    """Persist the settled answer to "do they study from a material?"
    — 'has_material' or 'no_material'. Transitions are allowed (a
    no-material user can acquire one later); same-value writes are
    skipped."""
    if status not in ("has_material", "no_material"):
        raise ValueError(f"bad material_status {status!r}")
    ensure_user_profile_row(user_id)
    prof = get_user_profile_by_id(user_id) or {}
    if (prof.get("material_status") or "").strip() == status:
        return
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET material_status = {_P} "
        f"WHERE user_id = {_P}", (status, user_id))
    conn.commit()
    conn.close()
    print(f"  [DB] material_status for {user_id}: {status}", flush=True)
    log_event(user_id, "material_aligned", {"status": status},
              source=source)


def set_pause(user_id, until_iso, source="analyze"):
    """Persist a user-requested silence window. until_iso: ISO
    timestamp (server clock) after which proactive sends resume;
    '' clears the pause. Emits pause_set / pause_cleared."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    _execute(conn,
        f"UPDATE user_profiles SET paused_until = {_P} "
        f"WHERE user_id = {_P}", (until_iso or "", user_id))
    conn.commit()
    conn.close()
    if until_iso:
        log_event(user_id, "pause_set", {"until": until_iso},
                  source=source)
    else:
        log_event(user_id, "pause_cleared", {}, source=source)


def get_pause(user_id):
    """The active pause's expiry ISO string, or None when not paused
    (never set, cleared, or expired)."""
    prof = get_user_profile_by_id(user_id) or {}
    until = (prof.get("paused_until") or "").strip()
    if not until:
        return None
    try:
        if datetime.now() >= datetime.fromisoformat(until):
            return None
    except ValueError:
        return None
    return until

def set_user_preference(user_id, key, value, evidence="",
                        source="analyze"):
    """Append one standing-preference version. Latest row per key
    wins. Returns True when it wrote (unchanged values are skipped —
    analyze re-reports on every pass)."""
    key = (key or "").strip().lower()[:60]
    value = (value or "").strip()[:300]
    if not key or not value:
        return False
    cur_val = get_user_preferences(user_id).get(key, {}).get("value")
    if cur_val == value:
        return False
    ensure_user_profile_row(user_id)
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO user_preferences (user_id, key, value, evidence, "
        f" ts, source) VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P})",
        (user_id, key, value, evidence[:300],
         datetime.now().isoformat(), source))
    conn.commit()
    conn.close()
    log_event(user_id, "preference_set",
              {"key": key, "value": value, "evidence": evidence[:200]},
              source=source)
    return True


def get_user_preferences(user_id):
    """→ {key: {'value', 'evidence', 'ts'}} — latest row per key."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT key, value, evidence, ts FROM user_preferences "
        f"WHERE user_id = {_P} ORDER BY id", (user_id,))
    rows = _fetchall(cur)
    conn.close()
    out = {}
    for r in rows:
        out[r["key"]] = {"value": r["value"],
                         "evidence": r["evidence"], "ts": r["ts"]}
    return out


# ─── v3 ledgers: accessors ──────────────────────────────────────────

def create_track(user_id, name, mode="drill", authority="file_wins",
                 exam_date="", performance_stage="", source="operator"):
    ensure_user_profile_row(user_id)
    conn = get_conn()
    cur = _execute(conn,
        f"INSERT INTO tracks (user_id, name, mode, authority, exam_date, "
        f" performance_stage, status, created_at) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, 'active', {_P})"
        + (" RETURNING id" if DB_TYPE == "postgres" else ""),
        (user_id, name, mode, authority, exam_date, performance_stage,
         datetime.now().isoformat()))
    track_id = _fetchone(cur)["id"] if DB_TYPE == "postgres" else cur.lastrowid
    conn.commit(); conn.close()
    log_event(user_id, "track_created",
              {"track_id": track_id, "name": name, "mode": mode},
              source=source)
    return track_id


def rename_track(track_id, name):
    """→ old name, or None if no such track. The name renders into
    every drill prompt, so it should be in the user's own words —
    first use: replacing the operator's import placeholder."""
    conn = get_conn()
    cur = _execute(conn, f"SELECT * FROM tracks WHERE id = {_P}",
                   (track_id,))
    row = _fetchone(cur)
    if not row:
        conn.close(); return None
    _execute(conn, f"UPDATE tracks SET name = {_P} WHERE id = {_P}",
             (name, track_id))
    conn.commit(); conn.close()
    log_event(row["user_id"], "track_renamed",
              {"track_id": track_id, "old": row["name"], "new": name},
              source="admin")
    return row["name"]


def get_tracks(user_id, mode=None):
    conn = get_conn()
    q = f"SELECT * FROM tracks WHERE user_id = {_P} AND status = 'active'"
    args = [user_id]
    if mode:
        q += f" AND mode = {_P}"; args.append(mode)
    cur = _execute(conn, q + " ORDER BY id", tuple(args))
    rows = _fetchall(cur); conn.close()
    return rows


# ─── Life tracks: config + generic items (2026-08-18 experiment) ────
# Everything here is conversation-driven: the tracks_ops hop emits
# operations, code validates them, these functions apply them. No
# operator gate — "유저가 대화로 activate 하라는걸 잘 알아듣고
# 알아서 해내는가"가 관찰 포인트다.

def enable_tracks(user_id, source="admin"):
    """Open the life-track lane for one user. Idempotent."""
    ensure_user_profile_row(user_id)
    prof = get_user_profile_by_id(user_id) or {}
    if (prof.get("tracks_enabled") or "").strip():
        return False
    conn = get_conn()
    _execute(conn,
             f"UPDATE user_profiles SET tracks_enabled = {_P} "
             f"WHERE user_id = {_P}",
             (datetime.now().isoformat(), user_id))
    conn.commit(); conn.close()
    log_event(user_id, "tracks_enabled", {}, source=source)
    return True


def tracks_lane_open(user_id):
    prof = get_user_profile_by_id(user_id) or {}
    return bool((prof.get("tracks_enabled") or "").strip())


def create_life_track(user_id, name, role, part_type, surfacing=None,
                      donts=None, profile_facts=None, cost_lane="",
                      status="active", source="conversation"):
    """A life track is born from conversation, already whole: role,
    part, surfacing rule, donts. status='held' records the judgment
    '지금은 안 만든다' — the deferred list is a ledger too."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    cur = _execute(conn,
        f"INSERT INTO tracks (user_id, name, mode, authority, status, "
        f" created_at, role, part_type, surfacing, donts, cost_lane, "
        f" profile_facts) "
        f"VALUES ({_P}, {_P}, 'life', 'user_wins', {_P}, {_P}, {_P}, "
        f" {_P}, {_P}, {_P}, {_P}, {_P})"
        + (" RETURNING id" if DB_TYPE == "postgres" else ""),
        (user_id, name, status, datetime.now().isoformat(), role,
         part_type, json.dumps(surfacing or {}, ensure_ascii=False),
         json.dumps(donts or [], ensure_ascii=False), cost_lane,
         json.dumps(profile_facts or [], ensure_ascii=False)))
    track_id = _fetchone(cur)["id"] if DB_TYPE == "postgres" else cur.lastrowid
    conn.commit(); conn.close()
    log_event(user_id, "life_track_created",
              {"track_id": track_id, "name": name, "part_type": part_type,
               "status": status}, source=source)
    return track_id


_TRACK_MUTABLE = ("name", "role", "surfacing", "donts", "cost_lane",
                  "profile_facts")


def update_track_config(track_id, fields, source="conversation"):
    """Conversation-driven config evolution — 유저의 상황은 항상
    변한다. Only _TRACK_MUTABLE keys move; part_type is NOT among
    them (changing the machine is a new part decision, zone B).
    → old row, or None."""
    fields = {k: v for k, v in (fields or {}).items()
              if k in _TRACK_MUTABLE}
    if not fields:
        return None
    conn = get_conn()
    cur = _execute(conn, f"SELECT * FROM tracks WHERE id = {_P}",
                   (track_id,))
    row = _fetchone(cur)
    if not row or row.get("mode") != "life":
        conn.close(); return None
    sets, args = [], []
    for k, v in fields.items():
        if k in ("surfacing", "donts", "profile_facts") \
                and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k} = {_P}"); args.append(v)
    args.append(track_id)
    _execute(conn,
             f"UPDATE tracks SET {', '.join(sets)} WHERE id = {_P}",
             tuple(args))
    conn.commit(); conn.close()
    log_event(row["user_id"], "life_track_updated",
              {"track_id": track_id, "fields": sorted(fields.keys())},
              source=source)
    return row


def set_track_status(track_id, status, source="conversation", reason=""):
    """active / held / retired — all reachable from conversation."""
    if status not in ("active", "held", "retired"):
        return None
    conn = get_conn()
    cur = _execute(conn, f"SELECT * FROM tracks WHERE id = {_P}",
                   (track_id,))
    row = _fetchone(cur)
    if not row:
        conn.close(); return None
    _execute(conn, f"UPDATE tracks SET status = {_P} WHERE id = {_P}",
             (status, track_id))
    conn.commit(); conn.close()
    log_event(row["user_id"], "life_track_status",
              {"track_id": track_id, "old": row["status"], "new": status,
               "reason": reason[:200]}, source=source)
    return row


def get_life_tracks(user_id, statuses=("active", "held")):
    """Life tracks across statuses — the ops hop needs held ones too
    (so a '다시 해보자' revives instead of duplicating)."""
    conn = get_conn()
    marks = ", ".join([_P] * len(statuses))
    cur = _execute(conn,
        f"SELECT * FROM tracks WHERE user_id = {_P} AND mode = 'life' "
        f"AND status IN ({marks}) ORDER BY id",
        tuple([user_id] + list(statuses)))
    rows = _fetchall(cur); conn.close()
    return rows


def add_track_item(track_id, user_id, kind, payload=None):
    conn = get_conn()
    now = datetime.now().isoformat()
    cur = _execute(conn,
        f"INSERT INTO track_items (track_id, user_id, kind, payload, "
        f" status, created_at, updated_at) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, 'open', {_P}, {_P})"
        + (" RETURNING id" if DB_TYPE == "postgres" else ""),
        (track_id, user_id, kind,
         json.dumps(payload or {}, ensure_ascii=False), now, now))
    item_id = _fetchone(cur)["id"] if DB_TYPE == "postgres" else cur.lastrowid
    conn.commit(); conn.close()
    return item_id


def update_track_item(item_id, payload=None, status=None):
    """Patch payload (merge, not replace — item shapes evolve) and/or
    move status (open → resolved). → updated row or None."""
    conn = get_conn()
    cur = _execute(conn, f"SELECT * FROM track_items WHERE id = {_P}",
                   (item_id,))
    row = _fetchone(cur)
    if not row:
        conn.close(); return None
    now = datetime.now().isoformat()
    new_payload = row["payload"]
    if payload is not None:
        try:
            merged = json.loads(row["payload"] or "{}")
        except Exception:
            merged = {}
        merged.update(payload)
        new_payload = json.dumps(merged, ensure_ascii=False)
    new_status = status or row["status"]
    resolved_at = now if (status == "resolved"
                          and row["status"] != "resolved") \
        else row.get("resolved_at") or ""
    _execute(conn,
             f"UPDATE track_items SET payload = {_P}, status = {_P}, "
             f"updated_at = {_P}, resolved_at = {_P} WHERE id = {_P}",
             (new_payload, new_status, now, resolved_at, item_id))
    conn.commit(); conn.close()
    row.update({"payload": new_payload, "status": new_status,
                "updated_at": now, "resolved_at": resolved_at})
    return row


def get_track_items(track_id, status="open"):
    conn = get_conn()
    q = f"SELECT * FROM track_items WHERE track_id = {_P}"
    args = [track_id]
    if status:
        q += f" AND status = {_P}"; args.append(status)
    cur = _execute(conn, q + " ORDER BY id", tuple(args))
    rows = _fetchall(cur); conn.close()
    return rows


def get_track_item(item_id):
    conn = get_conn()
    cur = _execute(conn, f"SELECT * FROM track_items WHERE id = {_P}",
                   (item_id,))
    row = _fetchone(cur); conn.close()
    return row


# ─── Reminders v0 (promise → row → cron-fired nudge) ────────────────

def create_reminder(user_id, fire_at, instruction, recur="",
                    source="operator"):
    """fire_at: UTC ISO string (aware, +00:00). recur: '' one-shot |
    'weekdays'. The instruction is the server-turn text send_nudge
    will hand the model — write it as an operator instruction, not
    as user-facing copy."""
    ensure_user_profile_row(user_id)
    conn = get_conn()
    cur = _execute(conn,
        f"INSERT INTO reminders (user_id, fire_at, recur, instruction, "
        f" status, created_at, source) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, 'open', {_P}, {_P})"
        + (" RETURNING id" if DB_TYPE == "postgres" else ""),
        (user_id, fire_at, recur, instruction,
         datetime.now().isoformat(), source))
    rid = _fetchone(cur)["id"] if DB_TYPE == "postgres" else cur.lastrowid
    conn.commit(); conn.close()
    log_event(user_id, "reminder_created",
              {"reminder_id": rid, "fire_at": fire_at, "recur": recur,
               "instruction": instruction[:200]}, source=source)
    return rid


def get_due_reminders(now_iso):
    """Open reminders whose fire_at has passed. Both sides are aware
    UTC ISO strings of the same shape, so string compare is time
    compare."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM reminders WHERE status = 'open' "
        f"AND fire_at <= {_P} ORDER BY fire_at", (now_iso,))
    rows = _fetchall(cur); conn.close()
    return rows


def get_reminders(user_id, status=None):
    conn = get_conn()
    q = f"SELECT * FROM reminders WHERE user_id = {_P}"
    args = [user_id]
    if status:
        q += f" AND status = {_P}"; args.append(status)
    cur = _execute(conn, q + " ORDER BY fire_at", tuple(args))
    rows = _fetchall(cur); conn.close()
    return rows


def mark_reminder_fired(reminder_id, next_fire_at=None, sent=True):
    """One-shot → done; recurring → reschedule to next_fire_at. A
    failed send still closes/advances the row (send_nudge already
    logged the failure) — a reminder must never fire-loop."""
    conn = get_conn()
    now = datetime.now().isoformat()
    if next_fire_at:
        _execute(conn,
            f"UPDATE reminders SET fire_at = {_P}, last_fired_at = {_P} "
            f"WHERE id = {_P}", (next_fire_at, now, reminder_id))
    else:
        _execute(conn,
            f"UPDATE reminders SET status = 'done', last_fired_at = {_P} "
            f"WHERE id = {_P}", (now, reminder_id))
    conn.commit(); conn.close()


# ─── Research requests ("find out X" → row → search hop → nudge) ────

def create_research_request(user_id, question, evidence_quote=""):
    """Dedupe-or-create: analyze re-reads the WHOLE transcript every
    turn, so the same ask would re-fire forever — an existing request
    with the same evidence quote (or same question text, any status)
    absorbs the report. → new id, or None if absorbed."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT id FROM research_requests WHERE user_id = {_P} "
        f"AND (question = {_P} OR (evidence_quote != '' "
        f"AND evidence_quote = {_P}))",
        (user_id, question, evidence_quote))
    if _fetchone(cur):
        conn.close()
        return None
    cur = _execute(conn,
        f"INSERT INTO research_requests (user_id, question, "
        f" evidence_quote, status, created_at) "
        f"VALUES ({_P}, {_P}, {_P}, 'open', {_P})"
        + (" RETURNING id" if DB_TYPE == "postgres" else ""),
        (user_id, question, evidence_quote,
         datetime.now().isoformat()))
    rid = _fetchone(cur)["id"] if DB_TYPE == "postgres" else cur.lastrowid
    conn.commit(); conn.close()
    log_event(user_id, "research_requested",
              {"request_id": rid, "question": question[:300]},
              source="analyze")
    return rid


def get_research_request(request_id):
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM research_requests WHERE id = {_P}",
        (request_id,))
    row = _fetchone(cur); conn.close()
    return row


def get_open_research_requests(user_id):
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM research_requests WHERE user_id = {_P} "
        f"AND status = 'open' ORDER BY id", (user_id,))
    rows = _fetchall(cur); conn.close()
    return rows


def finish_research_request(request_id, status, findings="",
                            llm_call_id=None):
    """status: 'done' | 'failed'. Findings are kept even on failure
    (whatever partial text existed) — the ledger records what
    happened, not just successes."""
    conn = get_conn()
    _execute(conn,
        f"UPDATE research_requests SET status = {_P}, findings = {_P}, "
        f"finished_at = {_P}, llm_call_id = {_P} WHERE id = {_P}",
        (status, findings, datetime.now().isoformat(), llm_call_id,
         request_id))
    conn.commit(); conn.close()


def cancel_reminder(reminder_id, user_id):
    """Ownership-checked cancel. → True if a row changed."""
    conn = get_conn()
    cur = _execute(conn,
        f"UPDATE reminders SET status = 'cancelled' "
        f"WHERE id = {_P} AND user_id = {_P} AND status = 'open'",
        (reminder_id, user_id))
    changed = cur.rowcount > 0
    conn.commit(); conn.close()
    if changed:
        log_event(user_id, "reminder_cancelled",
                  {"reminder_id": reminder_id}, source="operator")
    return changed


def add_knowledge_item(track_id, user_id, stem, anchor_type,
                       anchor_quote="", section_hint="", elements=None,
                       kind="", est_difficulty=2, source="extraction",
                       status="untested"):
    """An item that cannot quote its origin does not exist: file_chunk
    and conversation items REQUIRE an anchor_quote.

    One deliberate exception: status='needs_anchor' — the holding pen
    for items whose grounding must come from the USER ('your file
    doesn't state this compactly — where would you point a
    colleague?'). Anchorless items may exist there and ONLY there;
    selection never circulates them, and leaving the pen requires a
    real anchor."""
    if anchor_type in ("file_chunk", "conversation") \
            and not (anchor_quote or "").strip() \
            and status != "needs_anchor":
        raise ValueError("anchored item without anchor_quote")
    conn = get_conn()
    cur = _execute(conn,
        f"INSERT INTO knowledge_items (track_id, user_id, anchor_type, "
        f" anchor_quote, section_hint, stem, elements_json, kind, "
        f" est_difficulty, status, source, created_at) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, "
        f" {_P}, {_P}, {_P})"
        + (" RETURNING id" if DB_TYPE == "postgres" else ""),
        (track_id, user_id, anchor_type, anchor_quote, section_hint,
         stem, json.dumps(elements or [], ensure_ascii=False), kind,
         est_difficulty, status, source, datetime.now().isoformat()))
    item_id = _fetchone(cur)["id"] if DB_TYPE == "postgres" else cur.lastrowid
    conn.commit(); conn.close()
    return item_id


def set_attempt_item(attempt_id, item_id):
    """Link an attempt to its bank item (seed imports create items
    FROM answered questions, then point the original attempts at
    them)."""
    conn = get_conn()
    _execute(conn,
        f"UPDATE attempts SET item_id = {_P} WHERE id = {_P}",
        (item_id, attempt_id))
    conn.commit(); conn.close()


def delete_track_items(track_id):
    """Wipe a track's bank (rebank). Attempts keep their rows —
    their item_id links are cleared so history survives the wipe
    without dangling references."""
    conn = get_conn()
    _execute(conn,
        f"UPDATE attempts SET item_id = NULL WHERE track_id = {_P}",
        (track_id,))
    cur = _execute(conn,
        f"DELETE FROM knowledge_items WHERE track_id = {_P}",
        (track_id,))
    n = cur.rowcount
    conn.commit(); conn.close()
    return n


def get_knowledge_items(track_id, status=None):
    conn = get_conn()
    q = f"SELECT * FROM knowledge_items WHERE track_id = {_P}"
    args = [track_id]
    if status:
        q += f" AND status = {_P}"; args.append(status)
    cur = _execute(conn, q + " ORDER BY id", tuple(args))
    rows = _fetchall(cur); conn.close()
    for r in rows:
        try: r["elements"] = json.loads(r["elements_json"])
        except Exception: r["elements"] = []
    return rows


def set_item_status(item_id, status, source="server"):
    """untested|learning|solid|suspended — 'suspended' is the user's
    '이건 그만 물어봐', one sentence retires an item."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT user_id FROM knowledge_items WHERE id = {_P}", (item_id,))
    row = _fetchone(cur)
    _execute(conn,
        f"UPDATE knowledge_items SET status = {_P} WHERE id = {_P}",
        (status, item_id))
    conn.commit(); conn.close()
    if row:
        log_event(row["user_id"], "item_status_set",
                  {"item_id": item_id, "status": status}, source=source)


def record_attempt(track_id, user_id, verdict, question="",
                   answer_verbatim="", elements=None, item_id=None,
                   source="drill", self_confidence="",
                   confidence_marker="", note="", ts=None):
    conn = get_conn()
    cur = _execute(conn,
        f"INSERT INTO attempts (item_id, track_id, user_id, ts, source, "
        f" question, answer_verbatim, elements_json, verdict, "
        f" self_confidence, confidence_marker, note) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, "
        f" {_P}, {_P}, {_P})"
        + (" RETURNING id" if DB_TYPE == "postgres" else ""),
        (item_id, track_id, user_id, ts or datetime.now().isoformat(),
         source, question, answer_verbatim,
         json.dumps(elements or [], ensure_ascii=False), verdict,
         self_confidence, confidence_marker, note))
    attempt_id = _fetchone(cur)["id"] if DB_TYPE == "postgres" else cur.lastrowid
    conn.commit(); conn.close()
    log_event(user_id, "attempt_recorded",
              {"attempt_id": attempt_id, "item_id": item_id,
               "verdict": verdict, "source": source}, source="server")
    return attempt_id


def get_attempts(track_id, limit=200):
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM attempts WHERE track_id = {_P} "
        f"ORDER BY ts DESC LIMIT {_P}", (track_id, limit))
    rows = _fetchall(cur); conn.close()
    for r in rows:
        try: r["elements"] = json.loads(r["elements_json"])
        except Exception: r["elements"] = []
    return rows


def add_taught(track_id, user_id, quote, teaching, kind="",
               conflict_flag="", ts=None):
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO taught_ledger (track_id, user_id, ts, quote, "
        f" teaching, kind, conflict_flag, status) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P}, {_P}, 'active')",
        (track_id, user_id, ts or datetime.now().isoformat(), quote,
         teaching, kind, conflict_flag))
    conn.commit(); conn.close()
    log_event(user_id, "taught_recorded",
              {"track_id": track_id, "teaching": teaching[:120],
               "conflict": bool(conflict_flag)}, source="server")


def get_taught(track_id):
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM taught_ledger WHERE track_id = {_P} "
        f"AND status = 'active' ORDER BY id", (track_id,))
    rows = _fetchall(cur); conn.close()
    return rows


def add_person_note(user_id, observation, evidence="",
                    confidence="low", ts=None):
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO person_notes (user_id, ts, observation, evidence, "
        f" confidence, status) VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, "
        f" 'active')",
        (user_id, ts or datetime.now().isoformat(), observation,
         evidence, confidence))
    conn.commit(); conn.close()


def get_person_notes(user_id):
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM person_notes WHERE user_id = {_P} "
        f"AND status = 'active' ORDER BY id", (user_id,))
    rows = _fetchall(cur); conn.close()
    return rows


def record_prediction(item_id, user_id, predicted_verdict,
                      predicted_difficulty=None, reason=""):
    """Written BEFORE the answer exists. There is deliberately no
    update accessor for the prediction fields — score_prediction fills
    the outcome ONCE and refuses re-scoring. A prediction edited after
    the fact makes the KPI a lie."""
    conn = get_conn()
    cur = _execute(conn,
        f"INSERT INTO predictions (item_id, user_id, ts, "
        f" predicted_verdict, predicted_difficulty, reason) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P}, {_P})"
        + (" RETURNING id" if DB_TYPE == "postgres" else ""),
        (item_id, user_id, datetime.now().isoformat(), predicted_verdict,
         predicted_difficulty, reason))
    pred_id = _fetchone(cur)["id"] if DB_TYPE == "postgres" else cur.lastrowid
    conn.commit(); conn.close()
    return pred_id


def score_prediction(pred_id, actual_verdict):
    """One-shot: scoring an already-scored prediction raises."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM predictions WHERE id = {_P}", (pred_id,))
    row = _fetchone(cur)
    if not row:
        conn.close(); raise ValueError(f"no prediction {pred_id}")
    if (row["scored_at"] or "").strip():
        conn.close()
        raise ValueError(f"prediction {pred_id} already scored")
    hit = 1 if row["predicted_verdict"] == actual_verdict else 0
    _execute(conn,
        f"UPDATE predictions SET scored_at = {_P}, actual_verdict = {_P}, "
        f" hit = {_P} WHERE id = {_P}",
        (datetime.now().isoformat(), actual_verdict, hit, pred_id))
    conn.commit(); conn.close()
    return bool(hit)


def get_predictions(user_id, limit=200):
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM predictions WHERE user_id = {_P} "
        f"ORDER BY id DESC LIMIT {_P}", (user_id, limit))
    rows = _fetchall(cur); conn.close()
    return rows


def get_open_prediction(user_id, within_hours=48):
    """The outstanding drill question: the latest unscored
    prediction, if it's still fresh. One row is the whole open-
    question state — a prediction exists exactly when a question was
    asked, and scored_at fills exactly when it was answered."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM predictions WHERE user_id = {_P} "
        f"AND scored_at = '' ORDER BY id DESC LIMIT 1", (user_id,))
    row = _fetchone(cur); conn.close()
    if not row:
        return None
    try:
        age_h = (datetime.now()
                 - datetime.fromisoformat(row["ts"])).total_seconds() / 3600
    except Exception:
        return None
    return row if age_h < within_hours else None


def prediction_stats(user_id):
    """→ {'scored', 'hits', 'accuracy'} — the KPI surface."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT COUNT(*) AS n, COALESCE(SUM(hit), 0) AS h "
        f"FROM predictions WHERE user_id = {_P} AND scored_at != ''",
        (user_id,))
    row = _fetchone(cur); conn.close()
    n, h = row["n"] or 0, row["h"] or 0
    return {"scored": n, "hits": h,
            "accuracy": round(h / n, 3) if n else None}


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
    # A registered material IS the alignment answer (checklist v2):
    # an upload settles 'has_material' more authoritatively than any
    # conversation reading could.
    set_material_status(user_id, "has_material", source=source)
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
    mechanical fill condition for the material_understanding onboarding
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


# ─── Screen co-viewing sessions ─────────────────────────────────────

SESSION_DEAD_AFTER_S = 60


def record_consent(user_id, doc, version):
    """Record an explicit acceptance (just-in-time consent). The row
    IS the compliance artifact: who, which document, which version,
    when. Idempotent per (user, doc, version)."""
    if has_consent(user_id, doc, version):
        return False
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO user_consents (user_id, doc, version, ts) "
        f"VALUES ({_P}, {_P}, {_P}, {_P})",
        (user_id, doc, version, datetime.now().isoformat()))
    conn.commit()
    conn.close()
    log_event(user_id, "consent_accepted",
              {"doc": doc, "version": version}, source="web")
    return True


def has_consent(user_id, doc, version):
    """True if this user accepted THIS version of the document. A
    version bump reopens the question by construction."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT COUNT(*) AS n FROM user_consents "
        f"WHERE user_id = {_P} AND doc = {_P} AND version = {_P}",
        (user_id, doc, version))
    row = _fetchone(cur)
    conn.close()
    return bool(row and row["n"])


def start_screen_session(user_id, declared_source=""):
    """→ session_id. Emits session_started."""
    ensure_user_profile_row(user_id)
    sid = uuid.uuid4().hex[:12]
    now = datetime.now().isoformat()
    conn = get_conn()
    _execute(conn,
        f"INSERT INTO screen_sessions (session_id, user_id, "
        f" declared_source, started_at, last_seen) "
        f"VALUES ({_P}, {_P}, {_P}, {_P}, {_P})",
        (sid, user_id, declared_source, now, now))
    conn.commit()
    conn.close()
    log_event(user_id, "session_started",
              {"session_id": sid, "declared_source": declared_source},
              source="session")
    return sid


def touch_screen_session(session_id, frame=False):
    """Heartbeat (and frame counter). The heartbeat IS liveness:
    no reaper process, staleness is judged at read time."""
    conn = get_conn()
    _execute(conn,
        f"UPDATE screen_sessions SET last_seen = {_P}"
        + (", frames = frames + 1" if frame else "")
        + f" WHERE session_id = {_P} AND ended_at IS NULL",
        (datetime.now().isoformat(), session_id))
    conn.commit()
    conn.close()


def end_screen_session(session_id, reason="user"):
    """Idempotent close. Emits session_stopped once."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT user_id, started_at, frames FROM screen_sessions "
        f"WHERE session_id = {_P} AND ended_at IS NULL", (session_id,))
    row = _fetchone(cur)
    if not row:
        conn.close()
        return False
    _execute(conn,
        f"UPDATE screen_sessions SET ended_at = {_P} "
        f"WHERE session_id = {_P}",
        (datetime.now().isoformat(), session_id))
    conn.commit()
    conn.close()
    try:
        mins = round((datetime.now()
                      - datetime.fromisoformat(row["started_at"])
                      ).total_seconds() / 60, 1)
    except Exception:
        mins = None
    log_event(row["user_id"], "session_stopped",
              {"session_id": session_id, "reason": reason,
               "minutes": mins, "frames": row["frames"]},
              source="session")
    return True


def get_screen_session(session_id):
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM screen_sessions WHERE session_id = {_P}",
        (session_id,))
    row = _fetchone(cur)
    conn.close()
    return row


def get_active_screen_session(user_id):
    """The user's live session, or None. A session is live when it is
    unended AND its heartbeat is fresh (<{dead}s) — a closed laptop
    never sent its stop.""".format(dead=SESSION_DEAD_AFTER_S)
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM screen_sessions WHERE user_id = {_P} "
        f"AND ended_at IS NULL ORDER BY started_at DESC LIMIT 1",
        (user_id,))
    row = _fetchone(cur)
    conn.close()
    if not row:
        return None
    try:
        age = (datetime.now()
               - datetime.fromisoformat(row["last_seen"])).total_seconds()
    except Exception:
        return None
    return row if age < SESSION_DEAD_AFTER_S else None


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


# Order IS the onboarding arc (checklist v2, 2026-08-06): set
# expectations, discover the goal, agree what "started" looks like,
# settle whether they study from a material, understand that material
# (only if one exists), TELL THEM WHAT THE COACH WILL DO and get that
# confirmed, agree when to message. The prompt shows only the first
# unsettled item as the current focus.
#
# The order IS the arc. `bite` is deliberately NOT here: the first
# concrete task belongs to the first study session, which happens
# AFTER onboarding completes and the sequence plan exists. `path`
# (v1) is gone: live data showed it collapsing into a goal
# restatement; the goal carries the direction.
#
# - expectation_setting is SERVER-SENT: a fixed message identical
#   for every user, checked off by delivery — never an LLM's job.
# - material_alignment is the settled answer to "do they study from
#   something?" — 'has_material' or 'no_material', both fine. Stored
#   fact, not inference: the no-material parrot bug came from leaving
#   this to per-turn guessing.
# - material_understanding exists ONLY for has_material users, and
#   sits BEFORE offer on purpose: the offer is built FROM the
#   walkthrough. The user shows Theo the thing (/my upload, a link,
#   or naming it), Theo leads a walkthrough until it can produce a
#   sample of its offer, and the user confirming that sample is what
#   fills the field. No-material users go straight to offer.
# ignition_marker retired from the checklist (2026-08-12, PR-A):
# the ignition→flow frame is shelved pending a user who needs it —
# companion-era onboarding must not require agreeing an "ignition
# marker". Columns and past data remain; only the gate shrank.
ONBOARDING_FIELDS = ("expectation_setting", "goal",
                     "material_alignment", "material_understanding",
                     "offer", "schedule")


def get_onboarding_state(user_id):
    """→ {'started_at', 'completed_at', 'missing': [...], 'filled':
    [...], 'material_status'} — the checklist, computed mechanically
    from stored data. material_understanding is conditional: it only
    appears (in either list) once alignment settled on has_material."""
    prof = get_user_profile_by_id(user_id) or {}
    mat_status = (prof.get("material_status") or "").strip()
    if not mat_status and get_user_materials(user_id):
        # A registered material settles alignment by existing, even
        # if the analyze pass never wrote the column.
        mat_status = "has_material"
    filled = []
    if (prof.get("expectation_sent_at") or "").strip():
        filled.append("expectation_setting")
    if (prof.get("agreed_goal") or "").strip():
        filled.append("goal")
    if mat_status:
        filled.append("material_alignment")
    if mat_status == "has_material" and has_validated_material(user_id):
        filled.append("material_understanding")
    if get_user_schedule(user_id):
        filled.append("schedule")
    if (prof.get("agreed_offer") or "").strip():
        filled.append("offer")
    applicable = [f for f in ONBOARDING_FIELDS
                  if f != "material_understanding"
                  or mat_status == "has_material"]
    return {
        "started_at": prof.get("onboarding_started_at"),
        "completed_at": prof.get("onboarding_completed_at"),
        "material_status": mat_status,
        "filled": filled,
        "missing": [f for f in applicable if f not in filled],
    }


def check_and_complete_onboarding(user_id, force=False):
    """If every applicable field is filled (or force=True, operator
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
    if not force and tracks_lane_open(user_id):
        # Companion users have no onboarding checklist: completion —
        # and the genplan study plan it would trigger — is
        # legacy-lane machinery (founder decision 2026-08-20).
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


def get_sms_signup(signup_id):
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM sms_signups WHERE id = {_P}", (signup_id,))
    row = _fetchone(cur)
    conn.close()
    return row


def get_pending_signups():
    """Waiting consent records, oldest first — the operator's inbox."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT * FROM sms_signups WHERE status = 'pending' "
        f"ORDER BY id")
    rows = _fetchall(cur)
    conn.close()
    return rows


def set_signup_status(signup_id, status):
    conn = get_conn()
    _execute(conn,
        f"UPDATE sms_signups SET status = {_P} WHERE id = {_P}",
        (status, signup_id))
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


def get_session_observations(session_id, limit=12):
    """One session's observations, oldest-first — the journey the
    web-chat reply is grounded in."""
    conn = get_conn()
    cur = _execute(conn,
        f"SELECT ts, summary FROM observations "
        f"WHERE session_id = {_P} ORDER BY id DESC LIMIT {_P}",
        (session_id, limit))
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
