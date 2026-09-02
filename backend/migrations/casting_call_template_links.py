"""WhatsApp project-message links fix (2026-08-31) — updates the existing
"casting_call" WhatsApp template's body_text to clearly separate the two
actions ("SUBMIT FORM" / "VIEW SCRIPT / AUDITION MATERIAL") as plain,
WhatsApp-auto-linkified HTTPS URLs, and adds the new
`{{audition_material_link}}` placeholder alongside the existing
`{{submission_link}}`.

_seed_templates() (routers/whatsapp.py) only inserts default templates once,
on an EMPTY `whatsapp_templates` collection — any environment whose
collection was already seeded (i.e. every existing deployment) never picks
up a changed Python constant on its own. This migration is the one-off
backfill for that: it updates the EXISTING "casting_call" document by slug,
in place, so the change reaches an already-running deployment without a
fresh DB.

Idempotent: safe to re-run — it always sets the same target body_text /
variables (a `$set`, not an append), and only ever touches the doc with
slug == "casting_call". Skips (does not touch) any template row whose
`body_text` has already been edited away from a recognizably close variant
of the OLD default body — i.e. an admin's own hand-customized "Casting
Call" copy is left alone, only the still-original default text is upgraded.

Run:  MONGO_URL="mongodb+srv://..." python backend/migrations/casting_call_template_links.py
"""
import asyncio
import os

from motor.motor_asyncio import AsyncIOMotorClient

OLD_SNIPPET = "To proceed, please confirm your availability and submit your details here:"

NEW_BODY_TEXT = (
    "Hi {{talent_name}} 👋\n\n"
    "We'd love to have you for *{{project_name}}*!\n\n"
    "📅 Shoot Dates: {{shoot_dates}}\n"
    "💰 Budget: {{budget}}\n\n"
    "*SUBMIT FORM*\n"
    "Please confirm your availability and complete the form here:\n"
    "{{submission_link}}\n\n"
    "*VIEW SCRIPT / AUDITION MATERIAL*\n"
    "Script, reference video/audio and other audition material:\n"
    "{{audition_material_link}}\n\n"
    "Important:\n"
    "• Please complete the application form on the app.\n"
    "• Your application will only be reviewed once the form is completed.\n\n"
    "— Team Talentgram Agency"
)

NEW_VARIABLES = [
    "talent_name", "project_name", "shoot_dates", "budget",
    "submission_link", "audition_material_link",
]


async def main() -> None:
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ.get("DB_NAME", "talentgram")]

    doc = await db.whatsapp_templates.find_one({"slug": "casting_call"})
    if not doc:
        print("No 'casting_call' template found — nothing to migrate (a fresh "
              "seed will already include the new format).")
        client.close()
        return

    body = doc.get("body_text") or ""
    if OLD_SNIPPET not in body and "audition_material_link" in body:
        print("'casting_call' template already migrated — no change made.")
        client.close()
        return
    if OLD_SNIPPET not in body:
        print(
            "'casting_call' template's body_text no longer matches the "
            "recognized original default (looks admin-customized) — leaving "
            "it untouched. Add {{audition_material_link}} to it manually via "
            "the template editor if desired."
        )
        client.close()
        return

    from datetime import datetime, timezone
    await db.whatsapp_templates.update_one(
        {"id": doc["id"]},
        {"$set": {
            "body_text": NEW_BODY_TEXT,
            "variables": NEW_VARIABLES,
            "updated_at": datetime.now(timezone.utc),
        }},
    )
    print(f"Updated 'casting_call' template (id={doc['id']}) with the new SUBMIT FORM / "
          "VIEW SCRIPT / AUDITION MATERIAL link structure.")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
