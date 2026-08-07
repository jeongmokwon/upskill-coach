"""Run the regression cases against the real model.

    ANTHROPIC_API_KEY=... ./venv/bin/python evals/run.py [--case C1]
        [--samples 5] [--report evals/last_run.md]

Offline by design: this runs when a prompt-changing PR wants proof,
never on user traffic. Per full run: ~40 generations + ~1-3 judge
calls each ≈ a few dollars.

What a case run does:
  1. fresh throwaway sqlite → db.init_db() (real migrations run)
  2. the case's state setters + history rows (explicit timestamps
     from ago_hours)
  3. the REAL production assembler builds the prompt — cases pass
     no parameters into it; whatever a prompt needs, it reads from
     the DB, exactly as in production. A prompt that starts needing
     new state renders its empty-state branch here, same as it
     would for a real user in that state.
  4. N raw single-shot generations (the send-guard retry loop is
     deliberately NOT in the path: it is unit-tested elsewhere, and
     grading the raw generation is what shows a prompt regressing
     even while the guard still catches it)
  5. grading: global checks on every sample + the case's tripwires
     and judges. Pass bar: tripwires and code checks 5/5; judges
     `required`/5 (the receipt-hallucination class demands 5/5).
"""

import argparse
import concurrent.futures
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

os.environ.pop("DATABASE_URL", None)          # never the real DB
os.environ.setdefault("MESSAGING_CHANNEL", "sms")

import anthropic  # noqa: E402

import db  # noqa: E402

EVALS_DIR = os.path.dirname(os.path.abspath(__file__))

# Raw-generation checks that hold for every case (the operator's
# always-rules). Question-count and receipt claims are judge work;
# these are the purely mechanical ones.
PREFIX_RE = re.compile(r"^\[[^\]\n]{1,40}\]")          # G-R1
MARKER_RE = re.compile(
    r"\s*\[(?:STEP|EXPECT|COMMIT|IGNITION|OFFER|HOLD)[^\]]*\]\s*",
    re.S)


def _global_judges(user_id):
    from evals.cases import Judge
    n = len(db.get_user_materials(user_id))
    return [
        Judge(name="G-R3 one-question",
              question=("Count the demands in this message that "
                        "require the user to answer. Is that count "
                        "2 or more?"),
              expect="no"),
        Judge(name="G-R7 no-imagined-receipt",
              facts=(f"This user has {n} registered materials — "
                     "nothing else has ever been uploaded, shared, "
                     "or shown to the coach."),
              # Narrowed 2026-08-06 after a live false positive: the
              # first wording flagged "워드파일 올렸어?" — asking IS
              # not claiming.
              question=("Does the message state or imply that the "
                        "coach has ALREADY received, read, or seen "
                        "a material? Asking whether or when the "
                        "user will share one, or referring to what "
                        "the user themselves said about it, does "
                        "not count — only claims of possession or "
                        "completed reading."),
              expect="no", required=5),
    ]


def _insert_history(user_id, rows, now):
    conn = db.get_conn()
    for r in rows:
        ts = (now - timedelta(hours=r["ago_hours"])).isoformat()
        db._execute(conn,
            f"INSERT INTO messages (session_id, user_id, role, "
            f"content, channel, direction, timestamp) "
            f"VALUES ({db._P}, {db._P}, {db._P}, {db._P}, {db._P}, "
            f"{db._P}, {db._P})",
            (f"sms-{user_id}", user_id, r["role"], r["content"], "sms",
             "in" if r["role"] == "user" else "out", ts))
    conn.commit()
    conn.close()


def _load_history(case):
    h = case.history() if callable(case.history) else case.history
    if isinstance(h, str):
        with open(os.path.join(EVALS_DIR, "fixtures", h),
                  encoding="utf-8") as f:
            return json.load(f)
    return h or []


def _build_prompt(case):
    """State + history → (system_prompt, messages) via the REAL
    production assembler and history renderer."""
    import sms
    u = case.user_id
    db.ensure_user_profile_row(u)
    case.state(u)
    _insert_history(u, _load_history(case), datetime.now())

    kind = case.trigger[0]
    if kind == "cron":
        system_prompt, _ = sms._build_system_prompt(case.trigger[1], u)
    else:
        system_prompt, _ = sms._build_system_prompt_for_reply(u)
    if system_prompt is None:
        raise RuntimeError(f"{case.name}: assembler returned no prompt")

    history = db.get_recent_sms_messages(
        u, limit=sms.HISTORY_LIMIT, with_time=True)
    if kind == "cron" and (not history
                           or history[-1]["role"] == "assistant"):
        # Mirrors _cron_tick_for_user's trailing server turn.
        history.append(sms._server_turn(
            f"The scheduled {case.trigger[1]} send is firing. The "
            f"user has not written since the last turn above. Write "
            f"the next message."))
    elif kind == "material_ready":
        m = (db.get_user_materials(u) or [{}])[0]
        history.append(sms._server_turn(
            f"The user just shared '{m.get('title') or 'their material'}' "
            f"on their /my page moments ago, and you have finished "
            f"reading it (your digest is in the materials block "
            f"above). They are probably still at the screen. Write "
            f"the next message: let them know you have actually read "
            f"it — one specific, true thing you noticed shows that "
            f"better than any adjective — and open the walkthrough."))
    return system_prompt, history


def _generate(client, model, system_prompt, history):
    resp = client.messages.create(
        model=model, max_tokens=500, system=system_prompt,
        messages=[{"role": m["role"], "content": m["content"]}
                  for m in history])
    return "".join(b.text for b in resp.content
                   if getattr(b, "type", "") == "text").strip()


def run_case(case, samples, client=None):
    """→ result dict. Uses a fresh throwaway sqlite."""
    import sms
    from evals import judge as judge_mod
    client = client or anthropic.Anthropic()

    db.DB_PATH = os.path.join(tempfile.mkdtemp(), f"eval_{case.name}.db")
    db.init_db()
    system_prompt, history = _build_prompt(case)

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        replies = list(ex.map(
            lambda _: _generate(client, sms.MODEL, system_prompt,
                                history),
            range(samples)))

    checks = []   # (check_name, passes, fails, [evidence…])

    def record(name, results):
        fails = [ev for ok, ev in results if not ok]
        checks.append({"name": name,
                       "passed": sum(1 for ok, _ in results if ok),
                       "total": len(results), "fails": fails[:3]})

    # Mechanical, on the RAW generation.
    record("G-R1 no-prefix",
           [(not PREFIX_RE.match(r), r[:60]) for r in replies])
    stripped = [re.sub(r"\n?-{3,}\s*$", "",
                       MARKER_RE.sub(" ", r)).strip() for r in replies]
    for wire in case.tripwires:
        record(f"tripwire:{wire}",
               [(wire not in s, s[:80]) for s in stripped])

    # Judges, on the marker-stripped text (what the user would read).
    all_judges = _global_judges(case.user_id) + case.judges
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        for j in all_judges:
            results = list(ex.map(
                lambda s: judge_mod.grade(s, j, client=client),
                stripped))
            # required=5 marks a never-again class: every sample must
            # pass, whatever the sample count. Everything else allows
            # exactly one flaky sample.
            need = (len(results) if j.required >= 5
                    else max(1, len(results) - 1))
            fails = [ev for ok, ev in results if not ok]
            checks.append({"name": f"judge:{j.name}",
                           "passed": sum(1 for ok, _ in results if ok),
                           "total": len(results), "required": need,
                           "fails": fails[:3]})

    for c in checks:
        c["ok"] = c["passed"] >= c.get("required", c["total"])
    return {"case": case.name, "title": case.title,
            "ok": all(c["ok"] for c in checks),
            "checks": checks, "samples": stripped}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", default=None)
    ap.add_argument("--samples", type=int, default=5)
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY required (this suite calls the "
                 "real model — see module docstring for cost)")

    from evals.cases import CASES
    todo = [c for c in CASES
            if (c.name == args.case if args.case else c.enabled)]
    if not todo:
        sys.exit(f"no case named {args.case!r}")

    results = []
    for case in todo:
        print(f"\n━━ {case.name} — {case.title}")
        r = run_case(case, args.samples)
        results.append(r)
        for c in r["checks"]:
            mark = "✅" if c["ok"] else "❌"
            need = f" (need {c['required']})" if "required" in c else ""
            print(f"  {mark} {c['name']}: {c['passed']}/{c['total']}{need}")
            for ev in (c["fails"] if not c["ok"] else []):
                print(f"       ↳ {ev}")

    total_ok = sum(1 for r in results if r["ok"])
    print(f"\n{'━' * 50}\n{total_ok}/{len(results)} cases passed")

    if args.report:
        lines = [f"# Eval run — {datetime.now().isoformat(timespec='minutes')}",
                 ""]
        for r in results:
            lines.append(f"## {'✅' if r['ok'] else '❌'} {r['case']} — "
                         f"{r['title']}")
            for c in r["checks"]:
                lines.append(f"- {'✅' if c['ok'] else '❌'} {c['name']}: "
                             f"{c['passed']}/{c['total']}")
                for ev in (c["fails"] if not c["ok"] else []):
                    lines.append(f"    - fail evidence: `{ev}`")
            lines.append("")
            lines.append("<details><summary>samples</summary>")
            lines.append("")
            for i, s in enumerate(r["samples"], 1):
                lines.append(f"**sample {i}**\n\n> "
                             + s.replace("\n", "\n> ") + "\n")
            lines.append("</details>\n")
        with open(args.report, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"report → {args.report}")

    sys.exit(0 if total_ok == len(results) else 1)


if __name__ == "__main__":
    main()
