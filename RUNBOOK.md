# RUNBOOK — Theo operator procedures

Operational procedures for running the Theo pilot. Written for the
operator (the founder); every step is meant to be followed verbatim.
All admin endpoints authenticate with the shared secret — either an
`X-Cron-Secret: $CRON_SECRET` header or a `?secret=$CRON_SECRET`
query parameter. Never paste the real secret into documents or chat
logs; `$CRON_SECRET` below always means the value from Render's
environment.

---

## Swap rehearsal — making the founder's husband pilot user #1

The system is still single-user by env shim: one phone number maps to
one user_id via two Render environment variables. The "swap" replaces
the active user (the founder, `jeongmo`) with the husband for a
rehearsal of the real onboarding→plan→sequence pipeline. This section
is the verbatim procedure.

### 1. Identity rule — read this first, it is the one real hazard

**The husband gets a FRESH user_id (e.g. `hub`). NEVER reuse
`jeongmo`.**

The active user is defined by TWO Render env vars that must change
**together**:

| Env var | Meaning |
|---|---|
| `TUTOR_USER_ID` | which user_id all sends, markers, events, and admin defaults attribute to |
| `TUTOR_USER_PHONE` | which inbound phone number is accepted and where scheduled sends go |

**Failure mode to avoid:** changing only `TUTOR_USER_PHONE` and
leaving `TUTOR_USER_ID=jeongmo` makes the system deliver messages to
the husband's phone while attributing every inbound message, every
`[GOAL:]`/`[IGNITION_DEF:]`/plan marker, and every event to the
**founder's** account. His onboarding would silently **overwrite the
founder's agreed goal, ignition marker, and path** — corrupting both
users' data at once, in append-only tables that are deliberately hard
to clean up. Always change both vars in the same edit, and verify
(step 3) before any message is sent.

### 2. Pre-swap checklist

1. **Consent:** the husband opts in himself at
   <https://learningtheo.com/sms-signup>. This saves a consent row
   only — rows in `sms_signups` **never trigger sends**; activation
   is manual by design (the operator flips the env vars). The row is
   the compliance record; the swap is the activation.
2. **Channel:** leave `MESSAGING_CHANNEL=whatsapp` for the rehearsal.
   Toll-free SMS verification is still pending, so the rehearsal runs
   on the WhatsApp sandbox (with the join caveats in §4).
3. Have his phone number in E.164 form (`+1...`) ready, and decide
   his user_id (short, stable, lowercase — e.g. `hub`).

### 3. Swap steps

1. In the Render dashboard, set **both** env vars in one edit:
   - `TUTOR_USER_ID` → `hub`
   - `TUTOR_USER_PHONE` → husband's number, bare E.164 (no
     `whatsapp:` prefix — the code adds channel prefixes itself)
2. Save → Render redeploys. Wait for the deploy to go live.
3. **Verify identity BEFORE any send** (this is the guard against the
   §1 failure mode):

   ```bash
   curl "https://<app-host>/onboarding?secret=$CRON_SECRET"
   ```

   Must show `# onboarding — hub` with `started_at`/`completed_at`
   empty and **everything in `missing`** (a fresh user). If it shows
   `jeongmo`, or `hub` with fields already filled, STOP — the env
   vars are wrong or were reused.

   ```bash
   curl "https://<app-host>/sms/status?secret=$CRON_SECRET"
   ```

   Must show `"user_id": "hub"` with fresh phase state.

### 4. WhatsApp sandbox — join before first send, rejoin every 3 days

- **Before the first coach send**, the husband must text
  `join <sandbox-code>` to **+1 415 523 8886** from his phone.
  Order matters: `onboarding_started_at` is stamped at the first
  coach send, and send-after-join is what makes "sent ≈ received" a
  safe assumption for onboarding timing.
- **The sandbox join expires every 3 days, and the expiry is
  SILENT** — Twilio accepts the send, the phone never receives it,
  and nothing errors. A multi-day onboarding **will** cross the
  expiry at least once.
- Mitigations, both required:
  - **Rejoin cadence:** have the husband re-text the join message
    every 3rd day of the rehearsal (calendar reminder recommended).
  - **Detector:** the `whatsapp_expiry_suspected` infra event (T6
    watchdog; visible in `/debug/timeline`) fires when we are
    actively sending but the user has been silent ~2+ days — it is
    the signal that sends may be silently dropped. If it appears,
    have him rejoin before drawing any coaching conclusions from the
    "silence."

### 5. Observation surfaces — where to watch each rehearsal observable

| # | Observable | Where to watch |
|---|---|---|
| 1 | Onboarding conversation flows and fields get stored | `GET /debug/trace?secret=...&verbose=1` (step-language trace with message snippets) + `GET /onboarding?secret=...` (filled/missing checklist, path, schedule) |
| 2 | Initial notes + sequence plan generated at onboarding completion | `GET /plan?secret=...` and `GET /notes?secret=...`; the `plan_generated` event in `/debug/timeline` marks the moment. **Operator must review the plan and notes BEFORE the first sequence-mode send** — this is the manual-policy-engine gate. |
| 3 | Scheduled sends fire in his windows | `GET /debug/timeline?secret=...` — `cron_tick` / `sms_out` events (the `window` field in payloads, once P0-C lands) |
| 4 | Step progression (one step per turn, cursor advances) | `GET /plan?secret=...` cursor position + `steps` arrays in `sms_out` event payloads |
| 5 | Ignition gets recorded | `ignition_judgment` events in `/debug/timeline` (real-time scores) + `GET /debug/learner-state?secret=...` for the authoritative nightly judgment (requires the nightly annotation cron — P0-F; until it exists, trigger manually per "Backups & annotation" below) |
| 6 | Unknown-sender isolation (founder texts during swap) | `sms_in_unknown_sender` events in `/debug/timeline` — see §6 |

### 6. During the rehearsal

- If the founder texts the coach number while swapped, her number no
  longer matches `TUTOR_USER_PHONE`, so the message is logged as an
  `sms_in_unknown_sender` event (payload keeps the number + a text
  snippet) and is **not attributed to any user** — no data pollution
  in either account.
- Corollary: the founder's own coaching is **PAUSED** for the
  duration of the swap. Scheduled sends go to the husband; the
  founder gets nothing and her replies are ignored. This is expected.

### 7. Rollback

1. In Render, restore **both** env vars to the founder's values
   (`TUTOR_USER_ID=jeongmo`, `TUTOR_USER_PHONE=<founder's number>`)
   in one edit → redeploy.
2. Verify before resuming:

   ```bash
   curl "https://<app-host>/sms/status?secret=$CRON_SECRET"
   ```

   ```bash
   curl "https://<app-host>/onboarding?secret=$CRON_SECRET"
   ```

   Both must show `jeongmo` again, with onboarding **complete**
   (filled fields intact) — if anything looks blank, stop and check
   the env vars before any send fires.
3. The husband's data stays intact under his user_id (`hub`) —
   events, messages, notes, plan, learner-state snapshots are all
   keyed by user_id and remain queryable for rehearsal analysis
   (`/debug/timeline?user_id=hub`, `/debug/trace?user_id=hub`, ...).

---

## Backups & annotation (stub)

- **Nightly annotation:** the LearnerState annotation job runs via
  `POST /annotate/run?secret=$CRON_SECRET` — currently manual; the
  nightly Render cron is P0-F. Until it lands, run it by hand the
  morning after any day you care about (e.g. every rehearsal day).
- **Database backups:** owned pg_dump backups (nightly dump → R2,
  30-day rotation) are WEEK1_ORDER T6b — pending R2 signup. Until
  then the only safety net is Render's 3-day point-in-time recovery
  window.

---

## Appendix — T10 user_id plumbing audit (2026-07-29)

Scope: the PILOT-PATH modules only — `sms.py`, `features.py`,
`notes.py`, `trace.py`, `genplan.py`, `annotate.py`, `infra.py`,
`policy.py`, and the `db.py` helpers they call. The audit checks two
rules from brief §4.6 ("user_id everywhere"):

1. Every function takes `user_id` explicitly — no reliance on the
   legacy thread-local `db._uid()` / global `db.USER_ID` convention
   (that convention remains the WEB-chat pattern in `db.py`/
   `coach.py`; the web chat is out of scope and was not refactored).
2. `TUTOR_USER_ID` env reads are confined to entry points (cron tick,
   inbound resolution, admin-endpoint defaults) — never buried in
   helpers.

### Verdicts

| Module | Verdict | Notes |
|---|---|---|
| `sms.py` | ✅ pass (1 violation, fixed) | All functions take `user_id` explicitly. `TUTOR_USER_ID`/`TUTOR_USER_PHONE` reads confined to the two entry points: `_resolve_user_from_phone` (inbound) and `handle_cron_tick` (cron). **Violation (fixed in this PR):** `_format_recent_insights` called `db.set_thread_user(user_id)` so that thread-local `db.get_recent_insights()` would resolve the right user — the sole pilot-path use of the legacy convention. |
| `features.py` | ✅ pass | `compute_features(user_id)` / `render_features(feats)`; every db call passes `user_id` explicitly. |
| `notes.py` | ✅ pass | `render_notes_block(user_id)` → `db.get_user_notes(user_id)`. |
| `trace.py` | ✅ pass | `render_trace(user_id, ...)` → `db.get_events_with_ids(user_id, ...)`. |
| `genplan.py` | ✅ pass | `generate(user_id)` / `generate_async(user_id)` / `_build_system(user_id)`; all db calls explicit. |
| `annotate.py` | ✅ pass | `annotate_day(user_id, ...)`; `annotate_all` iterates `db.get_active_user_ids(start, end)` — already multi-user-shaped, no env reliance. |
| `infra.py` | ✅ pass | `TUTOR_USER_ID` read once at the top of `sweep()` (the module's entry point) and passed down explicitly to `_check_cron_staleness(user_id)` / `_check_whatsapp_expiry(user_id)`. `_check_capture_gaps` derives `user_id` from the open-session rows themselves — the correct multi-user pattern. |
| `policy.py` | ✅ pass | `decide(decision_point, user_id, ...)` — `user_id` is a required positional. |
| `db.py` helpers (pilot-path subset) | ✅ pass (1 violation, fixed) | All helpers the pilot path calls take `user_id` as an explicit first parameter, except: `get_recent_insights(limit)` used thread-local `_uid()` — **fixed** with a backward-compatible optional `user_id=None` parameter (web-chat callers keep the thread-local default; the pilot path now passes it explicitly). Helpers that legitimately take no `user_id`: `register_prompt_version` (user-agnostic), `get_last_observation_ts(session_id)` (session-keyed), `get_open_observe_sessions()` / `get_active_user_ids()` (multi-user sweeps that return user_ids). |

### Fix applied

`db.get_recent_insights(limit=3)` → `db.get_recent_insights(limit=3,
user_id=None)`; when `user_id` is None it falls back to the legacy
thread-local (web chat unchanged). `sms._format_recent_insights` now
calls `db.get_recent_insights(limit=3, user_id=user_id)` and the
`db.set_thread_user(user_id)` call was removed — the pilot path no
longer touches thread-local user state at all.

### Residual (reported, not fixed — web-chat territory)

`coach.py`'s web-chat request path still sets and reads thread-local
user state (`set_thread_user`, `_uid()`, global `USER_ID = "jeongmo"`
fallback in `db.py`). That is the documented legacy convention, out
of scope for T10; it becomes the target of the week-3 multi-user
routing work. No pilot-path module depends on it anymore.
