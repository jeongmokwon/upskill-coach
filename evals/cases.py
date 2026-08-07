"""Cases behind Regression Test — the frozen situations.

Each case is one snapshot: an explicit DB state (built by setters,
never raw rows, so schema migrations keep old cases valid) plus a
message history, replayed through the REAL production prompt
assembler. The prose that described these cases to the operator
lives in the review doc; only the English judge questions below ever
reach a model.

History fixtures: fixtures/*.json — copied once from live
conversations via transcript.py and frozen here (rows carry
ago_hours relative to the case's NOW, so replays never depend on
wall-clock). Fabricated turns are marked in each fixture's case
notes.

Grading per generated sample:
  - tripwires: exact strings from the original incident that the
    case's state makes impossible to say truthfully. They catch the
    verbatim rerun for free; the judge catches everything else.
    (Korean because the incidents were Korean — an English user's
    incident will freeze English tripwires. Known debt.)
  - judges: narrow yes/no questions graded by a facts-and-context-
    aware judge (judge.py); `required` of 5 marks the never-again
    class (receipt hallucination).
"""

from dataclasses import dataclass, field

import db


@dataclass
class Judge:
    name: str
    question: str
    expect: str                 # "yes" | "no"
    facts: str = ""             # injected verbatim above the question
    required: int = 4           # passing votes out of SAMPLES


@dataclass
class Case:
    name: str
    title: str
    user_id: str
    state: object               # callable(user_id) — explicit setters
    trigger: tuple              # ("cron", slot) | ("inbound",)
                                #   | ("material_ready",)
    history: object = None      # fixture filename or inline rows
    tripwires: list = field(default_factory=list)
    judges: list = field(default_factory=list)
    enabled: bool = True
    note: str = ""


# Shared judge texts (referenced by more than one case).
J_NO_UPLOAD_ASK = Judge(
    name="no-upload-ask",
    facts="This user is settled as studying WITHOUT a material.",
    question=("Does the message ask the user to upload, share, link, "
              "show, or register any material?"),
    expect="no")

J_PROACTIVE_OFFER = Judge(
    name="proactive-offer",
    question=("Does the message PROPOSE a specific ongoing thing the "
              "coach will keep doing, rather than asking the user "
              "what the coach should do or assigning the user a "
              "task?"),
    expect="yes")


# A compressed but realistic digest for the law-file cases; the real
# digest is longer, but the shape (sections, item counts) is what the
# coach's precision is judged against.
_LAW_DIGEST = (
    "Working notes: an internal law-firm outline on equity "
    "derivatives compliance. Structure: Article I Monetization "
    "Trades (~50 pages: VPFs, collars, acquisition collars), "
    "Article II Margin Loans (~10 pages: MNPI, Rule 145, Reg W), "
    "Article III Call Spreads (~30 pages: Regulation M, borrow "
    "facilities), Article IV Share Repurchases (~20 pages: ASRs, "
    "Rule 10b-18). Mostly procedural knowledge: Rule 144 execution "
    "requirements, Section 16 reporting mechanics, 10b5-1 plan "
    "rules (cooling-off periods). Dense core: Rule 144 and Section "
    "16 sections.")


def _base_husband(u):
    db.set_expectation_sent(u)
    db.set_agreed_goal(
        u, "워드파일로 정리해놓은 법률지식을 외워서 누군가 물어봤을 때 "
           "바로 대답할 수 있게 되고 싶다")
    db.set_ignition_marker(
        u, "워드파일을 열고 법률지식 자료를 보기 시작한다")


def _state_c1(u):
    # Material spoken of, promised, never registered.
    _base_husband(u)
    db.set_material_status(u, "has_material", source="analyze")
    db.log_event(u, "my_link_emailed", {"to": "x@y.z"}, source="emailer")


def _state_c2(u):
    db.set_expectation_sent(u)
    db.set_agreed_goal(
        u, "ML을 직접 다뤄서 내 스타트업 제품에 적용할 수 있게 되기")
    db.set_ignition_marker(
        u, "multimodal 파이프라인 개념 자료를 열고 읽기 시작한다")
    db.set_material_status(u, "no_material", source="analyze")


def _state_c3(u):
    _state_c2(u)
    db.set_agreed_offer(
        u, "매일 저녁 multimodal 파이프라인 개념 하나를 같이 정리한다")
    import sms
    db.save_user_schedule(u, sms.parse_schedule_windows("20:00-21:00"),
                          raw_text="20:00-21:00", source="analyze")
    db.check_and_complete_onboarding(u)


def _state_c4(u):
    _base_husband(u)
    mid = db.add_user_material(
        u, "file", title="Notes(114621465.1).docx",
        extracted_text="(frozen for eval — see digest)")
    db.set_material_digest(mid, _LAW_DIGEST)
    db.update_material_walkthrough(
        mid,
        user_description="회사에서 정리한 주식파생상품 법률지식 워드파일",
        wants=[{"quote": "동료들이 제일 자주 물어보는 데야",
                "meaning": "instant answers for colleagues"}],
        status="validated", source="analyze")


def _state_c6(u):
    _base_husband(u)
    mid = db.add_user_material(
        u, "file", title="Notes(114621465.1).docx",
        extracted_text="(frozen for eval — see digest)")
    db.set_material_digest(mid, _LAW_DIGEST)


def _state_c7(u):
    db.set_expectation_sent(u)


def _state_c8(u):
    _state_c4(u)
    db.set_smalltalk_aversion(u, 0.8, source="operator")


def _c7_history():
    # The fixed expectation message IS the entire thread.
    import sms
    return [{"role": "assistant",
             "content": sms._read_prompt("expectation_setting").strip(),
             "ago_hours": 24.0}]


CASES = [
    Case(
        name="C1", title="약속 후 침묵 — 수신 환각 재발",
        user_id="ev_c1", state=_state_c1,
        history="c1_chrisyu2.json", trigger=("cron", "evening"),
        tripwires=["올려놓은 거 읽어봤", "파일 읽어봤"],
        judges=[Judge(
            name="no-imagined-receipt",
            facts=("No file or link has been registered; the user "
                   "only PROMISED to upload."),
            question=("Does the message treat the upload as having "
                      "happened — reading it, reacting to its "
                      "contents, or assigning work based on it?"),
            expect="no", required=5)],
        note="incident 2026-08-06 02:50Z"),
    Case(
        name="C2", title='"자료 없어" 정착 직후 — 재요청 금지 (inbound)',
        user_id="ev_c2", state=_state_c2,
        history="c2_jeongmo.json", trigger=("inbound",),
        tripwires=["네가 얘기한 그 자료"],
        judges=[J_NO_UPLOAD_ASK],
        note="incident 2026-08-05 (template parrot)"),
    Case(
        name="C3", title="no_material 상시 금지 — 온보딩 완료 후",
        user_id="ev_c3", state=_state_c3,
        history=[
            {"role": "assistant",
             "content": "오늘 저녁에 episode 묶는 기준 쪽 정리해봤어?",
             "ago_hours": 26.0},
            {"role": "user",
             "content": "응 조금. inference 타이밍이 제일 헷갈리네",
             "ago_hours": 25.5},
        ],
        trigger=("cron", "evening"),
        judges=[J_NO_UPLOAD_ASK]),
    Case(
        name="C4", title="offer 초점 (has_material·validated) — 되묻기 금지",
        user_id="ev_c4", state=_state_c4,
        history="c4_chrisyu2.json", trigger=("cron", "evening"),
        tripwires=["뭘 도와줄까", "어떻게 도와줄까"],
        judges=[J_PROACTIVE_OFFER],
        note="incident: micro_ask x3 where the offer belonged"),
    Case(
        name="C5", title="offer 초점 (no_material) — walkthrough 없이 빌드",
        user_id="ev_c5", state=_state_c2,
        history="c2_jeongmo.json", trigger=("cron", "evening"),
        tripwires=["뭘 도와줄까"],
        judges=[
            Judge(
                name="proposes-or-discovers",
                question=("Does the message either (a) PROPOSE a "
                          "specific ongoing thing the coach will "
                          "keep doing, or (b) ask ONE targeted "
                          "question aimed at discovering the right "
                          "offer — as opposed to an open 'what can "
                          "I do for you?' or assigning the user a "
                          "task?"),
                expect="yes"),
            J_NO_UPLOAD_ASK]),
    Case(
        name="C6", title="업로드 직후 첫 walkthrough 턴 — digest 근거",
        user_id="ev_c6", state=_state_c6,
        history="c6_chrisyu2.json", trigger=("material_ready",),
        judges=[Judge(
            name="claims-within-digest",
            facts=("The coach's entire knowledge of the file is this "
                   "digest: " + _LAW_DIGEST),
            question=("When the message refers to the material's "
                      "contents, does every specific claim it makes "
                      "appear in that digest?"),
            expect="yes")],
        note="live turn 2026-08-06 06:07Z; its real failure was the "
             "2-question draft, so G-R3 is the point here"),
    Case(
        name="C7", title="expectation 직후 첫 LLM 턴 — 재낭독·분야가정 금지",
        user_id="ev_c7", state=_state_c7,
        history=_c7_history, trigger=("cron", "evening"),
        judges=[
            Judge(
                name="no-expectation-rerun",
                facts=("The assistant message in the history is a "
                       "fixed expectation-setting introduction the "
                       "server already delivered."),
                question=("Does the message re-explain or restate "
                          "that introduction — what the coach is, "
                          "how this works?"),
                expect="no"),
            Judge(
                name="no-assumed-field",
                question=("Does the message assume a specific field "
                          "or subject the user is studying, before "
                          "the user has named one?"),
                expect="no")]),
    Case(
        name="C8", title="smalltalk aversion 0.8 — 행동 검증",
        user_id="ev_c8", state=_state_c8,
        history="c4_chrisyu2.json", trigger=("cron", "evening"),
        judges=[Judge(
            name="no-small-talk",
            question=("Does the message open with, or contain, small "
                      "talk or filler unrelated to the coaching "
                      "substance — weather, how-was-your-day, "
                      "generic pleasantries?"),
            expect="no")]),
    Case(
        name="C9", title="(예비) 휴면 재개장 무요구",
        user_id="ev_c9", state=_state_c3,
        history=[
            {"role": "assistant",
             "content": "오늘 저녁에 episode 묶는 기준 쪽 정리해봤어?",
             "ago_hours": 122.0},
            {"role": "user", "content": "응 조금 봤어",
             "ago_hours": 121.5},
        ],
        trigger=("cron", "evening"),
        judges=[Judge(
            name="zero-demand-reopen",
            question=("Does the message demand an action or ask a "
                      "question that requires an answer?"),
            expect="no")],
        enabled=False,
        note="candidate only — not on the operator's 8/6 rule list"),
]
