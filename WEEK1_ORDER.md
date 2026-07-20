# WEEK1_ORDER — Data foundation (the non-retrofittable layer)

> Companion to PROJECT_BRIEF.md §6 Week 1. Read INVENTORY.md first.
> Tasks are ordered; each has acceptance criteria. Decisions D1-D4
> need the operator's call BEFORE the task that depends on them.

## Open decisions (operator input needed)

**D1 — Event store engine: keep the existing Postgres/SQLite duality.**
Brief §4.7 says "SQLite for the event store", but production already
runs Postgres on Render (Render disk is ephemeral, so server-side
SQLite would be wiped on every deploy — a non-starter). Proposal:
events live in the same dual-backend `db.py` layer as everything else
(Postgres in prod, SQLite locally). This honors the brief's intent
(boring, single-writer) with the engine that already exists.
→ **Default unless objected: keep duality.**

**D2 — Raw blob store: Render disk cannot hold "raw is sacred".**
Screenshots are currently discarded after summarization; the brief
demands permanent raw retention + re-annotation capability. Local
filesystem (brief §4.7) does not survive Render deploys. Proposal:
S3-compatible object store — Cloudflare R2 (free tier: 10GB, no
egress fees) — content-hash keys, written by /observe/capture before
summarization. Local-dir fallback for dev. Requires one new account +
2 env vars.
→ **Needs operator: approve R2 (or pick Backblaze B2 / AWS S3).**

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
New table `events`: `id, user_id, ts, kind, payload(JSON),
schema_version, source`. Append-only by convention: no UPDATE/DELETE
helpers exported. Emit from every existing pathway WITHOUT behavior
change: sms in/out (with prompt_version once T2 lands), cron
tick/skip + reason, phase transitions, GOAL/COMMIT marker parses,
observe start/capture/end (+ tier, + blob hash once T4 lands),
signup submissions, admin rescues, send failures.
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

**T7. Legacy quarantine** *(blocked on D3).*
`git mv` the confirmed list into `/legacy/`; no import breakage
(none of it is imported by active code per INVENTORY §9); note in
legacy/README.md that it is frozen.

**T8. user_id plumbing audit.**
Grep-level pass: every new helper takes user_id explicitly (no
thread-local reliance in pilot paths); TUTOR_USER_ID env remains the
single-user shim, isolated to entry points so multi-user routing
(Week 3) only touches those.

## Sequencing

T1 → T2 → T3 can land as three small PRs this week (no external
dependencies). T4 lands whenever D2 is decided (independent). T5
depends on T1 (+T4 for evidence links to blobs). T6 after T1.
T7/T8 anytime.

## Explicitly NOT this week (brief §2 non-goals)

Operator dashboard (Week 3), multi-user WhatsApp routing (Week 3),
capture apps for pilot users (Week 2 — pending device mix from 1:1
recruiting conversations), policy generation (Week 3 design),
any ML/RL loop.
