#!/usr/bin/env python3
# pyright: basic, reportMissingTypeArgument=false
"""
Widget unificado de cuota de IA para Windows / Hermes Agent.

Monitorea el uso restante de:
  1. Google Antigravity IDE  — vía antigravity-usage CLI (OAuth login)
  2. OpenAI Codex            — leyendo rate_limits de rollout-*.jsonl locales
  3. Z.AI Coding Plan         — probe + detección de HTTP 429 + límites del plan

Modos de uso:
  python ai_quota_widget.py              # Salida legible (terminal)
  python ai_quota_widget.py --json       # JSON para integración
  python ai_quota_widget.py --watch      # Actualización continua (cada 5 min)
"""

import base64
import ctypes
from ctypes import wintypes
import shutil
import urllib.parse
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from scripts.utils.zcode_session import read_zcode_session

# ─── Configuración ───

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/AppData/Local/hermes"))
CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
CODEX_AUTH_FILE = os.path.expanduser("~/.codex/auth.json")
HERMES_AUTH_FILE = os.path.join(HERMES_HOME, "auth.json")
OPENCODE_AUTH_FILE = os.path.expanduser("~/.local/share/opencode/auth.json")
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
REFRESH_INTERVAL = 300  # 5 minutos en modo --watch
QUOTA_SNAPSHOT_FILE = os.environ.get(
    "AI_QUOTA_SNAPSHOT_FILE", os.path.join(os.path.dirname(__file__), "quota_snapshot.json")
)
QUOTA_IDENTITIES_FILE = os.environ.get(
    "AI_QUOTA_IDENTITIES_FILE",
    os.path.join(os.path.dirname(__file__), "quota_identities.local.json"),
)
AGY_SNAPSHOT_FILE = os.environ.get(
    "AI_AGY_SNAPSHOT_FILE",
    os.path.join(os.path.dirname(__file__), "agy_quota_snapshot.json"),
)
AGY_FAMILY_KEYS = {"Gemini": "gemini", "Claude y GPT": "claude_gpt", "Gemini 4 Argon": "gemini_argon"}

# Límites conocidos del Z.AI Coding Plan (extraídos de la documentación oficial)
# https://docs.z.ai/coding-plan/overview
ZAI_PLAN_LIMITS = {
    "Lite": {"5h_prompts": 80, "weekly_prompts": 400},
    "Pro": {"5h_prompts": 400, "weekly_prompts": 2000},
    "Max": {"5h_prompts": 1600, "weekly_prompts": 8000},
}


# ═══════════════════════════════════════════════════════
ANTIGRAVITY_USAGE_COMMAND = os.environ.get("AI_ANTIGRAVITY_USAGE_COMMAND", "")

AGY_CREDENTIAL_TARGET = os.environ.get("AI_AGY_CREDENTIAL_TARGET", "gemini:antigravity")

AGY_OAUTH_CLIENT_ID = os.environ.get(
    "AI_AGY_OAUTH_CLIENT_ID",
    "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com",
)

AGY_OAUTH_CLIENT_SECRET_FILE = os.environ.get("AI_AGY_OAUTH_CLIENT_SECRET_FILE", "")

AGY_CLOUDCODE_BASE_URL = os.environ.get(
    "AI_AGY_CLOUDCODE_BASE_URL", "https://daily-cloudcode-pa.googleapis.com"
)

ZAI_PLATFORM_TOKEN = os.environ.get("ZAI_PLATFORM_TOKEN", "")

ZAI_PLATFORM_TOKEN_FILE = os.environ.get(
    "ZAI_PLATFORM_TOKEN_FILE",
    os.path.join(HERMES_HOME, ".zai_platform_token.txt"),
)

ZAI_SESSION_FILE = os.environ.get(
    "ZAI_SESSION_FILE",
    os.path.join(HERMES_HOME, ".zai_session.json"),
)

ZAI_API_KEY = os.environ.get("ZAI_API_KEY", os.environ.get("GLM_API_KEY", ""))

ZAI_API_KEY_FILE = os.environ.get(
    "ZAI_API_KEY_FILE",
    os.path.expanduser("~/.local/share/opencode/auth.json"),
)

ZAI_ORG_ID = os.environ.get(
    "ZAI_ORG_ID", "org-2363d4ce091a445D936A7D3B5EB0C57C"
)

ZAI_PROJECT_ID = os.environ.get(
    "ZAI_PROJECT_ID", "proj_F7F3eC68baF047658a790bcAA12B68d4"
)

class _WindowsCredential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]

def _read_windows_generic_credential(target: str) -> dict | None:
    """Lee una credencial genérica de Windows sin imprimir ni persistir su secreto."""
    if os.name != "nt" or not target:
        return None
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    credential_ptr = ctypes.POINTER(_WindowsCredential)()
    advapi32.CredReadW.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(_WindowsCredential)),
    ]
    advapi32.CredReadW.restype = wintypes.BOOL
    advapi32.CredFree.argtypes = [ctypes.c_void_p]
    if not advapi32.CredReadW(target, 1, 0, ctypes.byref(credential_ptr)):
        return None
    try:
        credential = credential_ptr.contents
        blob = bytes(credential.CredentialBlob[: credential.CredentialBlobSize])
        payload = json.loads(blob.decode("utf-8"))
        return payload if isinstance(payload, dict) else None
    except (UnicodeDecodeError, TypeError, json.JSONDecodeError):
        return None
    finally:
        advapi32.CredFree(credential_ptr)

def _antigravity_oauth_client_secret() -> str:
    """Obtiene el secreto público del cliente desde configuración o el paquete local."""
    if AGY_OAUTH_CLIENT_SECRET_FILE:
        try:
            with open(AGY_OAUTH_CLIENT_SECRET_FILE, encoding="utf-8") as handle:
                secret = handle.read().strip()
            if secret:
                return secret
        except OSError:
            pass

    command = _antigravity_usage_command()
    candidates = []
    if command:
        command_dir = os.path.dirname(os.path.abspath(command))
        candidates.extend(
            [
                os.path.join(command_dir, "node_modules", "antigravity-usage", "dist", "index.js"),
                os.path.join(os.path.dirname(command_dir), "node_modules", "antigravity-usage", "dist", "index.js"),
            ]
        )
    for package_path in candidates:
        try:
            with open(package_path, encoding="utf-8") as handle:
                source = handle.read()
            match = re.search(r'clientSecret:.*?\|\|\s*"([^"]+)"', source)
            if match:
                return match.group(1)
        except OSError:
            continue
    return ""

def _refresh_antigravity_access_token(refresh_token: str) -> str | None:
    """Renueva en memoria el access token guardado por AGY en Windows."""
    client_secret = _antigravity_oauth_client_secret()
    if not refresh_token or not client_secret:
        return None
    payload = urllib.parse.urlencode(
        {
            "refresh_token": refresh_token,
            "client_id": AGY_OAUTH_CLIENT_ID,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=payload, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.loads(response.read())
        token = data.get("access_token")
        return token if isinstance(token, str) and token else None
    except (OSError, TypeError, json.JSONDecodeError):
        return None

def _antigravity_keyring_access_token() -> str | None:
    """Obtiene un token fresco desde la credencial nativa que usa `agy`."""
    credentials = _read_windows_generic_credential(AGY_CREDENTIAL_TARGET)
    token_data = credentials.get("token") if isinstance(credentials, dict) else None
    if not isinstance(token_data, dict):
        return None
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    if isinstance(access_token, str) and access_token and isinstance(refresh_token, str):
        refreshed = _refresh_antigravity_access_token(refresh_token)
        return refreshed or access_token
    return access_token if isinstance(access_token, str) and access_token else None

def _cloudcode_post(endpoint: str, access_token: str, payload: dict) -> dict:
    """Ejecuta una llamada JSON al backend de cuota de Antigravity con fallback."""
    bases = [AGY_CLOUDCODE_BASE_URL]
    if "daily" in AGY_CLOUDCODE_BASE_URL:
        bases.append("https://cloudcode-pa.googleapis.com")
    else:
        bases.append("https://daily-cloudcode-pa.googleapis.com")

    last_error = None
    for base in bases:
        request = urllib.request.Request(
            f"{base}/v1internal:{endpoint}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                data = json.loads(response.read())
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return {}

def _get_antigravity_quota_from_keyring() -> dict:
    """Consulta la cuota de AGY CLI usando su credencial de Windows y Cloud Code."""
    access_token = _antigravity_keyring_access_token()
    if not access_token:
        return {"status": "error", "error": "Credencial gemini:antigravity no disponible"}

    metadata = {
        "ideType": "ANTIGRAVITY",
        "platform": "PLATFORM_UNSPECIFIED",
        "pluginType": "GEMINI",
    }
    loaded = _cloudcode_post("loadCodeAssist", access_token, {"metadata": metadata})
    project_id = loaded.get("cloudaicompanionProject")
    if not project_id:
        allowed_tiers = loaded.get("allowedTiers") or []
        tier = next((item for item in allowed_tiers if item.get("isDefault")), None)
        tier = tier or next((item for item in allowed_tiers if item.get("id")), None)
        if tier and tier.get("id"):
            onboarded = _cloudcode_post(
                "onboardUser", access_token, {"tierId": tier["id"], "metadata": metadata}
            )
            project_id = ((onboarded.get("response") or {}).get("cloudaicompanionProject"))
    if not isinstance(project_id, str) or not project_id:
        return {"status": "error", "error": "AGY no devolvió un proyecto de Cloud Code"}

    # 1. Consultar resumen de cuota por grupos (contiene límites reales 5h y semanal)
    summary = {}
    try:
        summary = _cloudcode_post(
            "retrieveUserQuotaSummary", access_token, {"project": project_id}
        )
    except Exception:
        summary = {}

    # 2. Consultar catálogo de modelos individuales
    available = {}
    try:
        available = _cloudcode_post(
            "fetchAvailableModels", access_token, {"project": project_id}
        )
    except Exception:
        available = {}

    models = available.get("models") if isinstance(available, dict) else {}
    model_rows = []
    if isinstance(models, dict):
        for model_id, model_data in models.items():
            quota_info = model_data.get("quotaInfo") or {}
            remaining = quota_info.get("remainingFraction")
            if remaining is None:
                continue
            model_rows.append(
                {
                    "modelId": model_id,
                    "label": model_id,
                    "remainingPercentage": float(remaining),
                    "resetTime": quota_info.get("resetTime"),
                }
            )

    summary_groups = summary.get("groups") if isinstance(summary, dict) else None
    if isinstance(summary_groups, list) and summary_groups:
        groups = _normalise_antigravity_summary_groups(summary_groups, model_rows)
    elif model_rows:
        groups = _normalise_antigravity_groups(model_rows)
    else:
        return {"status": "error", "error": "AGY no devolvió ventanas de cuota"}

    return {
        "status": "ok",
        "data": {
            "models": model_rows,
            "groups": groups,
            "tier": loaded.get("paidTier") or loaded.get("currentTier"),
        },
        "source": "AGY CLI Windows Credential Manager + Cloud Code",
    }

def _antigravity_usage_command() -> str | None:
    """Encuentra el shim instalado aunque el monitor arranque desde Task Scheduler."""
    candidates = []
    if ANTIGRAVITY_USAGE_COMMAND:
        candidates.append(ANTIGRAVITY_USAGE_COMMAND)
    discovered = shutil.which("antigravity-usage")
    if discovered:
        candidates.append(discovered)

    prefixes = [os.environ.get("NPM_CONFIG_PREFIX", "")]
    prefixes.append(os.path.join(os.environ.get("APPDATA", ""), "npm"))
    for prefix in prefixes:
        if prefix:
            candidates.append(os.path.join(prefix, "antigravity-usage.cmd"))

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        candidates.append(
            os.path.join(
                local_app_data,
                "Zed",
                "external_agents",
                "registry",
                "npx",
                "codex-acp",
                "antigravity-usage.cmd",
            )
        )

    for candidate in candidates:
        if os.path.isfile(candidate) or shutil.which(candidate):
            return candidate
    return None

def cleanup_orphan_mei(max_age_seconds: int = 1800) -> None:
    """Borra carpetas _MEI* de PyInstaller que el harness de Antigravity deja en Temp.

    El localharness de Antigravity extrae ~0,9 GB por ejecución y no siempre se
    limpia al salir; sin este barrido Temp crece hasta llenar el disco. Solo se
    tocan carpetas con más de max_age_seconds de antigüedad; las de procesos
    vivos tienen sus DLL bloqueadas y shutil las deja parcialmente intactas.
    """
    temp_dir = os.environ.get("TEMP", "")
    if not temp_dir:
        return
    cutoff = time.time() - max_age_seconds
    try:
        entries = list(os.scandir(temp_dir))
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith("_MEI"):
            continue
        try:
            if not entry.is_dir() or entry.stat().st_mtime > cutoff:
                continue
            shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            continue

def _iso_to_countdown(iso_str: str | None) -> str:
    """Convierte timestamp ISO (ej: '2026-09-08T01:49:44Z') a countdown legible (ej: '2h 48m' o '2d 22h')."""
    if not iso_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = dt - now
        total_secs = diff.total_seconds()
        if total_secs <= 0:
            return "Ya reseteado"
        hours = int(total_secs // 3600)
        mins = int((total_secs % 3600) // 60)
        if hours >= 24:
            days = hours // 24
            rem_hours = hours % 24
            return f"{days}d {rem_hours}h"
        return f"{hours}h {mins}m"
    except Exception:
        return "N/A"

def _normalise_antigravity_summary_groups(
    summary_groups: list[dict], model_rows: list[dict] | None = None
) -> list[dict]:
    """Extrae las cuotas 5h y semanal de cada familia desde retrieveUserQuotaSummary."""
    groups = []
    model_rows = model_rows or []

    for item in summary_groups:
        display_name = item.get("displayName", "")
        family = "Gemini" if "gemini" in display_name.lower() else "Claude y GPT"

        remaining_5h = None
        reset_5h_at = None
        desc_5h = ""
        weekly_remaining = None
        reset_weekly_at = None
        desc_weekly = ""

        for bucket in item.get("buckets", []):
            window = (bucket.get("window") or "").lower()
            bucket_id = (bucket.get("bucketId") or "").lower()
            frac = bucket.get("remainingFraction")
            pct = round(float(frac) * 100, 1) if frac is not None else 100.0
            reset_t = bucket.get("resetTime")
            desc = bucket.get("description", "")

            if "5h" in window or "5h" in bucket_id:
                remaining_5h = pct
                reset_5h_at = reset_t
                desc_5h = desc
            elif "week" in window or "weekly" in bucket_id:
                weekly_remaining = pct
                reset_weekly_at = reset_t
                desc_weekly = desc

        if model_rows:
            if family == "Gemini":
                count = sum(1 for m in model_rows if "gemini" in str(m.get("label", "")).lower())
            else:
                count = sum(1 for m in model_rows if "gemini" not in str(m.get("label", "")).lower())
        else:
            count = 18 if family == "Gemini" else 7

        groups.append(
            {
                "label": family,
                "remaining_5h_percent": remaining_5h if remaining_5h is not None else 100.0,
                "reset_5h_at": reset_5h_at,
                "reset_5h_in": _iso_to_countdown(reset_5h_at),
                "description_5h": desc_5h,
                "weekly_remaining_percent": weekly_remaining,
                "reset_weekly_at": reset_weekly_at,
                "reset_weekly_in": _iso_to_countdown(reset_weekly_at),
                "description_weekly": desc_weekly,
                "model_count": count,
            }
        )

    groups.sort(key=lambda g: 0 if g["label"] == "Gemini" else 1)
    return groups

def _read_zai_platform_token() -> str:
    """Lee un token explícitamente configurado sin imprimirlo ni persistirlo."""
    if ZAI_PLATFORM_TOKEN.strip():
        return ZAI_PLATFORM_TOKEN.strip()

    for path in (ZAI_PLATFORM_TOKEN_FILE, ZAI_SESSION_FILE):
        if not path or not os.path.isfile(path):
            continue
        try:
            if path == ZAI_PLATFORM_TOKEN_FILE:
                with open(path, encoding="utf-8") as handle:
                    token = handle.read().strip()
                if token:
                    return token
                continue

            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                for key in ("platform_token", "zai_platform_token", "access_token", "jwt"):
                    token = payload.get(key)
                    if isinstance(token, str) and token.strip():
                        return token.strip()
        except (OSError, TypeError, json.JSONDecodeError):
            continue
    return ""

def _read_zai_api_key() -> str:
    """Descubre el API key de Z.AI usado por OpenCode sin imprimirlo."""
    if ZAI_API_KEY.strip():
        return ZAI_API_KEY.strip()
    try:
        with open(ZAI_API_KEY_FILE, encoding="utf-8") as handle:
            payload = json.load(handle)
        provider = payload.get("zai-coding-plan") if isinstance(payload, dict) else None
        key = provider.get("key") if isinstance(provider, dict) else None
        return key.strip() if isinstance(key, str) else ""
    except (OSError, TypeError, AttributeError, json.JSONDecodeError):
        return ""

def _get_zai_quota_with_api_key(api_key: str) -> dict:
    """Consulta la cuota con el formato de autenticación usado por OpenCode."""
    import urllib.request

    url = "https://api.z.ai/api/monitor/usage/quota/limit"
    req = urllib.request.Request(url)
    # Z.AI espera el API key directamente, no el prefijo Bearer, en este endpoint.
    req.add_header("Authorization", api_key)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
        data = body.get("data", {})
        return {
            "status": "ok",
            "data": {
                "level": data.get("level", "unknown"),
                "limits": _normalise_zai_limits(data.get("limits", [])),
                "email": None,
            },
            "source": "Z.AI quota API key",
        }
    except urllib.error.HTTPError as e:
        return {"status": "error", "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _ts_to_iso(ts) -> str | None:
    """Convierte timestamp Unix o ISO a ISO string."""
    if not ts:
        return None
    if isinstance(ts, str):
        return ts
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()  # noqa: UP017
    except Exception:
        return str(ts)


def _ts_to_countdown(ts) -> str:
    """Convierte timestamp Unix o cadena ISO 8601 a countdown legible (ej: '3h 42m')."""
    if not ts:
        return "N/A"
    try:
        now = datetime.now(tz=timezone.utc)  # noqa: UP017
        if isinstance(ts, (int, float)):
            reset = datetime.fromtimestamp(ts, tz=timezone.utc)  # noqa: UP017
        elif isinstance(ts, str):
            clean_ts = ts.strip().replace("Z", "+00:00")
            reset = datetime.fromisoformat(clean_ts)
            if reset.tzinfo is None:
                reset = reset.replace(tzinfo=timezone.utc)
        elif isinstance(ts, datetime):
            reset = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        else:
            return "N/A"

        diff = reset - now
        total_secs = diff.total_seconds()
        if total_secs <= 0:
            return "Ya reseteado"
        hours = int(total_secs // 3600)
        mins = int((total_secs % 3600) // 60)
        if hours >= 24:
            days = hours // 24
            hours = hours % 24
            return f"{days}d {hours}h"
        return f"{hours}h {mins}m"
    except Exception:
        return "N/A"


#  1. ANTIGRAVITY IDE
# ═══════════════════════════════════════════════════════


def _antigravity_family(label: str) -> str:
    name = label.lower()
    if "argon" in name:
        return "Gemini 4 Argon"
    if "gemini" in name:
        return "Gemini"
    if "claude" in name or "gpt" in name:
        return "Claude y GPT"
    return label.strip()


def _run_quota_command(command: list[str], *, shell: bool = False) -> subprocess.CompletedProcess:
    startup = None
    if sys.platform == "win32":
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = subprocess.SW_HIDE
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        stdin=subprocess.DEVNULL,
        shell=shell,
        startupinfo=startup,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _parse_agy_usage_output(output: str) -> dict | None:
    """Convierte la salida oficial de `agy /usage` a grupos de cuota."""
    groups: dict[str, dict] = {}
    for line in output.splitlines():
        fields = [field.strip() for field in line.split("\t")]
        if len(fields) < 3:
            continue
        family_raw, window_raw, remaining_raw = fields[:3]
        window = window_raw.lower()
        if "weekly" not in window and "five hour" not in window:
            continue
        match = re.search(r"(\d+(?:\.\d+)?)\s*%", remaining_raw)
        if not match:
            continue
        remaining = float(match.group(1))
        family = _antigravity_family(family_raw)
        group = groups.setdefault(
            family,
            {
                "label": family,
                "remaining_5h_percent": None,
                "reset_5h_at": None,
                "reset_5h_in": None,
                "weekly_remaining_percent": None,
                "reset_weekly_at": None,
                "reset_weekly_in": None,
                "model_count": 0,
            },
        )
        reset_at = fields[3] if len(fields) > 3 and fields[3] else None
        if "weekly" in window:
            group["weekly_remaining_percent"] = remaining
            group["reset_weekly_at"] = reset_at
            group["reset_weekly_in"] = _ts_to_countdown(reset_at)
        else:
            group["remaining_5h_percent"] = remaining
            group["reset_5h_at"] = reset_at
            group["reset_5h_in"] = _ts_to_countdown(reset_at)

    if not groups:
        return None
    return {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "method": "agy",
        "groups": list(groups.values()),
    }


def get_antigravity_quota() -> dict:
    """Obtiene la cuota de Antigravity, conservando el último dato válido."""
    def _load_last_known() -> dict | None:
        try:
            with open(AGY_SNAPSHOT_FILE, encoding="utf-8") as handle:
                cached = json.load(handle)
            if isinstance(cached, dict) and isinstance(cached.get("data"), dict):
                cached["status"] = "stale"
                cached["data"]["stale"] = True
                for g in cached["data"].get("groups", []):
                    if g.get("reset_5h_at"):
                        g["reset_5h_in"] = _ts_to_countdown(g["reset_5h_at"])
                    if g.get("reset_weekly_at"):
                        g["reset_weekly_in"] = _ts_to_countdown(g["reset_weekly_at"])
                return cached
        except (OSError, TypeError, json.JSONDecodeError):
            return None
        return None

    def _save_last_known(payload: dict) -> None:
        try:
            temp = f"{AGY_SNAPSHOT_FILE}.tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            os.replace(temp, AGY_SNAPSHOT_FILE)
        except OSError:
            pass

    try:
        result = _run_quota_command(
            ["agy", "--print", "/usage"],
        )
        if result.returncode == 0:
            data = _parse_agy_usage_output(result.stdout)
            if data:
                data["stale"] = False
                payload = {"status": "ok", "data": data, "source": "agy /usage"}
                _save_last_known(payload)
                return payload
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass

    cached = _load_last_known()
    if cached and cached["data"].get("method") == "agy":
        return cached

    try:
        direct = _get_antigravity_quota_from_keyring()
        if direct.get("status") == "ok":
            direct["data"]["stale"] = False
            _save_last_known(direct)
            return direct
    except (OSError, TypeError, ValueError, json.JSONDecodeError, urllib.error.HTTPError):
        pass

    try:
        result = _run_quota_command(
            [_antigravity_usage_command() or "antigravity-usage", "quota", "--json", "--refresh"],
            shell=True,
        )
        if result.returncode == 0:
            try:
                data = json.loads(result.stdout)
                if isinstance(data.get("models"), list):
                    data["groups"] = _normalise_antigravity_groups(data["models"])
                data["stale"] = False
                payload = {
                    "status": "ok",
                    "data": data,
                    "source": "antigravity-usage CLI",
                }
                _save_last_known(payload)
                return payload
            except json.JSONDecodeError:
                pass
        err = (result.stderr or result.stdout or "").strip()
        cached = _load_last_known()
        if cached:
            cached["data"]["stale_error"] = err[:200]
            return cached
        return {
            "status": "error",
            "error": err[:200],
            "hint": "Ejecuta 'antigravity-usage login' o abre Antigravity para actualizar.",
        }
    except FileNotFoundError:
        cached = _load_last_known()
        if cached:
            return cached
        return {
            "status": "error",
            "error": "antigravity-usage no instalado",
            "hint": "Instala con: npm install -g antigravity-usage",
        }
    except subprocess.TimeoutExpired:
        cached = _load_last_known()
        if cached:
            return cached
        return {"status": "error", "error": "Timeout consultando Antigravity"}
    except Exception as e:
        cached = _load_last_known()
        if cached:
            return cached
        return {"status": "error", "error": str(e)}


def _normalise_antigravity_groups(models: list[dict]) -> list[dict]:
    """Colapsa aliases por familia usando la menor cuota y el primer reset disponible."""
    groups = {}
    for model in models:
        raw_remaining = model.get("remainingPercentage")
        reset_at = model.get("resetTime")
        if raw_remaining is None:
            continue
        try:
            remaining = float(raw_remaining)
        except (TypeError, ValueError):
            continue
        if remaining > 1:
            remaining /= 100
        if not 0 <= remaining <= 1:
            continue
        label = str(model.get("label") or model.get("modelId") or "")
        family = _antigravity_family(label)
        key = family
        group = groups.setdefault(
            key,
            {
                "label": family,
                "remaining_5h_percent": round(float(remaining) * 100, 1),
                "reset_5h_at": reset_at,
                "reset_5h_in": _ts_to_countdown(reset_at),
                "weekly_remaining_percent": None,
                "reset_weekly_at": None,
                "reset_weekly_in": None,
                "model_count": 0,
            },
        )
        group["remaining_5h_percent"] = min(group["remaining_5h_percent"], round(remaining * 100, 1))
        if reset_at and (not group["reset_5h_at"] or str(reset_at) < str(group["reset_5h_at"])):
            group["reset_5h_at"] = reset_at
            group["reset_5h_in"] = _ts_to_countdown(reset_at)
        group["model_count"] += 1
    return list(groups.values())


# ═══════════════════════════════════════════════════════
#  2. OPENAI CODEX  (lee rate_limits de rollout-*.jsonl)
# ═══════════════════════════════════════════════════════


def _jwt_claims(token: str) -> dict:
    """Decodifica claims locales para obtener el account id; no valida ni persiste tokens."""
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _local_zai_email() -> str | None:
    """Lee sólo el correo del JWT local de Z.AI; nunca devuelve el token."""
    token = _read_zai_platform_token()
    try:
        customer = _jwt_claims(token).get("customer") or {}
        email = customer.get("email") if isinstance(customer, dict) else None
        return email if isinstance(email, str) and "@" in email else None
    except (IndexError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _token_is_expired(token: str) -> bool:
    try:
        exp = _jwt_claims(token).get("exp")
    except (ValueError, IndexError, KeyError, json.JSONDecodeError):
        return True
    return isinstance(exp, (int, float)) and exp <= time.time()


def _dedupe_credentials(candidates: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """Deduplica por chatgpt_account_id; ante la misma cuenta prefiere el token vigente."""
    credentials: dict[str, tuple[str, str, bool]] = {}
    for label, token in candidates:
        try:
            openai_auth = _jwt_claims(token).get("https://api.openai.com/auth") or {}
            account_id = openai_auth.get("chatgpt_account_id")
        except (ValueError, IndexError, KeyError, json.JSONDecodeError):
            continue
        if not account_id:
            continue
        expired = _token_is_expired(token)
        if account_id not in credentials or (credentials[account_id][2] and not expired):
            credentials[account_id] = (label, token, expired)
    return [(label, token, account_id) for account_id, (label, token, _exp) in credentials.items()]


def _codex_credentials() -> list[tuple[str, str, str]]:
    """Descubre las cuentas OAuth ya gestionadas por Codex, Hermes y OpenCode."""
    candidates = []
    try:
        with open(CODEX_AUTH_FILE, encoding="utf-8") as f:
            token = json.load(f)["tokens"]["access_token"]
        candidates.append(("Codex", token))
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        pass

    try:
        with open(HERMES_AUTH_FILE, encoding="utf-8") as f:
            auth = json.load(f)
        provider = (auth.get("providers") or {}).get("openai-codex") or {}
        token = (provider.get("tokens") or {}).get("access_token")
        if token:
            candidates.append(("Hermes", token))
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        pass

    try:
        result = _run_quota_command(["opencode", "auth", "export", "openai"])
        if result.returncode == 0:
            exported = json.loads(result.stdout)
            if isinstance(exported, list):
                for account in exported:
                    if not isinstance(account, dict):
                        continue
                    value = account.get("value") or {}
                    if isinstance(value, dict) and value.get("type") == "oauth":
                        token = value.get("access")
                        if isinstance(token, str) and token:
                            candidates.append(("OpenCode", token))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass

    try:
        with open(OPENCODE_AUTH_FILE, encoding="utf-8") as f:
            provider = json.load(f).get("openai") or {}
        token = provider.get("access")
        if token:
            candidates.append(("OpenCode", token))
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        pass

    return _dedupe_credentials(candidates)


def _normalise_codex_window(window: dict, default_seconds: int) -> dict:
    raw_used = window.get("used_percent")
    used = float(raw_used) if raw_used is not None else None
    reset_at = window.get("reset_at")
    seconds = window.get("limit_window_seconds") or default_seconds
    return {
        "used_percent": used,
        "remaining_percent": round(max(0, 100 - used), 1) if used is not None else None,
        "window_hours": seconds / 3600,
        "resets_at": reset_at,
        "resets_at_iso": _ts_to_iso(reset_at),
        "resets_in": _ts_to_countdown(reset_at),
    }


def codex_windows_with_data(account: dict) -> list[tuple[str, dict]]:
    """Ventanas de cuota con datos reales, etiquetadas por su duración.

    La API de uso cambió de forma: hoy `primary_window` puede ser la ventana
    semanal (604800s) con `secondary_window` en null, así que no se asume
    la duración por posición (primary/secondary) sino por window_hours.
    """
    fallbacks = {"primary": "5h", "secondary": "7d"}
    windows = []
    for slot in ("primary", "secondary"):
        window = account.get(slot) or {}
        if window.get("remaining_percent") is None:
            continue
        windows.append((_codex_window_label(_codex_window_hours(window), fallbacks[slot]), window))
    return windows


def _codex_window_hours(window: dict) -> float | None:
    """Duración real de la ventana en horas; None si la fuente no la expone."""
    hours = window.get("window_hours")
    if hours is None:
        days = window.get("window_days")
        hours = days * 24 if days is not None else None
    return hours


def _codex_window_label(hours: float | None, fallback: str) -> str:
    if hours is None:
        return fallback
    if hours <= 5:
        return "5h"
    if hours >= 168:
        return "7d"
    return f"{hours:.0f}h"


def _codex_min_remaining(account: dict) -> float:
    """Restante mínimo entre ventanas expuestas; inf si no hay datos."""
    pcts = [window["remaining_percent"] for _, window in codex_windows_with_data(account)]
    return min(pcts) if pcts else float("inf")


def _normalise_codex_resets(
    reset_credits_payload: dict | None,
    fallback_data: dict | None = None,
) -> dict:
    avail = None
    if reset_credits_payload and isinstance(reset_credits_payload, dict):
        avail = reset_credits_payload.get("available_count")
    if avail is None and fallback_data and isinstance(fallback_data, dict):
        avail = fallback_data.get("available_count")
    if avail is None:
        avail = 0

    credits_list = []
    if reset_credits_payload and isinstance(reset_credits_payload.get("credits"), list):
        now = datetime.now(timezone.utc)
        for c in reset_credits_payload["credits"]:
            if c.get("status") != "available" or c.get("redeemed_at"):
                continue
            exp_str = c.get("expires_at")
            exp_dt = None
            if exp_str:
                try:
                    exp_dt = datetime.fromisoformat(exp_str.replace("Z", "+00:00"))
                    if exp_dt.tzinfo is None:
                        exp_dt = None
                except Exception:
                    pass
            countdown = "N/A"
            date_str = "N/A"
            is_expired = False
            if exp_dt:
                diff = exp_dt - now
                total_secs = diff.total_seconds()
                if total_secs <= 0:
                    is_expired = True
                    countdown = "Expirado"
                else:
                    hours = int(total_secs // 3600)
                    mins = int((total_secs % 3600) // 60)
                    if hours >= 24:
                        days = hours // 24
                        hours = hours % 24
                        countdown = f"{days}d {hours}h"
                    else:
                        countdown = f"{hours}h {mins}m"
                date_str = _format_local_datetime(exp_str)

            if not is_expired:
                credits_list.append({
                    "id": c.get("id"),
                    "expires_at": exp_str,
                    "date": date_str,
                    "countdown": countdown,
                    "exp_dt": exp_dt,
                })

    if credits_list:
        credits_list.sort(key=lambda x: x["exp_dt"] or datetime.max.replace(tzinfo=timezone.utc))
        if not reset_credits_payload or reset_credits_payload.get("available_count") is None:
            avail = len(credits_list)

    if avail == 0:
        summary = "Sin resets disponibles"
        short_summary = "0 disp."
    elif not credits_list:
        summary = f"{avail} disponibles" if avail > 1 else f"{avail} disponible"
        short_summary = f"{avail} disp."
    else:
        parts = [f"Reset {credit['date']} ({credit['countdown']})" for credit in credits_list]
        summary = " · ".join(parts)
        short_summary = f"{avail} disp. ({parts[0]})"

    return {
        "available_count": avail,
        "credits": [
            {k: v for k, v in c.items() if k != "exp_dt"}
            for c in credits_list
        ],
        "summary": summary,
        "short_summary": short_summary,
    }



def _fetch_codex_account(label: str, token: str, account_id: str) -> dict:
    headers = {
        "Authorization": f"Bearer {token}",
        "ChatGPT-Account-ID": account_id,
        "Accept": "application/json",
        "User-Agent": "ai-usage-monitor/1.0",
    }
    request = urllib.request.Request(CODEX_USAGE_URL, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    rate_limit = payload.get("rate_limit") or {}

    reset_credits_payload = None
    try:
        rc_request = urllib.request.Request(CODEX_RESET_CREDITS_URL, headers=headers)
        with urllib.request.urlopen(rc_request, timeout=10) as rc_resp:
            reset_credits_payload = json.load(rc_resp)
    except Exception:
        pass

    reset_credits = _normalise_codex_resets(
        reset_credits_payload=reset_credits_payload,
        fallback_data=payload.get("rate_limit_reset_credits"),
    )

    return {
        "label": label,
        "email": payload.get("email"),
        "plan_type": payload.get("plan_type", "unknown"),
        "primary": _normalise_codex_window(rate_limit.get("primary_window") or {}, 18_000),
        "secondary": _normalise_codex_window(rate_limit.get("secondary_window") or {}, 604_800),
        "credits": payload.get("credits"),
        "reset_credits": reset_credits,
        "source": "OpenAI usage API",
    }


def get_codex_quota() -> dict:
    """Consulta en vivo las cuentas distintas de Codex y Hermes."""
    accounts = []
    unavailable = []
    errors = []
    for label, token, account_id in _codex_credentials():
        try:
            accounts.append(_fetch_codex_account(label, token, account_id))
        except urllib.error.HTTPError as exc:
            errors.append(f"{label}: HTTP {exc.code}")
            unavailable.append({"label": label, "status": "error", "error": f"HTTP {exc.code}"})
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{label}: {exc}")
            unavailable.append({"label": label, "status": "error", "error": "No se pudo consultar la cuota"})

    if not accounts:
        fallback = get_codex_quota_from_sessions()
        if errors and fallback.get("status") != "ok":
            fallback["error"] = "; ".join(errors)
            fallback["data"] = {"accounts": unavailable}
        return fallback

    # Compatibilidad: consumidores antiguos ven la cuenta con menor cuota expuesta.
    summary = min(accounts, key=_codex_min_remaining)
    return {"status": "ok", "data": {**summary, "accounts": accounts + unavailable}}


def get_codex_quota_from_sessions() -> dict:
    """
    Lee el evento rate_limits más reciente de los archivos rollout-*.jsonl
    en ~/.codex/sessions/. No requiere API — es 100% local.

    El formato del evento en el JSONL es:
      "rate_limits": {
        "limit_id": "codex",
        "plan_type": "plus",
        "primary":   {"used_percent": 9.0, "window_minutes": 300, "resets_at": 1782507489},
        "secondary": {"used_percent": 17.0, "window_minutes": 10080, "resets_at": 1782967381},
        "credits": null
      }
    """
    # Buscar todos los archivos rollout-*.jsonl, ordenados por fecha (más reciente primero)
    pattern = os.path.join(CODEX_SESSIONS_DIR, "**", "rollout-*.jsonl")
    rollout_files = sorted(glob.glob(pattern, recursive=True), reverse=True)

    if not rollout_files:
        return {
            "status": "error",
            "error": "No se encontraron archivos de sesión de Codex",
            "hint": f"Verifica que existe: {CODEX_SESSIONS_DIR}",
        }

    # Buscar el rate_limits más reciente en cualquiera de los archivos
    latest_rate_limits = None
    latest_file = None

    for filepath in rollout_files[:10]:
        try:
            with open(filepath, encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
            # Iterar en reversa para encontrar el ÚLTIMO rate_limits del archivo
            for line in reversed(lines):
                if '"rate_limits"' not in line:
                    continue
                # Intentar parsear la línea completa como JSON primero
                try:
                    data = json.loads(line)
                    rl = data.get("rate_limits")
                    if not rl:
                        for key in ("token_count", "payload", "meta"):
                            nested = data.get(key, {})
                            if isinstance(nested, dict) and "rate_limits" in nested:
                                rl = nested["rate_limits"]
                                break
                    if rl and (rl.get("primary") or rl.get("secondary")):
                        latest_rate_limits = rl
                        latest_file = filepath
                        break  # Encontramos el más reciente de este archivo
                except json.JSONDecodeError:
                    # Regex fallback
                    match = re.search(
                        r'"rate_limits"\s*:\s*(\{.*?"primary"\s*:\s*\{[^}]*\}.*?\})', line
                    )
                    if match:
                        try:
                            rl = json.loads(match.group(1))
                            if rl.get("primary") or rl.get("secondary"):
                                latest_rate_limits = rl
                                latest_file = filepath
                                break
                        except json.JSONDecodeError:
                            continue
            # Si ya encontramos datos, no seguir con archivos más antiguos
            if latest_rate_limits:
                break
        except OSError:
            continue

    if not latest_rate_limits:
        return {
            "status": "error",
            "error": "No se encontraron datos de rate_limits en las sesiones de Codex",
            "hint": "Usa Codex CLI al menos una vez para generar datos de cuota",
        }

    # Procesar los datos
    plan_type = latest_rate_limits.get("plan_type", "unknown")
    limit_id = latest_rate_limits.get("limit_id", "codex")
    primary = latest_rate_limits.get("primary") or {}
    secondary = latest_rate_limits.get("secondary") or {}
    credits = latest_rate_limits.get("credits")

    result = {
        "status": "ok",
        "data": {
            "plan_type": plan_type,
            "limit_id": limit_id,
            "primary": {
                "used_percent": primary.get("used_percent", 0),
                "remaining_percent": round(100 - primary.get("used_percent", 0), 1),
                "window_hours": (primary.get("window_minutes", 300) or 300) / 60,
                "resets_at": primary.get("resets_at"),
                "resets_at_iso": _ts_to_iso(primary.get("resets_at")),
                "resets_in": _ts_to_countdown(primary.get("resets_at")),
            },
            "secondary": {
                "used_percent": secondary.get("used_percent", 0),
                "remaining_percent": round(100 - secondary.get("used_percent", 0), 1),
                "window_days": (secondary.get("window_minutes", 10080) or 10080) / 1440,
                "resets_at": secondary.get("resets_at"),
                "resets_at_iso": _ts_to_iso(secondary.get("resets_at")),
                "resets_in": _ts_to_countdown(secondary.get("resets_at")),
            },
            "credits": credits,
            "reset_credits": None,
            "source_file": os.path.basename(latest_file) if latest_file else None,
        },
    }

    return result



# ═══════════════════════════════════════════════════════
#  3. Z.AI CODING PLAN  (probe + detección de 429)
# ═══════════════════════════════════════════════════════


def get_zai_quota() -> dict:
    """
    Obtiene la cuota REAL de Z.AI Coding Plan desde la API interna.
    Endpoint: GET https://api.z.ai/api/monitor/usage/quota/limit
    Prefiere la sesión ZCode; conserva fuentes explícitas cuando no está instalada.
    """
    import urllib.error
    import urllib.request

    try:
        session = read_zcode_session()
    except ValueError as error:
        return {"status": "error", "error": str(error)}
    if session:
        return _get_zcode_zai_quota(session)
    api_key = _read_zai_api_key()
    token = os.environ.get("ZAI_PLATFORM_TOKEN", "").strip()
    if not api_key and not token:
        token_path = os.environ.get("ZAI_PLATFORM_TOKEN_FILE", os.path.expanduser("~/AppData/Local/hermes/.zai_platform_token.txt"))
        try:
            with open(token_path, encoding="utf-8") as handle:
                token = handle.read().strip()
        except OSError:
            pass
    if not api_key and not token:
        return {"status": "error", "error": "Falta autenticación de Z.AI. Configura ZAI_API_KEY o inicia sesión en OpenCode."}
    url = "https://api.z.ai/api/monitor/usage/quota/limit"
    req = urllib.request.Request(url)
    req.add_header("Authorization", api_key or f"Bearer {token}")
    if not api_key:
        for name, env in (("bigmodel-organization", "ZAI_ORG_ID"), ("bigmodel-project", "ZAI_PROJECT_ID")):
            if os.environ.get(env):
                req.add_header(name, os.environ[env])
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.load(resp)
        return _normalise_zai_response(body)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {
                "status": "error",
                "error": "Token expirado",
                "hint": "Abre z.ai en tu navegador y ejecuta extract_zai_cookies.py",
            }
        return {"status": "error", "error": f"Z.AI respondió HTTP {e.code}."}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"status": "error", "error": "No se pudo consultar la cuota de Z.AI."}



def _normalise_zai_limits(limits: list[dict]) -> list[dict]:
    """Etiqueta la ventana de modelos y la bolsa de herramientas sin inventar un 7d."""
    result = []
    for limit in limits:
        limit_type = limit.get("type", "")
        raw_used = limit.get("percentage")
        raw_remaining = limit.get("remaining")
        used = float(raw_used) if raw_used is not None else None
        remaining = (
            float(raw_remaining)
            if raw_remaining is not None
            else (100 - used if used is not None else None)
        )
        details = limit.get("usageDetails") or []
        if limit_type in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
            unit, number = limit.get("unit"), limit.get("number")
            minutes = {1: 1440, 3: 60, 5: 1, 6: 10080}.get(unit, 0) * (number or 0)
            if minutes == 10080:
                kind, label = "coding_weekly", "Modelos GLM · ventana 7d"
            elif minutes == 300 or limit_type == "TOKENS_LIMIT" and unit is None:
                kind, label = "coding_5h", "Modelos GLM · ventana 5h"
            else:
                kind, label = "other", "Modelos GLM · otra ventana"
        elif limit_type == "TIME_LIMIT":
            kind = "mcp_tools"
            label = "Herramientas MCP"
        else:
            kind = "other"
            label = limit_type

        item = {
            "type": limit_type,
            "kind": kind,
            "label": label,
            "used_percent": used,
            "remaining_percent": remaining,
            "reset_at": _ts_to_iso(limit["nextResetTime"] / 1000) if isinstance(limit.get("nextResetTime"), (int, float)) else None,
            "reset_in": _ts_to_countdown_ms(limit.get("nextResetTime")),
        }
        if details:
            item["models"] = {detail["modelCode"]: detail["usage"] for detail in details}
        result.append(item)
    return result



def _ts_to_countdown_ms(ts_ms) -> str:
    """Convierte timestamp en milisegundos a countdown legible."""
    if not ts_ms:
        return "N/A"
    try:
        ts = ts_ms / 1000
        return _ts_to_countdown(ts)
    except Exception:
        return "N/A"


# ═══════════════════════════════════════════════════════
#  RENDERIZADO
# ═══════════════════════════════════════════════════════


def render_terminal(all_data: dict) -> str:
    """Renderiza el dashboard en formato terminal."""
    now = datetime.now().strftime("%H:%M:%S")
    lines = []
    lines.append("")
    lines.append("  ╔═══════════════════════════════════════════════════════════════╗")
    lines.append("  ║          🤖 DASHBOARD DE CUOTA IA — Unificado                 ║")
    lines.append(f"  ║  Actualizado: {now}                                     ║")
    lines.append("  ╚═══════════════════════════════════════════════════════════════╝")

    # ─── Antigravity ───
    ag = all_data.get("antigravity", {})
    lines.append("")
    lines.append("  ┌─ 🛸 Antigravity IDE ──────────────────────────────────────────┐")
    if ag.get("status") == "ok":
        data = ag.get("data", {})
        groups = data.get("groups", [])
        if groups:
            for group in groups:
                name = group.get("label", "?")
                pct = group.get("remaining_5h_percent", 0)
                bar = _progress_bar(pct)
                emoji = "🟢" if pct > 50 else "🟡" if pct > 20 else "🔴"
                r5h = group.get("reset_5h_in") or _ts_to_countdown(group.get("reset_5h_at"))
                reset_info = f" · reset: {r5h}" if r5h and r5h != "N/A" else ""
                lines.append(f"  │ {emoji} {name[:20]:<20} {bar} {pct:>5.1f}% (5h{reset_info})")
                weekly = group.get("weekly_remaining_percent")
                if isinstance(weekly, (int, float)):
                    rw = group.get("reset_weekly_in") or _ts_to_countdown(group.get("reset_weekly_at"))
                    rw_info = f" (reset: {rw})" if rw and rw != "N/A" else ""
                    lines.append(f"  │    Cuota semanal (7d): {weekly:>5.1f}%{rw_info}")
                else:
                    lines.append("  │    Cuota semanal: no expuesta por la fuente de solo lectura")
        else:
            lines.append("  │ ✅ Conectado (sin datos detallados de modelo)                   │")
    elif ag.get("status") == "error":
        err = ag.get("error", "Error")[:55]
        lines.append(f"  │ ❌ {err:<59} │")
        if ag.get("hint"):
            hint = ag["hint"][:55]
            lines.append(f"  │ 💡 {hint:<59} │")
    lines.append("  └────────────────────────────────────────────────────────────────┘")

    # ─── Codex ───
    cx = all_data.get("codex", {})
    lines.append("")
    lines.append("  ┌─ 🔵 OpenAI Codex ─────────────────────────────────────────────┐")
    if cx.get("status") == "ok":
        data = cx.get("data", {})
        for account in data.get("accounts") or [data]:
            label = account.get("label", "Codex")
            plan = account.get("plan_type", "?")
            lines.append(f"  │ 📋 {label:<8} ChatGPT {plan.upper():<10}                         │")
            windows = codex_windows_with_data(account)
            if not windows:
                lines.append("  │    Sin ventanas de cuota expuestas por la API")
            for window_name, window in windows:
                remaining = window.get("remaining_percent", 0)
                lines.append(
                    f"  │    {window_name}: {remaining:>5.1f}% ({window.get('resets_in', '?')})"
                )
            reset_credits = account.get("reset_credits") or {}
            avail = reset_credits.get("available_count", 0)
            summary = reset_credits.get("summary")
            if avail > 0 and summary:
                lines.append(f"  │    🔄 Resets: {avail} disp. ({summary})")
            elif avail > 0:
                lines.append(f"  │    🔄 Resets: {avail} disponibles")

    elif cx.get("status") == "error":
        err = cx.get("error", "Error")[:55]
        lines.append(f"  │ ❌ {err:<59} │")
        if cx.get("hint"):
            hint = cx["hint"][:55]
            lines.append(f"  │ 💡 {hint:<59} │")
    lines.append("  └────────────────────────────────────────────────────────────────┘")

    # ─── Z.AI ───
    zai = all_data.get("zai", {})
    lines.append("")
    lines.append("  ┌─ 🧠 Z.AI Coding Plan ─────────────────────────────────────────┐")
    if zai.get("status") == "ok":
        data = zai.get("data", {})
        level = data.get("level", "?").upper()
        lines.append(f"  │ 📋 Plan: GLM Coding {level:<8}  API: quota/limit            │")
        lines.append("  │                                                                │")

        for lim in data.get("limits", []):
            pct = lim.get("used_percent", 0)
            remaining = lim.get("remaining_percent", 100 - pct)
            label = lim.get("label", lim.get("type", ""))

            bar = _progress_bar(remaining)
            emoji = "🟢" if remaining > 50 else "🟡" if remaining > 20 else "🔴"
            lines.append(f"  │ {emoji} {label}: {bar} {remaining:>5.1f}% restante       │")
            reset_in = lim.get("reset_in", "N/A")
            lines.append(f"  │   ↳ Reset en {str(reset_in):<46} │")

            # Detalle por modelo
            if lim.get("models"):
                for model_code, usage_pct in lim["models"].items():
                    if usage_pct > 0:
                        lines.append(
                            f"  │     • {model_code:<20} {usage_pct}% uso                   │"
                        )

    elif zai.get("status") == "error":
        err = zai.get("error", "Error")[:55]
        lines.append(f"  │ ❌ {err:<59} │")
        if zai.get("hint"):
            hint = zai["hint"][:55]
            lines.append(f"  │ 💡 {hint:<59} │")
    lines.append("  └────────────────────────────────────────────────────────────────┘")

    lines.append("")
    lines.append(f"  🔄 Auto-refresh: {REFRESH_INTERVAL}s | Ctrl+C para salir")
    return "\n".join(lines)


def _progress_bar(pct: float, width: int = 20) -> str:
    """Genera una barra de progreso ASCII."""
    filled = int(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


# ═══════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════


def build_quota_snapshot(all_data: dict) -> dict:
    """Contrato pequeño y sin credenciales para que SWARMS decida rutas."""
    providers = {}

    codex = all_data.get("codex", {})
    if codex.get("status") == "ok":
        data = codex["data"]
        for account in data.get("accounts") or [data]:
            label = account.get("label", "Codex")
            windows = {}
            for window_name, window in codex_windows_with_data(account):
                remaining = window["remaining_percent"]
                if window_name not in windows or remaining < windows[window_name]:
                    windows[window_name] = remaining
            if windows:
                providers[f"codex:{label}"] = {"windows": windows}

    zai = all_data.get("zai", {})
    if zai.get("status") == "ok":
        coding = next(
            (item for item in zai["data"].get("limits", []) if item.get("kind") == "coding_5h"),
            None,
        )
        if coding and coding.get("remaining_percent") is not None:
            providers["zai:coding"] = {"windows": {"5h": coding.get("remaining_percent")}}

    antigravity = all_data.get("antigravity", {})
    if antigravity.get("status") == "ok":
        family_minimums = {}
        for group in antigravity["data"].get("groups", []):
            label = _antigravity_family(group.get("label", ""))
            key = AGY_FAMILY_KEYS.get(label) or re.sub(r"\W+", "_", label.lower()).strip("_")
            if not key:
                continue
            windows = family_minimums.setdefault(key, {})
            for window_name, field in (("5h", "remaining_5h_percent"), ("7d", "weekly_remaining_percent")):
                remaining = group.get(field)
                if remaining is not None:
                    windows[window_name] = min(remaining, windows.get(window_name, remaining))
        for key, windows in family_minimums.items():
            providers[f"agy:{key}"] = {"windows": windows}

    return {
        "version": 1,
        "generated_at_epoch": int(time.time()),
        "generated_at": all_data.get("timestamp") or datetime.now(timezone.utc).isoformat(),  # noqa: UP017
        "quotas": providers,
    }


def write_quota_snapshot(all_data: dict, path: str = QUOTA_SNAPSHOT_FILE) -> None:
    """Publica el snapshot atómicamente; nunca deja JSON parcial al scheduler."""
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(build_quota_snapshot(all_data), handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except OSError:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def build_quota_identities(all_data: dict) -> dict:
    """Mapa local de plan a email; separado del snapshot público de cuotas."""
    identities = {}

    codex = all_data.get("codex", {})
    if codex.get("status") == "ok":
        data = codex["data"]
        for account in data.get("accounts") or [data]:
            email = account.get("email")
            if isinstance(email, str) and "@" in email:
                identities[f"codex:{account.get('label', 'Codex')}"] = email

    antigravity = all_data.get("antigravity", {})
    if antigravity.get("status") == "ok":
        email = antigravity["data"].get("email")
        if isinstance(email, str) and "@" in email:
            for key in AGY_FAMILY_KEYS.values():
                identities[f"agy:{key}"] = email

    zai = all_data.get("zai", {})
    email = (zai.get("data") or {}).get("email") or _local_zai_email()
    if isinstance(email, str) and "@" in email:
        identities["zai:coding"] = email

    return {"version": 1, "accounts": identities}


def write_quota_identities(all_data: dict, path: str = QUOTA_IDENTITIES_FILE) -> None:
    """Publica identidades atómicamente en un archivo local ignorado por Git."""
    temp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(build_quota_identities(all_data), handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, path)
    except OSError:
        try:
            os.remove(temp_path)
        except OSError:
            pass


def collect_all_quotas() -> dict:
    all_data = {
        "antigravity": get_antigravity_quota(),
        "codex": get_codex_quota(),
        "zai": get_zai_quota(),
        "timestamp": datetime.now(timezone.utc).isoformat(),  # noqa: UP017
    }
    write_quota_snapshot(all_data)
    write_quota_identities(all_data)
    return all_data


def main():
    as_json = "--json" in sys.argv
    watch = "--watch" in sys.argv

    while True:
        all_data = collect_all_quotas()

        if as_json:
            print(json.dumps(all_data, indent=2, ensure_ascii=False, default=str))
        else:
            os.system("cls" if os.name == "nt" else "clear")
            print(render_terminal(all_data))

        if not watch:
            break

        try:
            time.sleep(REFRESH_INTERVAL)
        except KeyboardInterrupt:
            print("\n\n  👋 ¡Hasta luego!")
            break


def _format_local_datetime(timestamp) -> str:
    """Fecha y hora del sistema con offset explícito, también en cambios de horario."""
    try:
        if isinstance(timestamp, (int, float)):
            value = datetime.fromtimestamp(timestamp, timezone.utc)
        else:
            value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if value.tzinfo is None:
            return "Hora no disponible"
        local = value.astimezone()
        offset = local.strftime("%z")
        return f"{local:%d/%m %H:%M} UTC{offset[:3]}:{offset[3:]}"
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return "Hora no disponible"


def _normalise_zai_response(body: dict) -> dict:
    """HTTP 200 puede contener un rechazo; exigir límites antes de declarar éxito."""
    if body.get("success") is False:
        code = body.get("code")
        code = str(code) if isinstance(code, (int, float)) else "desconocido"
        return {"status": "error", "error": f"Z.AI rechazó la consulta (código {code}). Revisa tu autenticación."}
    data = body.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("limits"), list) or not data["limits"]:
        return {"status": "error", "error": "Z.AI no devolvió límites de Coding Plan."}
    return {"status": "ok", "data": {
        "level": data.get("level") or "Coding Plan",
        "limits": _normalise_zai_limits(data["limits"]),
        "email": None,
    }}


def _zai_request(url: str, headers: dict) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", **headers})
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.load(response)
    if not isinstance(body, dict):
        raise ValueError("Respuesta Z.AI no válida")
    return body


def _normalise_zai_resets(body: dict) -> dict:
    data = body.get("data")
    if type(body.get("code")) is not int or body["code"] != 0 or not isinstance(data, dict):
        return {"status": "error", "error": "No se pudieron consultar las tarjetas Z.AI."}
    credits = []
    for field, kind in (("available_five_hour_resets", "coding_5h"), ("available_week_resets", "coding_weekly")):
        items = data.get(field)
        if not isinstance(items, list):
            return {"status": "error", "error": "Z.AI devolvió un inventario de tarjetas no válido."}
        for item in items:
            expiry = item.get("expire_at") if isinstance(item, dict) else None
            if type(expiry) not in (int, float) or expiry <= 0:
                return {"status": "error", "error": "Z.AI devolvió un vencimiento no válido."}
            if expiry > time.time() * 1000:
                credits.append({"kind": kind, "expires_at": datetime.fromtimestamp(expiry / 1000, timezone.utc).isoformat()})
    credits.sort(key=lambda credit: credit["expires_at"])
    return {"status": "ok", "available_count": len(credits), "credits": credits}


def _get_zcode_zai_quota(session: dict) -> dict:
    """Cuota y tarjetas conservan sus errores por separado y usan la misma sesión."""
    try:
        result = _normalise_zai_response(_zai_request(
            "https://api.z.ai/api/monitor/usage/quota/limit", {"Authorization": session["platform_token"]}))
    except (OSError, ValueError, TypeError):
        result = {"status": "error", "error": "No se pudo consultar la cuota Z.AI con ZCode."}
    data = result.setdefault("data", {"level": "Coding Plan", "limits": []})
    data["email"] = session.get("email")
    resets = {"status": "error", "error": "Falta la sesión ZCode para consultar tarjetas."}
    if session.get("zcode_token"):
        token = session["zcode_token"]
        try:
            resets = _normalise_zai_resets(_zai_request("https://zcode.z.ai/api/v1/coding-plan/reset/status", {
                "Authorization": token if token.lower().startswith("bearer ") else f"Bearer {token}",
                "X-Bigmodel-Authorization": session["platform_token"],
                "Bigmodel-Target-Type": "PERSONAL",
            }))
        except (OSError, ValueError, TypeError):
            resets = {"status": "error", "error": "No se pudo consultar el inventario de tarjetas Z.AI."}
    data["reset_credits"] = resets
    result["source"] = "ZCode"
    return result


def zai_popup_data(result: dict) -> dict:
    """Presenta cuotas y vencimientos agrupando tarjetas idénticas sin ocultar errores."""
    from collections import Counter

    data = result.get("data") or {}
    limits = data.get("limits") or []
    coding = next((limit for limit in limits if limit.get("kind") == "coding_5h"), {})
    lines = [(data.get("level") or "Coding Plan").upper()]
    if result.get("status") != "ok":
        lines.append(result.get("error") or "Cuota Z.AI no disponible")
    for limit in limits:
        if limit.get("kind") == "mcp_tools" and limit.get("remaining_percent") is not None:
            lines.append(f"MCP: {limit['remaining_percent']:.0f}% restante")
        if limit.get("kind") == "coding_weekly" and limit.get("remaining_percent") is not None:
            lines.append(f"7d: {limit['remaining_percent']:.0f}% restante")
            if limit.get("reset_at"):
                lines.append(f"Reinicio 7d: {_format_local_datetime(limit['reset_at'])}")
    if coding:
        lines.append(f"Reinicio 5h: {_format_local_datetime(coding['reset_at'])}" if coding.get("reset_at") else "Reinicio 5h: no informado")
    resets = data.get("reset_credits") or {}
    if resets.get("status") == "ok":
        credits = resets.get("credits") or []
        lines.append(f"{len(credits)} resets manuales")
        groups = Counter((credit["expires_at"], credit["kind"]) for credit in credits)
        for (expires, kind), count in sorted(groups.items()):
            window = "5h" if kind == "coding_5h" else "7d"
            lines.append(f"{count} × Reset {window} {_format_local_datetime(expires)}")
    else:
        lines.append(resets.get("error") or "Tarjetas: sesión ZCode no disponible")
    return {"pct": coding.get("remaining_percent"), "subtitle": "\n".join(lines)}


if __name__ == "__main__":
    main()
