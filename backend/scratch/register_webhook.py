import urllib.request
import json
import ssl

def main():
    account_id = "994281b3ef144148a824251eeb0893cd"
    notification_url = "https://api.talentgramagency.com/public/webhooks/cloudflare-stream"
    
    print("Cloudflare Webhook Registration Tool")
    print("====================================")
    api_token = input("Please paste your Cloudflare Stream API Token: ").strip()
    if not api_token:
        print("Error: API Token cannot be empty.")
        return

    # Disable SSL certificate validation verification check for custom environments if needed
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # 1. Register Webhook
    put_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/stream/webhook"
    payload = {"notificationUrl": notification_url}
    data = json.dumps(payload).encode("utf-8")
    
    req = urllib.request.Request(
        put_url,
        data=data,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        },
        method="PUT"
    )
    
    try:
        print("\n[Cloudflare] Registering webhook notificationUrl...")
        with urllib.request.urlopen(req, context=ctx) as response:
            res_data = json.loads(response.read().decode("utf-8"))
            print(f"Registration Response: {json.dumps(res_data, indent=2)}")
    except Exception as e:
        print(f"Failed to register webhook: {e}")
        if hasattr(e, "read"):
            print(e.read().decode("utf-8"))
        return

    # 2. Get webhook registration status to verify
    get_url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/stream/webhook"
    req_get = urllib.request.Request(
        get_url,
        headers={"Authorization": f"Bearer {api_token}"},
        method="GET"
    )
    
    try:
        print("\n[Cloudflare] Verifying webhook registration status...")
        with urllib.request.urlopen(req_get, context=ctx) as response:
            verify_data = json.loads(response.read().decode("utf-8"))
            print(f"Verification Response (GET): {json.dumps(verify_data, indent=2)}")
    except Exception as e:
        print(f"Failed to verify webhook: {e}")
        if hasattr(e, "read"):
            print(e.read().decode("utf-8"))

if __name__ == "__main__":
    main()
