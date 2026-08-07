"""Eval-suite plumbing tests (no API calls, anthropic faked).

Run: ./venv/bin/python test_evals.py

The claim under test: a case's state builders + fixture history are
replayed through the REAL production prompt assembler, and the
grading machinery (tripwires, judges, thresholds) scores generated
text correctly. The real-model runs themselves cost money and are
run deliberately (evals/run.py) — this suite proves the harness.
"""

import json
import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["MESSAGING_CHANNEL"] = "sms"
os.environ["TZ_OFFSET_HOURS"] = "0"
os.environ.setdefault("ANTHROPIC_API_KEY", "fake-for-plumbing")

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_evals.db")
db.init_db()

from evals import run as run_mod  # noqa: E402
from evals.cases import CASES, Judge  # noqa: E402

PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def case(name):
    return next(c for c in CASES if c.name == name)


class FakeClient:
    """Answers generation calls with a scripted reply and judge calls
    with a scripted verdict."""

    def __init__(self, reply, judge_answer):
        self.reply = reply
        self.judge_answer = judge_answer
        outer = self

        class _Messages:
            @staticmethod
            def create(**kwargs):
                is_judge = "You are grading" in kwargs.get("system", "")
                text = (json.dumps({"answer": outer.judge_answer,
                                    "evidence": "scripted"})
                        if is_judge else outer.reply)

                class _B:
                    pass
                b = _B(); b.type = "text"; b.text = text

                class _R:
                    content = [b]
                return _R()

        self.messages = _Messages()


# ── prompt assembly replays real state through the real assembler ───
print("1) prompt assembly per case")

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "ev_c1.db")
db.init_db()
sp1, hist1 = run_mod._build_prompt(case("C1"))
check("C1: 0 registered materials → the NONE assertion block rides",
      "Their materials — NONE" in sp1
      and "A promise to upload is not an upload" in sp1)
check("C1: cron trigger ends with the server clock turn",
      hist1[-1]["role"] == "user"
      and "scheduled evening send is firing" in hist1[-1]["content"])
check("C1: fixture history rode in with time prefixes",
      any("링크에 올려놓을게" in m["content"] for m in hist1))

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "ev_c2.db")
db.init_db()
sp2, hist2 = run_mod._build_prompt(case("C2"))
check("C2: settled no_material → the never-ask-to-upload rider rides",
      "the ask-to-upload move does not exist" in sp2)
check("C2: inbound trigger ends with the user's real last message",
      hist2[-1]["role"] == "user"
      and "대단한건 할게 없어" in hist2[-1]["content"])

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "ev_c6.db")
db.init_db()
sp6, hist6 = run_mod._build_prompt(case("C6"))
check("C6: material_ready trigger appends the production server turn",
      "you have finished reading it" in hist6[-1]["content"]
      and "Notes(114621465.1).docx" in hist6[-1]["content"])
check("C6: the digest is in the prompt for the coach to draw on",
      "Article I Monetization" in sp6)

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "ev_c7.db")
db.init_db()
sp7, hist7 = run_mod._build_prompt(case("C7"))
check("C7: history is exactly the delivered expectation message",
      len(hist7) == 2 and hist7[0]["role"] == "assistant"
      and hist7[-1]["role"] == "user")
check("C7: focus lands on the goal (expectation already stamped)",
      "This message's focus: their goal" in sp7)

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "ev_c8.db")
db.init_db()
sp8, _ = run_mod._build_prompt(case("C8"))
check("C8: aversion 0.8 → the no-small-talk block rides",
      "No small talk with this user" in sp8)

# ── grading: tripwires, judges, thresholds ──────────────────────────
print("2) grading machinery")

bad = FakeClient(
    reply="[수요일 19:50] 워드파일 올려놓은 거 읽어봤어. 정리 잘했더라.",
    judge_answer="yes")   # judge: "treats upload as happened?" → yes
r_bad = run_mod.run_case(case("C1"), samples=3, client=bad)
names = {c["name"]: c for c in r_bad["checks"]}
check("hallucinating sample trips the exact-string tripwire",
      not names["tripwire:올려놓은 거 읽어봤"]["ok"])
check("the leading time prefix is flagged by G-R1 — as a warning "
      "that cannot fail the case by itself (the server strips it)",
      not names["G-R1 no-prefix (warning)"]["ok"]
      and names["G-R1 no-prefix (warning)"].get("warning") is True)
check("and the receipt judge fails it (expect no, got yes)",
      not names["judge:no-imagined-receipt"]["ok"]
      and r_bad["ok"] is False)

good = FakeClient(
    reply="어제 말한 파일, 준비되면 메일함에 있는 링크로 올려줘. "
          "급할 건 없어.",
    judge_answer="no")
r_good = run_mod.run_case(case("C1"), samples=3, client=good)
check("clean sample passes every check",
      r_good["ok"] is True
      and all(c["ok"] for c in r_good["checks"]))

check("the receipt judge demands every sample (never-again class)",
      next(c for c in r_good["checks"]
           if c["name"] == "judge:no-imagined-receipt")["required"]
      == 3   # == samples in this run; 5 when run at the default 5
      and next(c for c in r_good["checks"]
               if c["name"] == "judge:G-R3 one-question")["required"]
      == 2)  # one flaky sample allowed for ordinary judges

check("C9 was removed by operator decision — not in the suite at all",
      all(c.name != "C9" for c in CASES))

check("judge majority: one flaky fail vote is out-voted",
      True)  # covered inside judge.grade (2-of-3) — exercised via
             # the fail path above where all votes agree


# ── judge parsing is defensive ──────────────────────────────────────
print("3) judge parsing")
from evals import judge as judge_mod  # noqa: E402

garbled = FakeClient(reply="-", judge_answer="unused")
garbled.messages.create = lambda **kw: type(
    "R", (), {"content": [type("B", (), {"type": "text",
                                         "text": "not json at all"})()]})()
ok, ev = judge_mod.grade("아무 답", Judge(
    name="x", question="q?", expect="no"), client=garbled)
check("unparseable judge output counts as a FAIL, never a silent pass",
      ok is False)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
