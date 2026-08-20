"""Research hop tests (2026-08-20).

Run: python test_research.py  (sqlite; anthropic + send_nudge mocked)

Claims under test: an extracted ask with a verified quote becomes a
request row and starts the hop; a fabricated quote is rejected
loudly; the ledger dedupes re-reports (analyze re-reads the whole
transcript every turn); the hop delivers findings through send_nudge
and closes the row; a search that fails twice ends in an honest
failure message, never silence; open requests render into the
companion prompt block and disappear once done.
"""

import os
import tempfile
import types

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "jm"
os.environ["TUTOR_USER_PHONE"] = "+15550001111"
os.environ.pop("TWILIO_ACCOUNT_SID", None)

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_research.db")
db.init_db()

import analyze_turn  # noqa: E402
import research  # noqa: E402
import sms  # noqa: E402

PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


started = []
research.start_async = lambda rid: started.append(rid)

QUOTE = "based on your research of big law firm partnership"
db.save_sms_message("jm", "user",
                    f"I would like your take {QUOTE} please.", "in")

print("apply: verified ask becomes a row and starts the hop")
payload = {"research_request": {
    "question": "BigLaw partnership path for a lateral Special "
                "Counsel in equity derivatives",
    "evidence_quote": QUOTE}}
applied = analyze_turn._apply("jm", payload, "call1")
reqs = db.get_open_research_requests("jm")
check("row created", len(reqs) == 1)
check("hop started", started == [reqs[0]["id"]])
check("applied reported", any("research_request" in a for a in applied))

print("apply: fabricated quote rejected")
bad = {"research_request": {"question": "Anything",
                            "evidence_quote": "never said this"}}
analyze_turn._apply("jm", bad, "call2")
check("no second row", len(db.get_open_research_requests("jm")) == 1)

print("dedupe: re-report absorbed by the ledger")
analyze_turn._apply("jm", payload, "call3")
check("same ask does not re-fire",
      len(db.get_open_research_requests("jm")) == 1 and
      len(started) == 1)
check("different question, same quote also absorbed",
      db.create_research_request("jm", "reworded question",
                                 evidence_quote=QUOTE) is None)

print("prompt block renders open requests")
block = sms._research_block("jm")
check("question in block", "equity derivatives" in block)
check("never-deny line present", "never say you can't research" in block)

print("run: happy path delivers and closes")
rid = reqs[0]["id"]
nudges = []
sms.send_nudge = lambda u, ins: nudges.append((u, ins)) or "sent"


class _FakeResp:
    content = [types.SimpleNamespace(type="text",
                                     text="FINDINGS: counsel track "
                                          "stats (Am Law)")]


class _FakeClient:
    class messages:
        @staticmethod
        def create(**kw):
            return _FakeResp()


research.anthropic = types.SimpleNamespace(Anthropic=lambda: _FakeClient)
out = research.run(rid)
row = db.get_research_request(rid)
check("findings returned", out and "FINDINGS" in out)
check("row done with findings stored",
      row["status"] == "done" and "Am Law" in row["findings"])
check("delivered via nudge with findings embedded",
      len(nudges) == 1 and nudges[0][0] == "jm"
      and "FINDINGS" in nudges[0][1])
check("prompt block empty once done", sms._research_block("jm") == "")
check("closed request is a no-op", research.run(rid) is None
      and len(nudges) == 1)

print("run: double failure ends honest, never silent")
rid2 = db.create_research_request("jm", "doomed question")
calls = []


class _Boom:
    class messages:
        @staticmethod
        def create(**kw):
            calls.append(1)
            raise RuntimeError("search down")


research.anthropic = types.SimpleNamespace(Anthropic=lambda: _Boom)
research.run(rid2)
row2 = db.get_research_request(rid2)
check("retried once", len(calls) == 2)
check("marked failed", row2["status"] == "failed")
check("user told honestly", len(nudges) == 2
      and "honestly" in nudges[1][1])


print("only the LATEST message can fire research (operator directive)")
db.save_sms_message("jm", "user", "thanks, unrelated chit chat", "in")
before = len(db.get_open_research_requests("jm")) 
out = analyze_turn._apply("jm", {"research_request": {
    "question": "BigLaw partnership path, reworded once more",
    "evidence_quote": QUOTE}}, "call4")
check("old ask re-report dropped once a newer message exists",
      len(db.get_open_research_requests("jm")) == before
      and not any("research_request" in a for a in out))
db.save_sms_message("jm", "user",
                    "can you look up SMS marketing benchmarks?", "in")
analyze_turn._apply("jm", {"research_request": {
    "question": "SMS marketing engagement benchmarks",
    "evidence_quote": "look up SMS marketing benchmarks"}}, "call5")
check("ask in the latest message still fires",
      any(r["question"].startswith("SMS marketing")
          for r in db.get_open_research_requests("jm")))

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
