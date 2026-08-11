"""Operator tool: push rehearsal-produced v3 ledgers into production.

    CRON_SECRET=... python import_ledgers.py chrisyu2 \
        --dir ledger_backfill --track "회사 PDF" --dry-run
    (drop --dry-run to actually import)

The rehearsal replayed a user's full history offline and wrote the
ledgers as review documents (rehearsal_A_attempts / B_taught /
C_person / D_bank). After the operator approves them, this script is
the one door from those files into production — a single POST to
/debug/import-ledgers, which refuses duplicate track names so a
re-run cannot double the ledgers.

Field note: the rehearsal called the fourth answer signal "hedging";
production calls it self_confidence (헤징 collides with financial
hedging in this domain). The mapping happens here.

Read-only against local files; stdlib only.
"""

import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ.get("THEO_BASE", "https://www.learningtheo.com")


def load_json(path):
    text = open(path, encoding="utf-8").read()
    start = min(x for x in (text.find("["), text.find("{")) if x >= 0)
    return json.loads(text[start:max(text.rfind("]"),
                                     text.rfind("}")) + 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("user_id")
    ap.add_argument("--dir", default="ledger_backfill")
    ap.add_argument("--track", required=True,
                    help="track name, in the user's own words")
    ap.add_argument("--mode", default="drill")
    ap.add_argument("--authority", default="file_wins")
    ap.add_argument("--performance-stage", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    secret = os.environ.get("CRON_SECRET", "").strip()
    if not secret and not args.dry_run:
        sys.exit("CRON_SECRET required")

    d = args.dir
    attempts_raw = load_json(f"{d}/rehearsal_A_attempts.out.txt")
    taught_raw = load_json(f"{d}/rehearsal_B_taught.out.txt")
    person_raw = load_json(f"{d}/rehearsal_C_person.out.txt")
    bank_raw = load_json(f"{d}/rehearsal_D_bank.out.txt")

    def ts_of(row):
        date = (row.get("date") or "").strip()
        return f"{date}T12:00:00" if date else None

    payload = {
        "user_id": args.user_id,
        "track": {"name": args.track, "mode": args.mode,
                  "authority": args.authority,
                  "performance_stage": args.performance_stage},
        "items": [{"stem": it.get("stem", ""),
                   "anchor_type": "file_chunk",
                   "anchor_quote": it.get("anchor_quote", ""),
                   "section_hint": it.get("section_hint", ""),
                   "elements": it.get("elements", []),
                   "kind": it.get("kind", ""),
                   "est_difficulty": it.get("est_difficulty", 2)}
                  for it in bank_raw],
        "attempts": [{"source": a.get("source", "drill"),
                      "question": a.get("question", ""),
                      "answer_verbatim": a.get("answer_verbatim", ""),
                      "elements": a.get("elements", []),
                      "verdict": a.get("verdict", ""),
                      "self_confidence":
                          (a.get("hedging") or {}).get("confidence", ""),
                      "confidence_marker":
                          (a.get("hedging") or {}).get("marker", ""),
                      "note": a.get("note", ""), "ts": ts_of(a)}
                     for a in attempts_raw],
        "taught": [{"quote": t.get("quote", ""),
                    "teaching": t.get("teaching", ""),
                    "kind": t.get("kind", ""),
                    "conflict_flag": t.get("conflict_flag", ""),
                    "ts": ts_of(t)}
                   for t in taught_raw],
        "person_notes": [{"observation": p.get("observation", ""),
                          "evidence": p.get("evidence", ""),
                          "confidence": p.get("confidence", "low"),
                          "ts": ts_of(p)}
                         for p in person_raw],
    }

    unanchored = [it["stem"][:50] for it in payload["items"]
                  if not it["anchor_quote"].strip()]
    print(f"track: {args.track} ({args.mode}, {args.authority}) "
          f"→ {args.user_id}")
    print(f"items: {len(payload['items'])} "
          f"({len(unanchored)} unanchored — server will refuse these)")
    for s in unanchored:
        print(f"  ⚠️ no anchor: {s}")
    print(f"attempts: {len(payload['attempts'])}  "
          f"taught: {len(payload['taught'])}  "
          f"person_notes: {len(payload['person_notes'])}")

    if args.dry_run:
        print("(dry run — nothing sent)")
        return

    req = urllib.request.Request(
        f"{BASE}/debug/import-ledgers",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Cron-Secret": secret},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            print(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8')}")


if __name__ == "__main__":
    main()
