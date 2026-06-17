import os
import sys
import json
import pymongo
from bson import json_util

def backup_db(mongo_url, db_name, out_dir):
    print(f"Connecting to MongoDB database '{db_name}'...")
    client = pymongo.MongoClient(mongo_url)
    db = client[db_name]
    
    os.makedirs(out_dir, exist_ok=True)
    
    collections = db.list_collection_names()
    print(f"Found collections: {collections}")
    
    for coll_name in collections:
        print(f"Backing up collection '{coll_name}'...")
        cursor = db[coll_name].find({})
        docs = list(cursor)
        
        file_path = os.path.join(out_dir, f"{coll_name}.json")
        with open(file_path, "w") as f:
            json.dump(docs, f, default=json_util.default, indent=2)
        print(f"Saved {len(docs)} documents to {file_path}")

def main():
    mongo_url = os.environ.get("PRODUCTION_MONGO_URL")
    db_name = os.environ.get("PRODUCTION_DB_NAME", "talentgram")
    out_dir = "./backup_data"
    
    if not mongo_url:
        print("Error: PRODUCTION_MONGO_URL environment variable is not set.")
        print("Usage: PRODUCTION_MONGO_URL='mongodb+srv://...' python3 scripts/backup_db.py")
        sys.exit(1)
        
    backup_db(mongo_url, db_name, out_dir)

if __name__ == "__main__":
    main()
