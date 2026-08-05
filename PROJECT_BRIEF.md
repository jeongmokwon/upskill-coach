# PROJECT BRIEF — Theo (AI learning coach): Pilot Instrument

> Read this file fully before writing any code. This is the persistent context for all build work.
> This document defines WHAT we are building and WHY. WEEK1_ORDER.md defines the detailed week-1 tasks **and carries the running status of everything since** — the operator decided (2026-07-24) that no further WEEK2/3/4 order files will be written; weeks 2-4 remain as roadmap sketches in §6 and work is picked from them as needed.
>
> **Working with the operator:** Converse with the operator in Korean; keep code, comments, commit messages, and docs in English. When the spec is ambiguous or conflicts with what you find in the repo, ask the operator instead of assuming.
>
> **Naming (decided 2026-07-21):** the product AND the coach persona are both named **Theo** (formerly "Upskill Coach"). Domain: **learningtheo.com** (theo.com was unavailable — the domain carries the "learning" qualifier; the product name does not). Internal identifiers keep the old name on purpose (repo, Render service `upskill-coach-dmmu`, DB name, env vars like `UPSKILL_SECRET`): they are invisible to users and renaming them is pure migration risk. The onrender.com URL stays live alongside the custom domain.

## 1. What this product is

An AI coach that sits as a **layer on top of whatever the user is using to upskill** (a bootcamp, YouTube tutorials, DataCamp, their own toy project). It carries no pre-authored content library — existing world content (MOOCs, YouTube, textbooks) is one-size-fits-all, and the coach's job is to make learning fit ONE person: **it owns the learner's route** (§7 "Learning path") **and generates ephemeral, personalized micro-content at runtime** when the route needs it — a 3-line exercise typed into Colab, an in-chat drill, a gap-filling task (§7 policy prior, Layer 3). The fixed line: nothing is authored in advance, stored as inventory, or reused across users. Its job:

1. **Ignition** — get the user to actually start a session (the core validated wedge: users don't fail at learning, they fail at sitting down).
2. **Observation** — watch the user's screen longitudinally (frequent captures, high-res tracking of their main workspace) to build a model of how *this specific user* learns, stalls, and bails.
3. **Per-user intervention policy** — learn which intervention sequences move this user into flow, and which trigger churn.
4. **Route-keeping** — maintain each user's learning path (direction / project / bite, §7) so every session has a concrete next step and progress stays visible.

The competitor is user inertia (YouTube, Netflix, webtoons), not other edtech.

**Pilot objective, sharpened (2026-07-24).** The pilot is not "observe
whether ignition happens" — for busy adults the base rate may be near
zero if approached blindly. The objective is **matching**: extract
maximally-diagnostic data in the user's freshest window (the first
onboarding conversations), and use it to assign each user the
intervention approach most likely to reach ignition — then refine
per user from ongoing conversation. Two structural commitments follow:
- **A sequence is not an atomic object.** It is a trajectory of
  per-moment step choices (see §7 "Step vocabulary"): user A's winning
  path (elicit their why → tiny dictation-level bite → merge into main
  content) shares individual steps with user B's different path, and
  user A when tired may need user C's opening. The unit of learning is
  the step decision, not the whole sequence — which also means step
  samples pool ACROSS users, the only way n=5-10 × 4 weeks yields
  enough data.
- **Theory structures the hypothesis space; the field disposes.**
  Decades of behavioral science (SDT, self-efficacy, implementation
  intentions, motivational interviewing...) provide the candidate
  levers and the diagnostic first-questions; the founder's n=1
  discoveries are instances of known constructs (ego friction =
  self-efficacy threat; cognitive-altitude jumps = load violations),
  not new physics. Blind generalization of the n=1 sequence to all
  users is explicitly rejected.

Current instrumentation stage (shipped): step vocabulary self-tagging
+ per-user ignition markers (§7) — descriptive today. The
prescriptive layer (featurize user → per-user sequence plan → step
selection → generation, with replan triggers on signals like repeated
no-reply) is the week-3 "initial policy generation" slot, sharpened;
its design session is pending. Until then the coach runs the global
prompt and tags what it did.

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

**Week 2 — Capture client + pipeline hardening.** *(SUPERSEDED
2026-08-05 — the extension is retired for the pilot; capture is the
web session page. See §8.3.)* Primary client is a **Chrome extension** (one artifact covers macOS + Windows + ChromeOS; the recruiting pool skews Chromebook, and this population's learning is browser-centric — Colab, MOOCs, docs). Capture core: cadence, active-tab/workspace high-res handling, tab-switch events (`browser_tabs_captured`-class observations — avoidance signal), upload protocol, retry/offline buffering. Distribution: Chrome Web Store unlisted (no signing certs, no notarization). Always-visible capture indicator + one-click pause. Server ingest hardening. **Final build order is decided by the recruit device mix from 1:1 conversations** — native clients (macOS menubar via ScreenCaptureKit, Windows tray) are post-pilot or for IDE-centric users; the founder's own `observer.py` terminal agent continues for n=1.

**Week 3 — Multi-user + operator tooling.** SMS pipeline user routing (per-user prompts, per-user silence-rule state, per-user phase estimate; toll-free number per §3.1). Operator dashboard: per-user timeline (capture summaries + conversation + sent messages + feature trends) and per-user prompt editing. Target: founder reviews all users in <10 min/morning. One-page data collection/retention policy document.

**Week 4 — Rehearsal + recruitment readiness.** Install-flow rehearsal with 1–2 friends; first-24h pipeline survival test; sequential onboarding support (users onboard one at a time, never in a batch).

## 7. Domain concepts the code will reference

- **Phase (user):** `dormant` / `ignition` / `sustain`. Interventions valid in one phase are harmful in another. Hard rule #1: no concrete task mentions while user is dormant; motivation-first ("goal talk") conversation must precede tasks.
- **Channel state:** `fresh` / `saturating` / `saturated`. Accumulated unanswered messages poison the channel; silence resets it. Hard rule #2: after any silence/reset, the first contact is a mini-onboarding (zero-demand motivational conversation), never a task reminder.
- **Friction:** signals that the last learning step was too big (rewrite loops, regressing questions, tab-switching to YouTube, early session exit).
- **Ego friction:** distinct axis from cognitive friction. Coach utterances that bruise ("this is easy — need me to explain?") are a confirmed churn trigger. Tracked separately.
- **Ignition ritual:** near-zero-cognitive-load starter task (e.g., "type these 3 lines") that changes posture/body state and is physically continuous with the real work.
- **Ignition marker (per user, shipped 2026-07-23):** each user's OWN observable definition of "it started" — the founder's is "sat at the laptop, typed code into an IDE/Colab"; the next user's will differ. Elicited during discovery as its 4th deliverable ("뭐가 보이면 시작한 거야?" — pushed past feelings toward something a screenshot could verify), persisted via `[IGNITION_DEF:]` to `user_profiles.ignition_marker`, refinable anytime. Judged two-tier: a cheap real-time `[IGNITION: 1-5]` score on reply paths (never pinging into silence — silence may BE ignition), and the authoritative nightly annotation, which sees the screen record and the silences and treats live scores as claims to verify. This makes D4's generic outcome_v1 wording the *fallback*, not the primary criterion.
- **Step vocabulary (shipped 2026-07-23):** the finite lexicon of coaching moves — 17 tags in 6 families (접촉 connect/validate · 동기 elicit_why/identity_frame/spark_curiosity · 구조 map/secure_commit · 효능감, Bandura's four sources: evoke_mastery/vicarious_model/affirm_ability/reframe_state · 점화 micro_ask/choice_offer/implementation_cue/handoff · 페이싱 release/hold · drain none), each with intensity 1-3 anchored by per-level example utterances in the prompt. Ignition-only scope: learning-steering moves (teach/consolidate/step-up) are deliberately excluded — this experiment measures getting people TO the highway, not driving on it. Every outbound message self-tags `[STEP: tag@n, ...]` → `steps` in the sms_out event; unsent slots are server-tagged `hold`. **Descriptive today, prescriptive later:** currently the LLM reports what it did (known weakness: post-hoc self-labeling mislabels — observed `connect@1` on a goal-recall message); the planned flip is choose-the-step-then-write, at which point the same `{tag, intensity}` shape becomes the planning language and the label becomes a decision record. The vocabulary itself is pruned by data: never-used tags die, confused pairs merge, `none` share >30-40% signals a coverage hole.
- **Learning path (per user):** the loose curriculum. Three layers defined by *psychological function*, with durations that flex per user:
  - **Direction** (motivation source; months-to-years; e.g., "career change into ML")
  - **Project** (the progress-visibility unit: a concrete deliverable WITH a completion condition; horizon flexes — one week for some users, ~3 months for others; e.g., "MNIST classifier from scratch, ≥95% accuracy, by end of August")
  - **Bite** (the ignition unit; 5-30 min; body-first sized)

  Stored as a small versioned per-user artifact — a capped list (direction, project + done-condition, done bites, current bite, 1-2 next candidates), explicitly NOT a knowledge graph; expanding beyond this cap requires pilot evidence, not intuition. Every path change is an event with a decision id. n=1 failure this fixes: with only direction + bite persisted, the user had no mid-horizon navigation point — "am I on track?" was unanswerable and the LLM hallucinated the missing middle. Therefore Phase 0 (discovery) targets a **three-layer agreement** (direction + concrete project + first bite), and Phase 1 must support **bite progression** (bites complete and advance — the ladder climbs).
- **Policy prior:** the founder-owned, hand-evolved set of coaching principles common to ALL users. It is a first-class versioned artifact (a document/prompt the founder edits) and has an explicit **two-layer structure**: **Layer 1 — psychological principles** (descriptive models of how people work: "people are persuaded by what they say out loud, not what they're told"; "utterances that bruise competence trigger churn independently of cognitive load"; "a saturated channel processes any message as noise") and **Layer 2 — coaching rules derived from them** (prescriptive: "no concrete tasks while dormant"; "after reset, first contact is motivational conversation"; "when stuck, consider waiting for self-breakthrough before explaining"). Both layers go in the prior — Layer 1 lets the generator *adapt* rules to unusual users instead of applying them mechanically. Each item is tagged with a confidence level: `established` (literature-backed: motivational interviewing, reactance, implementation intentions) vs `hypothesis` (founder's n=1 observations, e.g., transcription-as-ignition-ritual) — hypotheses are pilot test targets, not settled rules. **Layer 3 — concrete interventions ("type these 3 lines into Colab") — is explicitly NOT part of the prior**: it is generated fresh at runtime by the per-user policy in response to current state, never stored as reusable recipes. What IS stored is every [state + intervention + outcome] triple in the event log (successes AND failures), joinable via decision_id.
- **User notes (designed 2026-07-24; the artifact formerly discussed as "user model"):** the per-user layer of the exploration design. NOT a neural model — a sparse, append-only list of falsifiable conditional statements about THIS user, each: `{claim (prose), given (conditions from a closed feature set: position opener/mid/close, last_steps, silence_streak_days, reply_latency, user_signal tired/engaged/resistant — all mechanically computable from the log, judgment rules explicit in code), when (a step or short chain from the step vocabulary), expect (no_reply | reply | advance | withdraw | ignition), evidence (episode refs: day + event ids), confidence (hypothesis | confirmed | retired), source (onboarding | nightly | operator), version}`. Design principle: **we cannot model the trajectory tree** (exponential, unknowable at pilot scale) — notes are sparse locally-valid rules, like a chess player's pattern knowledge, and the LLM interpolates the gaps. Notes are subjective-but-scored: the nightly job proposes/confirms/retires them against evidence; the founder approves (manual policy engine). They are DERIVED — raw traces stay pristine, and any future trained model learns from raw traces only, never from notes (the notes layer is scaffolding for the data-poor era and can be discarded/regenerated).
- **The prediction call (exploration architecture, designed 2026-07-24):** every intervention decision is one LLM call conditioned on three blocks — **A: policy prior** (general behavioral-science principles, the §7 prior artifact) + **B: user notes** (this user's compressed history-as-rules) + **C: recent raw trajectory** in step-language notation (+ current computed features). Output: next step(s) with intensity **chosen BEFORE the message is written** (choose-then-write — the [STEP:] tag becomes a decision record, not a post-hoc self-description) **plus a predicted user reaction** ([EXPECT:], scored against the actual reaction by the nightly job — every step is a falsifiable bet, and persistent prediction misses on a user flag their notes as wrong). Division of labor is fixed: deterministic code owns hard gates (cron/skip/STOP/phase), feature computation, exploration sampling (T3 decide, weights logged) and guardrails; the LLM owns judgment (step choice, realization, nightly annotation, initial notes from onboarding); the founder owns note approval and prior evolution. The LLM is a replaceable predictor behind a fixed interface: the end-state is an outcome-conditioned sequence model over step tokens (Decision-Transformer-style — the step vocabulary is its tokenizer, ignition labels its outcome conditioning, the event log its training corpus); *(v1 originally had no separate "sequence plan" object; REVERSED 2026-07-27 by weekend field data: shown a full chain, the planner collapsed it into one send — a question bubble immediately followed by the ask bubble, killing the self-persuasion lever. Structure now beats instruction:)* **v2 — the sequence lives server-side as state** (`sequence_plans` append-only versions + a cursor on the profile; every cursor move is an event). Each LLM call receives ONLY the current step as its assignment (next step by name only, "do not perform"), so collapsing a sequence is structurally impossible rather than merely forbidden. Advancement is a semantic judgment the LLM makes on replies via markers — `[ADVANCE]` (reply completed the step's purpose; code moves the cursor) / `[STAY]` / `[REPLAN: "reason"]` (plan misfits; recorded for operator/nightly re-planning; planner falls back to prior+notes). Plans are set at onboarding end (P7) or by the operator via `/plan`; per-moment freedom survives as the recorded-exception path, not unlimited improvisation.
- **Trace notation (step-language serialization):** any user-period renders from the event log as an alternating two-author token sequence — coach tokens (chosen steps `tag@intensity`, or `hold` for deliberate silence) and world tokens in brackets (user replies with mechanical metadata `+latency, N words`, silences `[no reply]` / `[silence 2d]`, screen-session runs). P1 renders facts (raw numbers); bucketing/interpretation lives in the feature computer. This view is simultaneously: the planner's block C, the founder's morning-review reading format, the nightly scorer's input, and the exact serialization a future sequence model trains on.
- **Markers vs. the analysis call (decided 2026-07-30, after the third instance of the same failure).** Three times the generation call silently dropped a side duty — ignition scoring, plan `[ADVANCE]`, then the whole marker set collapsing mid-conversation (self-imitation: history is stored marker-stripped, so the model copies its own marker-free turns). The rule that follows: **a model that is talking will not reliably also do bookkeeping.** Markers therefore split by nature:
  - **Decision markers stay on the generation call** — `[STEP: tag@n]` (which move was chosen), `[EXPECT: ...]` (predicted reaction), `[REPLAN: "..."]`. These exist only in the model's head; nothing can extract them from the transcript afterward.
  - **Extraction moves to a dedicated ANALYSIS call** — goal, path, first bite, ignition marker, schedule, offer. These are facts already present in the conversation, so a single-task call reading the transcript recovers them far more reliably. Consequences that matter: it sees the WHOLE conversation (a fact stated three turns ago is still catchable — a missed turn is no longer permanent data loss), and it is **re-runnable over history**, so past conversations can be back-extracted.
  - Shape: one analysis call per inbound reply (it replaces/absorbs the step-completion judge), forced tool call, sections active by phase — field extraction while onboarding, step-completion judgment after. It runs BEFORE generation, so the generation call receives already-updated state and only has to talk well. Extraction never speculates: a field fills only when the user actually said or agreed to it; the server validates format and every write is an event.
- **Onboarding arc** *(SUPERSEDED 2026-08-05 — see §8.2 for the
  current arc: bite removed, material walkthrough added, completion
  redefined.)* **(original 2026-07-30 design:)** The checklist is now an ORDERED focus — one field per turn, in arc order: field/goal → goal elaboration → **what Theo will do for them (`agreed_offer`, confirmed by the user)** → messaging windows → ignition marker → first concrete task. The offer step was missing entirely: the user talked about themselves for ten turns and got nothing back, which is both a reciprocity failure and an expectations gap; it is also the natural place for a commitment (`secure_commit`). Its content is dictated by the user's **learning type** (below).
- **Question burden (pilot user #1 dropped out on exactly this).** Two rules, one mechanical: **(a) one question per message — server-enforced** (question-mark count across bubbles; one regeneration attempt, then send anyway and log the violation, because silence is worse than a flawed send). **(b) Answers must fit in one short sentence**: prefer guess-and-confirm ("워드나 노션 같은 데 정리해둔 거야?") over open elicitation, convert to explicit choices, and keep open questions narrow. This is in tension with the self-persuasion principle (which needs the user's own words) and the resolution is *narrow* open questions, not none — the user answered five short questions in six minutes and only quit when a message asked two open-ended things at once.
- **Learning types (per user, multi-label; drives offer + step selection).** Memorization/retention · **retrieval fluency** (instant recall under questioning — distinct: the target is speed and availability, not mere knowing) · skill practice & mastery · conceptual understanding · project completion · exam prep · language acquisition · habit formation · exploration (deciding whether to pursue). Pilot user #1 = memorization + retrieval fluency over self-authored notes, which prescribes active recall, spaced repetition and question-form conversion — and that prescription IS his offer. **Consequence for §7's learning path:** the `direction / project+done-condition / bite` middle layer is project-shaped and does not fit every type; `learning_paths.path_kind` records which framing applies (deliverable+done-condition / coverage target / duration of practice).
- **User profile brief (generated at onboarding completion, versioned, append-only).** Alongside notes and the initial plan, the generation call emits a brief: job/field, learning types, learning materials, **what they want from Theo — as verbatim quotes, never paraphrase** (raw is sacred applies to self-description), and a free-form personality read. Structured where a machine branches on it (learning types), free-form where only the LLM consumes it (personality).
- **Availability grid (evolving, derived).** A per-user day×hour grid seeded by self-report during onboarding and refined nightly by behavior — reply latency and hour-of-day are already in the event log, so "says 8pm, actually only answers at 10pm" surfaces on its own. Stored as versioned snapshots derived from raw events (same discipline as notes: the events are the truth, the grid is a rebuildable projection). Feeds per-user scheduling.
- **Initial policy generation** (design in week 3; schema-accommodated in week 1): the transform `(policy_prior, onboarding_data) → initial_per_user_policy`, performed by an LLM when a user completes onboarding. NOT blank-slate: the LLM instantiates the founder's prior against this specific user's goal / why / schedule / learning style / shape-of-inertia gathered during onboarding. The LLM instantiates and adapts the prior; it never invents new coaching principles. **Output is always `policy + rationale`, never policy alone.** The rationale records: (a) prior version used, (b) parameters extracted from onboarding, (c) for each major policy setting, which principle × which user parameter produced it. The rationale is what lets the operator distinguish "wrong principle" from "wrong reading of the user" when a policy underperforms, and accumulated rationales are the evidence base for revising the prior itself. This is the automatic bootstrap that makes onboarding user #2 through #10 possible without real-time founder involvement; ongoing adjustment thereafter is manual and asynchronous.


## 8. Live-state addendum — 2026-08-05

*Everything below is SHIPPED and live-verified unless marked open.
Where this section conflicts with §3/§6/§7, this section wins. TFV
approved 2026-08-05; the channel is SMS (toll-free +18555028436);
the WhatsApp sandbox era is over.*

### 8.1 Materials & the backbone ("text over pixels")

`user_materials`: one row per thing the user studies from — `file`
(upload on /my; **extracted text kept, original bytes discarded** —
the pilot's first file is a law-firm document), `link` (famous
resources are covered by latent knowledge; no fetch), `named` (only
spoken of). Upload triggers a one-time LLM read producing the
coach's working notes (structure, item counts, dense parts —
explicitly not study advice). Governing rule everywhere: **the
user's own walkthrough account outranks the digest**, and the digest
outranks raw pixels. The digest completion fires an immediate coach
follow-up (`material_ready`) when the upload is the awaited
onboarding step — the freshest window there is.

### 8.2 The walkthrough — onboarding completion redefined

Completion no longer means "fields collected"; it means **Theo
understands its job for this user well enough that the user
confirmed a sample of it.** `ONBOARDING_FIELDS = (goal, path,
ignition_marker, material_walkthrough, offer, schedule)` — order IS
the arc; `bite` is gone (the first task belongs to the first
session, after a plan exists). The walkthrough: Theo-led, anchored
to the material (on screen when a session is live), elicits via
EXAMPLES not abstractions, pins its questions to exact
sections/pages, one question a turn. Exit is licensed ONLY by the
user affirming a demonstrated sample ("맞아, 딱 그런 거") — the
model's own sense of readiness licenses nothing (it always feels
able), and the mechanical gate agrees: analyze flips
`walkthrough_status=validated` only on transcript evidence of
sample+affirmation, and **every want quote is verified verbatim
against the user's actual turns** (coach lines and inventions are
dropped and evented — the attribution guard, added after live
mis-attribution). The coach then DECLARES the close ("다 파악했어,
여기까지 하자") rather than letting the walkthrough trail. The
offer is built FROM the walkthrough and rendered on every
subsequent send as **YOUR STANDING PROMISE** — a debt actively
paid, never re-offered as new, never quietly dropped.

### 8.3 Screen co-viewing sessions (the pilot's whole purpose)

The extension is retired for the pilot; capture is the **web
session page** on /my (magic-link token = login; no accounts).
Perception is three-layered and was spike-validated before build:
**reading** (frame + backbone → located content: "3.2 정산 기준 표"
— alignment over OCR), **flow** (journey log → activity judgments:
"영상 보며 노트테이킹 중", including document-growth detection),
**change** (client-side 500ms diff loop, zero API cost, emits
context_switch / scroll_settle / dwell / chat — event-driven
attention, not clock sampling). **Frames are ephemeral**: read in
memory, never written to disk, discarded after the observation
text is stored; the flight record keeps context but not the image.
Honesty floor: '자료 밖' over forced alignment; unread over
guessed; confidence caps how strongly the coach may speak.

In-session conversation is **web chat on the same page** (the
getDisplayMedia stream dies on navigation, so it must be): each
turn attaches the CURRENT frame (~1s freshness — the raw frame
rides inside the reply call itself); replies stream over SSE with a
160-char holdback so trailing markers never reach the user; guards
are log-only on streamed turns (a read reply cannot be retried).
One thread, one memory: web turns store into the same messages
table (`channel='web'`) and both channels read both. Lifecycle is
server-templated in the understated register ("세션 시작했네. 보고
있을게." / "오늘 세션은 여기까지 기록해뒀어") — enthusiasm about
watching reads as surveillance wearing a smile; a dead session
(missed heartbeats, >60s) speaks nothing. Consent is layered:
Privacy Policy disclosure + versioned /screen-consent document +
just-in-time acceptance panel recorded per (user, doc, version) in
`user_consents`, with a 428 server gate.

### 8.4 Multi-user (M1-M4, shipped 2026-08-05)

Identity lives in the DB: `user_profiles.phone` (E.164; rebinding
another user's number is refused — a silent rebind reroutes a
whole conversation) + `status` active/paused. Routing is DB-first
with the TUTOR_* env pair as fallback. Both cron entry points fan
out over the roster with **per-user crash isolation** (one user's
failure becomes `cron_user_failed`; the loop continues — the
alternative is invisible whole-roster silence). Signup activation
is one operator click: profile + phone + real name from the form +
magic token + welcome email (the /my link; Resend, with a
User-Agent header because Cloudflare bans urllib's default) →
the next evening slot opens onboarding. SMS consent is structural:
a signup without the checked box cannot be activated for texting.
Operator tools: `nudge` (evening's twin, time-agnostic, honestly
labeled `cron_nudge`, per-user only — manual sends must not
corrupt slot-conditioned data), `reset-user` (back to birth
keeping phone/email/token; consent wiped so JIT runs again),
`bind-phone`, `/debug/signups`. Inbound bursts fold: per-user lock
+ dual freshness check → every burst gets exactly one reply,
written with the full burst in view.

### 8.5 Open — deliberately undesigned or deferred

- **Post-onboarding offer delivery machinery**: none yet, on
  purpose — observe the husband's first delivery before building
  (the question-bank idea was rejected as user-overfit; the generic
  layer, if needed, is material-content access at generation time).
- **KNOWN INCONSISTENCY (fix queued)**: the capability stock in
  sms_shared still says the coach cannot see the screen; sessions
  shipped. Update the stock + the offer surface to include live
  co-viewing.
- Quiet-prompt trigger during walkthrough sessions (speak-first on
  screen activity): policy undecided, v1.1.
- Name extraction in analyze (users who skip the form name).
- Phone-native capture (ReplayKit; founder's iOS background),
  extension + tab telemetry, RCS/iMessage typing indicators: all
  post-pilot.
