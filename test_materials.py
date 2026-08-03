"""
Learning materials + magic-link tokens (offer-loop arc, PR 1).

A material is one thing the user studies from — an uploaded file, a
shared link, or a source only named in conversation. The Theo-led
walkthrough fills user_description/wants with the user's OWN words,
and reaches 'validated' only when the coach's sample (an
insider-plausible question, a next-piece cut) was confirmed by the
user. has_validated_material() is the mechanical fill condition the
material_walkthrough onboarding field will key on.

Run: ./venv/bin/python test_materials.py  (sqlite)
"""

import json
import os
import tempfile

os.environ.pop("DATABASE_URL", None)
os.environ["TZ_OFFSET_HOURS"] = "0"

import db  # noqa: E402

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "test_materials.db")
db.init_db()

U = "m1"
PASS = []


def check(name, cond):
    print(f"  {'✅' if cond else '❌'} {name}")
    PASS.append(bool(cond))


def events_of(kind, user=U):
    return [r for r in db.get_events(user, limit=300) if r["kind"] == kind]


# ── 1. the three kinds ───────────────────────────────────────────────
print("1) kinds")
db.ensure_user_profile_row(U)
f_id = db.add_user_material(U, "file", title="파생상품_정리.docx",
                            orig_filename="파생상품_정리.docx",
                            extracted_text="제1장 스왑...")
l_id = db.add_user_material(U, "link", title="Karpathy — zero to hero",
                            source_url="https://youtube.com/watch?v=abc")
n_id = db.add_user_material(U, "named", title="그 책")
check("three kinds registered, ids distinct",
      len({f_id, l_id, n_id}) == 3)
check("registration events carry kind + title",
      len(events_of("material_added")) == 3
      and json.loads(events_of("material_added")[-1]["payload"])["kind"]
      in ("file", "link", "named"))
try:
    db.add_user_material(U, "telepathy")
    check("unknown kind rejected", False)
except ValueError:
    check("unknown kind rejected", True)

rows = db.get_user_materials(U)
check("listed newest first, wants decoded",
      rows[0]["id"] == n_id and rows[0]["wants"] == []
      and rows[-1]["orig_filename"] == "파생상품_정리.docx")

# ── 2. digest ────────────────────────────────────────────────────────
print("2) digest")
db.set_material_digest(f_id, "3개 장: 스왑/옵션/규제. 항목 41개.",
                       extracted_text="제1장 스왑... (전문)")
m = db.get_material(f_id)
check("digest + extracted text stored",
      "41개" in m["digest"] and "전문" in m["extracted_text"])

# ── 3. the walkthrough lands across turns ────────────────────────────
print("3) walkthrough")
db.update_material_walkthrough(f_id, user_description="업무에서 쌓은 걸 정리한 파일",
                               status="in_progress")
db.update_material_walkthrough(
    f_id,
    wants=[{"quote": "클라이언트가 물으면 바로 대답해야 해",
            "meaning": "instant recall under questioning"}])
m = db.get_material(f_id)
check("partial updates accumulate (description kept after wants-only update)",
      m["user_description"] == "업무에서 쌓은 걸 정리한 파일"
      and m["wants"][0]["quote"].startswith("클라이언트")
      and m["walkthrough_status"] == "in_progress")
check("each landing is an event",
      len(events_of("material_walkthrough_updated")) == 2)
try:
    db.update_material_walkthrough(f_id, status="perfect")
    check("unknown status rejected", False)
except ValueError:
    check("unknown status rejected", True)

# ── 4. validation is the gate ────────────────────────────────────────
print("4) validation gate")
check("not validated yet → field would stay missing",
      not db.has_validated_material(U))
db.update_material_walkthrough(f_id, status="validated")
check("coach sample confirmed → gate opens",
      db.has_validated_material(U))
check("another user is unaffected",
      not db.has_validated_material("someone_else"))

# ── 5. magic-link tokens ─────────────────────────────────────────────
print("5) tokens")
t1 = db.ensure_user_token(U)
check("token created once and stable",
      t1 == db.ensure_user_token(U) and len(t1) >= 32)
check("token → user (the /my auth check)",
      db.get_user_id_by_token(t1) == U
      and db.get_user_id_by_token("wrong") is None
      and db.get_user_id_by_token("") is None)
t2 = db.regenerate_user_token(U)
check("regeneration invalidates the leaked link",
      t2 != t1 and db.get_user_id_by_token(t1) is None
      and db.get_user_id_by_token(t2) == U)
check("regenerating a tokenless user still yields a working token",
      db.get_user_id_by_token(db.regenerate_user_token("fresh")) == "fresh")

print(f"\n{sum(PASS)}/{len(PASS)} passed")
raise SystemExit(0 if all(PASS) else 1)
