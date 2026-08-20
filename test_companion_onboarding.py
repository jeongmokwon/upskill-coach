"""Companion onboarding tests (2026-08-20, PR-1 of the pilot-onboarding
work).

Run: python test_companion_onboarding.py  (sqlite; email + Twilio mocked)

Claims under test: activation defaults to the companion lane (lane
flag set, first SMS sent at activation with the expectation copy
that ends in the opener question, English); ?lane=legacy preserves
the old behavior byte-for-byte (lane closed, no SMS at activation);
the expectation checklist item is stamped so the cron never re-sends
it; the companion prompt renders shared documents for grounding and
an integrity line when nothing is shared; material_ready fires for
companion users without the onboarding-focus gate.
"""

import asyncio
import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TUTOR_USER_ID"] = "jm"
os.environ["TUTOR_USER_PHONE"] = "+15550001111"
os.environ.pop("TWILIO_ACCOUNT_SID", None)
os.environ["CRON_SECRET"] = "sek"
os.environ["RESEND_API_KEY"] = "re_test"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_companion_onb.db")
db.init_db()

import coach  # noqa: E402
import emailer  # noqa: E402
import sms  # noqa: E402
from aiohttp.test_utils import make_mocked_request  # noqa: E402

PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


class FakeHTTP:
    def __init__(self, req, timeout=None):
        pass

    def __enter__(self):
        class R:
            @staticmethod
            def read():
                return b'{"id": "re_w"}'
        return R()

    def __exit__(self, *a):
        return False


emailer.urllib.request.urlopen = FakeHTTP


def hit(handler, path):
    async def go():
        return await handler(make_mocked_request("POST", path))
    return asyncio.run(go())


print("activation defaults to companion lane")
db.save_sms_signup("+15550008001", name="Dana", email="d@x.co",
                   consent_checkins=True)
sid = db.get_pending_signups()[0]["id"]
r = hit(coach._activate_handler,
        f"/debug/activate?secret=sek&signup_id={sid}&user_id=dana1")
check("activated", r.status == 200)
check("companion lane open", db.tracks_lane_open("dana1"))
msgs = db.get_recent_sms_messages("dana1", limit=5)
check("first SMS sent at activation",
      len(msgs) == 1 and msgs[0]["role"] == "assistant")
check("first SMS is the expectation copy ending in the opener",
      "I'm Theo" in msgs[0]["content"]
      and "Start anywhere" in msgs[0]["content"])
check("copy is English (no Korean, no placeholder)",
      "PLACEHOLDER" not in msgs[0]["content"]
      and not any("가" <= ch <= "힣" for ch in msgs[0]["content"]))
check("expectation stamped — cron will not re-send",
      not sms._expectation_due("dana1"))

print("lane=legacy preserves the old behavior")
db.save_sms_signup("+15550008002", name="Lex", email="l@x.co",
                   consent_checkins=True)
sid2 = db.get_pending_signups()[0]["id"]
r = hit(coach._activate_handler,
        f"/debug/activate?secret=sek&signup_id={sid2}&user_id=lex1"
        f"&lane=legacy")
check("activated", r.status == 200)
check("lane stays closed", not db.tracks_lane_open("lex1"))
check("no SMS at activation",
      db.get_recent_sms_messages("lex1", limit=5) == [])
check("expectation still due (cron sends it)",
      sms._expectation_due("lex1"))
r = hit(coach._activate_handler,
        f"/debug/activate?secret=sek&signup_id={sid2}&user_id=z"
        f"&lane=banana")
check("unknown lane refused", r.status == 400)

print("companion materials block")
blk = sms._companion_materials_block("dana1")
check("empty → integrity line",
      "NONE" in blk and "Never speak as if you have read" in blk)
mid = db.add_user_material("dana1", kind="file", title="BD plan.pdf")
db.set_material_digest(mid, "A 12-page BD plan: target clients, "
                            "quarterly outreach cadence.")
blk = sms._companion_materials_block("dana1")
check("shared doc renders with digest",
      "BD plan.pdf" in blk and "quarterly outreach" in blk)
check("grounding rule present", "never improvise contents" in blk)

print("material_ready fires for companion users (no onboarding gate)")
sent = {}
sms.generate_message = lambda *a, **k: ("Read it — the outreach "
                                        "cadence stood out.", [], None,
                                        "c1", None)
out = sms.handle_material_ready("dana1", mid)
check("companion follow-up sent", out is not None)
check("legacy user without focus still gated",
      sms.handle_material_ready("lex1", mid) is None)

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
