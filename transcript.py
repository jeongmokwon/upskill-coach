"""Operator tool: reconstruct a pilot user's full conversation as a
readable markdown document.

    CRON_SECRET=... python transcript.py chrisyu2
    → transcript_chrisyu2.md

Why reconstruction instead of a straight DB dump: this runs on the
operator's laptop, where the only door into production is the debug
HTTP endpoints — and /debug/timeline caps every payload at 300 chars.
So the timeline provides the SKELETON (exact per-message timestamps
and order), and texts the cap truncated are restored from the flight
recorder's per-call history windows (/debug/llm-call), which hold
every delivered message verbatim.

The flight recorder also holds what the user never saw: drafts the
send-guard rejected and the server's rewrite instructions. Those are
interleaved by call timestamp and labeled 내부 — for failure-case
review they are the most interesting rows in the file.

Read-only against production; no local DB, stdlib only.
"""

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request

BASE = os.environ.get("THEO_BASE",
                      "https://upskill-coach-dmmu.onrender.com")

MARKER_RE = re.compile(
    r"\s*\[(?:STEP|EXPECT|COMMIT|IGNITION|OFFER)[^\]]*\]\s*", re.S)
# Calls whose messages array IS the conversation thread. Everything
# else (analysis_*, material_digest, genplan_*, eyes_*) embeds the
# thread in other shapes and would corrupt the pool.
REPLY_TRIGGERS = ("cron_", "inbound_reply", "material_ready", "sms")


def fetch(path, **params):
    params["secret"] = os.environ["CRON_SECRET"]
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8")


def norm(s):
    """Content identity: ignore the per-render time prefixes and
    bubble separators that vary between recordings of one message."""
    s = re.sub(r"^\[[^\]\n]{1,40}\]\s*", "", (s or "").strip())
    s = re.sub(r"^-{3,}\s*$", "", s, flags=re.M)
    return re.sub(r"\s+", " ", s).strip()


def clean(s):
    s = MARKER_RE.sub(" ", s).strip()
    return re.sub(r"\n?-{3,}\s*$", "", s).strip()


def flatten_content(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c
                         if isinstance(b, dict))
    return str(c)


def parse_timeline(text):
    """→ (skeleton, all_call_ids, delivered_call_ids).
    Skeleton rows: {role, content, ts, partial} for every sms_in/out
    event; `partial` marks texts the 300-char payload cap cut."""
    skeleton, call_ids, delivered = [], [], set()
    for ln in text.splitlines():
        m = re.match(r"(\S+)\s+\[[^\]]+\]\s+(\w+)\s+(.*)", ln.strip())
        if not m:
            continue
        ts, kind, payload = m.groups()
        for cid in re.findall(r'"llm_call_id": "([a-f0-9]+)"', payload):
            call_ids.append(cid)
            if kind == "sms_out":
                delivered.add(cid)
        if kind not in ("sms_in", "sms_out"):
            continue
        role = "user" if kind == "sms_in" else "assistant"
        tm = re.search(r'"text": "((?:[^"\\]|\\.)*)"', payload)
        if tm:
            skeleton.append({"role": role, "ts": ts, "partial": False,
                             "content": json.loads(f'"{tm.group(1)}"')})
        else:
            pm = re.search(r'"text": "((?:[^"\\]|\\.)*)', payload)
            prefix = json.loads(f'"{pm.group(1)}"') if pm else ""
            skeleton.append({"role": role, "ts": ts, "partial": True,
                             "content": prefix})
    return skeleton, list(dict.fromkeys(call_ids)), delivered


def parse_call(raw, user_id):
    m = re.match(r"# llm_call (\S+) — user=(\S+) ts=(\S+)", raw)
    if not m or m.group(2) != user_id:
        return None
    tm = re.search(r"^# trigger=(\S+)", raw, re.M)
    if not (tm and tm.group(1).startswith(REPLY_TRIGGERS)):
        return None
    mm = re.search(r"═+ MESSAGES \(as sent\) ═+\n\n(.*?)\n\n═+ RESPONSE",
                   raw, re.S)
    rr = re.search(r"═+ RESPONSE[^\n]*\n\n(.*)$", raw, re.S)
    return {"id": m.group(1), "ts": m.group(3),
            "window": json.loads(mm.group(1)) if mm else [],
            "response": rr.group(1).strip() if rr else ""}


def build_pool(calls, delivered):
    """Every text the flight recorder holds: delivered history plus
    internal items (guard-rejected drafts, server instructions)."""
    pool = []
    for c in calls:
        for m in c["window"]:
            body = flatten_content(m["content"])
            internal = ("<server_instruction>" in body
                        or (m["role"] == "assistant"
                            and bool(MARKER_RE.search(body))))
            pool.append({"role": m["role"], "content": clean(body),
                         "internal": internal, "ts": c["ts"]})
        if c["response"]:
            pool.append({"role": "assistant",
                         "content": clean(c["response"]),
                         "internal": c["id"] not in delivered,
                         "ts": c["ts"]})
    return pool


def render(user_id, merged):
    lines = [f"# {user_id} 전체 대화 (UTC 시각)", "",
             "(전송된 메시지는 이벤트 로그의 실제 타임스탬프 순. "
             "*내부* 항목은 유저에게 보이지 않은 것 — 가드에 걸린 "
             "초안과 서버 지시.)", ""]
    day = ""
    for e in merged:
        if e["ts"][:10] != day:
            day = e["ts"][:10]
            lines += [f"## {day}", ""]
        if e["internal"] and e["content"].lstrip().startswith("[HOLD"):
            who = "*Theo — 홀드 판단(안 보내기로 함), 내부*"
        elif e["internal"] and e["role"] == "assistant":
            who = "*Theo — 내부 초안, 전송 안 됨*"
        elif e["internal"]:
            who = "*서버 지시 — 내부*"
        elif e["role"] == "assistant":
            who = "**Theo**"
        else:
            who = "**유저**"
        flag = "  ⚠️(잘림-미복원)" if e.get("partial") else ""
        lines.append(f"{who}  `{e['ts'][11:16]}Z`{flag}:")
        lines += [f"> {ln}" if ln.strip() else ">"
                  for ln in e["content"].strip().split("\n")]
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Reconstruct a user's conversation as markdown.")
    ap.add_argument("user_id")
    ap.add_argument("-o", "--out", default=None,
                    help="output path (default transcript_<user>.md)")
    args = ap.parse_args()
    if not os.environ.get("CRON_SECRET"):
        sys.exit("CRON_SECRET env var required")

    user_id = args.user_id
    skeleton, call_ids, delivered = parse_timeline(
        fetch("/debug/timeline", user_id=user_id, limit=1000, full=1))
    if not skeleton:
        sys.exit(f"no sms_in/sms_out events for {user_id}")

    calls = []
    for cid in call_ids:
        try:
            c = parse_call(fetch("/debug/llm-call", id=cid), user_id)
        except Exception as e:
            print(f"  ⚠️ call {cid} fetch failed: {e}", file=sys.stderr)
            c = None
        if c:
            calls.append(c)
    calls.sort(key=lambda c: c["ts"])
    pool = build_pool(calls, delivered)

    # Restore texts the payload cap truncated (match by prefix).
    unrestored = 0
    for e in skeleton:
        if not e["partial"]:
            continue
        p = norm(e["content"]).rstrip("…").strip()
        for item in pool:
            if item["role"] == e["role"] \
                    and norm(item["content"]).startswith(p[:60]):
                e["content"], e["partial"] = item["content"], False
                break
        else:
            unrestored += 1

    # Internal items, deduped, minus anything actually delivered.
    delivered_norms = {(e["role"], norm(e["content"])) for e in skeleton}
    seen, internals = set(), []
    for item in pool:
        key = (item["role"], norm(item["content"]))
        if (not item["internal"] or key in seen
                or key in delivered_norms or not key[1]):
            continue
        seen.add(key)
        internals.append(item)

    merged = sorted(
        [dict(e, internal=False) for e in skeleton] + internals,
        key=lambda e: e["ts"])
    out = args.out or f"transcript_{user_id}.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(render(user_id, merged))
    print(f"{user_id}: {len(skeleton)} delivered + {len(internals)} "
          f"internal ({unrestored} unrestored) → {out}")


if __name__ == "__main__":
    main()
