from typing import List, Dict, Any, Optional

IMPORT_FIELDS: Dict[str, Dict[str, Any]] = {
    "name": {
        "label": "Full Name",
        "type": "str",
        "required": True,
        "aliases": ["full name", "name", "talent name", "fullname", "first name", "last name"],
        "default": None
    },
    "email": {
        "label": "Email Address",
        "type": "str",
        "required": False,
        "aliases": ["email", "e-mail", "email address", "mail"],
        "default": None
    },
    "phone": {
        "label": "Phone (WhatsApp)",
        "type": "str",
        "required": True,
        "aliases": ["phone", "phone number", "whatsapp", "whatsapp number", "mobile", "mobile number", "contact", "contact number"],
        "default": None
    },
    "alternate_contact_number": {
        "label": "Alternate Contact Number",
        "type": "str",
        "required": False,
        "aliases": ["alternate contact", "backup contact", "alternate phone", "backup phone", "alt contact", "alt phone"],
        "default": None
    },
    "age": {
        "label": "Age",
        "type": "int",
        "required": False,
        "aliases": ["age", "years"],
        "default": None
    },
    "dob": {
        "label": "Date of Birth",
        "type": "str",
        "required": False,
        "aliases": ["dob", "date of birth", "birth date", "birthday", "d.o.b."],
        "default": None
    },
    "gender": {
        "label": "Gender",
        "type": "str",
        "required": False,
        "aliases": ["gender", "sex"],
        "default": None
    },
    "height": {
        "label": "Height",
        "type": "str",
        "required": False,
        "aliases": ["height", "tall"],
        "default": None
    },
    "location": {
        "label": "Location(s)",
        "type": "list",
        "required": False,
        "aliases": ["location", "city", "town", "locations", "cities", "current city"],
        "default": []
    },
    "ethnicity": {
        "label": "Ethnicity / Skin Tone",
        "type": "str",
        "required": False,
        "aliases": ["ethnicity", "skin tone", "skin color", "tone", "look"],
        "default": None
    },
    "instagram_handle": {
        "label": "Instagram Handle",
        "type": "str",
        "required": False,
        "aliases": ["instagram", "ig", "insta", "instagram handle", "ig handle", "instagram link", "instagram url"],
        "default": None
    },
    "instagram_followers": {
        "label": "Instagram Followers",
        "type": "str",
        "required": False,
        "aliases": ["instagram followers", "followers", "ig followers", "follower count"],
        "default": None
    },
    "bio": {
        "label": "Bio",
        "type": "str",
        "required": False,
        "aliases": ["bio", "about", "description", "summary", "about me"],
        "default": None
    },
    "work_links": {
        "label": "Work Links",
        "type": "list",
        "required": False,
        "aliases": ["work links", "links", "portfolio links", "websites", "work url", "video links"],
        "default": []
    },
    "skills": {
        "label": "Skills / Abilities",
        "type": "list",
        "required": False,
        "aliases": ["skills", "abilities", "special skills", "talents"],
        "default": []
    },
    "tags": {
        "label": "Internal Tags",
        "type": "list",
        "required": False,
        "aliases": ["tags", "labels", "category tags"],
        "default": []
    }
}

# Dynamically sync constraints from Pydantic TalentIn model to prevent drift.
# IMPORTANT: Pydantic fields that use default_factory (e.g. list, dict) expose
# `.default == PydanticUndefinedType` — not an actual value. We must guard against
# this sentinel to avoid overwriting explicit schema defaults (e.g. [] for list fields).
try:
    from core import TalentIn
    try:
        from pydantic_core import PydanticUndefinedType
    except ImportError:
        PydanticUndefinedType = type(None)  # Fallback: skip guard

    for f_name, m_field in TalentIn.model_fields.items():
        if f_name in IMPORT_FIELDS:
            IMPORT_FIELDS[f_name]["required"] = m_field.is_required()
            # Only overwrite default if it is a real value — never PydanticUndefined
            if m_field.default is not None and not isinstance(m_field.default, PydanticUndefinedType):
                IMPORT_FIELDS[f_name]["default"] = m_field.default
except Exception:
    # Fail-safe fallback if core models aren't loadable in testing/isolated environment
    pass

