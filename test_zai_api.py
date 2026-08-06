#!/usr/bin/env python3
"""Buscar la API interna de Z.ai para usage de coding plan"""
import json, os, urllib.request, urllib.error

jwt_path = os.path.expanduser("~/AppData/Local/hermes/.zai_jwt.txt")
with open(jwt_path) as f:
    jwt = f.read().strip()

base = "https://z.ai"
paths = [
    "api/coding-plan/personal/usage",
    "api/coding-plan/usage",
    "api/v1/coding-plan/personal/usage",
    "api/v1/coding-plan/usage",
    "api/v1s/coding-plan/personal/usage",
    "api/manage-apikey/coding-plan/personal/usage",
    "api/coding-plan",
    "api/v1/coding-plan",
    "api/v1/coding-plan/info",
    "api/v1/coding-plan/status",
    "api/v1/coding-plan/subscription",
    "api/v1/coding-plan/quota",
    "api/v1/coding-plan/personal",
    "api/coding-plan/personal",
    "api/coding-plan/personal/quota",
    "api/coding-plan/personal/status",
    "api/coding-plan/personal/info",
    "api/v1/coding-plan/personal/quota",
    "api/v1s/coding-plan",
    "api/v1s/coding-plan/personal/usage",
    # OpenWebUI style
    "api/v1/auths",
    "api/v1/users",
    "api/v1s/coding-plan/personal/usage",
    # Probar el path exacto del frontend
    "manage-apikey/api/coding-plan/personal/usage",
    "api/coding-plan/personal/usage/info",
    "api/coding-plan/personal/usage/list",
    "api/coding-plan/personal/usage/statistics",
]

for path in paths:
    url = f"{base}/{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {jwt}")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        body = resp.read().decode()[:500]
        print(f"  {resp.status} {path}")
        print(f"    -> {body}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:200]
        # Solo mostrar si no es 404 generico
        if "NOT_FOUND" not in body and e.code != 404:
            print(f"  {e.code} {path}")
            print(f"    -> {body}")
    except Exception as e:
        print(f"  ERR {path}: {str(e)[:60]}")
