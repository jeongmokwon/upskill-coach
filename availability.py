"""
Availability grid — exploration P5 (brief §7 "Availability grid").

A per-user day×hour grid, seeded by self-report during onboarding
and refined nightly by behaviour: reply hour-of-day and reply
latency are already in the event log, so "says 8pm, actually only
answers at 10pm" surfaces on its own.

DERIVED ARTIFACT DISCIPLINE (same as user notes): the events are the
truth, the grid is a rebuildable projection. Nothing here reads the
conversation, no LLM authors a cell — `recompute()` is pure
arithmetic over `user_schedule` + the event log, so re-running it
over the same events reproduces the same grid exactly. Snapshots are
append-only versions; a new version is written only when the grid
actually changed.

Grid shape (JSON-as-TEXT, `grid_json`):

    {"mon": {"20": 0.85, "21": 0.5}, "tue": {...}, ...}

— all seven day keys always present (mon…sun, the user's local
week), hour keys present only for cells with a non-zero score.
Scores are 0-1, rounded to 3 decimals so equality is stable.

`sources_json` keeps the per-cell contributions that produced the
score, so a human can see WHY a cell is high:

    {"cells": {"mon": {"20": {"self_report": 0.5, "observed": 0.35,
                              "replies": {"fast": 1, "mid": 0,
                                          "slow": 0, "spontaneous": 0},
                              "event_ids": [412]}}},
     "meta": {"window_start": ..., "window_end": ...,
              "self_report_version": 3,
              "self_report_windows": [{"start": "20:00", "end": "22:00"}],
              "observed_events": 9}}

Time convention: the codebase's fixed-offset TZ_OFFSET_HOURS (see
features.py / trace.py / annotate.py). No zoneinfo, deliberately —
one convention for the whole system.

This module is storage + computation only. It does NOT feed send
scheduling: `handle_schedule_tick` keeps using the agreed
`user_schedule`; letting observed behaviour move send times is a
later, operator-gated decision.
"""

import json
import os
from datetime import datetime, timedelta

import db

METHOD_VERSION = "v1"

TZ_OFFSET_H = int(os.environ.get("TZ_OFFSET_HOURS", "-8"))

DOWS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# ── Scoring rules (explicit, code-versioned — bump METHOD_VERSION
# whenever any of these change, so old snapshots stay interpretable) ──

OBSERVED_WINDOW_DAYS = 28   # how far back behaviour is read
ATTRIBUTION_MAX_H = 24      # a send older than this isn't "what they
                            # replied to" — the inbound counts as
                            # spontaneous instead

FAST_REPLY_MIN = 15         # replied within 15m of a send…
MID_REPLY_MIN = 120         # …within 2h…  (beyond that: slow)

# Evidence weight per inbound message. A fast reply says "I was
# available at this hour"; a 6-hour-old reply says more about the
# hour they READ it, so it counts, but much less. An unprompted
# inbound (no send to attribute it to) is a deliberate act at that
# hour, so it sits between fast and mid.
REPLY_WEIGHTS = {"fast": 1.0, "spontaneous": 0.7, "mid": 0.6, "slow": 0.3}

OBSERVED_SATURATION = 2.0   # summed weight that maxes out the
                            # observed contribution
OBSERVED_MAX = 0.7          # …which is worth this much of the score
SELF_REPORT_SCORE = 0.5     # an hour inside an agreed window


# ── helpers ──────────────────────────────────────────────────────────

def _local(ts_iso):
    """Server-naive ts → the user's local clock (fixed offset)."""
    return datetime.fromisoformat(ts_iso) + timedelta(hours=TZ_OFFSET_H)


def _empty_grid():
    return {d: {} for d in DOWS}


def _hours_of_window(w):
    """{"start": "20:00", "end": "22:00"} → [20, 21]. An hour is in
    the window when any part of it is; a window ending exactly on the
    hour does not claim that hour. Malformed windows yield []."""
    try:
        sh, sm = (int(x) for x in str(w["start"]).split(":")[:2])
        eh, em = (int(x) for x in str(w["end"]).split(":")[:2])
    except (KeyError, TypeError, ValueError):
        return []
    start = sh * 60 + sm
    end = eh * 60 + em
    if end <= start:
        end = 24 * 60          # "21:00–00:00" style: run to midnight
    first = start // 60
    last = (end - 1) // 60
    return [h for h in range(first, min(last, 23) + 1)]


def _latency_class(reply_ts, send_ts):
    """(inbound, preceding send) → fast | mid | slow | spontaneous."""
    if send_ts is None:
        return "spontaneous"
    minutes = (datetime.fromisoformat(reply_ts)
               - datetime.fromisoformat(send_ts)).total_seconds() / 60.0
    if minutes > ATTRIBUTION_MAX_H * 60:
        return "spontaneous"
    if minutes < FAST_REPLY_MIN:
        return "fast"
    if minutes < MID_REPLY_MIN:
        return "mid"
    return "slow"


def _blank_cell():
    return {"self_report": 0.0, "observed": 0.0,
            "replies": {"fast": 0, "mid": 0, "slow": 0, "spontaneous": 0},
            "event_ids": []}


# ── the computation ──────────────────────────────────────────────────

def recompute(user_id, now=None):
    """Rebuild this user's availability grid from scratch.

    Pure and deterministic: the same (schedule, events, now) always
    produces the same output, and nothing is written. Returns
        {"grid": {dow: {hour: score}}, "sources": {...}}
    `now` is injectable for tests — never call datetime.now() below
    this line."""
    now = now or datetime.now()
    window_end = now
    window_start = now - timedelta(days=OBSERVED_WINDOW_DAYS)

    cells = {}      # (dow, hour) → source dict

    def cell(dow, hour):
        return cells.setdefault((dow, hour), _blank_cell())

    # ── source 1: self-report (latest agreed windows) ───────────────
    sched = db.get_user_schedule(user_id)
    windows = []
    if sched:
        try:
            windows = json.loads(sched["windows_json"]) or []
        except (TypeError, ValueError):
            windows = []
    # user_schedule windows are per-day, not per-weekday: the same
    # agreed window seeds every day of the week.
    for w in windows:
        for hour in _hours_of_window(w):
            for dow in DOWS:
                cell(dow, hour)["self_report"] = SELF_REPORT_SCORE

    # ── source 2: observed behaviour (the event log) ────────────────
    # Read one extra day of history so an inbound near the window's
    # start can still find the send it answered.
    events = db.get_events_with_ids(
        user_id,
        (window_start - timedelta(days=1)).isoformat(),
        window_end.isoformat(), limit=5000)

    start_iso = window_start.isoformat()
    last_out_ts = None
    observed_events = 0
    for e in events:
        if e["kind"] == "sms_out":
            last_out_ts = e["ts"]
            continue
        if e["kind"] != "sms_in" or e["ts"] < start_iso:
            continue
        cls = _latency_class(e["ts"], last_out_ts)
        local = _local(e["ts"])
        c = cell(DOWS[local.weekday()], local.hour)
        c["observed"] += REPLY_WEIGHTS[cls]
        c["replies"][cls] += 1
        if len(c["event_ids"]) < 20:
            c["event_ids"].append(e["id"])
        observed_events += 1
        # An inbound consumes the send it answered: a second reply to
        # the same send is a follow-up the user chose to write, not
        # fresh evidence of a prompted answer.
        last_out_ts = None

    # ── combine ─────────────────────────────────────────────────────
    grid = _empty_grid()
    sources = {"cells": {d: {} for d in DOWS},
               "meta": {
                   "method_version": METHOD_VERSION,
                   "window_start": window_start.isoformat(),
                   "window_end": window_end.isoformat(),
                   "self_report_version": (sched or {}).get("version"),
                   "self_report_windows": windows,
                   "observed_events": observed_events,
               }}
    for (dow, hour), c in cells.items():
        observed = min(1.0, c["observed"] / OBSERVED_SATURATION) * OBSERVED_MAX
        score = round(min(1.0, c["self_report"] + observed), 3)
        if score <= 0:
            continue
        key = str(hour)
        grid[dow][key] = score
        sources["cells"][dow][key] = {
            "self_report": round(c["self_report"], 3),
            "observed": round(observed, 3),
            "observed_raw": round(c["observed"], 3),
            "replies": c["replies"],
            "event_ids": c["event_ids"],
        }
    return {"grid": grid, "sources": sources}


def _canonical(grid):
    """Stable serialization for change detection."""
    return json.dumps(grid, sort_keys=True, ensure_ascii=False)


def update(user_id, now=None):
    """Recompute and append a snapshot IFF the grid changed.

    Returns {"grid", "sources", "version", "changed", "changed_cells"}.
    A static grid costs no new row (and no event) per night."""
    result = recompute(user_id, now=now)
    latest = db.get_availability_snapshot(user_id)
    previous = {}
    if latest:
        try:
            previous = json.loads(latest["grid_json"])
        except (TypeError, ValueError):
            previous = {}
    changed_cells = _diff_cells(previous, result["grid"])
    if latest and not changed_cells:
        result.update({"version": latest["version"], "changed": False,
                       "changed_cells": []})
        return result
    version = db.save_availability_snapshot(
        user_id, result["grid"], result["sources"], METHOD_VERSION,
        changed_cells=changed_cells)
    result.update({"version": version, "changed": True,
                   "changed_cells": changed_cells})
    return result


def _diff_cells(before, after):
    """Cells whose score moved → ["mon 20: 0.5→0.85", ...] (capped)."""
    out = []
    for dow in DOWS:
        b, a = (before or {}).get(dow) or {}, (after or {}).get(dow) or {}
        for hour in sorted(set(b) | set(a), key=lambda h: int(h)):
            if b.get(hour, 0) != a.get(hour, 0):
                out.append(f"{dow} {int(hour):02d}: "
                           f"{b.get(hour, 0)}→{a.get(hour, 0)}")
    return out[:40]


# ── operator rendering ───────────────────────────────────────────────

def _marker(score):
    if score >= 0.67:
        return "#"
    if score >= 0.34:
        return "+"
    if score > 0:
        return "-"
    return "."


def render_grid(result):
    """Text grid: hours as rows, weekdays as columns.
    `#` strong · `+` moderate · `-` weak · `.` no signal."""
    grid = result["grid"]
    lines = ["      " + "  ".join(DOWS)]
    for hour in range(24):
        row = "  ".join(f" {_marker(grid[d].get(str(hour), 0))} "
                        for d in DOWS)
        lines.append(f"{hour:02d}:00 {row}")
    return "\n".join(lines)


def _fmt_hours(hours):
    """[20, 21, 22] → '20:00-23:00' (contiguous runs merged)."""
    if not hours:
        return "(none)"
    hours = sorted(hours)
    runs, start, prev = [], hours[0], hours[0]
    for h in hours[1:]:
        if h == prev + 1:
            prev = h
            continue
        runs.append((start, prev))
        start = prev = h
    runs.append((start, prev))
    return ", ".join(f"{a:02d}:00-{(b + 1) % 24:02d}:00" for a, b in runs)


def disagreement_line(result):
    """One line on self-report vs observed — the interesting finding.
    e.g. 'said 20:00-22:00, replies cluster at 22:00-00:00 (5 of 6
    replies outside the agreed window)'."""
    src = result["sources"]
    self_hours, obs = set(), {}
    for dow in DOWS:
        for hour, c in src["cells"][dow].items():
            if c["self_report"] > 0:
                self_hours.add(int(hour))
            n = sum(c["replies"].values())
            if n:
                obs[int(hour)] = obs.get(int(hour), 0) + n
    total = sum(obs.values())
    if not self_hours:
        if not obs:
            return "no self-reported window and no replies yet"
        return ("no self-reported window yet — observed only at "
                + _fmt_hours(sorted(obs)))
    if not total:
        return (f"said {_fmt_hours(sorted(self_hours))}; no replies "
                f"observed yet — nothing to compare")
    outside = sum(n for h, n in obs.items() if h not in self_hours)
    # "cluster" = the hours holding the bulk of the replies, busiest
    # first until we've covered most of them.
    ranked = sorted(obs.items(), key=lambda kv: (-kv[1], kv[0]))
    cluster, covered = [], 0
    for hour, n in ranked:
        cluster.append(hour)
        covered += n
        if covered >= total * 0.7:
            break
    said = _fmt_hours(sorted(self_hours))
    clustered = _fmt_hours(sorted(cluster))
    if outside > total / 2:
        return (f"⚠️ said {said}, replies cluster at {clustered} "
                f"({outside} of {total} replies outside the agreed window)")
    return (f"said {said}, replies cluster at {clustered} "
            f"({total - outside} of {total} replies inside the agreed "
            f"window — self-report holds)")


def render(user_id, result=None, now=None):
    """Full operator view: grid + disagreement + provenance."""
    result = result or recompute(user_id, now=now)
    meta = result["sources"]["meta"]
    snap = db.get_availability_snapshot(user_id)
    head = [f"# availability — {user_id}"]
    if snap:
        head.append(f"latest snapshot: v{snap['version']} at {snap['ts']} "
                    f"(method {snap['method_version']})")
    else:
        head.append("latest snapshot: (none stored yet — live recompute)")
    head.append(f"self-report: v{meta['self_report_version']} "
                f"{meta['self_report_windows'] or '(none)'}")
    head.append(f"observed: {meta['observed_events']} replies since "
                f"{meta['window_start'][:10]}")
    head.append("legend: # strong · + moderate · - weak · . no signal")
    head.append("")
    return "\n".join(head + [render_grid(result), "",
                             disagreement_line(result)])
