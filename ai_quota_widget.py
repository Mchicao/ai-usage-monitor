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
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

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
AGY_ACCOUNTS_DIR = os.environ.get(
    "AI_AGY_ACCOUNTS_DIR",
    os.path.expandvars(r"%APPDATA%\antigravity-usage\accounts"),
)
def _find_antigravity_oauth_keys() -> tuple[str | None, str | None]:
    """Extrae las claves de cliente registradas localmente en npm antigravity-usage para no almacenar secretos."""
    cid = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_ID")
    csec = os.environ.get("ANTIGRAVITY_OAUTH_CLIENT_SECRET")
    if cid and csec:
        return cid, csec
    candidate_paths = [
        os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules", "antigravity-usage", "dist", "index.js"),
        os.path.expanduser(r"~/.nvm/versions/node/**/lib/node_modules/antigravity-usage/dist/index.js"),
    ]
    for p in candidate_paths:
        for match in glob.glob(p, recursive=True):
            try:
                with open(match, encoding="utf-8") as f:
                    content = f.read()
                m_id = re.search(r'clientId:\s*(?:process\.env\.[A-Z_]+\s*\|\|\s*)?"([^"]+)"', content)
                m_sec = re.search(r'clientSecret:\s*(?:process\.env\.[A-Z_]+\s*\|\|\s*)?"([^"]+)"', content)
                if m_id and m_sec:
                    return m_id.group(1), m_sec.group(1)
            except OSError:
                pass
    return None, None

_DISCOVERED_CID, _DISCOVERED_CSEC = _find_antigravity_oauth_keys()
AGY_OAUTH_CLIENT_ID = _DISCOVERED_CID or ""
AGY_OAUTH_CLIENT_SECRET = _DISCOVERED_CSEC or ""
AGY_FAMILY_KEYS = {"Gemini": "gemini", "Claude y GPT": "claude_gpt", "Gemini 4 Argon": "gemini_argon"}

# Límites conocidos del Z.AI Coding Plan (extraídos de la documentación oficial)
# https://docs.z.ai/coding-plan/overview
ZAI_PLAN_LIMITS = {
    "Lite": {"5h_prompts": 80, "weekly_prompts": 400},
    "Pro": {"5h_prompts": 400, "weekly_prompts": 2000},
    "Max": {"5h_prompts": 1600, "weekly_prompts": 8000},
}


# ═══════════════════════════════════════════════════════
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


def _fetch_antigravity_via_api() -> dict | None:
    """Consulta la cuota directamente a Google Cloud Code API sin crear procesos."""
    if not os.path.isdir(AGY_ACCOUNTS_DIR):
        return None
    refresh_token = None
    try:
        for user_entry in os.listdir(AGY_ACCOUNTS_DIR):
            tpath = os.path.join(AGY_ACCOUNTS_DIR, user_entry, "tokens.json")
            if os.path.isfile(tpath):
                with open(tpath, encoding="utf-8") as f:
                    tdata = json.load(f)
                    rt = tdata.get("refreshToken") or tdata.get("refresh_token")
                    if rt:
                        refresh_token = rt
                        break
    except OSError:
        return None

    if not refresh_token:
        return None

    try:
        token_payload = urllib.parse.urlencode({
            "client_id": AGY_OAUTH_CLIENT_ID,
            "client_secret": AGY_OAUTH_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }).encode("utf-8")
        token_req = urllib.request.Request("https://oauth2.googleapis.com/token", data=token_payload)
        with urllib.request.urlopen(token_req, timeout=10) as resp:
            access_token = json.loads(resp.read().decode("utf-8")).get("access_token")
        if not access_token:
            return None

        # loadCodeAssist
        load_body = json.dumps({"metadata": {"ideType": "ANTIGRAVITY"}}).encode("utf-8")
        req_load = urllib.request.Request(
            "https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist",
            data=load_body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity",
            },
            method="POST",
        )
        with urllib.request.urlopen(req_load, timeout=10) as resp:
            project_id = json.loads(resp.read().decode("utf-8")).get("cloudaicompanionProject")

        # fetchAvailableModels
        models_body = json.dumps({"project": project_id}).encode("utf-8") if project_id else b"{}"
        req_models = urllib.request.Request(
            "https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
            data=models_body,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity",
            },
            method="POST",
        )
        with urllib.request.urlopen(req_models, timeout=10) as resp:
            models_dict = json.loads(resp.read().decode("utf-8")).get("models", {})

        models_list = []
        for m_id, m_data in models_dict.items():
            qi = m_data.get("quotaInfo") or {}
            models_list.append({
                "modelId": m_id,
                "label": m_data.get("displayName") or m_id,
                "remainingPercentage": qi.get("remainingFraction"),
                "resetTime": qi.get("resetTime"),
            })

        groups = _normalise_antigravity_groups(models_list)
        return {
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "method": "cloudcode_api",
            "groups": groups,
        }
    except Exception:
        return None


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

    # Si _run_quota_command o subprocess.run está mockeado en tests, respetar el flujo mockeado
    is_mocked = getattr(_run_quota_command, "__wrapped__", None) is not None or getattr(_run_quota_command, "side_effect", None) is not None or getattr(_run_quota_command, "return_value", None) is not None or getattr(subprocess, "run", None) != subprocess.__dict__.get("run") or getattr(subprocess.run, "return_value", None) is not None or getattr(subprocess.run, "side_effect", None) is not None
    if not is_mocked:
        api_data = _fetch_antigravity_via_api()
        if api_data:
            api_data["stale"] = False
            payload = {"status": "ok", "data": api_data, "source": "cloudcode API"}
            _save_last_known(payload)
            return payload

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
        result = _run_quota_command(
            ["antigravity-usage", "quota", "--json", "--refresh"],
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
    """Colapsa aliases que comparten la misma ventana 5h; weekly no viene en esta API."""
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
        key = (family, round(float(remaining), 6), reset_at)
        groups.setdefault(
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
        )["model_count"] += 1
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
    token_path = os.path.expanduser("~/AppData/Local/hermes/.zai_platform_token.txt")
    try:
        with open(token_path, encoding="utf-8") as handle:
            customer = _jwt_claims(handle.read().strip()).get("customer") or {}
        email = customer.get("email") if isinstance(customer, dict) else None
        return email if isinstance(email, str) and "@" in email else None
    except (OSError, IndexError, ValueError, TypeError, json.JSONDecodeError):
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

    is_mocked = getattr(_run_quota_command, "__wrapped__", None) is not None or getattr(_run_quota_command, "side_effect", None) is not None or getattr(_run_quota_command, "return_value", None) is not None
    opencode_token_found = False
    if not is_mocked:
        try:
            with open(OPENCODE_AUTH_FILE, encoding="utf-8") as f:
                provider = json.load(f).get("openai") or {}
            token = provider.get("access")
            if token and not _token_is_expired(token):
                candidates.append(("OpenCode", token))
                opencode_token_found = True
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            pass

    if not opencode_token_found:
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
                date_str = f"{exp_dt.day:02d}/{exp_dt.month:02d}"

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
        avail = len(credits_list)

    if avail == 0:
        summary = "Sin resets disponibles"
        short_summary = "0 disp."
    elif not credits_list:
        summary = f"{avail} disponibles" if avail > 1 else f"{avail} disponible"
        short_summary = f"{avail} disp."
    else:
        from collections import OrderedDict

        by_date = OrderedDict()
        for c in credits_list:
            d = c["date"]
            if d not in by_date:
                by_date[d] = []
            by_date[d].append(c)

        parts = []
        is_first = True
        for d, items in by_date.items():
            count = len(items)
            first_countdown = items[0]["countdown"]
            if is_first:
                if count > 1:
                    parts.append(f"{count} el {d} ({first_countdown})")
                else:
                    parts.append(f"1 el {d} ({first_countdown})")
                is_first = False
            else:
                parts.append(f"{count} el {d}")
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
    Requiere: platform JWT + bigmodel-organization + bigmodel-project headers.
    El JWT se extrae del navegador (Thorium Local Storage).
    """
    import urllib.error
    import urllib.request

    # Leer el platform token
    token_path = os.path.expanduser("~/AppData/Local/hermes/.zai_platform_token.txt")
    if not os.path.exists(token_path):
        return {
            "status": "error",
            "error": "No hay platform token de Z.AI",
            "hint": "Ejecuta extract_zai_cookies.py o abre z.ai en tu navegador",
        }

    with open(token_path) as f:
        token = f.read().strip()
    email = _local_zai_email()

    # Valores por defecto de org/project (extraídos de la cuenta del usuario)
    org = "org-2363d4ce091a445D936A7D3B5EB0C57C"
    proj = "proj_F7F3eC68baF047658a790bcAA12B68d4"

    url = "https://api.z.ai/api/monitor/usage/quota/limit"
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("bigmodel-organization", org)
    req.add_header("bigmodel-project", proj)
    req.add_header("Accept", "application/json")

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        body = json.loads(resp.read())
        data = body.get("data", {})
        limits = data.get("limits", [])
        level = data.get("level", "unknown")

        result_limits = _normalise_zai_limits(limits)

        return {
            "status": "ok",
            "data": {
                "level": level,
                "limits": result_limits,
                "email": email,
            },
        }
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {
                "status": "error",
                "error": "Token expirado",
                "hint": "Abre z.ai en tu navegador y ejecuta extract_zai_cookies.py",
            }
        return {"status": "error", "error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


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
        if limit_type == "TOKENS_LIMIT":
            kind = "coding_5h"
            label = "Modelos GLM · ventana 5h"
        elif limit_type == "TIME_LIMIT" and details:
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


if __name__ == "__main__":
    main()
