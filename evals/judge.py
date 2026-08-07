"""The facts-and-context-aware judge.

A deliberately narrow grader: it sees ONE generated coach message,
a few lines of injected facts, and ONE yes/no question. It never
sees the system prompt under test — so prompt changes cannot move
the goalposts — and it must quote its evidence, so a verdict is
checkable by a human who knows nothing about the product.

A sample that fails its expected answer is re-graded twice more and
settled by majority — one flaky vote must not fail a run.
"""

import json
import os

import anthropic

JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", "claude-sonnet-4-5")

_SYSTEM = """You are grading one message written by an AI coach to its user, against
one narrow yes/no question. You know nothing about the product and
must not guess beyond the message and the stated facts.

Answer with ONLY a JSON object:
{"answer": "yes" or "no", "evidence": "<the exact phrase from the
message your answer rests on, or 'none' if the answer rests on the
absence of something>"}

Output the JSON FIRST — no analysis before it. Judge only what the
question asks — not tone, not quality, not whether you would have
written it differently."""


def _one_vote(client, reply, judge):
    facts = f"Facts (true by construction):\n{judge.facts}\n\n" \
        if judge.facts else ""
    msg = (f"{facts}The coach's message:\n---\n{reply}\n---\n\n"
           f"Question: {judge.question}")
    # 400, not 200: verification-flavored questions tempt the grader
    # into analysis first, and a cap mid-analysis means no JSON at
    # all — which scores as a fail vote (observed: C6's digest check
    # dropped 5/5 → 2/5 purely on truncated grader output).
    resp = client.messages.create(
        model=JUDGE_MODEL, max_tokens=400, system=_SYSTEM,
        messages=[{"role": "user", "content": msg}])
    text = "".join(b.text for b in resp.content
                   if getattr(b, "type", "") == "text").strip()
    # The grader sometimes reasons in prose before/after the JSON;
    # hunt for the answer object itself rather than trusting the
    # outermost braces (observed: prose after the object made the
    # first-to-last-brace span unparseable → a spurious fail vote).
    import re as _re
    for m in _re.finditer(r"\{[^{}]*\"answer\"[^{}]*\}", text, _re.S):
        try:
            parsed = json.loads(m.group(0))
            return (str(parsed.get("answer", "")).strip().lower(),
                    str(parsed.get("evidence", ""))[:200])
        except Exception:
            continue
    return "unparseable", text[:200]


def grade(reply, judge, client=None):
    """→ (passed: bool, evidence: str). One vote; on failure, two
    more votes and majority rules."""
    client = client or anthropic.Anthropic()
    answer, evidence = _one_vote(client, reply, judge)
    if answer == judge.expect:
        return True, evidence
    votes = [(answer, evidence)]
    for _ in range(2):
        votes.append(_one_vote(client, reply, judge))
    passing = [v for v in votes if v[0] == judge.expect]
    if len(passing) >= 2:
        return True, passing[0][1]
    failing = [v for v in votes if v[0] != judge.expect]
    return False, failing[0][1]
