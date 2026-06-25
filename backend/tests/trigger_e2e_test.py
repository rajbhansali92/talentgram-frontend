import ssl
ssl.create_default_context = ssl._create_unverified_context
import asyncio
import os
import sys
from pathlib import Path

# Add backend to path
backend_path = Path(__file__).parent.parent
sys.path.insert(0, str(backend_path))

async def main():
    import core
    from core import db, make_token
    from routers.submissions import test_internal_notification_endpoint
    
    print("Initializing E2E Verification...")
    print(f"Connected to DB: {core.DB_NAME}")
    
    # Authenticated Admin Mock
    admin_mock = {
        "id": "adm-1",
        "email": core.ADMIN_EMAIL,
        "role": "admin"
    }
    
    print("STEP 1: Triggering endpoint function...")
    try:
        res = await test_internal_notification_endpoint(admin=admin_mock)
        print("Endpoint Result: SUCCESS")
        print(f"Response Payload: {res}")
        batch_id = res["batch_id"]
        job_id = res["job_id"]
        group_name = res["group_name"]
    except Exception as e:
        print(f"Endpoint Result: FAIL - {e}")
        return

    print("\nSTEP 2: Verifying Mongo Queue Records...")
    # Fetch records from database
    batch_doc = await db.whatsapp_batches.find_one({"id": batch_id})
    job_doc = await db.whatsapp_jobs.find_one({"id": job_id})
    
    if batch_doc:
        print(f"  whatsapp_batches record exists: YES (Status: {batch_doc.get('status')})")
    else:
        print("  whatsapp_batches record exists: NO")
        
    if job_doc:
        print(f"  whatsapp_jobs record exists: YES (Status: {job_doc.get('status')})")
        print(f"  Destination Type: {job_doc.get('destination_type')}")
        print(f"  Destination: {job_doc.get('destination')}")
        print(f"  Message Body Snippet: {job_doc.get('message_body')[:100]}...")
    else:
        print("  whatsapp_jobs record exists: NO")
        
    if not batch_doc or not job_doc:
        print("\nQueue Insertion Result: FAIL")
        return
        
    print("\nSTEP 3: Monitoring Worker pickup (polling DB job status)...")
    # Poll for 20 seconds
    picked_up = False
    completed = False
    for i in range(20):
        await asyncio.sleep(2)
        job_doc = await db.whatsapp_jobs.find_one({"id": job_id})
        status = job_doc.get("status")
        worker_picked = job_doc.get("worker_picked_at")
        print(f"  [T+{i*2}s] Job Status: {status} | Worker Picked: {worker_picked}")
        
        if worker_picked and not picked_up:
            picked_up = True
            print("  -> Worker picked up the job!")
            
        if status in ("sent", "failed"):
            completed = True
            break
            
    print(f"\nSTEP 4: Final Delivery Result...")
    if completed:
        print(f"  Job final status: {job_doc.get('status')}")
        if job_doc.get('status') == "sent":
            print(f"  Sent At: {job_doc.get('sent_at')}")
            print("\nFINAL STATUS: PASS")
        else:
            print(f"  Error Message: {job_doc.get('error_message')}")
            print("\nFINAL STATUS: FAIL (Worker failed to send)")
    else:
        print("  Worker did not process the job within the timeout period.")
        print("\nFINAL STATUS: FAIL (Worker Timeout/Idle)")

if __name__ == "__main__":
    asyncio.run(main())
