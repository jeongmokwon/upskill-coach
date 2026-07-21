"""
Decision hook — WEEK1_ORDER T3, brief §4.4.

Every intervention decision point passes through decide(). Today the
variation width is zero (weights make the first option certain), but
the choice still flows through the full sampling mechanism and every
call is logged with its inputs — because causal readability cannot
be retrofitted. When we later want to A/B an intervention (send vs
hold, tone A vs tone B), we widen the weights at ONE call site and
the logging/joining machinery is already in place.

The returned decision_id is the join key: attach it to whatever
events the decision caused (sms_out, phase_transition, ...) so
[state + intervention + outcome] triples can be assembled later.

Deliberately NOT here (brief §2 non-goals): any learning loop. The
weights are code/operator-set, never fitted.
"""

import random
import uuid

import db


def decide(decision_point, user_id, options, weights=None, context=None):
    """Sample one option at an intervention decision point.

    decision_point: stable string name, e.g. "evening_fire",
        "commit_marker_accept". Treat these like event kinds — rename
        only with care, they are analysis keys.
    options: list of option names (strings). First option is the
        current-policy default.
    weights: relative weights, same length as options. None → the
        default option is certain (width 0): [1, 0, 0, ...].
    context: small dict of decision inputs worth keeping alongside
        (phase, slot, staleness...). Logged verbatim — keep it small;
        heavyweight state lives in the surrounding events anyway.

    Returns (choice, decision_id). Never raises: on any internal
    failure it falls back to the default option with a decision_id
    so callers keep working and the failure itself is visible in
    the log.
    """
    decision_id = uuid.uuid4().hex[:12]
    if weights is None:
        weights = [1] + [0] * (len(options) - 1)
    try:
        choice = random.choices(options, weights=weights, k=1)[0]
    except Exception as e:
        choice = options[0]
        print(f"[POLICY] ⚠️ sampling failed at {decision_point}: {e} — "
              f"falling back to default", flush=True)

    db.log_event(user_id, "decision", {
        "decision_id": decision_id,
        "point": decision_point,
        "options": options,
        "weights": weights,
        "choice": choice,
        "context": context or {},
    }, source="policy")

    return choice, decision_id
