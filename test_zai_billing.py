#!/usr/bin/env python3
"""Probar todos los endpoints de billing/cuota posibles en Z.AI y bigmodel.cn"""
import json, os, urllib.request, urllib.error

env_path = os.path.expanduser("~/AppData/Local/hermes/.env")
api_key = None
with open(env_path, 'r') as f:
    for line in f:
        stripped = line.strip()
        if stripped.startswith("GLM_API_KEY") and not stripped.startswith("#"):
            api_key = stripped.split("=", 1)[1].strip()
            break

if not api_key:
    print("ERROR: GLM_API_KEY not found")
    exit(1)

print(f"API key: {api_key[:10]}... ({len(api_key)} chars)")
print()

endpoints = [
    "https://open.bigmodel.cn/api/paas/v4/billing/quota",
    "https://open.bigmodel.cn/api/paas/v4/billing/info",
    "https://open.bigmodel.cn/api/paas/v4/billing/subscription",
    "https://open.bigmodel.cn/api/paas/v4/billing/usage",
    "https://open.bigmodel.cn/api/paas/v4/resource/quota",
    "https://open.bigmodel.cn/api/paas/v4/account/info",
    "https://api.z.ai/api/paas/v4/billing/quota",
    "https://api.z.ai/api/paas/v4/billing/info",
    "https://api.z.ai/api/coding/paas/v4/billing/quota",
    "https://api.z.ai/api/coding/paas/v4/billing/info",
    "https://api.z.ai/api/coding/paas/v4/subscription",
    "https://api.z.ai/api/coding/paas/v4/account",
]

for url in endpoints:
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode()[:500]
        print(f"  {resp.status} {url}")
        print(f"    -> {body}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        status = f"{e.code}"
        if e.code == 401:
            status += " (auth ok but endpoint not found)"
        elif e.code == 404:
            status += " (not found)"
        elif e.code == 403:
            status += " (forbidden)"
        print(f"  {status} {url}")
        if e.code not in (404,):
            print(f"    -> {body}")
    except Exception as e:
        print(f"  ERR {url}: {str(e)[:80]}")
    print()
