import os
import sys
import asyncio

sys.path.append(os.path.abspath(os.path.dirname(__file__) + "/.."))

async def check_assets():
    import cloudinary.api
    import pprint
    
    # We will use the R2 key to construct the target folder and public_id
    # take_77805fba.mp4 and take_e73bb765.mp4
    # The sid for R2 key raw-uploads/submissions/dc76c18e-b199-4990-a777-7de611c1ea72/take/take_e73bb765.mp4 is:
    # dc76c18e-b199-4990-a777-7de611c1ea72
    
    from motor.motor_asyncio import AsyncIOMotorClient
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME", "talentgram")
    client = AsyncIOMotorClient(mongo_url, tlsAllowInvalidCertificates=True)
    db = client[db_name]
    
    sub = await db.submissions.find_one({"id": "dc76c18e-b199-4990-a777-7de611c1ea72"})
    if not sub:
        print("Submission not found")
        return
        
    print("Media items in submission:")
    for media in sub.get("media", []):
        print(f"- ID: {media.get('id')}")
        print(f"  Category: {media.get('category')}")
        print(f"  Status: {media.get('status')}")
        print(f"  Public ID: {media.get('public_id')}")
        print(f"  URL: {media.get('url')}")
        
        # Query Cloudinary
        pub_id = media.get("public_id")
        if pub_id:
            try:
                res = cloudinary.api.resource(pub_id, resource_type="video")
                print("  Cloudinary details:")
                pprint.pprint({
                    "public_id": res.get("public_id"),
                    "resource_type": res.get("resource_type"),
                    "bytes": res.get("bytes"),
                    "duration": res.get("duration"),
                    "format": res.get("format"),
                    "secure_url": res.get("secure_url"),
                    "status": "exists"
                })
            except Exception as e:
                print(f"  Cloudinary resource not found or failed to fetch: {e}")

if __name__ == "__main__":
    # Ensure Cloudinary is initialized
    import core
    asyncio.run(check_assets())
