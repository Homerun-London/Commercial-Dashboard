"""
Quick check that Homerun's Mariana Tek API key works.
Lists locations (with IDs) and tests which endpoints the key can reach.
Prints no secrets.
"""
import os
import sys
import requests

API_KEY = os.environ.get("MARIANA_API_KEY", "").strip()
BASE_URL = os.environ.get("MARIANA_BASE_URL", "").strip().rstrip("/")

if not API_KEY or not BASE_URL:
    sys.exit("Missing MARIANA_API_KEY or MARIANA_BASE_URL secret.")

# If your Enmei scripts send the key a different way, copy that header here.
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Accept": "application/vnd.api+json"}

print(f"Base URL: {BASE_URL}\n")


def get(path, params=None):
    return requests.get(f"{BASE_URL}{path}", headers=HEADERS, params=params, timeout=30)


# 1. Locations
r = get("/locations", {"page_size": 100})
print(f"[locations] HTTP {r.status_code}")
if r.status_code != 200:
    print(r.text[:500])
    sys.exit("Key or base URL isn't working. Check both secrets.")

for loc in r.json().get("data", []):
    name = (loc.get("attributes") or {}).get("name", "(no name)")
    print(f"  {loc.get('id'):>8}  {name}")

# 2. Raw data endpoint
r = get("/membership_instances", {"page_size": 1})
print(f"\n[membership_instances] HTTP {r.status_code}"
      f" -> {'OK' if r.status_code == 200 else 'NOT accessible'}")

# 3. Pre-made report endpoint (the kind Enmei is losing)
r = get("/table_report_data/294")
print(f"[table_report_data] HTTP {r.status_code}"
      f" -> {'OK' if r.status_code == 200 else 'NOT accessible (build on raw data)'}")

print("\nDone.")
