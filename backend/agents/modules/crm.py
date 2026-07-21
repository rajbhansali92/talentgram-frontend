"""Talentgram CRM Agent — the first registered WhatsApp agent.

Scoped to exactly one backend module: Marketing (backend/routers/marketing.py,
db.clients). This file is the only place in the whole agent platform that
knows what "name / phone / role" mean, what a supported role is, or how a
CRM contact gets written to the database. Every future agent (Projects,
Casting, Storage, Analytics, ...) follows this exact shape: field
validators + an executor + one register() call, registered from
agents/modules/__init__.py.
"""
from __future__ import annotations

import difflib
import functools
import re

from core import db
from routers.marketing import insert_client_doc
from routers.whatsapp import _normalize_phone

from agents.models import AgentDefinition, ExecContext, ExecResult, FieldSpec, IntentDefinition, ValidationResult
from agents.registry import register_agent
from agents.modules import crm_nlu

# value -> canonical display label, mirrors frontend/src/pages-components/
# MarketingHub.jsx's CONTACT_TYPES exactly (kept in sync manually — small,
# stable list; promote to a shared backend constant if it starts drifting).
SUPPORTED_ROLES = {
    "brand manager": "Brand Manager",
    "marketing manager": "Marketing Manager",
    "influencer marketing manager": "Influencer Marketing Manager",
    "influencer marketing": "Influencer Marketing Manager",
    "creative director": "Creative Director",
    "agency producer": "Agency Producer",
    "casting director": "Casting Director",
    "casting assistant": "Casting Assistant",
    "casting company": "Casting Company",
    "producer": "Producer",
    "executive producer": "Executive Producer",
    "production house": "Production House",
    "line producer": "Line Producer",
    "talent agency": "Talent Agency",
    "modeling agency": "Modeling Agency",
    "casting agency": "Casting Agency",
}

# Common abbreviations/nicknames -> the same canonical labels as
# SUPPORTED_ROLES (2026-07-21 understanding-layer upgrade). Kept as a
# separate dict so SUPPORTED_ROLES stays the "full phrase" registry and this
# stays purely additive shorthand — genuinely ambiguous 2-letter
# abbreviations (e.g. "CD" could mean Creative Director OR Casting
# Director) are deliberately left OUT rather than guessed; the fuzzy-match
# fallback in _validate_role/crm_nlu.extract_role will surface those as an
# explicit "did you mean" question instead.
ROLE_SYNONYMS = {
    "brand mgr": "Brand Manager",
    "brand head": "Brand Manager",
    "bm": "Brand Manager",
    "marketing": "Marketing Manager",
    "marketing head": "Marketing Manager",
    "mktg manager": "Marketing Manager",
    "mktg": "Marketing Manager",
    "marketing mgr": "Marketing Manager",
    "influencer mgr": "Influencer Marketing Manager",
    "influencer manager": "Influencer Marketing Manager",
    "creative head": "Creative Director",
    "agency prod": "Agency Producer",
    "casting head": "Casting Director",
    "casting": "Casting Director",
    "casting asst": "Casting Assistant",
    "prod": "Producer",
    "production": "Producer",
    "ep": "Executive Producer",
    "exec producer": "Executive Producer",
    "prod house": "Production House",
    "lp": "Line Producer",
}

# Combined lookup used everywhere a role phrase needs to be recognized —
# full phrases plus shorthand, all resolving to the same canonical labels.
ROLE_REGISTRY = {**SUPPORTED_ROLES, **ROLE_SYNONYMS}

# Label -> the snake_case slug MarketingHub.jsx's CONTACT_TYPES actually
# stores/filters/counts by (its <select> value, not its display label).
# SUPPORTED_ROLES' values stay as human-readable labels so the WhatsApp
# confirmation message keeps showing "Role: Casting Director" — this
# separate mapping is applied only at the point of writing contact_type to
# the database, so a WhatsApp-created contact shows up correctly in the
# Marketing UI's Contact Type filter/dropdown instead of silently not
# matching any option (found live 2026-07-21: the badge on the contact
# card fell back to displaying the raw stored label, masking that the
# dropdown/filter/count logic never matched it).
ROLE_LABEL_TO_SLUG = {
    "Brand Manager": "brand_manager",
    "Marketing Manager": "marketing_manager",
    "Influencer Marketing Manager": "influencer_marketing",
    "Creative Director": "creative_director",
    "Agency Producer": "agency_producer",
    "Casting Director": "casting_director",
    "Casting Assistant": "casting_assistant",
    "Casting Company": "casting_company",
    "Producer": "producer",
    "Executive Producer": "executive_producer",
    "Production House": "production_house",
    "Line Producer": "line_producer",
    "Talent Agency": "talent_agency",
    "Modeling Agency": "modeling_agency",
    "Casting Agency": "casting_agency",
}


_NAME_FORMAT_HINT = (
    "That doesn't look like a valid name.\n"
    "Please send name, phone, and role on separate lines, e.g.:\n"
    "Save\nTanu Malhotra\n9619015464\nCasting Director"
)


def _validate_name(raw: str) -> ValidationResult:
    name = " ".join((raw or "").strip().split())
    if len(name) < 2:
        return ValidationResult(ok=False, error="That doesn't look like a valid name. Please send the contact's full name.")
    if len(name) > 200:
        return ValidationResult(ok=False, error="That name is too long (max 200 characters). Please shorten it.")
    if not re.search(r"[A-Za-z]", name):
        return ValidationResult(ok=False, error="That doesn't look like a valid name. Please send the contact's full name.")
    if re.search(r"\d{3,}", name):
        # Live bug (2026-07-21): a single-line message like "add Tanu
        # Malhotra +91 96190 15464 casting director" has no newlines for
        # extract_initial_fields to split on, so the whole remainder was
        # swallowed into "name" — which then technically passed the old
        # validator (it has letters) and silently produced a garbled
        # contact name. A real name never contains a run of 3+ digits, so
        # reject it here with a hint toward the format that actually works,
        # rather than silently accepting a smashed-together value.
        return ValidationResult(ok=False, error=_NAME_FORMAT_HINT)
    return ValidationResult(ok=True, value=name)


def _validate_phone(raw: str) -> ValidationResult:
    norm = _normalize_phone(raw)
    if not norm:
        return ValidationResult(
            ok=False,
            error=(
                "I think this phone number is incomplete.\n"
                "Please send a valid mobile number including country code, e.g. 919876543210."
            ),
        )
    return ValidationResult(ok=True, value=norm)


# Auto-accept a close match above this similarity — covers ordinary typos
# ("Brand Manger", "castng director") where one candidate is clearly what
# was meant. Below it but still a plausible match, ask instead of guessing —
# several similarly-close roles (e.g. "Casting Head" vs Casting
# Director/Agency Producer/Casting Company) are genuinely ambiguous.
_ROLE_AUTOCORRECT_CUTOFF = 0.85


def _ambiguous_role_result(raw: str, options: list) -> ValidationResult:
    numbered = "\n".join(f"{i}. {opt}" for i, opt in enumerate(options, start=1))
    return ValidationResult(
        ok=False,
        error=f'I couldn\'t recognise "{raw.strip()}".\nDid you mean:\n{numbered}\n\nPlease send the correct one.',
    )


def _validate_role(raw: str) -> ValidationResult:
    raw = raw or ""
    if raw.startswith("__ambiguous__:"):
        # crm_nlu.extract_fields_for_intent found a role phrase that fuzzy-
        # matched several roles too closely to auto-pick one — encoded here
        # instead of silently guessing, surfaced as the same "did you mean"
        # question a low-confidence direct reply would get.
        options = raw[len("__ambiguous__:"):].split("|")
        return _ambiguous_role_result("that role", options)

    key = re.sub(r"\s+", " ", raw.strip().lower()).replace("_", " ")
    if key in ROLE_REGISTRY:
        return ValidationResult(ok=True, value=ROLE_REGISTRY[key])

    close = difflib.get_close_matches(key, ROLE_REGISTRY.keys(), n=3, cutoff=0.6)
    if close:
        best_ratio = difflib.SequenceMatcher(None, key, close[0]).ratio()
        if best_ratio >= _ROLE_AUTOCORRECT_CUTOFF:
            return ValidationResult(ok=True, value=ROLE_REGISTRY[close[0]])
        options = [ROLE_REGISTRY[k] for k in close]
        return _ambiguous_role_result(raw, options)

    supported = ", ".join(sorted(set(SUPPORTED_ROLES.values())))
    return ValidationResult(
        ok=False,
        error=f'"{raw.strip()}" isn\'t a supported role.\nSupported roles: {supported}',
    )


# Optional enrichment fields (2026-07-21 understanding-layer upgrade).
# company/email map straight to real clients-collection fields
# (insert_client_doc already supported them; the agent just never populated
# them before). city/country/instagram have NO dedicated schema field
# (confirmed by reading marketing.py's ClientCreate/ClientUpdate models —
# there is no city/country/social-handle column), so they're folded into
# the existing `tags` list instead of inventing a new one — a real,
# searchable value in a real field beats a fabricated schema change for a
# feature this session's brief scoped as "understanding layer only."
_EMAIL_VALIDATE_RE = re.compile(r"^[\w.+-]+@[\w-]+\.[A-Za-z]{2,}$")


def _validate_company(raw: str) -> ValidationResult:
    name = " ".join((raw or "").strip().split())
    if not name:
        return ValidationResult(ok=False, error="That doesn't look like a company name.")
    return ValidationResult(ok=True, value=name[:200])


def _validate_email(raw: str) -> ValidationResult:
    val = (raw or "").strip().lower()
    if not _EMAIL_VALIDATE_RE.match(val):
        return ValidationResult(ok=False, error="That doesn't look like a valid email address.")
    return ValidationResult(ok=True, value=val)


def _validate_freeform_location(raw: str) -> ValidationResult:
    val = " ".join((raw or "").strip().split())
    if not val:
        return ValidationResult(ok=False, error="I didn't catch that.")
    return ValidationResult(ok=True, value=val[:100])


def _validate_instagram(raw: str) -> ValidationResult:
    val = (raw or "").strip().lstrip("@")
    if not val:
        return ValidationResult(ok=False, error="That doesn't look like an Instagram handle.")
    return ValidationResult(ok=True, value=val[:60])


async def _create_contact_executor(collected: dict, ctx: ExecContext) -> ExecResult:
    name = collected.get("name", "")
    phone = collected.get("phone", "")
    role = collected.get("role", "")

    existing_phone = await db.clients.find_one({"phone_number": phone, "deleted": {"$ne": True}})
    if existing_phone:
        return ExecResult(
            ok=False,
            error="duplicate_phone",
            message=(
                f"A contact with phone {phone} already exists: {existing_phone.get('name')}.\n"
                "Nothing saved."
            ),
        )

    existing_name = await db.clients.find_one(
        {"name": {"$regex": f"^{re.escape(name)}$", "$options": "i"}, "deleted": {"$ne": True}}
    )
    if existing_name:
        return ExecResult(
            ok=False,
            error="duplicate_name",
            message=(
                f'A contact named "{name}" already exists '
                f"(phone {existing_name.get('phone_number') or '—'}).\n"
                "Nothing saved. Reply with a different name if this is a different person."
            ),
        )

    tags = []
    for key in ("city", "country", "instagram"):
        val = collected.get(key)
        if val:
            tags.append(f"@{val}" if key == "instagram" else val)

    doc = await insert_client_doc(
        name=name,
        phone_number=phone,
        contact_type=ROLE_LABEL_TO_SLUG.get(role, role),
        company_name=collected.get("company") or None,
        email=collected.get("email") or None,
        tags=tags or None,
        source=f"whatsapp_agent:{ctx.agent_id}",
    )
    return ExecResult(ok=True, message=f"Saved successfully\nCRM ID: {doc['id']}", data=doc)


CREATE_CONTACT_INTENT = IntentDefinition(
    intent_id="crm.create_contact",
    triggers=["save", "add", "new contact"],
    fields=[
        FieldSpec(key="name", label="Name", question="What's the name?", validate=_validate_name),
        FieldSpec(key="phone", label="Phone", question="Got it. What's {name}'s phone number?",
                   validate=_validate_phone, aliases=["phone number", "mobile", "number"]),
        FieldSpec(key="role", label="Role", question="Great. What's {name}'s role or designation?",
                   validate=_validate_role, aliases=["contact type", "designation"]),
        # Optional enrichment — never blocks confirmation, never prompted
        # for; only shown/saved when the message actually mentioned them.
        FieldSpec(key="company", label="Company", question="What company are they with?",
                   validate=_validate_company, aliases=["company name", "studio", "organisation", "organization"],
                   required=False),
        FieldSpec(key="email", label="Email", question="What's their email?",
                   validate=_validate_email, aliases=["email address"], required=False),
        FieldSpec(key="city", label="City", question="Which city are they in?",
                   validate=_validate_freeform_location, aliases=["location"], required=False),
        FieldSpec(key="country", label="Country", question="Which country are they in?",
                   validate=_validate_freeform_location, required=False),
        FieldSpec(key="instagram", label="Instagram", question="What's their Instagram handle?",
                   validate=_validate_instagram, aliases=["insta", "ig"], required=False),
    ],
    executor=_create_contact_executor,
    extract_fields=functools.partial(crm_nlu.extract_fields_for_intent, role_registry=ROLE_REGISTRY),
    parse_edits=lambda text, fields: crm_nlu.parse_edits_for_intent(text, fields, role_registry=ROLE_REGISTRY),
)

CRM_AGENT = AgentDefinition(
    agent_id="crm-agent",
    name="Talentgram CRM",
    module="marketing",
    intents=[CREATE_CONTACT_INTENT],
)


def register() -> None:
    register_agent(CRM_AGENT)
