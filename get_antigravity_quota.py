#!/usr/bin/env python3
"""
Obtiene la cuota restante de Google Antigravity IDE.
Lee el token OAuth del state.vscdb del IDE y consulta la API de Google Cloud Code.

Dos modos de funcionamiento:
  1. Si el IDE está abierto: usa el Language Server local (gRPC)
  2. Si el IDE está cerrado: usa el token OAuth guardado para llamar a cloudcode-pa.googleapis.com

Uso:
  python get_antigravity_quota.py              # Salida legible
  python get_antigravity_quota.py --json       # Salida JSON para scripts
"""

import sqlite3
import json
import os
import glob
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone

# ─── Constantes extraídas del código fuente de las extensiones ───

CLOUDCODE_BASE = "https://cloudcode-pa.googleapis.com"
CLOUDCODE_DAILY_BASE = "https://daily-cloudcode-pa.googleapis.com"

# Client ID oficial de Google Cloud Code (extraído de auth/constants.js)
GOOGLE_CLIENT_ID = "1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"

# Perfil de Antigravity (usar el primero encontrado)
AG_STATE_PATTERN = os.path.expandvars(
    r"%APPDATA%\Antigravity\User\profiles\*\globalStorage\state.vscdb"
)


def find_antigravity_token():
    """Busca el token OAuth de Google en el state.vscdb de Antigravity."""
    patterns = [
        os.path.expandvars(r"%APPDATA%\Antigravity\User\profiles\*\globalStorage\state.vscdb"),
        os.path.expandvars(r"%APPDATA%\Antigravity\User\globalStorage\state.vscdb"),
    ]

    for pattern in patterns:
        db_paths = glob.glob(pattern)
        for db_path in db_paths:
            try:
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                # Buscar la clave de auth
                cur.execute(
                    "SELECT key, value FROM ItemTable WHERE key LIKE '%antigravityAuth%' OR key LIKE '%cloudcode%' OR key LIKE '%codeium.auth%'"
                )
                for key, value in cur.fetchall():
                    try:
                        data = json.loads(value)
                        # Extraer el token y email
                        token = (
                            data.get("apiKey")
                            or data.get("access_token")
                            or data.get("token")
                        )
                        email = data.get("email") or data.get("userEmail", "")
                        if token:
                            conn.close()
                            return {
                                "token": token,
                                "email": email,
                                "source": db_path,
                                "key": key,
                                "raw": data,
                            }
                    except (json.JSONDecodeError, TypeError):
                        continue
                conn.close()
            except sqlite3.Error:
                continue

    return None


def fetch_quota_from_google(token: str, base_url: str = CLOUDCODE_BASE) -> dict:
    """
    Consulta la API de Google Cloud Code para obtener la cuota.
    Flujo de 2 pasos:
      1. loadCodeAssist → obtiene tier y projectId
      2. fetchAvailableModels → obtiene cuota por modelo
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    result = {
        "tier": None,
        "project_id": None,
        "models": {},
        "error": None,
    }

    # ─── Paso 1: loadCodeAssist ───
    load_data = json.dumps({"metadata": {"ideType": "ANTIGRAVITY"}}).encode()
    req = urllib.request.Request(
        f"{base_url}/v1internal:loadCodeAssist",
        data=load_data,
        headers=headers,
        method="POST",
    )

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        load_resp = json.loads(resp.read())

        result["tier"] = load_resp.get("currentTier", {})
        result["project_id"] = load_resp.get("cloudaicompanionProject", "")

        # Extraer info de tier
        tiers = load_resp.get("allowedTiers", [])
        current_tier_id = load_resp.get("currentTier", {}).get("id", "unknown")

    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        result["error"] = f"loadCodeAssist failed: HTTP {e.code} - {body}"
        return result
    except Exception as e:
        result["error"] = f"loadCodeAssist error: {str(e)}"
        return result

    # ─── Paso 2: fetchAvailableModels ───
    models_data = json.dumps({"project": result["project_id"]}).encode()
    req2 = urllib.request.Request(
        f"{base_url}/v1internal:fetchAvailableModels",
        data=models_data,
        headers=headers,
        method="POST",
    )

    try:
        resp2 = urllib.request.urlopen(req2, timeout=15)
        models_resp = json.loads(resp2.read())

        models = models_resp.get("models", {})
        for model_name, model_data in models.items():
            quota_info = model_data.get("quotaInfo", {})
            remaining = quota_info.get("remainingFraction")
            reset_time = quota_info.get("resetTime")

            # Si remainingFraction falta, la cuota está agotada (0)
            if remaining is None:
                remaining = 0.0

            result["models"][model_name] = {
                "remaining_fraction": remaining,
                "remaining_percent": round(remaining * 100, 1),
                "reset_time": reset_time,
                "max_tokens": model_data.get("maxTokens"),
            }

    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        result["error"] = f"fetchAvailableModels failed: HTTP {e.code} - {body}"
    except Exception as e:
        result["error"] = f"fetchAvailableModels error: {str(e)}"

    return result


def format_output(data: dict) -> str:
    """Formatea la salida para lectura humana."""
    lines = []
    lines.append("╔══════════════════════════════════════════════════╗")
    lines.append("║       🛸 Antigravity IDE — Cuota Restante        ║")
    lines.append("╚══════════════════════════════════════════════════╝")

    if data.get("error"):
        lines.append(f"\n❌ Error: {data['error']}")
        return "\n".join(lines)

    # Tier info
    tier = data.get("tier", {})
    tier_id = tier.get("id", "unknown")
    lines.append(f"\n📋 Plan: {tier_id}")

    if data.get("email"):
        lines.append(f"👤 Cuenta: {data['email']}")

    # Modelos
    models = data.get("models", {})
    if not models:
        lines.append("\n⚠️  No se encontraron modelos con información de cuota.")
        return "\n".join(lines)

    lines.append(f"\n📊 Modelos ({len(models)} disponibles):")
    lines.append("─" * 52)

    for model_name in sorted(models.keys()):
        model = models[model_name]
        pct = model["remaining_percent"]
        reset = model.get("reset_time", "N/A")

        # Barra de progreso visual
        bar_len = 20
        filled = int(pct / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        # Emoji según nivel
        if pct > 50:
            emoji = "🟢"
        elif pct > 20:
            emoji = "🟡"
        else:
            emoji = "🔴"

        lines.append(f"  {emoji} {model_name:<30} {bar} {pct:>5.1f}%")
        if reset and reset != "N/A":
            try:
                dt = datetime.fromisoformat(reset.replace("Z", "+00:00"))
                now = datetime.now(timezone.utc)
                diff = dt - now
                hours = int(diff.total_seconds() // 3600)
                mins = int((diff.total_seconds() % 3600) // 60)
                lines.append(f"     ↳ Reset en {hours}h {mins}m")
            except Exception:
                lines.append(f"     ↳ Reset: {reset}")

    lines.append("─" * 52)
    return "\n".join(lines)


def main():
    as_json = "--json" in sys.argv

    # 1. Buscar el token OAuth
    auth = find_antigravity_token()
    if not auth:
        if as_json:
            print(json.dumps({"error": "No se encontró el token OAuth de Antigravity. ¿Está el IDE instalado y has iniciado sesión?"}))
        else:
            print("❌ No se encontró el token OAuth de Antigravity.")
            print("   Asegúrate de que el IDE esté instalado y hayas iniciado sesión.")
        sys.exit(1)

    # 2. Consultar la cuota
    quota = fetch_quota_from_google(auth["token"])
    quota["email"] = auth.get("email", "")

    # 3. Mostrar resultado
    if as_json:
        print(json.dumps(quota, indent=2, ensure_ascii=False))
    else:
        print(format_output(quota))


if __name__ == "__main__":
    main()
