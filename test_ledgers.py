"""v3 ledger tests (C-layer step 1 — tables and discipline only).

Run: ./venv/bin/python test_ledgers.py  (sqlite)

The claims under test: anchored items cannot exist without their
anchor; predictions are write-once-then-score-once; suspended items
leave circulation by one status change; the whole set wipes with
reset_user.
"""

import json
import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_ledgers.db")
db.init_db()

U = "ledgeruser"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


# ── tracks ──────────────────────────────────────────────────────────
print("1) tracks")
db.ensure_user_profile_row(U)
t1 = db.create_track(U, "회사 PDF", mode="drill", authority="file_wins",
                     performance_stage="동료 질문")
check("a track exists with its mode and truth-authority",
      db.get_tracks(U)[0]["name"] == "회사 PDF"
      and db.get_tracks(U, mode="drill")[0]["id"] == t1
      and db.get_tracks(U, mode="companion") == [])

# ── items: no anchor, no existence ──────────────────────────────────
print("2) items")
try:
    db.add_knowledge_item(t1, U, stem="x", anchor_type="file_chunk")
    check("anchored item without a quote is refused", False)
except ValueError:
    check("anchored item without a quote is refused", True)
i1 = db.add_knowledge_item(
    t1, U, stem="Rule 144 volume condition의 완전한 계산",
    anchor_type="file_chunk",
    anchor_quote="greater of 1% of outstanding or average weekly",
    elements=["greater-of 구조", "1% outstanding", "4주 평균",
              "자기 거래량 제외"],
    kind="numeric_comparison", est_difficulty=3)
i2 = db.add_knowledge_item(
    t1, U, stem="canonical fallback item", anchor_type="canonical",
    kind="concept")
check("items load with parsed elements, canonical needs no quote",
      len(db.get_knowledge_items(t1)) == 2
      and db.get_knowledge_items(t1)[0]["elements"][1] == "1% outstanding")
db.set_item_status(i2, "suspended")
check("one status change retires an item ('이건 그만 물어봐')",
      [r["id"] for r in db.get_knowledge_items(t1, status="untested")]
      == [i1])

# ── attempts (오답노트) ─────────────────────────────────────────────
print("3) attempts")
a1 = db.record_attempt(
    t1, U, verdict="partial", item_id=i1,
    question="dealer's Rule 144 requirements?",
    answer_verbatim="manner of sale (f)/(g), volume (e). "
                    "I am not sure about the third one.",
    elements=[{"name": "manner of sale", "verdict": "hit"},
              {"name": "volume", "verdict": "hit"},
              {"name": "holding period", "verdict": "miss"}],
    self_confidence="low",
    confidence_marker="I am not sure about the third one")
real = db.record_attempt(
    t1, U, verdict="missed", source="real_world",
    answer_verbatim="(동료에게 설명하다 volume condition을 빠뜨림)")
rows = db.get_attempts(t1)
check("drill and real_world attempts both land, elements parsed",
      len(rows) == 2
      and any(r["source"] == "real_world" for r in rows)
      and any(e["verdict"] == "miss" for r in rows
              for e in r.get("elements", [])))

# ── taught ledger ───────────────────────────────────────────────────
print("4) taught ledger")
db.add_taught(t1, U,
              quote="Rule 102(d)(1) is an exception for securities "
                    "exceeding the ADTV threshold",
              teaching="102(d)(1)=ADTV exception, not a hedging carveout",
              kind="correction_of_coach")
db.add_taught(t1, U, quote="3% of outstanding",
              teaching="(가짜 예시) 3% comparison",
              conflict_flag="file says 1% — raise in conversation")
taught = db.get_taught(t1)
check("teachings persist; conflicts carry their flag instead of "
      "silently absorbing",
      len(taught) == 2
      and any("1%" in (t["conflict_flag"] or "") for t in taught))

# ── person notes ────────────────────────────────────────────────────
print("5) person notes")
db.add_person_note(U, "모호한 설정을 만나면 사실관계부터 명료화한 뒤 "
                      "답한다 → 문제 설정을 정밀하게 줄 것",
                   evidence="To understand the question, ...",
                   confidence="high")
check("condition→response note with evidence lands",
      db.get_person_notes(U)[0]["confidence"] == "high")

# ── predictions: write-once, score-once ─────────────────────────────
print("6) predictions")
p1 = db.record_prediction(i1, U, "partial", 3, "비교형 둘째 가지 위험")
check("scoring once works and computes hit",
      db.score_prediction(p1, "partial") is True
      and db.prediction_stats(U) == {"scored": 1, "hits": 1,
                                     "accuracy": 1.0})
try:
    db.score_prediction(p1, "complete")
    check("re-scoring is refused (edited predictions make the KPI lie)",
          False)
except ValueError:
    check("re-scoring is refused (edited predictions make the KPI lie)",
          True)
p2 = db.record_prediction(i1, U, "complete", 2, "")
db.score_prediction(p2, "missed")
check("KPI aggregates across scored predictions",
      db.prediction_stats(U) == {"scored": 2, "hits": 1,
                                 "accuracy": 0.5})

# ── import endpoint ─────────────────────────────────────────────────
print("7) import endpoint")
import asyncio  # noqa: E402

os.environ["CRON_SECRET"] = "s3cret"
import coach  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402


def hit(path, body):
    req = make_mocked_request("POST", path)

    async def _json():
        return body
    req.json = _json

    async def go():
        return await coach._ledger_import_handler(req)
    return asyncio.run(go())


IMPORT_BODY = {
    "user_id": "importuser",
    "track": {"name": "회사 PDF", "mode": "drill",
              "authority": "file_wins"},
    "items": [
        {"stem": "good", "anchor_type": "file_chunk",
         "anchor_quote": "greater of 1%", "elements": ["a", "b"],
         "kind": "numeric_comparison", "est_difficulty": 3},
        {"stem": "bad — no anchor", "anchor_type": "file_chunk",
         "anchor_quote": ""},
    ],
    "attempts": [
        {"source": "drill", "question": "q", "answer_verbatim": "ans",
         "verdict": "partial", "self_confidence": "low",
         "confidence_marker": "I am not sure",
         "ts": "2026-07-20T12:00:00"},
    ],
    "taught": [{"quote": "Rule 102(d)(1)...", "teaching": "ADTV",
                "kind": "correction_of_coach",
                "ts": "2026-07-25T12:00:00"}],
    "person_notes": [{"observation": "obs", "evidence": "ev",
                      "confidence": "medium",
                      "ts": "2026-07-25T12:00:00"}],
}

r = hit("/debug/import-ledgers?secret=wrong", IMPORT_BODY)
check("wrong secret → 403, nothing written",
      r.status == 403 and db.get_tracks("importuser") == [])
r = hit("/debug/import-ledgers?secret=s3cret", IMPORT_BODY)
j = json.loads(r.text)
imported_track = db.get_tracks("importuser")[0]
check("import lands: track + counted rows, unanchored item refused "
      "and reported",
      r.status == 200 and j["counts"] == {"items": 1, "attempts": 1,
                                          "taught": 1,
                                          "person_notes": 1}
      and len(j["skipped"]) == 1 and "anchor" in j["skipped"][0]
      and imported_track["name"] == "회사 PDF")
check("backfilled ts survives (마치 원래 있었던 것처럼)",
      db.get_attempts(imported_track["id"])[0]["ts"]
      == "2026-07-20T12:00:00"
      and db.get_attempts(imported_track["id"])[0]["self_confidence"]
      == "low")
r = hit("/debug/import-ledgers?secret=s3cret", IMPORT_BODY)
check("re-run refused (409) — no doubled ledgers",
      r.status == 409 and len(db.get_tracks("importuser")) == 1)

# ── reset wipes the whole set ───────────────────────────────────────
print("8) reset scope")
db.reset_user(U)
check("reset_user wipes every ledger",
      db.get_tracks(U) == [] and db.get_person_notes(U) == []
      and db.prediction_stats(U)["scored"] == 0)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
