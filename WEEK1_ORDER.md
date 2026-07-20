# WEEK1_ORDER — Data foundation (the non-retrofittable layer)

> Companion to PROJECT_BRIEF.md §6 Week 1. Read INVENTORY.md first.
> Tasks are ordered; each has acceptance criteria. Decisions D1-D4
> need the operator's call BEFORE the task that depends on them.

## Open decisions (operator input needed)

**D1 — RESOLVED (2026-07-20): keep the Postgres/SQLite duality, with
conditions.** Brief §4.7's "SQLite" was written without knowledge of
the deployed infra; Render disk is ephemeral, so server-side SQLite
would be wiped on every deploy. Events live in the same dual-backend
`db.py` layer as everything else (Postgres in prod, SQLite locally).
Conditions attached by the operator (via brief-author review):
1. Blob storage must be object storage (→ D2), never local disk —
   otherwise timeline events reference files that no longer exist.
2. ✅ VERIFIED (2026-07-20, Render console): single live instance
   `upskill-coach-db-dmmu`, paid Basic-256mb, Blueprint-managed.
   Point-in-Time Recovery window is **3 days** (7 needs a Pro
   workspace); manual logical Export available (retained ≥7 days).
   The 3-day window is thin for pilot data → T6b owned backups
   (nightly pg_dump → R2, 30-day rotation) are the real safety net,
   not an extra.
3. Dialect discipline: `db.py` is raw-SQL with manual branching, so
   event-store code uses only the dialect-neutral subset — plain
   INSERT/SELECT, JSON stored as TEXT via json.dumps (no engine JSON
   functions), no upserts (append-only needs none). Escalation
   trigger: if a dialect bug bites twice, unify local dev on
   dockerized Postgres.

**D2 — RESOLVED (2026-07-20, operator approved): Cloudflare R2.**
S3-compatible, free at pilot scale, no egress fees. Content-hash
keys; local-dir fallback for dev. Operator-confirmed pipeline order:
**capture → raw image to R2 → LLM summarize → summary stored** (the
raw write happens BEFORE and independent of summarization — a vision
failure must never lose the raw). Setup: one R2 account + bucket,
2 env vars (credentials), boring boto3-compatible client. Supersedes
the original local `blobs/<user_id>/<sha256>` instruction.

**D3 — Legacy quarantine list: confirm INVENTORY.md §9.**
→ **Needs operator: yes/no per line (or approve as proposed).**

**D4 — Outcome v1 (operator owns this definition; draft to edit):**
"An *ignition success* = within 120 min of an evening anchor message
(or user-initiated evening conversation), the user performs ≥1
observable learning action (code run / notebook edit / study-material
interaction visible in observations OR an in-conversation exercise
attempt), with ≥10 min of continued engagement. A *flow session* =
ignition success + ≥25 min total engagement without a ≥10-min
avoidance gap." Versioned as `outcome_v1`; expected to be wrong;
revised against raw data later.
→ **Needs operator: edit/approve wording.**

## Tasks

**T0. Docs land in repo** *(this PR)* — PROJECT_BRIEF.md copied in,
INVENTORY.md + WEEK1_ORDER.md added.

**T1. Unified append-only event log.**
New table `events`: `id, user_id, ts, kind, payload(JSON-as-TEXT),
schema_version, source`. Append-only by convention: no UPDATE/DELETE
helpers exported. Dialect discipline per D1.3: INSERT/SELECT only,
payload serialized with json.dumps (no engine JSON functions, no
upserts). Emit from every existing pathway WITHOUT behavior change:
sms in/out (with prompt_version once T2 lands), cron tick/skip +
reason, phase transitions, GOAL/COMMIT marker parses, observe
start/capture/end (+ tier, + blob hash once T4 lands), signup
submissions, admin rescues, send failures.
*Accept:* one evening of normal use produces a human-readable
per-user timeline from `events` alone; existing behavior unchanged.

**T2. Prompt version registry.**
Table `prompt_versions`: `(hash, name, content, first_seen)`.
`_read_prompt()` content-hashes on read, registers unseen versions,
and the assembled-prompt hash rides along to the send path so every
outbound message event records `prompt_version`. Rendered-prompt
hash also logged (context differs per call; template hash is the
stable id).
*Accept:* given any outbound message event, the exact prompt template
text that produced it is retrievable by hash.

**T3. Decision hook with randomization structure.**
`decide(decision_point, user_id, options, weights) → (choice,
decision_id)` — logs an event with inputs, sampled choice, weights
(width 0 today), and returns `decision_id` for joining outcomes
later. Wire through: evening-slot fire/skip, morning fire/skip,
fresh-screen wait, phase-transition acceptance.
*Accept:* every intervention in the timeline carries a decision_id;
`[state + intervention + outcome]` triples are joinable.

**T4. Raw blob store** *(blocked on D2).*
`blob.py`: `put(bytes) → content_hash`, `get(hash)`. R2 (S3 API) in
prod, local dir in dev. /observe/capture stores the image FIRST,
then summarizes; observation rows + events reference the hash.
Weekly conversation snapshots optional (messages table already
retains raw text — lower priority).
*Accept:* a screenshot from any past capture is retrievable by hash;
deploys do not lose blobs.

**T5. LearnerState v1 + nightly annotation job.**
Schema v1 (deliberately small): `{phase, momentum, last_ignition_at,
friction_signals[], ego_friction_events[], channel_state,
outcome_v1_events[]}` — each snapshot tagged with schema_version +
prompt_version + model. Nightly job (cron slot or on-demand admin
endpoint) reads the day's events + raw, writes
`learner_state_snapshots` row per active user. Re-runnable over
historical days (re-annotation per brief §4.2).
*Accept:* after two nights, snapshots exist and cite event ids as
evidence; re-running a past day overwrites nothing (new
schema/prompt version → new row).

**T6. Infra events + capture-gap detection.**
Observer heartbeats (poll requests count as liveness). Server-side
sweep emits events for: open observe session with no capture >5 min,
expected cron slot that didn't fire (daily self-check), Twilio send
failures, WhatsApp-sandbox-expiry suspicion (send success but
delivery unknown → flagged). The brief's "pivotal natural
experiments" (outages) must self-record.
*Accept:* killing the observer mid-session produces a gap event
without human action.

**T6b. Owned backups (D1 condition 2).**
Nightly `pg_dump` of the Render Postgres → gzip → R2 (content-hash
named, 30-day rotation). Runs as a Render cron (same curl-image
pattern as the SMS slots, hitting an admin endpoint) or a small
scheduled job on the founder's machine — whichever is more boring
after checking Render's built-in backup status. Also: one-time
dashboard verification that the live DB instance is the paid
basic-256mb and note its backup policy in this file.
*Accept:* a dated dump exists in R2; restore drill documented (one
command) in a RUNBOOK section.

**T7. Legacy quarantine** *(blocked on D3).*
`git mv` the confirmed list into `/legacy/`; no import breakage
(none of it is imported by active code per INVENTORY §9); note in
legacy/README.md that it is frozen.

**T8. Learning-path schema (brief §7 "Learning path", §4.9).**
Table `learning_paths`: `(id, user_id, version, ts, direction,
project, project_done_condition, bites_done JSON, current_bite,
next_candidates JSON, changed_by, decision_id)`. Append-only
versions (a path change = new row). Migrate current two-layer state
(`agreed_goal` → direction, `agreed_first_bite` → current_bite) into
each user's path v1; existing columns stay (read paths win when
present). No prompt/UX changes this week.
*Accept:* founder's own path exists as v1; a manual path edit
produces v2 + an event; old code paths unaffected.

**T9. Phase-1 bite progression** *(week 1-2 boundary, small).*
`[COMMIT:]` in Phase 1 advances the ladder: current_bite → bites_done,
new bite becomes current (new path version + event) instead of being
ignored. Prompt nudge so the LLM proposes the next bite at natural
completion moments.
*Accept:* completing a bite in conversation yields a climbing ladder
without founder intervention.

**T10. user_id plumbing audit.**
Grep-level pass: every new helper takes user_id explicitly (no
thread-local reliance in pilot paths); TUTOR_USER_ID env remains the
single-user shim, isolated to entry points so multi-user routing
(Week 3) only touches those.

## Sequencing

T1 → T2 → T3 can land as three small PRs this week (no external
dependencies). T4 lands whenever D2 is decided (independent). T5
depends on T1 (+T4 for evidence links to blobs). T6 after T1.
T7/T10 anytime. T8 after T1+T3 (path versions cite decision ids);
T9 after T8, may slip into week 2.

## Explicitly NOT this week (brief §2 non-goals)

Operator dashboard (Week 3), multi-user WhatsApp routing (Week 3),
capture apps for pilot users (Week 2 — pending device mix from 1:1
recruiting conversations), policy generation (Week 3 design),
any ML/RL loop.
