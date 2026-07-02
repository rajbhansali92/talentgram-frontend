import os
import sys
import asyncio

# Safely set default mocks only if NOT present in environment
if "MONGO_URL" not in os.environ:
    os.environ["MONGO_URL"] = "mongodb://localhost:27017/test"
if "DB_NAME" not in os.environ:
    os.environ["DB_NAME"] = "test-db"
if "JWT_SECRET" not in os.environ:
    os.environ["JWT_SECRET"] = "test-secret"
if "ADMIN_EMAIL" not in os.environ:
    os.environ["ADMIN_EMAIL"] = "admin@test.com"
if "ADMIN_PASSWORD" not in os.environ:
    os.environ["ADMIN_PASSWORD"] = "password"
if "CLOUDINARY_CLOUD_NAME" not in os.environ:
    os.environ["CLOUDINARY_CLOUD_NAME"] = "mock-cloud"
if "CLOUDINARY_API_KEY" not in os.environ:
    os.environ["CLOUDINARY_API_KEY"] = "mock-key"
if "CLOUDINARY_API_SECRET" not in os.environ:
    os.environ["CLOUDINARY_API_SECRET"] = "mock-secret"

sys.path.append(os.path.abspath(os.path.dirname(__file__) + "/.."))

async def main():
    from motor.motor_asyncio import AsyncIOMotorClient
    mongo_url = os.environ.get("MONGO_URL")
    db_name = os.environ.get("DB_NAME", "talentgram")
    
    client = AsyncIOMotorClient(mongo_url, tlsAllowInvalidCertificates=True)
    db = client[db_name]
    
    import core
    core.db = db
    import routers.submissions
    routers.submissions.db = db
    
    sub = await db.submissions.find_one({"id": "dc76c18e-b199-4990-a777-7de611c1ea72"})
    if not sub:
        print("Submission not found in DB")
        return
        
    token = sub.get("access_token")
    print(f"Access Token: {token}")
    
    from routers.submissions import submission_finalize
    
    print("Triggering submission_finalize...")
    try:
        res = await submission_finalize(
            sid="dc76c18e-b199-4990-a777-7de611c1ea72",
            authorization=f"Bearer {token}"
        )
        print("Finalize Response:")
        print(res)
    except Exception as e:
        print(f"Finalize failed with exception: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
