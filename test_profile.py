"""
User profile brief + learning_paths.path_kind tests — step ④.

Run: ./venv/bin/python test_profile.py  (sqlite; anthropic mocked)

Covers: the brief's storage (versioned, append-only, event-emitting),
the learning-type taxonomy validated server-side with the
retry-with-feedback loop, `wants` kept VERBATIM (paraphrase is
rejected, not stored), path_kind stamped onto a new path version,
the compact profile block that every send carries, the operator's
GET /plan surface, and zero writes on persistent failure.
"""

import asyncio
import json
import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "brief_u"
os.environ["TUTOR_USER_PHONE"] = "+15550004444"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ.pop("TWILIO_FROM_NUMBER", None)
os.environ["TZ_OFFSET_HOURS"] = "0"
os.environ["CRON_SECRET"] = "s3cr3t"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_profile.db")
db.init_db()

import coach  # noqa: E402
import genplan  # noqa: E402
import notes as notes_mod  # noqa: E402

U = "brief_u"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind, user_id=U):
    return [r for r in db.get_events(user_id, limit=300)
            if r["kind"] == kind]


class ToolBlock:
    type = "tool_use"
    name = "submit_initial_plan"

    def __init__(self, payload):
        self.input = payload


class FakeAnthropic:
    """Returns queued tool payloads, one per attempt; records calls."""
    queue = []
    seen = []

    def __init__(self, *a, **kw):
        pass

    class messages:
        @staticmethod
        def create(**kwargs):
            FakeAnthropic.seen.append(kwargs)

            class _Resp:
                content = [ToolBlock(FakeAnthropic.queue.pop(0))]
            return _Resp()


genplan.anthropic.Anthropic = FakeAnthropic

# The user's own words — the only legitimate source of `wants` quotes.
QUOTE_1 = "내가 정리한 노트를 자꾸 까먹어서 물어봐 주면 좋겠어"
QUOTE_2 = "질문 받으면 바로 답이 튀어나올 정도가 되고 싶어"

VALID = {
    "notes": [
        {"claim": "자기 노트를 근거로 물으면 답을 길게 함",
         "given": {"position": "mid"},
         "when": ["micro_ask@1"], "expect": "reply",
         "evidence_quotes": [QUOTE_1]},
        {"claim": "속도 자체가 목표 — 느린 설명은 지루해함",
         "when": ["choice_offer@2"], "expect": "reply",
         "evidence_quotes": [QUOTE_2]},
    ],
    "plan": {
        "steps": [
            {"tag": "elicit_why", "intensity": 2, "intent": "his own why"},
            {"tag": "micro_ask", "intensity": 1, "intent": "one recall Q"},
        ],
        "rationale": "recall-first per note 1",
    },
    "profile": {
        "job": "백엔드 개발자",
        "learning_types": ["memorization", "retrieval_fluency"],
        "materials": ["본인이 정리한 노트", "사내 위키"],
        "wants": [
            {"quote": QUOTE_1, "meaning": "노트 기반 능동 회상 질문"},
            {"quote": QUOTE_2, "meaning": "속도 목표 — 즉답 유창성"},
        ],
        "personality": "짧게 답하지만 꾸준함, 설명보다 질문에 반응",
        "path_kind": "coverage",
        "rationale": "본인 노트를 '까먹는다'고 말했고 '바로 답이 튀어나올' "
                     "속도를 원함 → 암기 + 인출 유창성, 분량 기준 경로",
    },
}


def fresh(**profile_overrides):
    """A deep copy of VALID with profile fields overridden."""
    p = json.loads(json.dumps(VALID))
    p["profile"].update(profile_overrides)
    return p


# fixture: onboarding conversation + the four onboarding fields
db.ensure_user_profile_row(U)
db.save_sms_message(U, "assistant", "뭘 배우고 있어?", "out")
db.save_sms_message(U, "user", QUOTE_1, "in")
db.save_sms_message(U, "user", QUOTE_2, "in")
db.set_agreed_goal(U, "노트 내용 즉답")
db.set_ignition_marker(U, "노트를 열고 첫 질문에 답함")
db.save_learning_path(U, "개발 실력", "노트 3장 90% 즉답", "3장 전체")

# ── 1. happy path: the brief is saved, versioned, and used ───────────
print("1) happy path")
FakeAnthropic.queue = [fresh()]
FakeAnthropic.seen = []
result = genplan.generate(U)
check("generate returns a brief version", result and result["brief_version"] == 1)

brief = db.get_user_profile_brief(U)
check("brief v1 stored with job + personality",
      brief and brief["version"] == 1
      and brief["job"] == "백엔드 개발자"
      and "꾸준함" in brief["personality"])
check("learning types + materials stored as JSON lists",
      json.loads(brief["learning_types_json"])
      == ["memorization", "retrieval_fluency"]
      and "사내 위키" in json.loads(brief["materials_json"]))
check("brief stores its OWN rationale (not the plan's)",
      "인출 유창성" in brief["rationale"]
      and brief["rationale"] != VALID["plan"]["rationale"])
check("brief carries source + the llm_call_id that produced it",
      brief["source"] == "p7"
      and brief["llm_call_id"] == result["llm_call_id"]
      and db.get_llm_call(brief["llm_call_id"]) is not None)
check("profile_brief_saved event emitted",
      len(events_of("profile_brief_saved")) == 1)

wants = json.loads(brief["wants_json"])
check("wants preserved VERBATIM (exact user characters, both quotes)",
      [w["quote"] for w in wants] == [QUOTE_1, QUOTE_2])
check("each want carries its one-line reading",
      wants[0]["meaning"] == "노트 기반 능동 회상 질문")

sys_prompt = FakeAnthropic.seen[0]["system"]
check("call carried the taxonomy + the verbatim rule",
      "retrieval_fluency" in sys_prompt and "VERBATIM" in sys_prompt
      and "path_kind" in sys_prompt)

# ── 2. path_kind lands on a new path version ─────────────────────────
print("2) path_kind")
path = db.get_current_path(U)
check("path_kind stamped onto an appended path version (v2, p7)",
      path["version"] == 2 and path["path_kind"] == "coverage"
      and path["changed_by"] == "p7")
check("the three path layers carried forward, not lost",
      path["direction"] == "개발 실력"
      and path["project"] == "노트 3장 90% 즉답"
      and path["project_done_condition"] == "3장 전체")
check("path_set event records the kind",
      json.loads(events_of("path_set")[-1]["payload"])["path_kind"]
      == "coverage")
check("db.save_learning_path defaults path_kind to ''",
      db.save_learning_path("path_only_u", "d")
      and db.get_current_path("path_only_u")["path_kind"] == "")

# ── 3. taxonomy validation rejects, feedback retry succeeds ──────────
print("3) taxonomy validation")
bad = fresh(learning_types=["memorization", "vibes_based_osmosis"])
FakeAnthropic.queue = [bad, fresh()]
FakeAnthropic.seen = []
result = genplan.generate(U)
check("second attempt succeeds after feedback", result is not None)
feedback = FakeAnthropic.seen[1]["messages"][-1]["content"]
check("retry told the model exactly which value was unknown",
      "vibes_based_osmosis" in feedback and "learning_types" in feedback)
check("brief versioned append-only (v2 now latest, v1 still there)",
      db.get_user_profile_brief(U)["version"] == 2
      and len(events_of("profile_brief_saved")) == 2)
check("unknown path_kind also caught",
      any("path_kind" in e for e in genplan._validate(
          fresh(path_kind="vision_quest"), require_profile=True)))
check("empty learning_types caught",
      any("learning_types" in e for e in genplan._validate(
          fresh(learning_types=[]), require_profile=True)))
check("a reading without its grounds caught",
      any("rationale" in e for e in genplan._validate(
          fresh(rationale="  "), require_profile=True)))
check("missing profile caught when required",
      genplan._validate({"notes": [], "plan": VALID["plan"]},
                        require_profile=True)
      and not genplan._validate({"notes": [], "plan": VALID["plan"]}))

# ── 4. a paraphrased want is a bug, not a nuance ─────────────────────
print("4) verbatim enforcement")
user_text = genplan._norm(f"{QUOTE_1} {QUOTE_2}")
errs = genplan._validate(
    fresh(wants=[{"quote": "노트를 자주 잊어버린다고 함",
                  "meaning": "회상 필요"}]),
    user_text=user_text, require_profile=True)
check("paraphrase rejected with a paraphrase-specific message",
      any("wants[0]" in e and "paraphrase" in e for e in errs))
check("verbatim quote passes",
      not genplan._validate(fresh(), user_text=user_text,
                            require_profile=True))
check("wrapping punctuation forgiven, wording is not",
      not genplan._validate(
          fresh(wants=[{"quote": f'"{QUOTE_1}."', "meaning": "m"}]),
          user_text=user_text, require_profile=True))

# ── 5. persistent failure → nothing partially written ────────────────
print("5) persistent failure")
brief_before = db.get_user_profile_brief(U)["version"]
path_before = db.get_current_path(U)["version"]
notes_before = len(db.get_user_notes(U))
FakeAnthropic.queue = [fresh(learning_types=["telepathy"]) for _ in range(3)]
result = genplan.generate(U)
check("returns None + failure event",
      result is None and len(events_of("p7_generation_failed")) == 1)
check("NO brief, path or note written",
      db.get_user_profile_brief(U)["version"] == brief_before
      and db.get_current_path(U)["version"] == path_before
      and len(db.get_user_notes(U)) == notes_before)
failed_call = db.get_llm_call(
    json.loads(events_of("p7_generation_failed")[0]["payload"])["llm_call_id"])
check("the rejected raw attempt survives in llm_calls",
      failed_call is not None and "telepathy" in failed_call["response_text"])

# ── 6. the read side: a compact block in every send ──────────────────
print("6) profile block")
block = notes_mod.render_profile_block(U)
check("block shows job, types, path framing, materials",
      "백엔드 개발자" in block and "retrieval_fluency" in block
      and "coverage" in block and "사내 위키" in block)
check("block quotes their words verbatim with the reading",
      QUOTE_1 in block and "노트 기반 능동 회상 질문" in block)
check("block shows the personality read", "꾸준함" in block)
check("block is compact (goes into every send)",
      len(block.splitlines()) <= 12)
check("block empty for a user with no brief",
      notes_mod.render_profile_block("nobody") == "")

combined = notes_mod.render_notes_block(U)
check("block B carries profile THEN notes",
      combined.index("Who this user is") < combined.index("scored notes")
      and "micro_ask@1" in combined)
check("no brief + no notes still renders empty",
      notes_mod.render_notes_block("nobody") == "")

# ── 7. operator surface ──────────────────────────────────────────────
print("7) operator surface")


class FakeRequest:
    method = "GET"

    def __init__(self, **query):
        self.query = query
        self.headers = {}


def get_plan(**query):
    resp = asyncio.new_event_loop().run_until_complete(
        coach._plan_handler(FakeRequest(**query)))
    return resp.status, getattr(resp, "text", "")


status, text = get_plan(secret="s3cr3t", user_id=U)
check("GET /plan surfaces job / types / materials / path kind",
      status == 200 and "백엔드 개발자" in text
      and "retrieval_fluency" in text and "coverage" in text
      and "사내 위키" in text)
check("GET /plan surfaces the verbatim wants, personality, rationale",
      QUOTE_2 in text and "꾸준함" in text and "인출 유창성" in text)
check("no sequence plan rendered (generation retired 2026-08-12)",
      "micro_ask@1" not in text)
status, text = get_plan(secret="s3cr3t", user_id="nobody")
check("brief-less user says so instead of 500",
      status == 200 and "none generated" in text)
check("bad secret rejected",
      get_plan(secret="nope", user_id=U)[0] == 403)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
