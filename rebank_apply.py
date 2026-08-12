"""Operator tool: apply a reviewed bank rebuild to production.

    CRON_SECRET=... python rebank_apply.py chrisyu2 --track-id 1 \
        --file ledger_backfill/rebank_items.json --dry-run
    (drop --dry-run to apply)

Reads the offline pass's reviewed output (seed items born from the
user's answered questions + newly mined items + needs_anchor holds),
maps seed references (A1..A9 = attempt import order = attempt ids
1..9) to attempt ids, and POSTs one /debug/rebank call: wipe the
track's current bank, insert the reviewed set, link the attempts.

Stdlib only; nothing is sent without the explicit non-dry-run flag.
"""

import argparse
import json
import os
import sys
import urllib.request

BASE = os.environ.get("THEO_BASE", "https://www.learningtheo.com")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("user_id")
    ap.add_argument("--track-id", type=int, required=True)
    ap.add_argument("--file", default="ledger_backfill/rebank_items.json")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    secret = os.environ.get("CRON_SECRET", "").strip()
    if not secret and not args.dry_run:
        sys.exit("CRON_SECRET required")

    d = json.load(open(args.file, encoding="utf-8"))

    def attempt_ids(refs):
        # "A3+A4" / ["A1"] → [3, 4] / [1]; import order = attempt id.
        out = []
        for r in refs:
            for part in r.split("+"):
                part = part.strip().lstrip("A")
                if part.isdigit():
                    out.append(int(part))
        return out

    items = []
    for it in d.get("seed", []) + d.get("needs_anchor_extra", []):
        refs = it.get("link_attempts") or [it.get("seed_of", "")]
        items.append({
            "stem": it["stem"], "anchor_type": "file_chunk",
            "anchor_quote": it.get("anchor_quote", ""),
            "section_hint": it.get("section_hint", ""),
            "elements": it.get("elements", []),
            "kind": it.get("kind", ""),
            "est_difficulty": it.get("est_difficulty", 2),
            "status": it.get("status", "untested"),
            "link_attempt_ids": attempt_ids(refs),
        })
    for it in d.get("mined", []):
        items.append({
            "stem": it["stem"], "anchor_type": "file_chunk",
            "anchor_quote": it.get("anchor_quote", ""),
            "section_hint": it.get("section_hint", ""),
            "elements": it.get("elements", []),
            "kind": it.get("kind", ""),
            "est_difficulty": it.get("est_difficulty", 2),
            "status": "untested",
        })

    payload = {"user_id": args.user_id, "track_id": args.track_id,
               "wipe": bool(d.get("wipe", True)), "items": items}

    by_status = {}
    for it in items:
        by_status[it["status"]] = by_status.get(it["status"], 0) + 1
    links = sum(len(it.get("link_attempt_ids") or []) for it in items)
    print(f"track {args.track_id} ({args.user_id}) — wipe: "
          f"{payload['wipe']}")
    print(f"items: {len(items)} {by_status} · attempt links: {links}")
    for it in items:
        if it["status"] == "needs_anchor":
            print(f"  [hold] {it['stem'][:70]}")

    if args.dry_run:
        print("(dry run — nothing sent)")
        return

    req = urllib.request.Request(
        f"{BASE}/debug/rebank",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-Cron-Secret": secret},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            print(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8')}")


if __name__ == "__main__":
    main()
