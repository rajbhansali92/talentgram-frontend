import requests
import sys

def verify_production_metadata():
    urls = [
        "https://talentgramagency.com/apply",
        "https://talentgramagency.com/submit/dark-fantasy-cookie-tvc-565513",
        "https://talentgramagency.com/l/talentgram-x-testotp-179a66"
    ]
    
    headers = {
        "User-Agent": "WhatsApp/2.21.12.21 A" # WhatsApp crawler user-agent simulation
    }
    
    all_passed = True
    print("=== LIVE PRODUCTION METADATA CRAWLER VALIDATION ===")
    
    for url in urls:
        print(f"\nVerifying: {url}")
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code != 200:
                print(f"  [FAIL] HTTP status {r.status_code}")
                all_passed = False
                continue
            
            html = r.text
            
            # Check Title
            title_tag = "<title>Talentgram Agency</title>"
            og_title = '<meta property="og:title" content="Talentgram Agency"/>'
            og_title_alt = '<meta property="og:title" content="Talentgram Agency" />'
            twitter_title = '<meta name="twitter:title" content="Talentgram Agency"/>'
            twitter_title_alt = '<meta name="twitter:title" content="Talentgram Agency" />'
            
            title_ok = (title_tag in html) or (og_title in html) or (og_title_alt in html) or (twitter_title in html) or (twitter_title_alt in html)
            
            # Check Description
            desc_tag = '<meta name="description" content="India - UAE"/>'
            desc_tag_alt = '<meta name="description" content="India - UAE" />'
            og_desc = '<meta property="og:description" content="India - UAE"/>'
            og_desc_alt = '<meta property="og:description" content="India - UAE" />'
            twitter_desc = '<meta name="twitter:description" content="India - UAE"/>'
            twitter_desc_alt = '<meta name="twitter:description" content="India - UAE" />'
            
            desc_ok = (desc_tag in html) or (desc_tag_alt in html) or (og_desc in html) or (og_desc_alt in html) or (twitter_desc in html) or (twitter_desc_alt in html)
            
            # Check OG/Twitter image structure and webmanifest link
            manifest_ok = 'rel="manifest" href="/site.webmanifest"' in html or 'href="/site.webmanifest"' in html
            
            print(f"  Title metadata matches 'Talentgram Agency': {title_ok}")
            print(f"  Description metadata matches 'India - UAE': {desc_ok}")
            
            if not title_ok or not desc_ok:
                all_passed = False
                print("  [FAIL] Missing or incorrect metadata strings.")
                # Print a small debug window of header tags
                for line in html.split('\n'):
                    if 'og:' in line or 'twitter:' in line or '<title>' in line or 'description' in line:
                        print(f"    Debug tag found: {line.strip()}")
            else:
                print("  [PASS] Metadata verified successfully.")
                
        except Exception as e:
            print(f"  [ERROR] {str(e)}")
            all_passed = False
            
    if all_passed:
        print("\n[SUCCESS] All production routes return exact expected metadata parameters!")
        sys.exit(0)
    else:
        print("\n[FAILURE] Validation check failed on some routes.")
        sys.exit(1)

if __name__ == "__main__":
    verify_production_metadata()
