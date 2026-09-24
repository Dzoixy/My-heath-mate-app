import json
import os
import sys
import urllib.request

base = os.environ.get("API_BASE", "").rstrip("/")
token = os.environ.get("FIREBASE_ID_TOKEN", "")

if not base:
    print("Set API_BASE=https://your-service.onrender.com")
    sys.exit(1)

print("GET", base + "/health")
with urllib.request.urlopen(base + "/health", timeout=20) as r:
    print(r.status, r.read().decode())

if token:
    req = urllib.request.Request(
        base + "/v1/foods",
        data=json.dumps({"query": "cooked rice"}).encode(),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    print("POST", base + "/v1/foods")
    with urllib.request.urlopen(req, timeout=30) as r:
        print(r.status, r.read().decode())
else:
    print("FIREBASE_ID_TOKEN not set; authenticated endpoint test skipped.")
