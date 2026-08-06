"""
Onboarding state-machine tests (P0-A).

Run: ./venv/bin/python test_onboarding.py  (sqlite; anthropic mocked)

The claim under test: completion is a DERIVED predicate over five
stored fields — the LLM fills fields via markers, the server flips
started/completed timestamps and the phase; the checklist block
steers every onboarding call toward the missing fields.
"""

import json
import os
import tempfile
from datetime import datetime

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "hub"
os.environ["TUTOR_USER_PHONE"] = "+15550002222"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_onboarding.db")
db.init_db()

import sms  # noqa: E402

U = "hub"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


# ── 1. fresh user: empty checklist, discovery, checklist block ───────
print("1) fresh user")
db.ensure_user_profile_row(U)
s = db.get_onboarding_state(U)
check("all five fields missing", s["missing"] == list(db.ONBOARDING_FIELDS)
      and s["started_at"] is None and s["completed_at"] is None)
check("the first concrete task is NOT an onboarding field — it "
      "belongs to the first session, after a plan exists",
      "bite" not in db.ONBOARDING_FIELDS and "offer" in db.ONBOARDING_FIELDS)

prompt, _ = sms._build_system_prompt("evening", U)
check("checklist block injected, focused on the first missing field",
      "Onboarding — what is still unsettled" in prompt
      and "This message's focus: their goal" in prompt
      and "Sequence assignment" not in prompt)

# ── 2. markers fill fields one by one ────────────────────────────────
print("2) field fills")
db.set_agreed_bite(U, "read 3.1 and note confusions", source="analyze")
check("early bite saves WITHOUT phase flip",
      db.get_user_phase(U)["phase"] == "discovery"
      and db.get_user_phase(U)["agreed_first_bite"] != ""
      and len(events_of("bite_committed")) == 1)
check("and it moves no onboarding field either way",
      "bite" not in db.get_onboarding_state(U)["filled"]
      and "bite" not in db.get_onboarding_state(U)["missing"])

db.save_learning_path(U, "career change into ML", "tiny classifier",
                      "trained & evaluated", source="analyze")
check("path saved", db.get_current_path(U)["direction"]
      == "career change into ML")

check("schedule windows parse (shared with the analysis call)",
      sms.parse_schedule_windows("20:00-22:00, 08:00-08:30")[0]
      == {"start": "20:00", "end": "22:00"}
      and sms.parse_schedule_windows("8pm to 10") == [])
db.save_user_schedule(U, sms.parse_schedule_windows("20:00-22:00"),
                      raw_text="20:00-22:00", source="analyze")

db.set_agreed_goal(U, "become the ML-capable founder")
db.set_agreed_offer(U, "daily question drills from your notes")
db.set_ignition_marker(U, "opens the notebook and types")
check("everything agreed but no validated walkthrough → NOT complete",
      not db.check_and_complete_onboarding(U)
      and db.get_onboarding_state(U)["missing"] == ["material_walkthrough"])
_mid = db.add_user_material(U, "link", title="karpathy",
                            source_url="https://yt.be/x")
db.update_material_walkthrough(_mid, status="in_progress")
check("a material alone is not enough — the sample must be validated",
      db.get_onboarding_state(U)["missing"] == ["material_walkthrough"])

# ── 3. last field → completion flips, phase transitions ──────────────
print("3) completion")
db.update_material_walkthrough(_mid, status="validated")
check("user-confirmed sample is the last fill → completes onboarding",
      db.check_and_complete_onboarding(U) is True)
s = db.get_onboarding_state(U)
check("completed_at stamped, event emitted",
      s["completed_at"] and len(events_of("onboarding_completed")) == 1)
check("phase flipped via onboarding, with event",
      db.get_user_phase(U)["phase"] == "first_bite"
      and any(json.loads(e["payload"]).get("via") == "onboarding_completed"
              for e in events_of("phase_transition")))
check("idempotent — second check is a no-op",
      db.check_and_complete_onboarding(U) is False
      and len(events_of("onboarding_completed")) == 1)

prompt, _ = sms._build_system_prompt("evening", U)
check("checklist block gone after completion",
      "Onboarding checklist" not in prompt)

# ── 4. started_at stamped by the first send; force-backfill ──────────
print("4) started_at + force")


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            class _B:
                text = "응 좋아, 내일 봐!\n[STEP: connect@1]\n[EXPECT: reply]"

            class _R:
                content = [_B()]
            return _R()


sms.anthropic.Anthropic = FakeClient
# P0-C: this user agreed a schedule above, so fixed crons stand down
# (user_schedule_active) — the send now arrives via the hourly
# schedule tick hitting the 20:00 window's start hour.
sms.handle_schedule_tick(now=datetime.now().replace(hour=20, minute=5))
check("first coach send stamps onboarding_started_at",
      db.get_onboarding_state(U)["started_at"] is not None
      and len(events_of("onboarding_started")) == 1)

# ── the focus outranks the notes while onboarding runs ──────────────
print("focus outranks notes")
U4 = "outrank"
db.ensure_user_profile_row(U4)
db.save_user_note(U4, "Tiny first action will land better than a review ask",
                  given={}, when=["micro_ask@2"], expect="advance")
p_on, _ = sms._build_system_prompt("evening", U4)
check("the note's claim still informs HOW to speak",
      "Tiny first action will land better" in p_on)
check("but its move prescription is withheld — it read as an order",
      "micro_ask@2" not in p_on and "expect advance" not in p_on)
check("and the focus block says so out loud",
      "This block outranks everything below it" in p_on
      and "never WHAT this message has to settle" in p_on)

db.check_and_complete_onboarding(U4, force=True)
p_off, _ = sms._build_system_prompt("evening", U4)
check("completion hands the moves back",
      "micro_ask@2" in p_off and "expect advance" in p_off
      and "This block outranks" not in p_off)

# ── the focus block rides second, right under the clock ─────────────
print("focus block placement")
U3 = "placement"
db.ensure_user_profile_row(U3)
for label, p in (("scheduled", sms._build_system_prompt("evening", U3)[0]),
                 ("reply", sms._build_system_prompt_for_reply(U3)[0])):
    heads = [ln for ln in p.split("\n") if ln.startswith("## ")]
    check(f"{label}: clock first, then what this message is for",
          heads[0].startswith("## Right now")
          and heads[1].startswith("## Onboarding"))

check("ignition judgment is asked only about an inbound reply",
      "## Ignition judgment" in sms._build_system_prompt_for_reply(U)[0]
      and "## Ignition judgment" not in sms._build_system_prompt("evening", U)[0])

# ── the walkthrough arc drives the prompt ───────────────────────────
print("walkthrough arc in the prompt")
U6 = "walker"
db.ensure_user_profile_row(U6)
db.set_agreed_goal(U6, "g"); db.save_learning_path(U6, "d", "p", "c")
db.set_ignition_marker(U6, "m")
p_show, _ = sms._build_system_prompt("evening", U6)
check("no material yet → the label reads the situation, three ways",
      "FIRST read the conversation" in p_show
      and "There IS no material yet" in p_show
      and "template parrot" in p_show
      and "do NOT send that link over SMS" in p_show)
db.log_event(U6, "my_link_emailed", {"to": "x@y.z"}, source="emailer")
p_mail, _ = sms._build_system_prompt("evening", U6)
check("once the link email went out, the focus points at the inbox",
      "point them at their INBOX" in p_mail
      and "메일함 봐봐" in p_mail)
_wm = db.add_user_material(U6, "file", title="정리본.docx",
                           extracted_text="...")
db.set_material_digest(_wm, "two sections, ~40 recall items")
db.update_material_walkthrough(
    _wm, user_description="업무에서 쌓은 걸 정리한 파일",
    wants=[{"quote": "클라이언트가 물으면 바로", "meaning": "recall"}],
    status="in_progress")
p_walk, _ = sms._build_system_prompt("evening", U6)
check("material exists → focus is the walkthrough to a validated sample",
      "lead a walkthrough of 정리본.docx" in p_walk
      and "SAMPLE" in p_walk and "rings true" in p_walk)
check("a material-less user gets the NONE assertion, not silence",
      "Their materials — NONE" in sms._build_materials_block("matless")
      and "A promise to upload is not an upload"
      in sms._build_materials_block("matless"))
check("materials block carries both readings, user's words on top",
      "Their materials — what they study from" in p_walk
      and "~40 recall items" in p_walk
      and "클라이언트가 물으면 바로" in p_walk
      and "THEIR words win" in p_walk)
check("capability stock is in the prompt (offers draw only on it)",
      "the only stock your promises draw on" in p_walk
      and "sit with them while they study" in p_walk
      and "can NOT (today): see anything outside a session" in p_walk)
db.update_material_walkthrough(_wm, status="validated")
p_offer, _ = sms._build_system_prompt("evening", U6)
check("validated → focus moves to the offer, built FROM the walkthrough",
      "This message's focus: what YOU will do for them, ongoing"
      in p_offer and "the sample they already confirmed" in p_offer)

# ── the agreed offer is a standing promise the coach can SEE ────────
print("offer persistence")
U5 = "promiser"
db.ensure_user_profile_row(U5)
p_before, _ = sms._build_system_prompt("evening", U5)
check("before agreement the promise slot says so",
      "YOUR STANDING PROMISE" in p_before
      and "(not yet agreed)" in p_before)
db.set_agreed_offer(U5, "매주 워드파일에서 질문 뽑아 불시에 던져주기")
p_after, _ = sms._build_system_prompt("evening", U5)
check("once agreed, every later prompt carries the promise verbatim",
      "매주 워드파일에서 질문 뽑아 불시에 던져주기" in p_after
      and "debt you are actively paying" in p_after)
r_after, _ = sms._build_system_prompt_for_reply(U5)
check("the reply path carries it too",
      "매주 워드파일에서 질문 뽑아 불시에 던져주기" in r_after)

U2 = "veteran"   # deliberately NO ensure_user_profile_row: a forced
                 # completion used to log success and write nothing
check("force completes an incomplete user (operator backfill)",
      db.check_and_complete_onboarding(U2, force=True) is True
      and db.get_onboarding_state(U2)["completed_at"] is not None)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
