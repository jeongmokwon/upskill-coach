"""Life-track operations hop — the 2026-08-18 experiment.

One job: read the recent conversation of a tracks-enabled user and
emit track OPERATIONS (create / update / hold / retire / item ops),
which code validates and applies. This is the "SMS로 멀티 에이전트
생성" machine: the user's situation always changes, so this hop rides
EVERY inbound for enabled users, not just an onboarding window.

The two-zone boundary (agreed with the founder):
  Zone A — anything expressible as config/data changes freely here:
           track lifecycle, role wording, cadence params, donts,
           items and their evolving payload shapes.
  Zone B — a request that needs a machine that does not exist (a
           part_type outside PART_TYPES). The op is NOT applied;
           it is recorded as a new_part_requested event (founder
           notify), and the conversation layer tells the user
           honestly: 이건 우리가 만들어 가지고 와야 한다.

Contract discipline (홉 사이 계약): every op is code-validated —
part types against the registry, surfacing against its schema,
track ownership against the DB — and invalid ops are dropped
loudly, never silently absorbed.
"""

import json
import os

import anthropic

import db

MODEL = os.environ.get("TRACKS_MODEL", "claude-sonnet-4-5")

# ─── Parts registry ─────────────────────────────────────────────────
# The library of machines a track can run on. Derived from the
# 2026-08-18 rehearsal (10 tracks collapsed onto these). This list
# GROWS via build sessions — it is a discovery log, not a taxonomy;
# an unknown part is a normal event that routes to the founder.
PART_TYPES = {
    "capture_list": "받아적고 요청 시 돌려주는 리스트 (장보기, 전달사항)",
    "cadence": "주기 추적 — 마지막 수행일 + 임계 (stock up, 전화, 청소)",
    "owed_ledger": "갚아야 할 것 목록 (답장 밀린 사람들)",
    "event_watch": "다가오는 날짜 추적 (배송 예정일)",
    "research": "유저 취향/맥락 기반 리서치·추천 (장난감/책)",
    "companion": "생각 정리 상대 + 열린 스레드 장부 (일/커리어)",
}

SURFACING_KINDS = ("on_demand", "threshold", "slot", "event_date")

_VALID_TRACK_STATUS = ("active", "held", "retired")


def validate_surfacing(s):
    """Surfacing rule → error string or None. The conductor consumes
    these; a rule it cannot execute must not enter the DB."""
    if not isinstance(s, dict):
        return "surfacing must be an object"
    kind = s.get("kind")
    if kind not in SURFACING_KINDS:
        return f"unknown surfacing kind {kind!r}"
    if kind == "threshold":
        days = s.get("days")
        if not isinstance(days, (int, float)) or days <= 0:
            return "threshold surfacing needs days > 0"
    if kind == "slot" and s.get("slot") not in ("morning", "evening"):
        return "slot surfacing needs slot: morning|evening"
    return None


def validate_item_payload(part_type, payload):
    """Per-part MINIMUM contract — deliberately thin. Item shapes
    differ per track and evolve; code pins only what the part's
    machinery cannot run without."""
    if not isinstance(payload, dict):
        return "payload must be an object"
    need = {
        "capture_list": ("text",),
        "cadence": ("task",),          # last_done/interval live here too
        "owed_ledger": ("who",),
        "event_watch": ("what",),      # expected_date when known
        "research": (),
        "companion": ("thread",),
    }.get(part_type, ())
    for k in need:
        if not str(payload.get(k, "")).strip():
            return f"{part_type} item needs '{k}'"
    return None


# ─── The ops tool ───────────────────────────────────────────────────

_OPS_TOOL = {
    "name": "submit_track_ops",
    "description": "Report track operations the conversation has "
                   "actually agreed on. Empty list is the normal "
                   "output when nothing new was agreed.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ops": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string",
                               "enum": ["create_track", "update_track",
                                        "set_track_status", "add_item",
                                        "update_item", "resolve_item",
                                        "new_part_needed"]},
                        "evidence": {
                            "type": "string",
                            "description": "Verbatim user words that "
                                           "authorize this op."},
                        # create_track / new_part_needed
                        "name": {"type": "string"},
                        "role": {"type": "string"},
                        "part_type": {"type": "string"},
                        "surfacing": {"type": "object"},
                        "donts": {"type": "array",
                                  "items": {"type": "string"}},
                        "profile_facts": {"type": "array",
                                          "items": {"type": "string"}},
                        "cost_lane": {"type": "string",
                                      "enum": ["haiku", "sonnet"]},
                        "status": {"type": "string"},
                        "reason": {"type": "string"},
                        # update_track / set_track_status / item ops
                        "track_id": {"type": "integer"},
                        "fields": {"type": "object"},
                        "item_id": {"type": "integer"},
                        "kind": {"type": "string"},
                        "payload": {"type": "object"},
                    },
                    "required": ["op", "evidence"],
                },
            },
            "notes_for_operator": {"type": "string"},
        },
        "required": ["ops"],
    },
}

_SYSTEM = """You are the track-operations layer of an SMS life \
companion. You read the recent conversation and report which track \
operations the user and coach have ACTUALLY AGREED on — nothing \
speculative.

## The track model
A track is one ongoing concern the companion carries for the user \
(grocery list, nanny notes, call-parents cadence, their work). Each \
track runs on a PART — a machine this system already has:

{parts}

Surfacing rules (when the track may come up):
- on_demand: only when the user asks
- threshold: {{"kind":"threshold","days":N}} — raise when N days \
passed since last done
- slot: {{"kind":"slot","slot":"morning"|"evening"}} — belongs to a \
daily touchpoint
- event_date: raise on a tracked date (payload carries the date)

## Current tracks for this user (the state you diff against)
{current_tracks}

## Rules
1. ONLY emit ops the conversation agreed on. The user saying "좋아, \
그렇게 해줘" / "yes, set that up" over a concrete proposal IS \
agreement. A proposal the user has not yet answered is NOT.
2. Every op carries `evidence`: the user's verbatim words that \
authorize it. No evidence in the transcript → no op.
3. Diff against current state: do not re-create a track that already \
exists; prefer update_track / set_track_status on the existing id. \
Reviving a held track = set_track_status active.
4. An agreed concern that fits NO existing part: emit new_part_needed \
(name + role + why no part fits, in `reason`). Never force-fit the \
wrong part.
5. "지금은 만들지 말자" on a discussed track = create with \
status "held" (the deferred list is a ledger too).
6. Items: add_item when the user hands over content ("우유 떨어졌어" \
to a grocery track), update_item to patch payload, resolve_item when \
done. Payload keys are free-form beyond each part's minimum; carry \
what the conversation gives you.
7. Empty ops list is the normal output for most messages.

Today is {today}."""


def _current_tracks_block(user_id):
    rows = db.get_life_tracks(user_id, statuses=("active", "held"))
    if not rows:
        return "(none yet)"
    lines = []
    for t in rows:
        lines.append(
            f"- id={t['id']} [{t['status']}] {t['name']} "
            f"(part={t['part_type']}, surfacing={t['surfacing']}) — "
            f"{t['role']}")
        items = db.get_track_items(t["id"], status="open")
        for it in items[:10]:
            lines.append(f"    item id={it['id']} [{it['kind']}] "
                         f"{it['payload'][:120]}")
    return "\n".join(lines)


def run(user_id, trigger="inbound", client=None):
    """One ops pass. Never raises — this hop must not be able to
    break the reply path. Returns a summary dict or None."""
    try:
        if not db.tracks_lane_open(user_id):
            return None
        msgs = db.get_recent_sms_messages(user_id, limit=40)
        if not msgs:
            return None
        transcript = "\n".join(
            f"{'USER' if m['role'] == 'user' else 'COACH'}: {m['content']}"
            for m in msgs)
        from datetime import datetime
        system = _SYSTEM.format(
            parts="\n".join(f"- {k}: {v}" for k, v in PART_TYPES.items()),
            current_tracks=_current_tracks_block(user_id),
            today=datetime.now().strftime("%A, %Y-%m-%d"))
        messages = [{"role": "user",
                     "content": "## Conversation (oldest first)\n\n"
                                + transcript}]
        if client is None:
            client = anthropic.Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=1500, system=system,
            messages=messages, tools=[_OPS_TOOL],
            tool_choice={"type": "tool", "name": "submit_track_ops"})
        payload = next((b.input for b in resp.content
                        if getattr(b, "type", "") == "tool_use"), None)
        llm_call_id = db.save_llm_call(
            user_id, f"track_ops_{trigger}", MODEL, system, messages,
            prompt_versions={},
            response_text=json.dumps(payload, ensure_ascii=False)
            if payload else "")
        if not payload:
            return None
        applied, rejected = apply_ops(user_id, payload.get("ops") or [])
        if applied or rejected:
            db.log_event(user_id, "track_ops_applied",
                         {"trigger": trigger, "applied": applied,
                          "rejected": rejected,
                          "llm_call_id": llm_call_id},
                         source="tracks_ops")
            print(f"[TRACKS] {user_id}: applied={applied} "
                  f"rejected={rejected}", flush=True)
        return {"applied": applied, "rejected": rejected,
                "llm_call_id": llm_call_id}
    except Exception as e:
        print(f"[TRACKS] ⚠️ ops pass failed for {user_id}: {e}",
              flush=True)
        return None


def apply_ops(user_id, ops):
    """Validate and apply. Returns (applied, rejected) — each a list
    of short human-readable strings for the event log. Rejections are
    loud by design: a dropped op the operator never sees is how
    silent drift starts."""
    applied, rejected = [], []
    own = {t["id"]: t for t in db.get_life_tracks(
        user_id, statuses=("active", "held", "retired"))}
    names = {(t["name"] or "").strip().lower(): t for t in own.values()}

    for op in ops or []:
        kind = op.get("op", "")
        try:
            if kind == "create_track":
                err = _check_create(op, names)
                if err:
                    rejected.append(f"create_track: {err}")
                    if err.startswith("unknown part"):
                        _notify_new_part(user_id, op, err)
                    continue
                tid = db.create_life_track(
                    user_id, op["name"].strip(), (op.get("role") or "").strip(),
                    op["part_type"], surfacing=op.get("surfacing") or {},
                    donts=op.get("donts") or [],
                    profile_facts=op.get("profile_facts") or [],
                    cost_lane=op.get("cost_lane") or "",
                    status=op.get("status")
                    if op.get("status") in ("active", "held") else "active")
                t = {"id": tid, "name": op["name"].strip(),
                     "part_type": op["part_type"], "status": "active"}
                own[tid] = t
                names[op["name"].strip().lower()] = t
                applied.append(f"create:{op['name']}#{tid}")

            elif kind == "update_track":
                tid = op.get("track_id")
                if tid not in own:
                    rejected.append(f"update_track: not our track {tid}")
                    continue
                fields = op.get("fields") or {}
                if "surfacing" in fields:
                    err = validate_surfacing(fields["surfacing"])
                    if err:
                        rejected.append(f"update_track#{tid}: {err}")
                        continue
                if "part_type" in fields:
                    # Zone B: changing the machine is not a config edit.
                    rejected.append(
                        f"update_track#{tid}: part_type is immutable")
                    continue
                if db.update_track_config(tid, fields):
                    applied.append(
                        f"update:#{tid}:{','.join(sorted(fields))}")
                else:
                    rejected.append(f"update_track#{tid}: no-op")

            elif kind == "set_track_status":
                tid = op.get("track_id")
                if tid not in own:
                    rejected.append(f"set_status: not our track {tid}")
                    continue
                if op.get("status") not in _VALID_TRACK_STATUS:
                    rejected.append(
                        f"set_status#{tid}: bad {op.get('status')!r}")
                    continue
                db.set_track_status(tid, op["status"],
                                    reason=op.get("reason") or "")
                applied.append(f"status:#{tid}:{op['status']}")

            elif kind == "add_item":
                tid = op.get("track_id")
                t = own.get(tid)
                if not t:
                    rejected.append(f"add_item: not our track {tid}")
                    continue
                payload = op.get("payload") or {}
                err = validate_item_payload(t["part_type"], payload)
                if err:
                    rejected.append(f"add_item#{tid}: {err}")
                    continue
                iid = db.add_track_item(tid, user_id,
                                        op.get("kind") or t["part_type"],
                                        payload)
                applied.append(f"item:+#{tid}:{iid}")

            elif kind in ("update_item", "resolve_item"):
                iid = op.get("item_id")
                row = db.get_track_item(iid) if iid else None
                if not row or row["user_id"] != user_id:
                    rejected.append(f"{kind}: not our item {iid}")
                    continue
                db.update_track_item(
                    iid, payload=op.get("payload"),
                    status="resolved" if kind == "resolve_item" else None)
                applied.append(
                    f"item:{'✓' if kind == 'resolve_item' else '~'}{iid}")

            elif kind == "new_part_needed":
                _notify_new_part(user_id, op,
                                 op.get("reason") or "no part fits")
                applied.append(f"new_part:{op.get('name', '?')}")

            else:
                rejected.append(f"unknown op {kind!r}")
        except Exception as e:
            rejected.append(f"{kind}: {e}")
    return applied, rejected


def _check_create(op, names):
    name = (op.get("name") or "").strip()
    if not name:
        return "missing name"
    if name.lower() in names:
        return f"duplicate of existing track {names[name.lower()]['id']}"
    pt = op.get("part_type")
    if pt not in PART_TYPES:
        return f"unknown part {pt!r}"
    err = validate_surfacing(op.get("surfacing") or {})
    if err:
        return err
    return None


def _notify_new_part(user_id, op, reason):
    """Zone B detection — a normal event, not an accident. The
    founder-notify mechanism for the pilot is this event plus the
    operator's timeline reading; a push channel comes later."""
    db.log_event(user_id, "new_part_requested",
                 {"name": op.get("name") or "",
                  "role": op.get("role") or "",
                  "reason": reason[:300],
                  "evidence": (op.get("evidence") or "")[:200]},
                 source="tracks_ops")
    print(f"[TRACKS] 🔔 new part requested by {user_id}: "
          f"{op.get('name')!r} — {reason}", flush=True)
