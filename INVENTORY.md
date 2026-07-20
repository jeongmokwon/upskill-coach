# Repo Inventory — pre-Week-1 baseline (2026-07-20)

> Required by PROJECT_BRIEF.md §5: "First task of week 1 is a written
> inventory of this repo before any changes." Classifications below
> drive the Week-1 rewiring plan and the legacy quarantine list.
> Nothing has been moved or refactored yet.

## 1. Entry points & deployment

| Thing | What it is |
|---|---|
| `coach.py` | The server. aiohttp on one port: WebSocket web chat, HTTP routes (admin, SMS, observe, legal, signup). Runs on Render (`upskill-coach-dmmu.onrender.com`); local via `run.sh`/`run_web.sh` |
| `observer.py` | Local screen-capture agent (founder's laptop, stdlib-only, macOS). Not deployed; run from terminal |
| `render.yaml` | Render Blueprint for the web service only. Cron jobs are MANUAL dashboard entries (Blueprint cron+image runtime is broken — documented in the file) |
| `index.html` | Web app single page (chat + onboarding + animation panel) |

## 2. Datastores

`db.py` abstracts Postgres (Render, via `DATABASE_URL`) / SQLite (local
fallback). Same code, `_P` placeholder switching. Tables:

| Table | Purpose | Pilot-relevant? |
|---|---|---|
| `sessions` | Web chat sessions (start/end, topic). Walked by analyzer + orphan cleanup | web only |
| `user_state` | Last/current web session per user | web only |
| `interactions` | Web learning events (Q&A, practice, pause/resume) | web only |
| `messages` | ALL chat messages. `channel` ('web'/'sms') + `direction` ('in'/'out') columns. SMS thread uses synthetic session_id `sms-<user_id>` | **yes — core** |
| `insights` | Post-session LLM analyses (web analyzer output) | web only |
| `user_profiles` | Identity + goal/studying + **phase-flow columns**: `phase`, `phase_started_at`, `agreed_goal`, `agreed_first_bite`, `agreed_at` | **yes — core** |
| `observe_sessions` / `observations` | Screen-observation sessions + per-capture TEXT summaries. Deliberately isolated from `sessions` | **yes — core** |
| `sms_signups` | Web opt-in consent records (phone, consented_at, status='pending'). Not wired to messaging | **yes** |

Known hazard (fixed, stay vigilant): phase-state writers were silent
no-op UPDATEs when the profile row was missing; all writers now call
`ensure_user_profile_row()` first.

## 3. Messaging pipeline (SMS/WhatsApp)

- `sms.py`: Twilio send (`MESSAGING_CHANNEL` env toggles sms/whatsapp
  addressing), webhook signature verify, slot dispatch, phase logic.
- Slots (manual Render cron → `POST /sms/cron-tick?slot=X&secret=`):
  morning 7:00 PT (thread-keeping; skips if no history this phase),
  lunch/afternoon (disabled in code), evening 19:50 PT (anchor).
  Schedules pinned PDT; manual +1h UTC shift needed in November.
- Phase machine on `user_profiles`: `discovery` → (LLM emits
  `[COMMIT: "..."]` on user agreement; server parses, strips,
  transitions) → `first_bite`. `[GOAL: "..."]` marker persists the
  agreed goal in any phase. Conversation history fed to the LLM is
  scoped to `since=phase_started_at`, last `HISTORY_LIMIT=50`.
- Inbound (`POST /sms/inbound`): meta-commands skip/later, on-demand
  screen capture request (below), then reply via phase-aware prompt.
- Current channel: **WhatsApp Sandbox** (interim; 3-day rejoin).
  Toll-free SMS pending Twilio verification (see memory/PR history:
  first TFV rejected 30530 entity-type; business profile resubmitted,
  under review).

## 4. Observation pipeline

- `observer.py` (laptop): capture main display every 60s → downscale
  1568px → upload. sha256 skip when screen unchanged. Long-polls
  `GET /observe/poll` between timer ticks; inbound messages flip the
  poll to force an immediate capture (⚡, PNG, frontmost-window region
  when Accessibility granted; falls back to full screen; chat apps
  excluded from window capture).
- `observe.py` (server): vision summarization. Two tiers — ambient
  Haiku (1-3 sentence gist) / deep Sonnet on `forced=1` (verbatim code
  transcription, `[unreadable]` over guessing).
- **Images are processed in memory and DISCARDED** — only text
  summaries persist. ⚠️ Conflicts with brief §4.2 "Raw is sacred"
  (see WEEK1_ORDER.md decision D2).
- Prompt guardrails in `prompts/sms_shared.md`: screen context to
  help never police; chat history is never evidence of current screen.

## 5. Prompts

`prompts/` — re-read from disk on every LLM call (edit → push →
next message uses it; no restart):

- `sms_shared.md` — persona, hard rules 1-8 (incl. never-dismiss-
  confusion, one-cognitive-altitude/pacing with wrong-right pairs,
  anti-fabrication for screen), GOAL/COMMIT marker protocol, screen
  usage guidance, Korean-language directive
- `sms_discovery.md` — Phase 0 evening (goal + first-bite elicitation,
  3-day cap, goal-aware skip of re-elicitation)
- `sms_first_bite.md` — Phase 1 evening (body-first ask sizing,
  goal-chain anchoring for "what's next")
- `sms_morning.md` — thread-keeping only
- ⚠️ No versioning today: a message can't be traced to the prompt text
  that produced it (brief §4.3 violation → Week-1 T2).

## 6. Web app (active product, NOT pilot instrument)

WS chat with tutor, 3-question onboarding, Manim animation pipeline
(`animation_extractor/` — LLM generates Manim → headless extraction →
JSON timeline → browser SVG playback), quiz/apprentice handlers,
admin pages (`/admin*`, ADMIN_PASSWORD gated). Deployed and live.
**Not to be quarantined** — it's the running product and future
"visual explanation card" — but per brief §2 it gets no new work
during the pilot build.

## 7. Compliance surfaces

`/privacy`, `/terms` (inline HTML in coach.py, Green Gables Studio
LLC attribution), `/sms-signup` (carrier-checklist opt-in form →
`sms_signups`), topbar attribution + links (z-200 above screen
overlays).

## 8. Admin/rescue endpoints (shared-secret auth: X-Cron-Secret header or `?secret=`)

`/sms/cron-tick` · `/sms/reset-and-fire` · `/sms/set-goal` ·
`/sms/set-bite` · `/sms/status` · `/observe/start|capture|end|poll`

## 9. Legacy-quarantine candidates (brief §5 — CONFIRM before moving)

Propose moving to `/legacy` (no refactor, no delete), excluded from
imports:

| Candidate | Why |
|---|---|
| `kg_claude.py`, `kg_engine.py`, `ontology.json` | Knowledge-graph track; not wired into main flow; brief lists KG work as out of scope |
| `ontology_to_pdf.py`, `tutor_prompt_to_pdf.py`, `ontology_print.html`, `tutor_prompt_print.html` | One-off print/debug artifacts |
| `test_font_size_drift.py`, `test_generator.py`, `test_pipeline.py`, `test_repro_via_extract.py` | Ad-hoc debug scripts from the animation-font era (root-level, not a test suite) |
| `upskill_coach_data.json`, `render.yaml.bak`, `Aptfile` | Stale artifacts |

NOT legacy (stays): `animation_extractor/`, `quiz_assets/`, `media/`,
`fonts/` — used by the live web app.

## 10. Known gotchas (operational)

- Render filesystem is EPHEMERAL — anything not in Postgres dies on
  deploy. (Drives blob-store decision D2.)
- WhatsApp Sandbox 3-day rejoin; silent send failures when expired.
- Cron schedules are UTC, pinned PDT (November +1h manual shift).
- `screencapture` needs macOS Screen Recording permission (granted to
  the terminal app); window-region capture needs Accessibility.
- Env vars (Render): ANTHROPIC_API_KEY, DATABASE_URL, TWILIO_*,
  TUTOR_USER_ID/PHONE, CRON_SECRET, MESSAGING_CHANNEL, ADMIN_PASSWORD.
