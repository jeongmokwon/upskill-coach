# PROJECT BRIEF — AI Upskill Coach: Pilot Instrument

> Read this file fully before writing any code. This is the persistent context for all build work.
> This document defines WHAT we are building and WHY. Weekly order files (WEEK1_ORDER.md, etc.) define the current tasks.
>
> **Working with the operator:** Converse with the operator in Korean; keep code, comments, commit messages, and docs in English. When the spec is ambiguous or conflicts with what you find in the repo, ask the operator instead of assuming.

## 1. What this product is

An AI coach that sits as a **layer on top of whatever the user is using to upskill** (a bootcamp, YouTube tutorials, DataCamp, their own toy project). It carries no pre-authored content library — existing world content (MOOCs, YouTube, textbooks) is one-size-fits-all, and the coach's job is to make learning fit ONE person: **it owns the learner's route** (§7 "Learning path") **and generates ephemeral, personalized micro-content at runtime** when the route needs it — a 3-line exercise typed into Colab, an in-chat drill, a gap-filling task (§7 policy prior, Layer 3). The fixed line: nothing is authored in advance, stored as inventory, or reused across users. Its job:

1. **Ignition** — get the user to actually start a session (the core validated wedge: users don't fail at learning, they fail at sitting down).
2. **Observation** — watch the user's screen longitudinally (frequent captures, high-res tracking of their main workspace) to build a model of how *this specific user* learns, stalls, and bails.
3. **Per-user intervention policy** — learn which intervention sequences move this user into flow, and which trigger churn.
4. **Route-keeping** — maintain each user's learning path (direction / project / bite, §7) so every session has a concrete next step and progress stays visible.

The competitor is user inertia (YouTube, Netflix, webtoons), not other edtech.

**Vision vs. current wedge:** the product's end state is an AI tutor
that multiplies learning efficiency. Fast/frequent/deep flow entry
(ignition) is the current wedge because it gates everything
(efficiency = frequency × duration × quality; no sitting down = ×0),
not because ignition is the product. Pilot success criteria are
ignition-scoped; product success criteria are not. Do not let
ignition metrics Goodhart the mission (e.g., permanently-trivial
bites ignite reliably and teach nothing — the bite ladder must climb).

## 2. What we are building RIGHT NOW (and what we are not)

We are building a **research instrument (pilot equipment)**, not a product. The founder has been running a manual n=1 experiment on herself (WhatsApp coach messages + screen capture). We are now hardening that setup so 5–10 external users can run it, while the founder operates the "coach brain" manually.

**Explicit NON-GOALS for this phase — do not build these, do not scaffold for them:**
- Chat UI / onboarding screens / mobile app (onboarding happens over SMS — see §3.1 for why not WhatsApp)
- **Automated policy *learning*** — no ML/RL loop that updates policy from outcomes. Not enough data at n=5–10, and the founder must understand hand-tuning before automating it.
- Content infrastructure: no content library, no exercise banks, no new animation work. Two things this non-goal does NOT cover: the per-user **learning path** (§7 — route state, in scope) and the **runtime micro-content the coach already generates in conversation** (§7 Layer 3 — e.g., "type these 3 lines"; continues as-is, no new build needed).
- App Store / Mac App Store distribution (direct download only)
- Payments, auth flows, marketing site

**IN scope but detailed later (do NOT confuse with the non-goal above):**
- **Initial policy *generation*** (week 3): when a user finishes onboarding, their first coach configuration is generated automatically by an LLM — the founder cannot and should not hand-write it in real time (users onboard at all hours; several may onboard the same evening). This is a structured transform (onboarding conversation → initial per-user policy), NOT a learning loop. See §7 "Policy prior & initial policy generation." The *design* of this is a week-3 task to work out with the operator; week-1 schema must not preclude it (see §4.9).
- **Ongoing policy *adjustment*** by the founder (week 3+): asynchronous, not real-time. The founder reviews each user's timeline (~10 min/morning) and hand-edits per-user prompts. This is the manual "policy engine" — deliberate at this stage, so the founder learns what to change and why before any of it is automated.

If a task seems to require one of these, stop and flag it instead of building it.

## 3. Architecture (target state, end of week 4)

```
[User desktop]                     [Server]                        [Operator = founder]
 capture client            ──►  ingest API ──► event store  ──►  operator dashboard
 (Chrome extension primary;           │        (append-only,         (timeline per user,
  native clients post-pilot)          ▼         Postgres)             per-user prompt editing)
[User phone + desktop]          featurization       │
 SMS (toll-free via Twilio) ◄──► job (LLM →         ▼
 coach conversation             LearnerState)   raw blob store
 (desktop surface: §3.2)                        (Cloudflare R2:
                                                 screenshots, dumps)
```

### 3.1 Channel decision: SMS, not WhatsApp (decided 2026-07, do not revert casually)

The conversation channel is **SMS via a Twilio toll-free number**.
WhatsApp was tried first (sandbox) and rejected for production on
structural — not preference — grounds:

1. **WhatsApp's 24-hour customer-service window forbids
   business-initiated free-form messages to anyone silent >24h.**
   The coach's single highest-value intervention is exactly that
   message: re-engaging a dormant user with a personalized,
   free-form motivational conversation (Hard rule #2's
   mini-onboarding). Outside 24h WhatsApp allows only pre-approved
   templates — which is precisely the "task reminder" shape that
   rule #2 bans. The channel forbids the product's core move.
2. Meta business verification is a 1-2 week external dependency,
   and the founder's Meta business account carries an unexplained
   ads restriction (creating a fresh portfolio to bypass it risks
   circumvention flags).
3. Sandbox requires participants to re-join every 3 days — unusable
   onboarding for external users.

WhatsApp sandbox remains acceptable only as the founder's own
interim n=1 channel until toll-free verification clears. The code
keeps the `MESSAGING_CHANNEL` env toggle; the pilot ships on SMS.
Consequence for recruiting: **pilot users need US phone numbers**
(toll-free SMS is US/Canada domestic).

SMS compliance & trust (non-negotiable, carrier-facing AND
user-facing):
- **Opt-in consent is collected at onboarding** before any message
  is sent — the web consent form (`/sms-signup`: checkbox not
  pre-selected, frequency + data-rates disclosure, ToS/Privacy
  links) is both the carrier-verification proof and the real
  consent record (timestamped rows in `sms_signups`).
- **STOP opt-out is honored immediately** (Twilio-level block +
  our skip handling); HELP returns identification + support info.
  Every disclosure the user saw promises this — it must stay true.
- **Onboarding includes a "save the coach's number as a contact"
  step.** Dual purpose: deliverability/trust (an unsaved toll-free
  number reads as spam; saved "Coach" reads as a relationship — the
  psychological framing matters as much as the filtering), and it
  gives the evening ping a name instead of a number.

### 3.2 Desktop conversation surface (load-bearing requirement)

Users must be able to converse with the coach FROM THE LAPTOP during
study sessions — typing code fragments on a phone is a validated
churn-level friction (founder n=1). Strategy is per device-combo,
using surfaces that already exist:

| Combo | Desktop SMS surface |
|---|---|
| Mac + iPhone | Messages app via Text Message Forwarding (one-time setup; documented step in onboarding) |
| Any laptop + Android | Google Messages for Web (QR pairing; requires Google Messages as default SMS app — recruiting screener question) |
| Chromebook + iPhone | **Gap.** Phone-only conversation; handled per the policy below |

**Chromebook+iPhone policy (count it, don't hand-wave it):**
- The recruiting screener/1:1 explicitly records each applicant's
  laptop OS + phone OS, so this combo is COUNTED, not discovered
  after onboarding.
- First cohort: deprioritize this combo when equivalent candidates
  exist. If accepted anyway, the user is labeled
  **`degraded-condition`** in their profile/events — their churn and
  engagement data are analyzed separately, never pooled with
  full-condition users (otherwise the combo's friction reads as a
  coaching-policy failure).
- **Web-chat contingency trigger:** if this combo reaches a
  meaningful share of applicants (2-3+ people), that is the
  pre-agreed activation condition for building the web chat
  surface — not before. Until triggered, chat UI stays a §2
  non-goal.

## 4. Non-negotiable engineering principles

1. **Log everything, append-only.** Every event in the system (message sent/received, screenshot captured, prompt version changed, infra failure, capture gap) goes into one per-user timeline. The founder's two best discoveries so far survived only by luck and memory (a Twilio outage and a prompt change turned out to be the pivotal natural experiments). At 10 users, memory does not scale. **Nothing that happens in the system may be unrecorded.**
2. **Raw is sacred.** Store raw screenshots and full conversation text permanently. Feature schemas WILL be wrong and WILL be re-run over historical raw data (re-annotation). Never store only derived features.
3. **Schema-versioned everything.** Every derived artifact (feature snapshot, LLM annotation) records: schema version, prompt version, model name. Every outbound coach message records which prompt version produced it.
4. **Randomization hooks from day one.** Every intervention decision point must pass through a policy function that can apply probabilistic variation and logs what was sampled and why. Variation width may be 0 for now; the *structure and logging* must exist now, because causal readability cannot be retrofitted.
5. **The coach brain stays manual.** Per-user system prompts are files/records the founder edits. The system routes and logs; it does not decide.
6. **user_id everywhere.** Current n=1 (the founder) but every table, path, and function takes user_id from day one.
7. **Boring tech.** Python. **Postgres in production (Render, paid basic-256mb) / SQLite locally, behind the single existing `db.py` abstraction** — new code writes dialect-neutral SQL only (INSERT/SELECT, JSON-as-TEXT; see WEEK1_ORDER D1). Render's disk is EPHEMERAL: nothing durable ever goes on the server filesystem. **Raw blobs live in Cloudflare R2** (S3-compatible, content-hash names; local dir only as dev fallback). No microservices, no queues unless something measurably breaks. *(Amended 2026-07-20: the original "SQLite + local filesystem" spec predated knowledge of the deployed infra — see WEEK1_ORDER D1/D2 for the decision record.)*
8. **Founder's stack context:** primary language Python (founder reads Python but is not deeply fluent — write clear, well-commented, boring Python; no clever metaprogramming). Founder has Swift/iOS background — relevant for eventual native capture clients (post-pilot), not for the pilot's Chrome extension.
9. **Week-1 schema must not preclude the learning path (§7).** The path is a first-class, version-tracked per-user artifact (like the policy); path changes land in the event log with decision ids. Week 1 builds the schema + migrates the existing two-layer state (agreed_goal, agreed_first_bite) into path v1; path UX and prompt work follow in weeks 2-3.
10. **Week-1 schema must not preclude initial policy generation (§7).** Concretely: onboarding conversations are stored as structured, retrievable raw (not just free text lost in the message log); a generated per-user policy is a first-class, version-tracked artifact in the prompt registry, tagged with the policy-prior version and the onboarding data it was generated from. We are not building the generator in week 1 — we are making sure week-3 can build it without retrofitting the event/prompt schema.

## 5. Existing assets (inventory before touching anything)

Already built, in varying states:
- Twilio scheduled messaging (morning/evening sends; WhatsApp sandbox interim, SMS toll-free pending verification, `MESSAGING_CHANNEL` toggle in code)
- A `*.py` script that screen-captures every 60s when run from terminal
- Screenshot-on-message: an on-demand screen capture fires whenever the founder messages the coach (channel-agnostic; inbound-triggered)
- The coach LLM prompt(s) used for coach conversations (channel-agnostic; `prompts/sms_*.md`)
- **Legacy experiment code from earlier iterations — this is known debt. QUARANTINE it (move to `/legacy`, exclude from imports), do not refactor it, do not delete it.**

First task of week 1 is a written inventory of this repo before any changes.

## 6. Four-week roadmap

**Week 1 — Data foundation (the non-retrofittable layer).** Unified append-only event store + per-user timeline; raw blob store; prompt version registry; LearnerState feature schema v1 + LLM annotation job; randomization/decision hook with logging; infra-event and capture-gap detection. Existing components rewired to emit events. *(Detailed in WEEK1_ORDER.md.)*

**Week 2 — Capture client + pipeline hardening.** Primary client is a **Chrome extension** (one artifact covers macOS + Windows + ChromeOS; the recruiting pool skews Chromebook, and this population's learning is browser-centric — Colab, MOOCs, docs). Capture core: cadence, active-tab/workspace high-res handling, tab-switch events (`browser_tabs_captured`-class observations — avoidance signal), upload protocol, retry/offline buffering. Distribution: Chrome Web Store unlisted (no signing certs, no notarization). Always-visible capture indicator + one-click pause. Server ingest hardening. **Final build order is decided by the recruit device mix from 1:1 conversations** — native clients (macOS menubar via ScreenCaptureKit, Windows tray) are post-pilot or for IDE-centric users; the founder's own `observer.py` terminal agent continues for n=1.

**Week 3 — Multi-user + operator tooling.** SMS pipeline user routing (per-user prompts, per-user silence-rule state, per-user phase estimate; toll-free number per §3.1). Operator dashboard: per-user timeline (capture summaries + conversation + sent messages + feature trends) and per-user prompt editing. Target: founder reviews all users in <10 min/morning. One-page data collection/retention policy document.

**Week 4 — Rehearsal + recruitment readiness.** Install-flow rehearsal with 1–2 friends; first-24h pipeline survival test; sequential onboarding support (users onboard one at a time, never in a batch).

## 7. Domain concepts the code will reference

- **Phase (user):** `dormant` / `ignition` / `sustain`. Interventions valid in one phase are harmful in another. Hard rule #1: no concrete task mentions while user is dormant; motivation-first ("goal talk") conversation must precede tasks.
- **Channel state:** `fresh` / `saturating` / `saturated`. Accumulated unanswered messages poison the channel; silence resets it. Hard rule #2: after any silence/reset, the first contact is a mini-onboarding (zero-demand motivational conversation), never a task reminder.
- **Friction:** signals that the last learning step was too big (rewrite loops, regressing questions, tab-switching to YouTube, early session exit).
- **Ego friction:** distinct axis from cognitive friction. Coach utterances that bruise ("this is easy — need me to explain?") are a confirmed churn trigger. Tracked separately.
- **Ignition ritual:** near-zero-cognitive-load starter task (e.g., "type these 3 lines") that changes posture/body state and is physically continuous with the real work.
- **Learning path (per user):** the loose curriculum. Three layers defined by *psychological function*, with durations that flex per user:
  - **Direction** (motivation source; months-to-years; e.g., "career change into ML")
  - **Project** (the progress-visibility unit: a concrete deliverable WITH a completion condition; horizon flexes — one week for some users, ~3 months for others; e.g., "MNIST classifier from scratch, ≥95% accuracy, by end of August")
  - **Bite** (the ignition unit; 5-30 min; body-first sized)

  Stored as a small versioned per-user artifact — a capped list (direction, project + done-condition, done bites, current bite, 1-2 next candidates), explicitly NOT a knowledge graph; expanding beyond this cap requires pilot evidence, not intuition. Every path change is an event with a decision id. n=1 failure this fixes: with only direction + bite persisted, the user had no mid-horizon navigation point — "am I on track?" was unanswerable and the LLM hallucinated the missing middle. Therefore Phase 0 (discovery) targets a **three-layer agreement** (direction + concrete project + first bite), and Phase 1 must support **bite progression** (bites complete and advance — the ladder climbs).
- **Policy prior:** the founder-owned, hand-evolved set of coaching principles common to ALL users. It is a first-class versioned artifact (a document/prompt the founder edits) and has an explicit **two-layer structure**: **Layer 1 — psychological principles** (descriptive models of how people work: "people are persuaded by what they say out loud, not what they're told"; "utterances that bruise competence trigger churn independently of cognitive load"; "a saturated channel processes any message as noise") and **Layer 2 — coaching rules derived from them** (prescriptive: "no concrete tasks while dormant"; "after reset, first contact is motivational conversation"; "when stuck, consider waiting for self-breakthrough before explaining"). Both layers go in the prior — Layer 1 lets the generator *adapt* rules to unusual users instead of applying them mechanically. Each item is tagged with a confidence level: `established` (literature-backed: motivational interviewing, reactance, implementation intentions) vs `hypothesis` (founder's n=1 observations, e.g., transcription-as-ignition-ritual) — hypotheses are pilot test targets, not settled rules. **Layer 3 — concrete interventions ("type these 3 lines into Colab") — is explicitly NOT part of the prior**: it is generated fresh at runtime by the per-user policy in response to current state, never stored as reusable recipes. What IS stored is every [state + intervention + outcome] triple in the event log (successes AND failures), joinable via decision_id.
- **Initial policy generation** (design in week 3; schema-accommodated in week 1): the transform `(policy_prior, onboarding_data) → initial_per_user_policy`, performed by an LLM when a user completes onboarding. NOT blank-slate: the LLM instantiates the founder's prior against this specific user's goal / why / schedule / learning style / shape-of-inertia gathered during onboarding. The LLM instantiates and adapts the prior; it never invents new coaching principles. **Output is always `policy + rationale`, never policy alone.** The rationale records: (a) prior version used, (b) parameters extracted from onboarding, (c) for each major policy setting, which principle × which user parameter produced it. The rationale is what lets the operator distinguish "wrong principle" from "wrong reading of the user" when a policy underperforms, and accumulated rationales are the evidence base for revising the prior itself. This is the automatic bootstrap that makes onboarding user #2 through #10 possible without real-time founder involvement; ongoing adjustment thereafter is manual and asynchronous.
