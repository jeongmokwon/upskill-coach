"""
Transactional email via Resend (PR 6, pulled forward).

Email is plumbing, not a conversation channel: the coach talks over
SMS and only ever POINTS at an email ("메일함 봐봐"). The one email
that exists today carries the user's /my magic link — the thing we
must never paste into SMS.

Env: RESEND_API_KEY (Render), EMAIL_FROM (default
"Theo <hello@learningtheo.com>").
"""

import json
import os
import urllib.request

import db

RESEND_API = "https://api.resend.com/emails"
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Theo <hello@learningtheo.com>")
REPLY_TO = os.environ.get("EMAIL_REPLY_TO", "jeongmo.kwon@learningtheo.com")


def send_email(to, subject, text):
    """→ (ok, detail). Never raises. Plain text on purpose — a
    coach's note, not a newsletter."""
    key = os.environ.get("RESEND_API_KEY", "").strip()
    if not key:
        return False, "RESEND_API_KEY not set"
    body = json.dumps({"from": EMAIL_FROM, "to": [to],
                       "reply_to": REPLY_TO,
                       "subject": subject, "text": text}).encode()
    req = urllib.request.Request(
        RESEND_API, data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 # Cloudflare in front of Resend bans urllib's default
                 # Python-urllib/3.x signature (observed live: 403,
                 # error code 1010, while the same key + payload
                 # succeeded via curl). Any honest custom UA passes.
                 "User-Agent": "theo-coach/1.0 (learningtheo.com)"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return True, json.loads(resp.read().decode()).get("id", "")
    except urllib.error.HTTPError as e:
        # Resend explains itself in the response body ("domain not
        # verified", "API key restricted", ...). str(e) alone is just
        # "HTTP Error 403: Forbidden" — observed while debugging
        # blind; keep the body.
        try:
            detail = e.read().decode()[:500]
        except Exception:
            detail = ""
        return False, f"{e} — {detail}"
    except Exception as e:
        return False, str(e)


def send_welcome(user_id, to_email):
    """The activation welcome email: expectation-setting content plus
    the user's personal /my upload link, from
    prompts/welcome_email.md (first line = subject). Records
    welcome_emailed AND my_link_emailed — the link IS in it, and the
    walkthrough focus label + resend safety net key on the latter.
    → (ok, detail)."""
    import re
    token = db.ensure_user_token(user_id)
    link = f"https://www.learningtheo.com/my?k={token}"
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "prompts", "welcome_email.md")
    with open(path, encoding="utf-8") as f:
        raw = re.sub(r"<!--.*?-->\s*", "", f.read(), flags=re.DOTALL)
    subject, _, body = raw.strip().partition("\n")
    text = body.strip().replace("{link}", link) + "\n"
    ok, detail = send_email(to_email, subject.strip(), text)
    if ok:
        db.set_user_email(user_id, to_email)
        db.log_event(user_id, "welcome_emailed",
                     {"to": to_email, "resend_id": detail},
                     source="emailer")
        db.log_event(user_id, "my_link_emailed",
                     {"to": to_email, "via": "welcome"},
                     source="emailer")
    else:
        db.log_event(user_id, "welcome_email_failed",
                     {"to": to_email, "error": detail}, source="emailer")
    return ok, detail


def send_my_link(user_id, to_email):
    """Email the user their /my magic link. Records my_link_emailed
    on success — the walkthrough focus label keys on that event to
    switch from 'get them to show you' to 'point them at the email'.
    → (ok, detail)."""
    token = db.ensure_user_token(user_id)
    link = f"https://www.learningtheo.com/my?k={token}"
    text = f"""Hi — it's Theo.

This is your private page with me — if you ever want me to think
through a document with you (a plan, a deck, a doc, or a link),
share it here and I'll actually read it:

{link}

The link is yours alone — there's no login, so please don't share
it with anyone.

Once you share something, I'll read it properly and we'll pick it
up over text.

— Theo
"""
    ok, detail = send_email(to_email, "Theo — your private page link",
                            text)
    if ok:
        db.set_user_email(user_id, to_email)
        db.log_event(user_id, "my_link_emailed",
                     {"to": to_email, "resend_id": detail},
                     source="emailer")
    else:
        db.log_event(user_id, "my_link_email_failed",
                     {"to": to_email, "error": detail}, source="emailer")
    return ok, detail
