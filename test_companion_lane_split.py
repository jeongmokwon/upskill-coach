"""Companion/legacy lane split tests (2026-08-20, PR-2).

Run: python test_companion_lane_split.py  (sqlite; anthropic mocked)

Claims under test: a lane-open user gets NO generic scheduled sends
(reactive-by-default) while a drill send still fires for them; a
legacy user's scheduled send is unchanged; onboarding completion
(and the genplan it triggers) cannot fire for lane users except by
operator force; analyze's edtech extractions (goal, bite, offer,
path, material machinery) are not applied for lane users while
preferences, pause, and the user's own document description still
are; the my-link auto-email never fires for lane users.
"""

import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "jm"
os.environ["TUTOR_USER_PHONE"] = "+15550001111"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("MESSAGING_CHANNEL", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_lane_split.db")
db.init_db()

import analyze_turn  # noqa: E402
import sms  # noqa: E402

PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            class _B:
                text = "Evening thoughts — how did today go?"

            class _R:
                content = [_B()]
                stop_reason = "end_turn"
            return _R()


sms.anthropic.Anthropic = FakeClient


def events_of(uid, kind):
    return [r for r in db.get_events(uid, limit=100)
            if r["kind"] == kind]


print("scheduled sends")
db.ensure_user_profile_row("comp1")
db.enable_tracks("comp1", source="admin")
db.set_expectation_sent("comp1")
out = sms._cron_tick_for_user("comp1", "+15550003001", "evening")
import json  # noqa: E402
skips = [json.loads(e["payload"]) for e in events_of("comp1", "cron_tick")]
check("companion user: evening tick skips (reactive-by-default)",
      out is None and any(s.get("reason") == "companion_reactive_only"
                          for s in skips))

db.ensure_user_profile_row("leg1")
db.set_expectation_sent("leg1")
db.save_sms_message("leg1", "user", "hi", "in")
out = sms._cron_tick_for_user("leg1", "+15550003002", "evening")
check("legacy user: evening tick still generates the LLM turn",
      out == "Evening thoughts — how did today go?")

drill_fired = {}
import drill  # noqa: E402
drill.prepare_scheduled_question = lambda uid: {
    "item": {"id": 1}, "prediction_id": 7, "reask": False, "why": "t"}
drill.active_drill_track = lambda uid: True
sms._build_drill_prompt = lambda uid, ctx: ("DRILL PROMPT", {})
drill.leaks_answer = lambda text, item: False
out = sms._cron_tick_for_user("comp1", "+15550003001", "morning")
check("companion user WITH a due drill item: the drill send fires",
      out == "Evening thoughts — how did today go?"
      and any(json.loads(e["payload"]).get("trigger")
              == "cron_morning_drill"
              for e in events_of("comp1", "sms_out")))

print("onboarding completion gate")
for setter in (lambda: db.set_agreed_goal("comp1", "g"),
               lambda: db.set_agreed_offer("comp1", "o"),
               lambda: db.save_user_schedule("comp1", [
                   {"start": "20:00", "end": "21:00"}], raw_text="x"),
               lambda: db.set_material_status("comp1", "no_material")):
    setter()
check("all fields filled but lane open → completion refuses",
      db.check_and_complete_onboarding("comp1") is False)
check("operator force still completes",
      db.check_and_complete_onboarding("comp1", force=True) is True)

print("analyze apply gating")
db.ensure_user_profile_row("comp2")
db.enable_tracks("comp2", source="admin")
db.save_sms_message("comp2", "user",
                    "please always skip greetings with me", "in")
payload = {
    "goal": "become a great lawyer",
    "first_bite": "read one case",
    "offer": "daily drills",
    "path_direction": "make partner",
    "material_status": "no_material",
    "material_named": {"title": "Some PDF"},
    "preferences": [{"key": "opening",
                     "value": "no greetings, straight to the point",
                     "evidence_quote": "always skip greetings"}],
}
applied = analyze_turn._apply("comp2", payload, "c1")
phase = db.get_user_phase("comp2")
check("edtech fields not applied for lane user",
      (phase["agreed_goal"] or "") == ""
      and (phase["agreed_first_bite"] or "") == ""
      and not db.get_user_materials("comp2")
      and db.get_current_path("comp2") is None
      and not any(a.startswith(("goal", "bite", "offer", "path",
                                "material_status", "material_named"))
                  for a in applied))
check("preferences still applied",
      "opening" in db.get_user_preferences("comp2"))

mid = db.add_user_material("comp2", "file", title="Deck.pdf")
applied = analyze_turn._apply(
    "comp2", {"material_description": "our Q3 fundraising deck",
              "material_wants": [{"quote": "never said"}],
              "walkthrough_sample_validated": True}, "c2")
m = db.get_material(mid)
check("their own document description flows in (grounding)",
      m["user_description"] == "our Q3 fundraising deck"
      and "material_description" in applied)
check("walkthrough machinery stays legacy-only",
      m["walkthrough_status"] != "validated")

legacy_payload = dict(payload)
db.ensure_user_profile_row("leg2")
db.save_sms_message("leg2", "user",
                    "please always skip greetings with me", "in")
applied = analyze_turn._apply("leg2", legacy_payload, "c3")
check("same payload applies fully for a legacy user",
      db.get_user_phase("leg2")["agreed_goal"] == "become a great lawyer"
      and "goal" in applied)

print("my-link auto-email gate")
db.set_user_email("comp2", "c@x.co")
sms.ensure_my_link_delivered("comp2")
check("no my-link email for lane users",
      not events_of("comp2", "my_link_emailed"))

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
