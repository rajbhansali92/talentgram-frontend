import os
import sys
import asyncio
import logging

# Ensure backend folder is in path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set environment defaults if not set
os.environ.setdefault("JWT_SECRET", "supersecretkey123")
os.environ.setdefault("MONGO_URL", "mongodb+srv://team_db_user:Wxp0xYSOiwzb9GyE@cluster0.sipmssu.mongodb.net/talentgram?retryWrites=true&w=majority")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("worker_entry")

async def main():
    logger.info("=========================================")
    logger.info("Initializing Data Hub Background Worker...")
    logger.info("=========================================")
    
    # Trigger database seeds
    from services.import_seed import seed_data_hub_config
    await seed_data_hub_config()
    
    # Import and start background worker
    from services.import_worker import start_import_worker
    start_import_worker()
    
    # Keep the worker loop alive indefinitely
    try:
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Worker received interrupt, shutting down.")
    except Exception as e:
        logger.fatal(f"Fatal worker exception: {e}", exc_info=True)

if __name__ == "__main__":
    asyncio.run(main())
