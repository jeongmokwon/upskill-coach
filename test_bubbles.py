"""Bubble-ordering tests for send_sms.

Run: ./venv/bin/python test_bubbles.py  (Twilio faked)

The claim under test: the second bubble of a '---' split is not
released until Twilio reports the first handed off to the carrier
(field report 2026-08-11: long Korean first bubbles, riding 4-6
UCS-2 segments, kept arriving AFTER their short second bubble).
"""

import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TZ_OFFSET_HOURS"] = "0"
os.environ["TWILIO_FROM_NUMBER"] = "+15550000001"
os.environ.pop("MESSAGING_CHANNEL", None)

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_bubbles.db")
db.init_db()

import sms  # noqa: E402

PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


timeline = []          # ordered record of sends and polls
sleeps = []


class FakeMsgHandle:
    def __init__(self, sid, statuses):
        self.sid, self._statuses = sid, statuses

    def fetch(self):
        timeline.append(("poll", self.sid))
        status = (self._statuses.pop(0) if len(self._statuses) > 1
                  else self._statuses[0])

        class _M:
            pass
        m = _M()
        m.status = status
        return m


class FakeTwilio:
    """First bubble reports 'queued' twice before 'sent' — the slow
    multi-segment case."""
    handles = {}

    class messages:
        _n = 0

        @classmethod
        def create(cls, from_=None, to=None, body=None):
            cls._n += 1
            sid = f"SM{cls._n}"
            timeline.append(("send", sid, body[:20]))
            FakeTwilio.handles[sid] = FakeMsgHandle(
                sid, ["queued", "queued", "sent"])

            class _R:
                pass
            r = _R()
            r.sid = sid
            return r

    def __call__(self, *a):
        pass


fake = FakeTwilio()
fake.messages = FakeTwilio.messages
# client.messages(sid) must return the handle; client.messages.create
# must create. Emulate twilio's dual interface with a shim:


class MessagesShim:
    def __call__(self, sid):
        return FakeTwilio.handles[sid]

    def create(self, **kw):
        return FakeTwilio.messages.create(**kw)


fake.messages = MessagesShim()
sms._twilio = lambda: fake
sms.time.sleep = lambda s: sleeps.append(s)

# ── two bubbles: second waits for first's handoff ───────────────────
print("1) ordering gate")
sid = sms.send_sms("+15550009999",
                   "긴 첫 버블 — 한국어 세그먼트 여러 개짜리\n---\nshort second",
                   user_id="bub")
sends = [t for t in timeline if t[0] == "send"]
polls = [t for t in timeline if t[0] == "poll"]
first_polls_done = all(
    timeline.index(p) < timeline.index(sends[1]) for p in polls)
check("two real sends, in order", len(sends) == 2 and sid == "SM2")
check("second bubble released only after first reports handoff "
      "(polled until 'sent', all polls before send #2)",
      len(polls) == 3 and first_polls_done)
check("settle gap still applied after handoff", 1.5 in sleeps)

# ── single message: no polling at all ───────────────────────────────
print("2) single bubble")
timeline.clear(); sleeps.clear()
sms.send_sms("+15550009999", "one bubble only", user_id="bub")
check("no split → no polls, no gap",
      len([t for t in timeline if t[0] == "poll"]) == 0 and not sleeps)

# ── edge separators shaved ──────────────────────────────────────────
print("2b) edge separators")
timeline.clear(); sleeps.clear()
sms.send_sms("+15550009999", "---\n\nack line\n---\nthe question",
             user_id="bub")
sends2b = [t for t in timeline if t[0] == "send"]
check("a LEADING '---' never reaches the user's first line "
      "(observed in the PR-A smoke run)",
      len(sends2b) == 2 and sends2b[0][2].startswith("ack line"))

# ── timeout: stuck first bubble doesn't block forever ───────────────
print("3) timeout")
timeline.clear(); sleeps.clear()


class StuckShim(MessagesShim):
    def __call__(self, sid):
        return FakeMsgHandle(sid, ["queued"])   # never advances


fake.messages = StuckShim()
real_time = sms.time.time
t = {"now": 1000.0}


def fake_time():
    t["now"] += 2.0        # each check advances the clock
    return t["now"]


sms.time.time = fake_time
sms.send_sms("+15550009999", "first\n---\nsecond", user_id="bub")
sms.time.time = real_time
check("stuck first bubble times out and the second still goes",
      len([x for x in timeline if x[0] == "send"]) == 2)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
