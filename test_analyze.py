"""
Per-turn analysis call tests — brief §7 "Markers vs. the analysis
call".

Run: ./venv/bin/python test_analyze.py  (sqlite; anthropic mocked)

Claims under test: extraction is recovered from the TRANSCRIPT (not
from markers the speaker must remember to emit), speculation is
refused by construction (omitted fields write nothing), writes are
idempotent so history re-runs are safe, the step judgment rides the
same call, and the generation path no longer acts on extraction
markers (it only strips them so nothing leaks).
"""

import json
import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "u1"
os.environ["TUTOR_USER_PHONE"] = "+15550009999"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_analyze.db")
db.init_db()

import analyze_turn  # noqa: E402
import sms  # noqa: E402

U = "u1"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind):
    return [r for r in db.get_events(U, limit=300) if r["kind"] == kind]


class ToolFake:
    payload = None
    seen = []

    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            ToolFake.seen.append(kwargs)

            class _B:
                type = "tool_use"
                input = ToolFake.payload

            class _R:
                content = [_B()]
            return _R()


analyze_turn.anthropic.Anthropic = ToolFake

db.ensure_user_profile_row(U)
db.save_sms_message(U, "assistant", "뭐 배우고 싶어서 왔어?", "out")
db.save_sms_message(U, "user", "로펌에서 일하는데 업무 지식을 기억하고 싶어", "in")

# ── 1. extraction from the transcript ────────────────────────────────
print("1) extraction")
ToolFake.payload = {
    "goal": "업무에서 쌓은 법률 지식을 질문받으면 바로 답할 수 있게 유지하기",
    "step_completed": "not_applicable", "step_reason": "no plan",
}
res = analyze_turn.analyze(U)
check("goal written from the conversation, no marker involved",
      res and "goal" in res["applied"]
      and db.get_user_phase(U)["agreed_goal"].startswith("업무에서"))
check("turn_analyzed event + flight-recorded call",
      len(events_of("turn_analyzed")) == 1
      and db.get_llm_call(res["llm_call_id"]) is not None)
sys_prompt = ToolFake.seen[0]["system"]
check("prompt states what's known and what's missing",
      "Already known" in sys_prompt and "Still missing" in sys_prompt)
check("forced tool call", ToolFake.seen[0]["tool_choice"]["name"]
      == "submit_analysis")

# ── 2. omission = silence; idempotent re-runs ────────────────────────
print("2) omission & idempotency")
before = db.get_onboarding_state(U)["filled"]
ToolFake.payload = {"step_completed": "not_applicable", "step_reason": "-"}
res = analyze_turn.analyze(U)
check("empty analysis writes nothing",
      res["applied"] == []
      and db.get_onboarding_state(U)["filled"] == before)

ToolFake.payload = {
    "goal": db.get_user_phase(U)["agreed_goal"],
    "step_completed": "not_applicable", "step_reason": "-",
}
res = analyze_turn.analyze(U)
check("re-reporting an unchanged value is a no-op (history re-runs safe)",
      res["applied"] == [])

# ── 3. validated fields ──────────────────────────────────────────────
print("3) validation")
ToolFake.payload = {
    "schedule": "저녁 여덟시쯤", "step_completed": "not_applicable",
    "step_reason": "-",
}
res = analyze_turn.analyze(U)
check("unparseable schedule refused",
      "schedule" not in res["applied"] and db.get_user_schedule(U) is None)

ToolFake.payload = {
    "path_direction": "업무 전문성 유지",
    "path_project": "정리 자료 3장까지 즉답",
    "path_done_condition": "무작위 질문 90% 즉답",
    "schedule": "21:00-22:30",
    "ignition_marker": "워드 자료 열고 질문에 소리내서 답하기 시작",
    "ignition_marker_basis": "stated",
    "ignition_marker_confidence": "high",
    "offer": "네 자료에서 질문을 뽑아 한가한 시간에 하나씩 던지고 피드백",
    "first_bite": "자료 1장에서 질문 5개 뽑기",
    "step_completed": "not_applicable", "step_reason": "-",
}
res = analyze_turn.analyze(U)
check("all remaining fields written — ignition payload ignored "
      "(retired 2026-08-12, PR-A)",
      {"path", "schedule", "offer", "bite"} <= set(res["applied"])
      and not any(a.startswith("ignition_marker") for a in res["applied"]))
check("retired ignition payload writes nothing",
      not (db.get_user_phase(U)["ignition_marker"] or "").strip())
check("schedule parsed to windows",
      json.loads(db.get_user_schedule(U)["windows_json"])[0]
      == {"start": "21:00", "end": "22:30"})
check("offer stored + evented",
      (db.get_user_profile_by_id(U) or {}).get("agreed_offer", "")
      .startswith("네 자료에서")
      and len(events_of("offer_set")) == 1)
_mid = db.add_user_material(U, "file", title="정리본.docx",
                            extracted_text="...")
db.update_material_walkthrough(_mid, status="validated")
db.set_expectation_sent(U)   # server-sent item, stamped by delivery
db.check_and_complete_onboarding(U)
check("all applicable fields complete → onboarding completed by the server",
      db.get_onboarding_state(U)["completed_at"] is not None
      and db.get_user_phase(U)["phase"] == "first_bite")

# ── 3b. the ignition marker MAY be inferred (it is an instrument,
#        not an agreement) — but a low-confidence guess is refused ──
print("3b) ignition inference")
db.ensure_user_profile_row("u_inf")
db.save_sms_message("u_inf", "user", "워드파일에 정리해둔 걸 외우려고", "in")
ToolFake.payload = {
    "ignition_marker": "그 워드파일 열어서 암기 시작",
    "ignition_marker_basis": "inferred",
    "ignition_marker_confidence": "high",
}
res = analyze_turn.analyze("u_inf")
check("ignition extraction retired — even a confident marker payload "
      "writes nothing",
      not (db.get_user_phase("u_inf")["ignition_marker"] or "").strip()
      and not any(a.startswith("ignition") for a in res["applied"]))

# ── 3c. the walkthrough lands through analysis ───────────────────────
print("3c) walkthrough extraction")
W = "u_walk"
db.ensure_user_profile_row(W)
_wm = db.add_user_material(W, "file", title="정리본.docx",
                           extracted_text="...")
db.save_sms_message(W, "user", "그 파일은 내가 업무하면서 정리한 거야", "in")
db.save_sms_message(W, "assistant",
                    "혹시 시험 준비용이야? 아니면 회의 대비?", "out")
db.save_sms_message(W, "user", "클라이언트가 물으면 바로 대답해야 해", "in")
ToolFake.payload = {
    "material_description": "업무하면서 직접 정리한 법률지식 파일",
    "material_wants": [{"quote": "클라이언트가 물으면 바로 대답해야 해",
                        "meaning": "instant recall"}],
    "step_completed": "not_applicable", "step_reason": "-",
}
analyze_turn.analyze(W)
_m = db.get_material(_wm)
check("description + verbatim wants land, status → in_progress",
      _m["user_description"].startswith("업무하면서")
      and _m["wants"][0]["quote"].startswith("클라이언트")
      and _m["walkthrough_status"] == "in_progress")

# ── 3c-2. the attribution guard: coach lines can NEVER become wants ──
ToolFake.payload = {
    "material_wants": [
        {"quote": "클라이언트가 물으면 바로 대답해야 해",
         "meaning": "real user words"},
        {"quote": "혹시 시험 준비용이야? 아니면 회의 대비?",
         "meaning": "the COACH said this — must be dropped"},
        {"quote": "이건 아무도 말한 적 없는 문장",
         "meaning": "invented — must be dropped"}],
    "step_completed": "not_applicable", "step_reason": "-",
}
analyze_turn.analyze(W)
_m = db.get_material(_wm)
check("coach-mouthed and invented quotes are dropped; user's survive",
      [w["quote"] for w in _m["wants"]]
      == ["클라이언트가 물으면 바로 대답해야 해"]
      and any(r["kind"] == "want_quote_rejected"
              and len(json.loads(r["payload"])["quotes"]) == 2
              for r in db.get_events(W, limit=30)))
check("whitespace differences are not paraphrase (normalized match)",
      analyze_turn._user_said(W, "클라이언트가  물으면\n바로 대답해야 해"))
check("the analysis prompt showed the material's state",
      "newest material: 정리본.docx"
      in ToolFake.seen[-1]["system"])

ToolFake.payload = {
    "walkthrough_sample_validated": False,
    "step_completed": "not_applicable", "step_reason": "-",
}
analyze_turn.analyze(W)
check("a coach proposal without the user's yes does NOT validate",
      db.get_material(_wm)["walkthrough_status"] == "in_progress")

ToolFake.payload = {
    "walkthrough_sample_validated": True,
    "walkthrough_sample_evidence":
        'COACH: "조기상환 트리거 발동 시 정산 기준은?" USER: "어 맞아 딱 그런 거"',
    "step_completed": "not_applicable", "step_reason": "-",
}
analyze_turn.analyze(W)
check("sample + user affirmation → validated, gate opens, evidence logged",
      db.get_material(_wm)["walkthrough_status"] == "validated"
      and db.has_validated_material(W)
      and len([r for r in db.get_events(W, limit=50)
               if r["kind"] == "walkthrough_validated"]) == 1)
check("idempotent — re-running the same payload writes nothing new",
      analyze_turn.analyze(W) is not None
      and len([r for r in db.get_events(W, limit=50)
               if r["kind"] == "walkthrough_validated"]) == 1)

# ── 3d. no material exists → the agreed first material gets a row ────
print("3d) named material creation")
N = "u_nomat"
db.ensure_user_profile_row(N)
db.save_sms_message(N, "user", "자료가 딱히 없어", "in")
db.save_sms_message(N, "assistant", "그럼 첫 자료를 정하자", "out")
db.save_sms_message(N, "user",
                    "multimodal pipeline 개념 가이드 같은 걸 네가 "
                    "만들어주면 그걸로 하자", "in")
ToolFake.payload = {
    "material_named": {"title": "Multimodal pipeline 개념 가이드",
                       "description": "저장 방식, episode 묶기, "
                                      "inference 타이밍의 판단 기준"},
    "step_completed": "not_applicable", "step_reason": "-",
}
analyze_turn.analyze(N)
_nm = db.get_user_materials(N)
check("the agreed first material becomes a named row, in_progress",
      len(_nm) == 1 and _nm[0]["kind"] == "named"
      and _nm[0]["title"].startswith("Multimodal")
      and _nm[0]["walkthrough_status"] == "in_progress")
ToolFake.payload = {
    "material_named": {"title": "다른 제목"},
    "step_completed": "not_applicable", "step_reason": "-",
}
analyze_turn.analyze(N)
check("a second naming while one exists is ignored (no dup anchor)",
      len(db.get_user_materials(N)) == 1)

# ── 3e. smalltalk aversion — a living judgment, prompt-enforced ──────
print("3e) smalltalk aversion")
S = "u_terse"
db.ensure_user_profile_row(S)
db.save_sms_message(S, "assistant", "좋은 아침! 오늘 하루 어때?", "out")
db.save_sms_message(S, "user", "본론만.", "in")
ToolFake.payload = {
    "smalltalk_aversion": 0.8,
    "step_completed": "not_applicable", "step_reason": "-",
}
res = analyze_turn.analyze(S)
check("reported aversion stored + evented",
      any(a.startswith("smalltalk_aversion") for a in res["applied"])
      and (db.get_user_profile_by_id(S) or {}).get("smalltalk_aversion")
      == 0.8
      and len([r for r in db.get_events(S, limit=30)
               if r["kind"] == "smalltalk_judged"]) == 1)

ToolFake.payload = {
    "smalltalk_aversion": 0.82,
    "step_completed": "not_applicable", "step_reason": "-",
}
res = analyze_turn.analyze(S)
check("re-report within 0.05 is skipped (no churn, no second event)",
      res["applied"] == []
      and (db.get_user_profile_by_id(S) or {}).get("smalltalk_aversion")
      == 0.8
      and len([r for r in db.get_events(S, limit=30)
               if r["kind"] == "smalltalk_judged"]) == 1)

ToolFake.payload = {
    "smalltalk_aversion": 0.3,
    "step_completed": "not_applicable", "step_reason": "-",
}
res = analyze_turn.analyze(S)
check("a materially different re-report updates (early misread unsticks)",
      any(a.startswith("smalltalk_aversion") for a in res["applied"])
      and (db.get_user_profile_by_id(S) or {}).get("smalltalk_aversion")
      == 0.3
      and len([r for r in db.get_events(S, limit=30)
               if r["kind"] == "smalltalk_judged"]) == 2)

# prompt enforcement: at/above the threshold the block appears; below
# it or unjudged, nothing renders
db.set_smalltalk_aversion(S, 0.8, source="operator")
ctx, _ = sms._build_context_blocks(S)
check("aversion 0.8 → no-small-talk block in the prompt",
      "## No small talk with this user" in ctx
      and "confidence 0.8" in ctx)
db.ensure_user_profile_row("u_chatty")
db.set_smalltalk_aversion("u_chatty", 0.4, source="operator")
ctx, _ = sms._build_context_blocks("u_chatty")
check("aversion 0.4 (below threshold) → no block",
      "## No small talk with this user" not in ctx)
db.ensure_user_profile_row("u_unjudged")
ctx, _ = sms._build_context_blocks("u_unjudged")
check("unjudged (NULL) → no block",
      "## No small talk with this user" not in ctx)

# ── 4. step judgment retired (2026-08-12, PR-A) ─────────────────────
print("4) step judgment retired")
db.save_sequence_plan(U, [
    {"tag": "elicit_why", "intensity": 2, "intent": "why, his words"},
    {"tag": "micro_ask", "intensity": 1, "intent": "one tiny thing"}],
    rationale="t", source="operator")
ToolFake.payload = {"step_completed": "yes", "step_reason": "he said why"}
analyze_turn.analyze(U)
check("a step_completed payload no longer moves any cursor",
      db.get_current_plan(U)["cursor"] == 0
      and len(events_of("step_judged")) == 0)

# ── 5. generation no longer acts on extraction markers ───────────────
print("5) generation path")
stale = ('좋아!\n[GOAL: "완전히 다른 목표"]\n'
         '[IGNITION_DEF: "아무거나"]\n[SCHEDULE: "03:00-04:00"]')
goal_before = db.get_user_phase(U)["agreed_goal"]
out = sms._strip_extraction_markers(U, stale)
check("stale markers stripped from the outbound text",
      "[GOAL" not in out and "[IGNITION_DEF" not in out
      and "[SCHEDULE" not in out and out.startswith("좋아!"))
check("and they change NOTHING (analysis owns those fields)",
      db.get_user_phase(U)["agreed_goal"] == goal_before
      and json.loads(db.get_user_schedule(U)["windows_json"])[0]["start"]
      == "21:00")
check("stripping is recorded for prompt-hygiene monitoring",
      len(events_of("stale_marker_stripped")) == 1)

# ── standing preferences: the relationship contract ─────────────────
print("standing preferences")
UPF = "prefuser"
db.ensure_user_profile_row(UPF)
db.save_sms_message(UPF, "user",
                    "앞으로는 영어로 대화하자 그게 더 편해", "in")
ok = db.set_user_preference(
    UPF, "language", "English — reply in English",
    evidence="앞으로는 영어로 대화하자 그게 더 편해", source="analyze")
check("a preference writes once and skips unchanged re-reports",
      ok is True
      and db.set_user_preference(
          UPF, "language", "English — reply in English") is False
      and db.get_user_preferences(UPF)["language"]["value"]
      == "English — reply in English")
check("latest wins on change",
      db.set_user_preference(UPF, "language", "Korean again") is True
      and db.get_user_preferences(UPF)["language"]["value"]
      == "Korean again")

# the verbatim guard, through _apply
applied = analyze_turn._apply(UPF, {
    "preferences": [
        {"key": "opening", "value": "question first, no greetings",
         "evidence_quote": "앞으로는 영어로 대화하자 그게 더 편해"},
        {"key": "tone", "value": "formal",
         "evidence_quote": "존댓말로 해주세요"},   # never said
    ]}, "call-x")
check("verified quote lands, invented quote is dropped and evented",
      "preference(opening)" in applied
      and "tone" not in db.get_user_preferences(UPF)
      and any(r["kind"] == "preference_quote_rejected"
              for r in db.get_events(UPF, limit=10)))

import sms as _sms_pref
p_pref, _ = _sms_pref._build_system_prompt("evening", UPF)
check("the contract renders in the prompt, evidence attached",
      "Standing preferences (user-stated, binding)" in p_pref
      and "question first, no greetings" in p_pref)
UNPF = "noprefs"
db.ensure_user_profile_row(UNPF)
check("no preferences → no block",
      "Standing preferences" not in
      _sms_pref._build_system_prompt("evening", UNPF)[0])

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
