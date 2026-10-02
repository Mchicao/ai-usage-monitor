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
import urllib.request
from datetime import datetime, timezone

# ─── Configuración ───

HERMES_HOME = os.environ.get("HERMES_HOME", os.path.expanduser("~/AppData/Local/hermes"))
CODEX_SESSIONS_DIR = os.path.expanduser("~/.codex/sessions")
CODEX_AUTH_FILE = os.path.expanduser("~/.codex/auth.json")
HERMES_AUTH_FILE = os.path.join(HERMES_HOME, "auth.json")
OPENCODE_AUTH_FILE = os.path.expanduser("~/.local/share/opencode/auth.json")
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
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

# Límites conocidos del Z.AI Coding Plan (extraídos de la documentación oficial)
# https://docs.z.ai/coding-plan/overview
ZAI_PLAN_LIMITS = {
    "Lite": {"5h_prompts": 80, "weekly_prompts": 400},
    "Pro": {"5h_prompts": 400, "weekly_prompts": 2000},
    "Max": {"5h_prompts": 1600, "weekly_prompts": 8000},
}


# ═══════════════════════════════════════════════════════
#  1. ANTIGRAVITY IDE
# ═══════════════════════════════════════════════════════


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
        family = "Gemini" if "gemini" in family_raw.lower() else "Claude y GPT"
        group = groups.setdefault(
            family,
            {
                "label": family,
                "remaining_5h_percent": None,
                "reset_5h_at": None,
                "weekly_remaining_percent": None,
                "reset_weekly_at": None,
                "model_count": 0,
            },
        )
        reset_at = fields[3] if len(fields) > 3 and fields[3] else None
        if "weekly" in window:
            group["weekly_remaining_percent"] = remaining
            group["reset_weekly_at"] = reset_at
        else:
            group["remaining_5h_percent"] = remaining
            group["reset_5h_at"] = reset_at

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
        result = subprocess.run(
            ["agy", "--print", "/usage"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            shell=True,
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

    try:
        result = subprocess.run(
            ["antigravity-usage", "quota", "--json", "--refresh"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
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
        family = "Gemini" if "gemini" in label.lower() else "Claude y GPT"
        key = (family, round(float(remaining), 6), reset_at)
        groups.setdefault(
            key,
            {
                "label": family,
                "remaining_5h_percent": round(float(remaining) * 100, 1),
                "reset_5h_at": reset_at,
                "weekly_remaining_percent": None,
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


def _fetch_codex_account(label: str, token: str, account_id: str) -> dict:
    request = urllib.request.Request(
        CODEX_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "ChatGPT-Account-ID": account_id,
            "Accept": "application/json",
            "User-Agent": "ai-usage-monitor/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.load(response)
    rate_limit = payload.get("rate_limit") or {}
    return {
        "label": label,
        "email": payload.get("email"),
        "plan_type": payload.get("plan_type", "unknown"),
        "primary": _normalise_codex_window(rate_limit.get("primary_window") or {}, 18_000),
        "secondary": _normalise_codex_window(rate_limit.get("secondary_window") or {}, 604_800),
        "credits": payload.get("credits"),
        "source": "OpenAI usage API",
    }


def get_codex_quota() -> dict:
    """Consulta en vivo las cuentas distintas de Codex y Hermes."""
    accounts = []
    errors = []
    for label, token, account_id in _codex_credentials():
        try:
            accounts.append(_fetch_codex_account(label, token, account_id))
        except urllib.error.HTTPError as exc:
            errors.append(f"{label}: HTTP {exc.code}")
        except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{label}: {exc}")

    if not accounts:
        fallback = get_codex_quota_from_sessions()
        if errors and fallback.get("status") != "ok":
            fallback["error"] = "; ".join(errors)
        return fallback

    # Compatibilidad: consumidores antiguos ven la cuenta con menor cuota expuesta.
    summary = min(accounts, key=_codex_min_remaining)
    return {"status": "ok", "data": {**summary, "accounts": accounts}}


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
            "source_file": os.path.basename(latest_file) if latest_file else None,
        },
    }

    return result


def _ts_to_iso(ts) -> str | None:
    """Convierte timestamp Unix a ISO string."""
    if not ts:
        return None
    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()  # noqa: UP017
    except Exception:
        return str(ts)


def _ts_to_countdown(ts) -> str:
    """Convierte timestamp Unix a countdown legible (ej: '3h 42m')."""
    if not ts:
        return "N/A"
    try:
        now = datetime.now(tz=timezone.utc)  # noqa: UP017
        reset = datetime.fromtimestamp(ts, tz=timezone.utc)  # noqa: UP017
        diff = reset - now
        total_secs = diff.total_seconds()
        if total_secs <= 0:
            return "Ya reseteado"
        hours = int(total_secs // 3600)
        mins = int((total_secs % 3600) // 60)
        if hours > 24:
            days = hours // 24
            hours = hours % 24
            return f"{days}d {hours}h"
        return f"{hours}h {mins}m"
    except Exception:
        return "N/A"


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
                lines.append(f"  │ {emoji} {name[:28]:<28} {bar} {pct:>5.1f}% (5h)")
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
            key = "gemini" if group.get("label") == "Gemini" else "claude_gpt"
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
            identities["agy:claude_gpt"] = email
            identities["agy:gemini"] = email

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
