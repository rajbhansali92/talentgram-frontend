import logging
from core import db

logger = logging.getLogger(__name__)

async def seed_data_hub_config():
    """Seeds default configurations for auto-labeling rules and media validators in MongoDB."""
    try:
        # 1. Seed label rules
        rules_count = await db.label_rules.count_documents({})
        if rules_count == 0:
            default_rules = [
                {
                    "field": "location",
                    "operator": "city_equals",
                    "value": "Mumbai",
                    "label": "Mumbai"
                },
                {
                    "field": "gender",
                    "operator": "equals",
                    "value": "Female",
                    "label": "Female"
                },
                {
                    "field": "height",
                    "operator": "height_greater_than",
                    "value": "5'8\"",
                    "label": "Tall"
                }
            ]
            await db.label_rules.insert_many(default_rules)
            logger.info("[Data Hub Seed] Seeded default auto-labeling rules.")
            
        # 2. Seed media validation configs
        config_count = await db.media_validation_config.count_documents({})
        if config_count == 0:
            default_config = {
                "max_size_bytes": 200 * 1024 * 1024, # 200MB
                "allowed_mime_types": ["image/", "video/", "application/pdf", "octet-stream"]
            }
            await db.media_validation_config.insert_one(default_config)
            logger.info("[Data Hub Seed] Seeded default media validation configurations.")
            
    except Exception as e:
        logger.error(f"[Data Hub Seed] Seeding configuration failed: {e}", exc_info=True)
